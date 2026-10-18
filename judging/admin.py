from django.contrib import admin, messages
from django.db.models import Count, Q
from django.utils.html import format_html

from exams.services import ExamError

from . import services
from .models import CaseResult, Language, QuestionTestCase, Submission


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "judge0_language_id", "editor_mode", "position", "is_enabled", "scope")
    list_editable = ("position", "is_enabled")
    list_filter = ("is_enabled", "editor_mode")
    search_fields = ("name", "slug")
    filter_horizontal = ("competitions",)
    prepopulated_fields = {"slug": ("name",)}

    @admin.display(description="Available in")
    def scope(self, obj):
        count = obj.competitions.count()
        return "All competitions" if count == 0 else f"{count} competition(s)"


@admin.register(QuestionTestCase)
class QuestionTestCaseAdmin(admin.ModelAdmin):
    list_display = ("question", "label", "is_public", "weight", "position")
    list_filter = ("is_public", "question__competition")
    search_fields = ("question__title", "label")
    list_select_related = ("question",)
    autocomplete_fields = ("question",)


class CaseResultInline(admin.TabularInline):
    model = CaseResult
    extra = 0
    can_delete = False
    fields = ("position", "is_public", "weight", "status", "time_ms", "memory_kb", "message")
    readonly_fields = fields
    verbose_name_plural = "Per-test results"

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "participant", "competition", "question", "language", "kind", "status",
        "verdict", "tests", "score", "created_at",
    )
    list_filter = ("kind", "status", "verdict", "language", "question__competition")
    search_fields = ("session__user__username", "session__user__email", "question__title", "source_code")
    date_hierarchy = "created_at"
    list_select_related = ("session__user", "question__competition", "language")
    inlines = [CaseResultInline]
    actions = ["rejudge_selected"]
    readonly_fields = (
        "session", "question", "language", "kind", "status", "verdict", "created_at", "finished_at",
        "passed_count", "total_count", "passed_weight", "total_weight", "score", "max_time_ms",
        "max_memory_kb", "message", "compile_output", "source_view",
    )
    fieldsets = (
        ("Submission", {"fields": ("session", "question", "language", "kind", "created_at", "finished_at")}),
        ("Outcome (calculated by the server)", {"fields": (
            "status", "verdict", "message", "passed_count", "total_count", "passed_weight", "total_weight",
            "score", "max_time_ms", "max_memory_kb",
        )}),
        ("Code", {"fields": ("source_view", "compile_output")}),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Participant", ordering="session__user__username")
    def participant(self, obj):
        return obj.session.user.get_username()

    @admin.display(description="Competition", ordering="question__competition__title")
    def competition(self, obj):
        return obj.question.competition

    @admin.display(description="Tests passed")
    def tests(self, obj):
        return f"{obj.passed_count}/{obj.total_count}" if obj.is_finished and obj.status == Submission.Status.COMPLETED else "-"

    @admin.display(description="Source code")
    def source_view(self, obj):
        return format_html('<pre style="white-space:pre-wrap;max-width:900px">{}</pre>', obj.source_code)

    @admin.action(description="Re-evaluate selected against the current test cases")
    def rejudge_selected(self, request, queryset):
        done = skipped = 0
        for submission in queryset:
            try:
                services.rejudge(submission)
                done += 1
            except ExamError:
                skipped += 1
        self.message_user(request, f"Queued {done} submission(s) for re-evaluation.", messages.SUCCESS)
        if skipped:
            self.message_user(request, f"Skipped {skipped} (still running or no test cases).", messages.WARNING)
