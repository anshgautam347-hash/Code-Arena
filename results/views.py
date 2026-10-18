from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from competitions.models import Competition
from exams import services as exam_services
from exams.models import ExamSession
from judging import services as judging_services
from judging.models import Submission

from .services import build_standings


def _competition(slug):
    return get_object_or_404(Competition.objects.exclude(status=Competition.Status.DRAFT), slug=slug)


@login_required
@never_cache
def mine(request, slug):
    """The signed-in participant's own results and submission history."""
    competition = _competition(slug)
    session = ExamSession.objects.filter(user=request.user, competition=competition).first()
    if session is None:
        return redirect("exams:start", slug=slug)
    exam_services.sync_session(session, timezone.now())
    if session.is_active:
        return redirect("exams:exam", slug=slug)

    judging_services.reconcile(Submission.objects.filter(session=session))
    rows, questions = build_standings(competition)
    mine_row = next((r for r in rows if r["session"].pk == session.pk), None)
    published = competition.results_published
    ranked = [r for r in rows if not r["excluded"]]

    question_rows = []
    for q in questions:
        best = mine_row["best"].get(q.pk) if mine_row else None
        question_rows.append({
            "question": q, "best": best,
            "attempts": mine_row["attempts_by_question"].get(q.pk, 0) if mine_row else 0,
            "score": mine_row["scores"][q.pk] if mine_row else None,
        })
    submissions = session.submissions.select_related("question", "language").order_by("-created_at")[:200]
    pending = session.submissions.filter(status__in=Submission.PENDING_STATUSES).exists()
    return render(request, "results/mine.html", {
        "competition": competition, "session": session, "published": published, "row": mine_row,
        "question_rows": question_rows, "submissions": submissions, "pending": pending,
        "participants": len(ranked), "active_nav": "competitions",
    })


@login_required
@never_cache
def submission_detail(request, slug, submission_id):
    """One of the signed-in participant's own submissions. Other people's give 404."""
    competition = _competition(slug)
    submission = get_object_or_404(
        Submission.objects.select_related("question", "language", "session"),
        pk=submission_id, session__user=request.user, question__competition=competition,
    )
    submission = judging_services.refresh_submission(submission)
    payload = judging_services.participant_payload(submission)
    return render(request, "results/submission.html", {
        "competition": competition, "submission": submission, "payload": payload,
        "published": competition.results_published, "active_nav": "competitions",
    })


@never_cache
def leaderboard(request, slug):
    competition = _competition(slug)
    published = competition.results_published
    preview = request.user.is_staff and not published
    context = {"competition": competition, "published": published, "preview": preview, "active_nav": "competitions"}
    if published or preview:
        judging_services.reconcile(
            Submission.objects.filter(session__competition=competition), limit=50
        )
        rows, questions = build_standings(competition)
        rows = [r for r in rows if not r["excluded"]][: getattr(settings, "LEADERBOARD_MAX_ROWS", 200)]
        context.update({"rows": rows, "questions": questions, "me": request.user.get_username() if request.user.is_authenticated else None})
    return render(request, "results/leaderboard.html", context)
