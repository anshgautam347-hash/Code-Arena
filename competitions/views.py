from django.db.models import Case, IntegerField, Value, When
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from .models import Competition

HOME_CARD_LIMIT = 6

# The Stage 1 cards used green, amber and blue in that order. Cards keep that rhythm.
CARD_THEMES = ["green", "amber", "blue"]


def home(request):
    status_order = Case(
        When(status=Competition.Status.ONGOING, then=Value(0)),
        When(status=Competition.Status.UPCOMING, then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )
    competitions = list(
        Competition.objects.exclude(status=Competition.Status.DRAFT)
        .annotate(status_order=status_order)
        .order_by("status_order", "start_time")
        .prefetch_related("questions")[:HOME_CARD_LIMIT]
    )
    for index, competition in enumerate(competitions):
        questions = list(competition.questions.all())
        competition.theme = CARD_THEMES[index % len(CARD_THEMES)]
        competition.question_count = len(questions)
        competition.total_marks = sum(q.marks for q in questions)
        competition.preview_questions = questions[:3]
        competition.more_questions = max(len(questions) - 3, 0)

    return render(
        request,
        "competitions/home.html",
        {"competitions": competitions, "active_nav": "overview"},
    )


def detail(request, slug):
    competition = get_object_or_404(Competition, slug=slug)

    # Drafts are only previewable by signed-in admin staff.
    if competition.is_draft and not request.user.is_staff:
        raise Http404("Competition not found.")

    questions = list(competition.questions.all())
    context = {
        "competition": competition,
        "questions": questions,
        "total_marks": sum(q.marks for q in questions),
        # Full problem details stay hidden until the competition is over.
        # Staff can always see them (they are previewing from the admin).
        "show_question_details": competition.is_completed or request.user.is_staff,
        "active_nav": "competitions",
    }
    return render(request, "competitions/detail.html", context)
