from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from competitions.models import CodingQuestion, Competition


class ExamSession(models.Model):
    """One participant's single attempt at one competition."""

    class Status(models.TextChoices):
        PENDING = "pending", "Not begun"
        IN_PROGRESS = "in_progress", "In progress"
        SUBMITTED = "submitted", "Submitted"
        AUTO_SUBMITTED = "auto_submitted", "Auto-submitted (violation limit)"
        EXPIRED = "expired", "Time expired"

    class Review(models.TextChoices):
        UNREVIEWED = "unreviewed", "Not reviewed"
        CLEARED = "cleared", "Reviewed, no concern"
        FLAGGED = "flagged", "Flagged for follow-up"

    ACTIVE_STATUSES = (Status.PENDING, Status.IN_PROGRESS)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="exam_sessions"
    )
    competition = models.ForeignKey(
        Competition, on_delete=models.PROTECT, related_name="exam_sessions"
    )
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    consented_at = models.DateTimeField(
        default=timezone.now, editable=False,
        help_text="When the participant accepted the monitoring notice.",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(
        null=True, blank=True, help_text="Set by the server when the exam begins."
    )
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=40, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    review_status = models.CharField(
        max_length=12, choices=Review.choices, default=Review.UNREVIEWED
    )
    review_notes = models.TextField(blank=True)
    leaderboard_excluded = models.BooleanField(
        default=False,
        help_text="Hide this participant from the public leaderboard (for example after review). "
                  "Their submissions are kept. Record the reason in the review notes.",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "competition"], name="one_exam_session_per_user_per_competition"
            ),
        ]

    def __str__(self):
        return f"{self.user} / {self.competition}"

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    @property
    def is_finished(self):
        return not self.is_active

    def seconds_remaining(self, now=None):
        if self.status != self.Status.IN_PROGRESS or not self.deadline:
            return 0
        now = now or timezone.now()
        return max(0, int((self.deadline - now).total_seconds()))

    def counted_violations(self):
        return self.violations.filter(counted=True).count()


class Violation(models.Model):
    """A browser event that may indicate the participant left the exam environment.

    These are indicators for human review. They are not proof of misconduct.
    """

    class Kind(models.TextChoices):
        TAB_HIDDEN = "tab_hidden", "Tab or window hidden"
        FULLSCREEN_EXIT = "fullscreen_exit", "Left fullscreen"
        FOCUS_LOST = "focus_lost", "Window lost focus"
        NAVIGATION_ATTEMPT = "navigation_attempt", "Tried to leave or reload the page"
        CONNECTION_GAP = "connection_gap", "No contact from browser (server-detected)"

    CLIENT_REPORTABLE = (Kind.TAB_HIDDEN, Kind.FULLSCREEN_EXIT, Kind.FOCUS_LOST, Kind.NAVIGATION_ATTEMPT)

    session = models.ForeignKey(ExamSession, on_delete=models.CASCADE, related_name="violations")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    detail = models.CharField(max_length=200, blank=True, help_text="Unverified text from the browser.")
    occurred_at = models.DateTimeField(
        default=timezone.now, editable=False, help_text="Server time when the event was received."
    )
    counted = models.BooleanField(
        default=True,
        help_text="Whether this event counts toward the competition's violation limit.",
    )

    class Meta:
        ordering = ["occurred_at", "id"]
        verbose_name = "violation indicator"

    def __str__(self):
        return f"{self.get_kind_display()} at {self.occurred_at:%Y-%m-%d %H:%M:%S}"


class Answer(models.Model):
    """The code a participant has saved for one question (placeholder editor; not executed)."""

    session = models.ForeignKey(ExamSession, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(CodingQuestion, on_delete=models.CASCADE, related_name="answers")
    code = models.TextField(blank=True)
    language_slug = models.CharField(max_length=30, blank=True, help_text="Language chosen in the editor.")
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["question_id"]
        constraints = [
            models.UniqueConstraint(fields=["session", "question"], name="one_answer_per_question_per_session"),
        ]

    def __str__(self):
        return f"Answer to {self.question.title}"

    def clean(self):
        super().clean()
        if self.session_id and self.question_id and self.question.competition_id != self.session.competition_id:
            raise ValidationError("The question does not belong to this session's competition.")
