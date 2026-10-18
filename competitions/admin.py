from django.contrib import admin
from django.db.models import Count, Sum
from django.urls import path, reverse
from django.utils.html import format_html

from judging.admin_inlines import TestCaseInline

from .models import CodingQuestion, Competition


class CodingQuestionInline(admin.StackedInline):
    model = CodingQuestion
    extra = 0
    show_change_link = True
    fields = ("title", "difficulty", "marks", "problem_statement")
    classes = ("collapse",)


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = (
        "title", "status", "start_time", "end_time", "question_count", "total_marks", "results_state",
        "standings_link", "created_at",
    )
    list_editable = ("status",)
    list_filter = ("status", "start_time")
    search_fields = ("title", "slug", "description")
    prepopulated_fields = {"slug": ("title",)}
    date_hierarchy = "start_time"
    readonly_fields = ("created_at", "results_published_at")
    inlines = [CodingQuestionInline]
    actions = ["mark_upcoming", "mark_ongoing", "mark_completed", "publish_results", "unpublish_results"]
    fieldsets = (
        (None, {"fields": ("title", "slug", "status")}),
        ("Schedule", {"fields": ("start_time", "end_time", "created_at")}),
        ("Details", {"fields": ("description", "rules")}),
        ("Exam policy", {"fields": ("duration_minutes", "max_violations")}),
        ("Results", {
            "description": "Participants see their scores and the leaderboard only after you publish. "
                           "Use the actions on the competition list, or the Standings page.",
            "fields": ("results_published_at",),
        }),
    )

    def get_urls(self):
        from results import admin_views
        custom = [path("<int:pk>/standings/", self.admin_site.admin_view(admin_views.standings),
                       name="competitions_competition_standings")]
        return custom + super().get_urls()

    @admin.display(description="Results")
    def results_state(self, obj):
        return "Published" if obj.results_published_at else "Not published"

    @admin.display(description="Standings")
    def standings_link(self, obj):
        return format_html('<a href="{}">View</a>', reverse("admin:competitions_competition_standings", args=[obj.pk]))

    @admin.action(description="Publish results for selected")
    def publish_results(self, request, queryset):
        from results.admin_views import set_published
        self.message_user(request, f"Published results for {set_published(queryset, True)} competition(s).")

    @admin.action(description="Unpublish results for selected")
    def unpublish_results(self, request, queryset):
        from results.admin_views import set_published
        self.message_user(request, f"Unpublished results for {set_published(queryset, False)} competition(s).")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(_question_count=Count("questions"), _total_marks=Sum("questions__marks"))
        )

    @admin.display(description="Questions", ordering="_question_count")
    def question_count(self, obj):
        return obj._question_count

    @admin.display(description="Total marks", ordering="_total_marks")
    def total_marks(self, obj):
        return obj._total_marks or 0

    def _set_status(self, request, queryset, status):
        updated = queryset.update(status=status)
        self.message_user(request, f"{updated} competition(s) marked as {status}.")

    @admin.action(description="Mark selected as upcoming")
    def mark_upcoming(self, request, queryset):
        self._set_status(request, queryset, Competition.Status.UPCOMING)

    @admin.action(description="Mark selected as ongoing")
    def mark_ongoing(self, request, queryset):
        self._set_status(request, queryset, Competition.Status.ONGOING)

    @admin.action(description="Mark selected as completed")
    def mark_completed(self, request, queryset):
        self._set_status(request, queryset, Competition.Status.COMPLETED)


@admin.register(CodingQuestion)
class CodingQuestionAdmin(admin.ModelAdmin):
    list_display = ("title", "competition", "difficulty", "marks", "test_case_counts")
    inlines = [TestCaseInline]
    list_filter = ("difficulty", "competition")
    search_fields = ("title", "problem_statement", "competition__title")
    list_select_related = ("competition",)
    autocomplete_fields = ("competition",)
    fieldsets = (
        (None, {"fields": ("competition", "title", "difficulty", "marks")}),
        ("Execution limits", {"fields": ("time_limit_seconds", "memory_limit_mb")}),
        ("Problem", {"fields": ("problem_statement", "input_format", "output_format", "constraints")}),
        ("Sample", {"fields": ("sample_input", "sample_output")}),
    )

    def get_queryset(self, request):
        from django.db.models import Q
        return super().get_queryset(request).annotate(
            _public=Count("test_cases", filter=Q(test_cases__is_public=True), distinct=True),
            _private=Count("test_cases", filter=Q(test_cases__is_public=False), distinct=True),
        )

    @admin.display(description="Test cases (public / private)")
    def test_case_counts(self, obj):
        return f"{obj._public} / {obj._private}"
