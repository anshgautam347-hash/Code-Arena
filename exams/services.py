"""Server-side exam rules.

Everything that decides whether an action is allowed lives here, so the browser
is never trusted: it only displays what the server decides.
"""
import re
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from competitions.models import CodingQuestion, Competition

from .models import Answer, ExamSession, Violation

Status = ExamSession.Status


class ExamError(Exception):
    """A rule was broken. `code` is machine-readable; `http_status` is for JSON views."""

    def __init__(self, code, message, http_status=409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass
class Eligibility:
    ok: bool
    code: str = "ok"
    message: str = ""


def _setting(name, default):
    return getattr(settings, name, default)


def _fmt(dt):
    return timezone.localtime(dt).strftime("%d %b %Y, %I:%M %p")


# ---------------------------------------------------------------- eligibility
def check_eligibility(user, competition, now=None, session=None):
    """Can this user start (or resume) an exam for this competition right now?"""
    now = now or timezone.now()
    if session is None:
        session = ExamSession.objects.filter(user=user, competition=competition).first()

    if session is not None and session.is_finished:
        return Eligibility(False, "already_attempted", "You have already completed this exam. Each participant gets one attempt.")

    S = Competition.Status
    if competition.status == S.DRAFT:
        return Eligibility(False, "unavailable", "This competition is not available.")
    if competition.status == S.COMPLETED or now >= competition.end_time:
        return Eligibility(False, "closed", "This competition has ended.")
    if competition.status == S.UPCOMING:
        return Eligibility(False, "not_open", "This competition has not opened yet.")
    if now < competition.start_time:
        return Eligibility(False, "not_started", f"The exam opens on {_fmt(competition.start_time)}.")
    if not competition.questions.exists():
        return Eligibility(False, "no_questions", "This competition has no questions yet.")
    return Eligibility(True)


def create_session(user, competition, now=None):
    """Create the participant's session after they accepted the notice. Idempotent."""
    now = now or timezone.now()
    existing = ExamSession.objects.filter(user=user, competition=competition).first()
    result = check_eligibility(user, competition, now, session=existing)
    if not result.ok:
        raise ExamError(result.code, result.message)
    if existing:
        return existing
    try:
        with transaction.atomic():
            return ExamSession.objects.create(
                user=user, competition=competition, consented_at=now, created_at=now
            )
    except IntegrityError:  # two quick clicks: the other request won
        return ExamSession.objects.get(user=user, competition=competition)


# ---------------------------------------------------------------- state machine
def finalize(session, status, reason, ended_at=None):
    session.status = status
    session.end_reason = reason
    session.ended_at = ended_at or timezone.now()
    session.save(update_fields=["status", "end_reason", "ended_at"])
    return session


def sync_session(session, now=None):
    """Apply server-side deadlines. Called at the start of every request."""
    now = now or timezone.now()
    competition = session.competition
    closed = competition.status in (Competition.Status.COMPLETED, Competition.Status.DRAFT)

    if session.status == Status.PENDING:
        if closed or now >= competition.end_time:
            finalize(session, Status.EXPIRED, "never_started", now)
    elif session.status == Status.IN_PROGRESS:
        grace = timedelta(seconds=_setting("EXAM_SUBMIT_GRACE_SECONDS", 5))
        if closed:
            finalize(session, Status.EXPIRED, "competition_closed", now)
        elif now > session.deadline + grace:
            finalize(session, Status.EXPIRED, "time_expired", session.deadline)
    return session


def touch(session, now=None):
    """Record contact from the browser; log a review-only indicator after a long silence."""
    now = now or timezone.now()
    if session.status != Status.IN_PROGRESS:
        return
    gap = _setting("EXAM_CONNECTION_GAP_SECONDS", 90)
    if session.last_seen_at and (now - session.last_seen_at).total_seconds() > gap:
        seconds = int((now - session.last_seen_at).total_seconds())
        _log(session, Violation.Kind.CONNECTION_GAP, f"No contact for {seconds} seconds", now, counted=False)
    session.last_seen_at = now
    session.save(update_fields=["last_seen_at"])


def begin_session(session, now=None):
    """Start the clock. The deadline is computed here, on the server."""
    now = now or timezone.now()
    sync_session(session, now)
    if session.status == Status.IN_PROGRESS:
        return session  # idempotent
    if session.status != Status.PENDING:
        raise ExamError("not_pending", "This exam can no longer be started.")

    result = check_eligibility(session.user, session.competition, now, session=session)
    if not result.ok:
        raise ExamError(result.code, result.message)

    competition = session.competition
    deadline = competition.end_time
    if competition.duration_minutes:
        deadline = min(deadline, now + timedelta(minutes=competition.duration_minutes))
    session.status = Status.IN_PROGRESS
    session.started_at = now
    session.deadline = deadline
    session.last_seen_at = now
    session.save(update_fields=["status", "started_at", "deadline", "last_seen_at"])
    return session


def require_in_progress(session):
    if session.status != Status.IN_PROGRESS:
        raise ExamError("not_active", "This exam is not in progress.")


# ---------------------------------------------------------------- answers
def save_answer(session, question_id, code, now=None, language=""):
    now = now or timezone.now()
    sync_session(session, now)
    require_in_progress(session)

    max_length = _setting("EXAM_MAX_CODE_LENGTH", 20000)
    if len(code) > max_length:
        raise ExamError("too_long", f"Answers are limited to {max_length} characters.", 413)
    try:
        question = CodingQuestion.objects.get(pk=question_id, competition_id=session.competition_id)
    except CodingQuestion.DoesNotExist:
        raise ExamError("bad_question", "That question is not part of this exam.", 404)

    language = language if re.fullmatch(r"[a-z0-9_+\-]{1,30}", language or "") else ""
    answer, created = Answer.objects.get_or_create(
        session=session, question=question,
        defaults={"code": code, "language_slug": language, "created_at": now, "updated_at": now},
    )
    if not created:
        answer.code = code
        answer.language_slug = language
        answer.updated_at = now
        answer.save(update_fields=["code", "language_slug", "updated_at"])
    return answer


# ---------------------------------------------------------------- violations
def _log(session, kind, detail, now, counted):
    cap = _setting("EXAM_MAX_LOGGED_EVENTS", 200)
    if session.violations.count() >= cap:
        return None
    return Violation.objects.create(
        session=session, kind=kind, detail=detail[:200], occurred_at=now, counted=counted
    )


def record_violation(session, kind, detail="", now=None):
    """Log a browser-reported indicator. The server decides whether it counts."""
    now = now or timezone.now()
    sync_session(session, now)
    require_in_progress(session)

    if kind not in Violation.CLIENT_REPORTABLE:
        raise ExamError("bad_kind", "Unknown event type.", 400)

    window = _setting("EXAM_VIOLATION_DEDUPE_SECONDS", 3)
    last = session.violations.filter(counted=True).order_by("-occurred_at").first()
    counted = not (last and (now - last.occurred_at).total_seconds() < window)
    violation = _log(session, kind, detail, now, counted)

    limit = session.competition.max_violations
    if counted and violation and limit and session.counted_violations() >= limit:
        finalize(session, Status.AUTO_SUBMITTED, "violation_limit", now)
    return violation


# ---------------------------------------------------------------- submit
def submit_session(session, now=None):
    now = now or timezone.now()
    sync_session(session, now)
    if session.is_finished:
        return session  # idempotent: already ended (submitted, auto-submitted or expired)
    if session.status != Status.IN_PROGRESS:
        raise ExamError("not_active", "This exam has not begun.")
    return finalize(session, Status.SUBMITTED, "submitted_by_participant", now)


# ---------------------------------------------------------------- payloads
def state_payload(session, now=None):
    now = now or timezone.now()
    return {
        "status": session.status,
        "status_label": session.get_status_display(),
        "ended": session.is_finished,
        "seconds_remaining": session.seconds_remaining(now),
        "violations_counted": session.counted_violations(),
        "max_violations": session.competition.max_violations,
        "end_reason": session.end_reason,
    }


def questions_payload(session):
    saved = {a.question_id: a for a in session.answers.all()}
    return [
        {
            "id": q.id,
            "title": q.title,
            "difficulty": q.get_difficulty_display(),
            "marks": q.marks,
            "problem_statement": q.problem_statement,
            "input_format": q.input_format,
            "output_format": q.output_format,
            "constraints": q.constraints,
            "sample_input": q.sample_input,
            "sample_output": q.sample_output,
            "code": saved[q.id].code if q.id in saved else "",
            "language": saved[q.id].language_slug if q.id in saved else "",
        }
        for q in session.competition.questions.all()
    ]
