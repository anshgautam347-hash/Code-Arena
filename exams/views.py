from functools import wraps

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from competitions.models import Competition

from . import services
from .forms import ParticipantLoginForm, RegistrationForm, StartExamForm
from .models import ExamSession
from .services import ExamError


# ---------------------------------------------------------------- accounts
class ParticipantLoginView(LoginView):
    template_name = "exams/login.html"
    authentication_form = ParticipantLoginForm
    redirect_authenticated_user = True


def register(request):
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = ""
    if request.user.is_authenticated:
        return redirect(next_url or "competitions:home")

    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect(next_url or "competitions:home")
    return render(request, "exams/register.html", {"form": form, "next": next_url})


# ---------------------------------------------------------------- helpers
def _public_competition(slug):
    return get_object_or_404(Competition.objects.exclude(status=Competition.Status.DRAFT), slug=slug)


def _session_or_none(user, competition):
    return (
        ExamSession.objects.select_related("competition", "user")
        .filter(user=user, competition=competition)
        .first()
    )


def ajax_login_required(view):
    """Like login_required, but answers API calls with JSON instead of a redirect."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "auth", "message": "Please sign in again."}, status=401)
        return view(request, *args, **kwargs)

    return wrapper


def _error(exc, session=None):
    body = {"error": exc.code, "message": exc.message}
    if session is not None:
        body["state"] = services.state_payload(session)
    return JsonResponse(body, status=exc.http_status)


def _load_session(request, slug):
    """The signed-in user's own session. There is no way to name someone else's."""
    competition = _public_competition(slug)
    session = _session_or_none(request.user, competition)
    if session is None:
        raise ExamError("no_session", "You have not started this exam.", 404)
    now = timezone.now()
    services.sync_session(session, now)
    services.touch(session, now)
    return session


def _api(view):
    """Wrap an endpoint: login required, never cached, rule violations become JSON errors."""

    @wraps(view)
    @ajax_login_required
    @never_cache
    def wrapper(request, slug, **kwargs):
        session = None
        try:
            session = _load_session(request, slug)
            return view(request, session, **kwargs)
        except ExamError as exc:
            return _error(exc, session)

    return wrapper


# ---------------------------------------------------------------- pages
@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def start(request, slug):
    competition = _public_competition(slug)
    now = timezone.now()
    session = _session_or_none(request.user, competition)
    if session:
        services.sync_session(session, now)
        if session.is_finished or session.is_active:
            return redirect("exams:exam", slug=slug)

    eligibility = services.check_eligibility(request.user, competition, now, session)
    form = StartExamForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and eligibility.ok and form.is_valid():
        try:
            services.create_session(request.user, competition, now)
        except ExamError as exc:
            eligibility = services.Eligibility(False, exc.code, exc.message)
        else:
            return redirect("exams:exam", slug=slug)

    return render(request, "exams/start.html", {
        "competition": competition,
        "eligibility": eligibility,
        "form": form,
        "question_count": competition.questions.count(),
        "total_marks": sum(competition.questions.values_list("marks", flat=True)),
        "active_nav": "competitions",
    })


@login_required
@never_cache
@require_GET
def exam(request, slug):
    competition = _public_competition(slug)
    session = _session_or_none(request.user, competition)
    if session is None:
        return redirect("exams:start", slug=slug)

    now = timezone.now()
    services.sync_session(session, now)
    if session.is_finished:
        answered = session.answers.exclude(code="").count()
        return render(request, "exams/finished.html", {
            "competition": competition,
            "session": session,
            "answered": answered,
            "question_count": competition.questions.count(),
            "violations_counted": session.counted_violations(),
            "active_nav": "competitions",
        })

    services.touch(session, now)
    config = {
        "csrfToken": get_token(request),
        "examUrl": reverse("exams:exam", args=[slug]),
        "urls": {
            "state": reverse("exams:state", args=[slug]),
            "begin": reverse("exams:begin", args=[slug]),
            "questions": reverse("exams:questions", args=[slug]),
            "violation": reverse("exams:violation", args=[slug]),
            "submit": reverse("exams:submit", args=[slug]),
            "answerTemplate": reverse("exams:answer", args=[slug, 0]),
            "judging": reverse("judging:overview", args=[slug]),
            "runTemplate": reverse("judging:run", args=[slug, 0]),
            "submitTemplate": reverse("judging:submit", args=[slug, 0]),
            "submissionTemplate": reverse("judging:submission", args=[slug, 0]),
        },
        "state": services.state_payload(session, now),
        "maxCodeLength": getattr(settings, "EXAM_MAX_CODE_LENGTH", 20000),
        "syncSeconds": 15,
    }
    return render(request, "exams/exam.html", {"competition": competition, "config": config})


# ---------------------------------------------------------------- JSON endpoints
@_api
@require_GET
def state(request, session):
    return JsonResponse({"state": services.state_payload(session)})


@_api
@require_POST
def begin(request, session):
    if request.POST.get("fullscreen") != "1":
        raise ExamError("fullscreen_required", "Enter fullscreen mode to begin the exam.", 400)
    services.begin_session(session)
    return JsonResponse({"state": services.state_payload(session)})


@_api
@require_GET
def questions(request, session):
    services.require_in_progress(session)
    return JsonResponse({
        "questions": services.questions_payload(session),
        "state": services.state_payload(session),
    })


@_api
@require_POST
def answer(request, session, question_id):
    services.save_answer(
        session, question_id, request.POST.get("code", ""), language=request.POST.get("language", "")
    )
    return JsonResponse({"saved": True, "state": services.state_payload(session)})


@_api
@require_POST
def violation(request, session):
    recorded = services.record_violation(
        session, request.POST.get("kind", ""), request.POST.get("detail", "")
    )
    return JsonResponse({"recorded": recorded is not None, "state": services.state_payload(session)})


@_api
@require_POST
def submit(request, session):
    services.submit_session(session)
    return JsonResponse({"state": services.state_payload(session)})
