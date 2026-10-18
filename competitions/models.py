from django.core.exceptions import ValidationError
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.urls import reverse


class Competition(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        UPCOMING = "upcoming", "Upcoming"
        ONGOING = "ongoing", "Ongoing"
        COMPLETED = "completed", "Completed"

    title = models.CharField(max_length=200)
    slug = models.SlugField(
        max_length=220,
        unique=True,
        help_text="Used in the competition's web address, for example 'injection-hunt'.",
    )
    description = models.TextField()
    start_time = models.DateTimeField("start date and time")
    end_time = models.DateTimeField("end date and time")
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
        help_text="Draft competitions are hidden from the public site.",
    )
    rules = models.TextField(blank=True)
    created_at = models.DateTimeField("creation date", auto_now_add=True)

    # Exam policy (Stage 3). Both are optional.
    duration_minutes = models.PositiveIntegerField(
        "time limit (minutes)",
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="Time each participant gets after they begin. Leave blank to run until the end time.",
    )
    results_published_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when results are published. Participants see scores and the leaderboard only after this.",
    )
    max_violations = models.PositiveSmallIntegerField(
        "automatic-submission threshold",
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text="Automatically submit an exam after this many counted violation indicators. "
                  "Leave blank to only warn and record them for review.",
    )

    class Meta:
        ordering = ["-start_time"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")),
                name="competition_end_after_start",
            ),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError({"end_time": "End time must be later than the start time."})

    def get_absolute_url(self):
        return reverse("competitions:detail", kwargs={"slug": self.slug})

    @property
    def results_published(self):
        return self.results_published_at is not None

    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    @property
    def is_completed(self):
        return self.status == self.Status.COMPLETED

    @property
    def duration_display(self):
        """Human-friendly length, such as '90 min', '2 h' or '3 days'."""
        if not (self.start_time and self.end_time):
            return ""
        minutes = int((self.end_time - self.start_time).total_seconds() // 60)
        if minutes < 60:
            return f"{minutes} min"
        if minutes < 24 * 60:
            hours, rest = divmod(minutes, 60)
            return f"{hours} h {rest} min" if rest else f"{hours} h"
        days, rest = divmod(minutes, 24 * 60)
        hours = rest // 60
        label = "day" if days == 1 else "days"
        return f"{days} {label} {hours} h" if hours else f"{days} {label}"


class CodingQuestion(models.Model):
    class Difficulty(models.TextChoices):
        EASY = "easy", "Easy"
        MEDIUM = "medium", "Medium"
        HARD = "hard", "Hard"

    competition = models.ForeignKey(
        Competition, on_delete=models.CASCADE, related_name="questions"
    )
    title = models.CharField(max_length=200)
    problem_statement = models.TextField()
    difficulty = models.CharField(
        max_length=10, choices=Difficulty.choices, default=Difficulty.EASY
    )
    input_format = models.TextField()
    output_format = models.TextField()
    constraints = models.TextField()
    sample_input = models.TextField(blank=True)
    sample_output = models.TextField()
    marks = models.PositiveIntegerField(
        default=10, validators=[MinValueValidator(1)], help_text="Points awarded for a correct solution."
    )
    time_limit_seconds = models.DecimalField(
        "time limit per test (seconds)", max_digits=4, decimal_places=1, default=Decimal("2.0"),
        validators=[MinValueValidator(Decimal("0.5")), MaxValueValidator(Decimal("10"))],
        help_text="CPU time allowed for each test case.",
    )
    memory_limit_mb = models.PositiveIntegerField(
        "memory limit (MB)", default=128,
        validators=[MinValueValidator(16), MaxValueValidator(512)],
        help_text="Memory allowed for each test case.",
    )

    class Meta:
        ordering = ["competition", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "title"], name="unique_question_title_per_competition"
            ),
        ]

    def __str__(self):
        return f"{self.title} ({self.competition})"
