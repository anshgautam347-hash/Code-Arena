"""Submission lifecycle: validate, dispatch to the external runner, collect results, score.

All rules are enforced here on the server. Code is never executed in this process.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from competitions.models import CodingQuestion
from exams import services as exam_services
from exams.services import ExamError

from . import runner, scoring
from .models import CaseResult, Language, Submission

logger = logging.getLogger(__name__)

Status = Submission.Status
Verdict = Submission.Verdict
CaseStatus = CaseResult.Status

UNAVAILABLE_MESSAGE = (
    "The code runner is not available right now, so your code was saved but not evaluated. "
    "Nothing was scored. Please try again in a moment."
)
NOT_CONFIGURED_MESSAGE = (
    "No code runner is configured on this server, so code cannot be evaluated. "
    "Your code was saved but nothing was scored. Please tell the organizers."
)

VERDICT_FROM_CASE = {
    CaseStatus.WRONG_ANSWER: Verdict.WRONG_ANSWER,
    CaseStatus.TIME_LIMIT: Verdict.TIME_LIMIT,
    CaseStatus.RUNTIME_ERROR: Verdict.RUNTIME_ERROR,
    CaseStatus.COMPILE_ERROR: Verdict.COMPILE_ERROR,
}


def _setting(name, default):
    return getattr(settings, name, default)


def clip(text, limit=None):
    limit = limit or _setting("JUDGE_MAX_OUTPUT_CHARS", 10000)
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def normalize_output(text):
    """Compare outputs ignoring line-ending style, trailing spaces and trailing blank lines."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).rstrip("\n")


# ---------------------------------------------------------------- languages
def languages_for(competition):
    return (
        Language.objects.filter(is_enabled=True)
        .filter(Q(competitions__isnull=True) | Q(competitions=competition))
        .distinct()
        .order_by("position", "name")
    )


# ---------------------------------------------------------------- creating a submission
def create_submission(session, question_id, language_slug, code, kind, now=None):
    now = now or timezone.now()
    exam_services.sync_session(session, now)
    exam_services.require_in_progress(session)

    if kind not in (Submission.Kind.RUN, Submission.Kind.SUBMIT):
        raise ExamError("bad_kind", "Unknown submission type.", 400)
    try:
        question = CodingQuestion.objects.get(pk=question_id, competition_id=session.competition_id)
    except CodingQuestion.DoesNotExist:
        raise ExamError("bad_question", "That question is not part of this exam.", 404)
    language = languages_for(session.competition).filter(slug=language_slug).first()
    if language is None:
        raise ExamError("bad_language", "That language is not available for this competition.", 400)

    code = code or ""
    if not code.strip():
        raise ExamError("empty_code", "Write some code first.", 400)
    max_length = _setting("EXAM_MAX_CODE_LENGTH", 20000)
    if len(code) > max_length:
        raise ExamError("too_long", f"Code is limited to {max_length} characters.", 413)

    cases = list(question.test_cases.all())
    if kind == Submission.Kind.RUN:
        cases = [c for c in cases if c.is_public]
        if not cases:
            raise ExamError("no_tests", "This question has no public test cases to run. Use Submit instead.", 409)
    elif not cases:
        raise ExamError("no_tests", "This question has no test cases yet, so it cannot be evaluated.", 409)

    _enforce_rate_limits(session, question, now)

    with transaction.atomic():
        submission = Submission.objects.create(
            session=session, question=question, language=language, source_code=code, kind=kind,
            created_at=now, total_count=len(cases), total_weight=sum(c.weight for c in cases),
        )
        CaseResult.objects.bulk_create([
            CaseResult(
                submission=submission, test_case=c, position=i, is_public=c.is_public, weight=c.weight,
                input_data=c.input_data, expected_output=c.expected_output,
            )
            for i, c in enumerate(cases)
        ])
    dispatch(submission)
    return submission


def _enforce_rate_limits(session, question, now):
    if session.submissions.filter(status__in=Submission.PENDING_STATUSES).exists():
        raise ExamError("busy", "Wait for your previous run to finish before starting another.", 429)
    gap = _setting("JUDGE_MIN_SECONDS_BETWEEN_SUBMISSIONS", 3)
    last = session.submissions.order_by("-created_at").first()
    if gap and last and (now - last.created_at).total_seconds() < gap:
        wait = int(gap - (now - last.created_at).total_seconds()) + 1
        raise ExamError("too_fast", f"Please wait {wait} second(s) before running again.", 429)
    cap = _setting("JUDGE_MAX_SUBMISSIONS_PER_QUESTION", 60)
    used = session.submissions.filter(question=question).exclude(status=Status.RUNNER_UNAVAILABLE).count()
    if cap and used >= cap:
        raise ExamError("limit_reached", f"You have reached the limit of {cap} runs for this question.", 429)


# ---------------------------------------------------------------- sending to the runner
def _mark_unavailable(submission, message, now=None):
    submission.results.all().delete()  # placeholders only; never fabricate results
    submission.status = Status.RUNNER_UNAVAILABLE
    submission.verdict = ""
    submission.score = None
    submission.message = message[:200]
    submission.finished_at = now or timezone.now()
    submission.save()


def dispatch(submission, client=None):
    """Send the submission's test cases to the runner. Never raises for runner problems."""
    client = client if client is not None else runner.get_client()
    if client is None:
        _mark_unavailable(submission, NOT_CONFIGURED_MESSAGE)
        return submission
    question = submission.question
    results = list(submission.results.order_by("position", "id"))
    jobs = [
        {
            "source_code": submission.source_code,
            "language_id": submission.language.judge0_language_id,
            "stdin": r.input_data,
            "cpu_time_limit": float(question.time_limit_seconds),
            "memory_limit_kb": question.memory_limit_mb * 1024,
        }
        for r in results
    ]
    try:
        tokens = client.submit_batch(jobs)
    except runner.RunnerUnavailable as exc:
        logger.warning("Runner unavailable while dispatching submission %s: %s", submission.pk, exc)
        _mark_unavailable(submission, UNAVAILABLE_MESSAGE)
        return submission
    except runner.RunnerError as exc:
        logger.error("Runner rejected submission %s: %s", submission.pk, exc)
        _finish_error(submission, "The code runner could not process this submission. It was not scored.")
        return submission
    with transaction.atomic():
        for result, token in zip(results, tokens):
            result.token = token
            result.save(update_fields=["token"])
        submission.status = Status.RUNNING
        submission.save(update_fields=["status"])
    return submission


def _finish_error(submission, message, now=None):
    submission.results.filter(status=CaseStatus.PENDING).update(status=CaseStatus.INTERNAL_ERROR, message=message[:200])
    submission.status = Status.ERROR
    submission.verdict = ""
    submission.score = None
    submission.message = message[:200]
    submission.finished_at = now or timezone.now()
    submission.save()


# ---------------------------------------------------------------- collecting results
def map_case(result, info):
    """Turn one runner answer into (status, stdout, stderr, message)."""
    sid = info["status_id"]
    stdout = clip(info.get("stdout"))
    stderr = clip(info.get("stderr") or info.get("compile_output"), 4000)
    message = (info.get("message") or "")[:200]
    if sid == runner.ACCEPTED:
        passed = normalize_output(stdout) == normalize_output(result.expected_output)
        return (CaseStatus.PASSED if passed else CaseStatus.WRONG_ANSWER), stdout, stderr, message
    if sid == runner.WRONG_ANSWER:
        return CaseStatus.WRONG_ANSWER, stdout, stderr, message
    if sid == runner.TIME_LIMIT:
        return CaseStatus.TIME_LIMIT, stdout, stderr, message
    if sid == runner.COMPILE_ERROR:
        return CaseStatus.COMPILE_ERROR, stdout, stderr, message
    if sid in runner.RUNTIME_ERRORS:
        return CaseStatus.RUNTIME_ERROR, stdout, stderr, message
    return CaseStatus.INTERNAL_ERROR, stdout, stderr, message or "The runner reported an internal error."


def refresh_submission(submission, now=None, client=None):
    """Ask the runner about unfinished test cases and finalize the submission when all are done."""
    now = now or timezone.now()
    submission = Submission.objects.select_related("question", "language", "session").get(pk=submission.pk)
    if submission.is_finished:
        return submission

    pending = list(submission.results.filter(status=CaseStatus.PENDING).order_by("position", "id"))
    if not any(r.token for r in pending):
        # Created but never reached the runner (for example the server stopped in between).
        if now - submission.created_at > timedelta(seconds=30):
            return dispatch(submission, client=client)
        return submission

    timeout = _setting("JUDGE_EVALUATION_TIMEOUT_SECONDS", 120)
    client = client if client is not None else runner.get_client()
    infos = {}
    if client is not None:
        try:
            infos = client.get_batch([r.token for r in pending if r.token])
        except runner.RunnerError as exc:
            logger.warning("Could not poll runner for submission %s: %s", submission.pk, exc)

    with transaction.atomic():
        submission = Submission.objects.select_for_update().get(pk=submission.pk)
        if submission.is_finished:
            return submission
        for result in submission.results.filter(status=CaseStatus.PENDING):
            info = infos.get(result.token)
            if info is None or info["status_id"] in runner.PENDING_IDS:
                continue
            result.status, result.stdout, result.stderr, result.message = map_case(result, info)
            result.time_ms = info.get("time_ms")
            result.memory_kb = info.get("memory_kb")
            result.save()
            if result.status == CaseStatus.COMPILE_ERROR and not submission.compile_output:
                submission.compile_output = clip(info.get("compile_output") or info.get("stderr"), 4000)
        if submission.results.filter(status=CaseStatus.PENDING).exists():
            if now - submission.created_at > timedelta(seconds=timeout):
                _finish_error(submission, "Evaluation timed out. This attempt was not scored.", now)
            else:
                submission.save()
            return submission
        _finalize(submission, now)
    return submission


def _finalize(submission, now):
    results = list(submission.results.order_by("position", "id"))
    if any(r.status == CaseStatus.INTERNAL_ERROR for r in results):
        _finish_error(submission, "The runner reported an internal error. This attempt was not scored.", now)
        return

    passed = [r for r in results if r.status == CaseStatus.PASSED]
    submission.passed_count = len(passed)
    submission.total_count = len(results)
    submission.passed_weight = sum(r.weight for r in passed)
    submission.total_weight = sum(r.weight for r in results)
    times = [r.time_ms for r in results if r.time_ms is not None]
    memories = [r.memory_kb for r in results if r.memory_kb is not None]
    submission.max_time_ms = max(times) if times else None
    submission.max_memory_kb = max(memories) if memories else None

    if any(r.status == CaseStatus.COMPILE_ERROR for r in results):
        submission.verdict = Verdict.COMPILE_ERROR
    else:
        failed = next((r for r in results if r.status != CaseStatus.PASSED), None)
        submission.verdict = VERDICT_FROM_CASE[failed.status] if failed else Verdict.ACCEPTED

    if submission.kind == Submission.Kind.SUBMIT:
        submission.score = scoring.score_for(
            submission.question.marks, submission.passed_weight, submission.total_weight
        )
    submission.status = Status.COMPLETED
    submission.message = ""
    submission.finished_at = now
    submission.save()


def rejudge(submission, client=None):
    """Re-evaluate against the question's CURRENT test cases (for example after fixing one)."""
    if submission.status in Submission.PENDING_STATUSES:
        raise ExamError("busy", "That submission is still being evaluated.", 409)
    cases = list(submission.question.test_cases.all())
    if submission.kind == Submission.Kind.RUN:
        cases = [c for c in cases if c.is_public]
    if not cases:
        raise ExamError("no_tests", "The question has no test cases to run against.", 409)
    with transaction.atomic():
        submission.results.all().delete()
        CaseResult.objects.bulk_create([
            CaseResult(
                submission=submission, test_case=c, position=i, is_public=c.is_public, weight=c.weight,
                input_data=c.input_data, expected_output=c.expected_output,
            )
            for i, c in enumerate(cases)
        ])
        submission.status = Status.QUEUED
        submission.verdict = ""
        submission.score = None
        submission.message = ""
        submission.compile_output = ""
        submission.finished_at = None
        submission.passed_count = 0
        submission.total_count = len(cases)
        submission.passed_weight = 0
        submission.total_weight = sum(c.weight for c in cases)
        submission.max_time_ms = submission.max_memory_kb = None
        submission.save()
    return dispatch(submission, client=client)


def reconcile(queryset=None, limit=100, client=None):
    """Finish evaluations whose participants have gone away. Returns how many were examined."""
    queryset = queryset if queryset is not None else Submission.objects.all()
    pending = queryset.filter(status__in=Submission.PENDING_STATUSES).order_by("created_at")[:limit]
    count = 0
    for submission in pending:
        refresh_submission(submission, client=client)
        count += 1
    return count


# ---------------------------------------------------------------- what participants may see
def participant_payload(submission, *, detail=True):
    """JSON-safe description of a submission for its OWNER.

    Public cases show input, expected and actual output. Private cases show ONLY
    pass/fail: their input, expected output and output are never included.
    """
    data = {
        "id": submission.pk,
        "kind": submission.kind,
        "question": submission.question_id,
        "language": submission.language.slug,
        "status": submission.status,
        "status_label": submission.get_status_display(),
        "finished": submission.is_finished,
        "verdict": submission.verdict,
        "verdict_label": submission.get_verdict_display() if submission.verdict else "",
        "message": submission.message,
        "created_at": submission.created_at.isoformat(),
        "passed": submission.passed_count,
        "total": submission.total_count,
        "compile_output": submission.compile_output,
        "max_time_ms": submission.max_time_ms,
        "cases": [],
    }
    if submission.status in (Status.RUNNER_UNAVAILABLE,) or not detail:
        return data
    for r in submission.results.all():
        case = {
            "index": r.position + 1,
            "public": r.is_public,
            "status": r.status,
            "status_label": r.get_status_display(),
            "time_ms": r.time_ms,
        }
        if r.is_public:
            case.update({
                "input": clip(r.input_data, 2000),
                "expected": clip(r.expected_output, 2000),
                "output": clip(r.stdout, 2000),
                "error": clip(r.stderr, 1000),
            })
        data["cases"].append(case)
    return data


def overview_payload(session):
    """Languages, runner status and per-question progress for the exam screen."""
    languages = [
        {"slug": l.slug, "name": l.name, "mode": l.editor_mode, "starter": l.starter_code}
        for l in languages_for(session.competition)
    ]
    questions = {}
    for q in session.competition.questions.all():
        questions[q.id] = {"submitted": 0, "best": None}
    submits = (
        session.submissions.filter(kind=Submission.Kind.SUBMIT, status=Status.COMPLETED)
        .order_by("created_at", "id")
    )
    for s in submits:
        entry = questions.setdefault(s.question_id, {"submitted": 0, "best": None})
        entry["submitted"] += 1
        best = entry["best"]
        if best is None or s.passed_weight > best["_weight"]:
            entry["best"] = {"passed": s.passed_count, "total": s.total_count,
                             "verdict_label": s.get_verdict_display(), "_weight": s.passed_weight}
    for entry in questions.values():
        if entry["best"]:
            entry["best"].pop("_weight")
    return {
        "languages": languages,
        "runner_configured": runner.get_client() is not None,
        "questions": questions,
        "limits": {
            "min_seconds_between": _setting("JUDGE_MIN_SECONDS_BETWEEN_SUBMISSIONS", 3),
            "max_per_question": _setting("JUDGE_MAX_SUBMISSIONS_PER_QUESTION", 60),
        },
    }
