"""Competition standings.

Everything is calculated on the server from stored submissions. The ranking rules are:

1. A participant's score for a question is the HIGHEST score of any completed, scored
   submission for it (ties: the earliest such submission). The total is the sum over questions.
2. Higher total score ranks first.
3. Tie-break 1: less time to reach the final total, measured from when the participant
   began the exam to the submission that earned their last improvement. Lower is better.
4. Tie-break 2: fewer scored submissions. Fewer is better.
5. Participants equal on all three share a rank (1, 1, 3, ...).
6. Participants with no points share the last rank: the time and attempt tie-breaks only
   apply once a score has been reached, so not trying never beats trying.

Runs, runner-unavailable attempts and evaluation errors never count. Recorded violation
indicators are NOT used for ranking: they are indicators for human review. Administrators
can exclude a participant from the leaderboard after review.
"""
from decimal import Decimal

from django.db.models import Count, Q

from exams.models import ExamSession
from judging.models import Submission

INF = float("inf")


def format_duration(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def build_standings(competition):
    """Return (rows, questions). Ranked rows come first; excluded participants follow with rank None."""
    questions = list(competition.questions.all())
    sessions = list(
        ExamSession.objects.filter(competition=competition, started_at__isnull=False)
        .select_related("user")
        .annotate(counted=Count("violations", filter=Q(violations__counted=True)))
    )
    scored = (
        Submission.objects.filter(
            session__competition=competition, kind=Submission.Kind.SUBMIT, status=Submission.Status.COMPLETED
        )
        .defer("source_code", "compile_output")
        .select_related("question")
        .order_by("created_at", "id")
    )

    by_session = {
        s.pk: {
            "session": s, "user": s.user, "username": s.user.get_username(), "email": s.user.email,
            "scores": {q.pk: Decimal("0.00") for q in questions}, "best": {}, "attempts": 0,
            "attempts_by_question": {}, "violations": s.counted, "excluded": s.leaderboard_excluded,
            "review_status": s.get_review_status_display(), "status": s.get_status_display(),
        }
        for s in sessions
    }
    for sub in scored:
        row = by_session.get(sub.session_id)
        if row is None:
            continue
        row["attempts"] += 1
        row["attempts_by_question"][sub.question_id] = row["attempts_by_question"].get(sub.question_id, 0) + 1
        current = row["scores"].get(sub.question_id, Decimal("0.00"))
        if sub.question_id not in row["best"] or sub.score > current:
            row["scores"][sub.question_id] = sub.score
            elapsed = (sub.created_at - row["session"].started_at).total_seconds()
            row["best"][sub.question_id] = {
                "submission_id": sub.pk, "score": sub.score, "passed": sub.passed_count,
                "total": sub.total_count, "verdict": sub.get_verdict_display(), "elapsed": max(0, elapsed),
            }

    for row in by_session.values():
        row["total"] = sum(row["scores"].values(), Decimal("0.00"))
        improving = [b["elapsed"] for b in row["best"].values() if b["score"] > 0]
        row["finish_seconds"] = max(improving) if improving else None
        row["finish_display"] = format_duration(row["finish_seconds"])
        row["score_list"] = [row["scores"][q.pk] for q in questions]
        row["rank"] = None

    def key(r):
        if r["total"] <= 0:
            return (Decimal("0"), INF, 0)
        return (-r["total"], r["finish_seconds"] if r["finish_seconds"] is not None else INF, r["attempts"])

    included = sorted((r for r in by_session.values() if not r["excluded"]), key=lambda r: (key(r), r["username"].lower()))
    previous, rank = None, 0
    for position, row in enumerate(included, start=1):
        if key(row) != previous:
            rank = position
            previous = key(row)
        row["rank"] = rank
    excluded = sorted((r for r in by_session.values() if r["excluded"]), key=lambda r: r["username"].lower())
    return included + excluded, questions
