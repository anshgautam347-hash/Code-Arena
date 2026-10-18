from django.contrib import admin
from django.db.models import Count, Q

from .models import Answer, ExamSession, Violation


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ViolationInline(ReadOnlyInline):
    model = Violation
    fields = ("occurred_at", "kind", "counted", "detail")
    readonly_fields = fields
    verbose_name_plural = "Violation indicators (browser events, for review only)"


class AnswerInline(ReadOnlyInline):
    model = Answer
    fields = ("question", "updated_at", "code")
    readonly_fields = fields
    verbose_name_plural = "Saved answers"


@admin.register(ExamSession)
class ExamSessionAdmin(admin.ModelAdmin):
    list_display = (
        "user", "competition", "status", "started_at", "deadline", "ended_at",
        "counted_violations_col", "events_col", "review_status", "leaderboard_excluded",
    )
    list_filter = ("status", "review_status", "leaderboard_excluded", "competition")
    search_fields = ("user__username", "user__email", "competition__title")
    date_hierarchy = "created_at"
    list_select_related = ("user", "competition")
    actions = ["mark_cleared", "mark_flagged", "exclude_from_leaderboard", "include_in_leaderboard"]
    inlines = [ViolationInline, AnswerInline]
    readonly_fields = (
        "user", "competition", "status", "end_reason", "created_at", "consented_at",
        "started_at", "deadline", "ended_at", "last_seen_at", "counted_violations_col", "events_col",
    )
    fieldsets = (
        ("Participant", {"fields": ("user", "competition")}),
        ("Timing (set by the server)", {"fields": (
            "status", "end_reason", "consented_at", "started_at", "deadline", "ended_at", "last_seen_at",
        )}),
        ("Violation indicators", {
            "description": "Browser events are indicators for human review. They are not proof of misconduct.",
            "fields": ("counted_violations_col", "events_col"),
        }),
        ("Review", {"fields": ("review_status", "review_notes", "leaderboard_excluded")}),
    )

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _counted=Count("violations", filter=Q(violations__counted=True), distinct=True),
            _events=Count("violations", distinct=True),
        )

    @admin.display(description="Counted violations", ordering="_counted")
    def counted_violations_col(self, obj):
        return getattr(obj, "_counted", None) if hasattr(obj, "_counted") else obj.counted_violations()

    @admin.display(description="All events logged", ordering="_events")
    def events_col(self, obj):
        return getattr(obj, "_events", None) if hasattr(obj, "_events") else obj.violations.count()

    @admin.action(description="Mark selected as reviewed, no concern")
    def mark_cleared(self, request, queryset):
        updated = queryset.update(review_status=ExamSession.Review.CLEARED)
        self.message_user(request, f"{updated} session(s) marked as reviewed.")

    @admin.action(description="Exclude selected from the leaderboard")
    def exclude_from_leaderboard(self, request, queryset):
        updated = queryset.update(leaderboard_excluded=True)
        self.message_user(request, f"{updated} session(s) excluded from the leaderboard. Add the reason in the review notes.")

    @admin.action(description="Include selected in the leaderboard")
    def include_in_leaderboard(self, request, queryset):
        updated = queryset.update(leaderboard_excluded=False)
        self.message_user(request, f"{updated} session(s) included in the leaderboard.")

    @admin.action(description="Flag selected for follow-up")
    def mark_flagged(self, request, queryset):
        updated = queryset.update(review_status=ExamSession.Review.FLAGGED)
        self.message_user(request, f"{updated} session(s) flagged.")


class ViewOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Violation)
class ViolationAdmin(ViewOnlyAdmin):
    list_display = ("session", "kind", "occurred_at", "counted")
    list_filter = ("kind", "counted", "session__competition")
    search_fields = ("session__user__username", "session__user__email", "session__competition__title")
    list_select_related = ("session__user", "session__competition")
    date_hierarchy = "occurred_at"


@admin.register(Answer)
class AnswerAdmin(ViewOnlyAdmin):
    list_display = ("session", "question", "updated_at", "code_length")
    list_filter = ("session__competition",)
    search_fields = ("session__user__username", "question__title", "code")
    list_select_related = ("session__user", "session__competition", "question")

    @admin.display(description="Characters")
    def code_length(self, obj):
        return len(obj.code)
