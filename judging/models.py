from django.core.validators import MaxLengthValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from competitions.models import CodingQuestion, Competition
from exams.models import ExamSession

MAX_TEST_TEXT = 100_000


class Language(models.Model):
    """A programming language participants may use. Configured by administrators."""

    class EditorMode(models.TextChoices):
        PYTHON = "python", "Python"
        JAVASCRIPT = "text/javascript", "JavaScript"
        C = "text/x-csrc", "C"
        CPP = "text/x-c++src", "C++"
        JAVA = "text/x-java", "Java"
        PLAIN = "text/plain", "Plain text"

    name = models.CharField(max_length=60, unique=True, help_text="Shown to participants, e.g. 'Python 3'.")
    slug = models.SlugField(max_length=30, unique=True, help_text="Short identifier, e.g. 'python'.")
    judge0_language_id = models.PositiveIntegerField(
        "Judge0 language ID",
        help_text="The numeric ID your Judge0 server uses. Run 'manage.py check_runner' to list them; "
                  "IDs differ between Judge0 versions.",
    )
    editor_mode = models.CharField(max_length=20, choices=EditorMode.choices, default=EditorMode.PLAIN)
    starter_code = models.TextField(blank=True, help_text="Pre-filled in an empty editor.")
    is_enabled = models.BooleanField(default=True)
    position = models.PositiveIntegerField(
        default=0, help_text="Lower numbers are listed first. The first language is the default in the editor."
    )
    competitions = models.ManyToManyField(
        Competition, blank=True, related_name="allowed_languages",
        help_text="Leave empty to allow this language in every competition.",
    )

    class Meta:
        ordering = ["position", "name"]

    def __str__(self):
        return self.name


class QuestionTestCase(models.Model):
    """One input/expected-output pair. Private cases are never shown to participants."""

    question = models.ForeignKey(CodingQuestion, on_delete=models.CASCADE, related_name="test_cases")
    label = models.CharField(max_length=100, blank=True)
    input_data = models.TextField(blank=True, validators=[MaxLengthValidator(MAX_TEST_TEXT)])
    expected_output = models.TextField(validators=[MaxLengthValidator(MAX_TEST_TEXT)])
    is_public = models.BooleanField(
        default=False,
        help_text="Public cases are shown to participants and used by 'Run'. Private cases are only used when "
                  "submitting and their input and expected output are never shown.",
    )
    weight = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1)],
        help_text="Share of the question's marks this case is worth, relative to the other cases.",
    )
    position = models.PositiveIntegerField(default=0, help_text="Order in which cases run and are listed.")

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "test case"

    def __str__(self):
        kind = "public" if self.is_public else "private"
        return f"{self.label or 'Case'} ({kind}) for {self.question.title}"


class Submission(models.Model):
    """One run or submission of a participant's code. Evaluated by the external runner."""

    class Kind(models.TextChoices):
        RUN = "run", "Run (public tests only)"
        SUBMIT = "submit", "Submission (all tests, scored)"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        RUNNER_UNAVAILABLE = "runner_unavailable", "Runner unavailable (not evaluated)"
        ERROR = "error", "Evaluation error (not scored)"

    class Verdict(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        WRONG_ANSWER = "wrong_answer", "Wrong answer"
        TIME_LIMIT = "time_limit", "Time limit exceeded"
        RUNTIME_ERROR = "runtime_error", "Runtime error"
        COMPILE_ERROR = "compile_error", "Compilation error"

    PENDING_STATUSES = (Status.QUEUED, Status.RUNNING)

    session = models.ForeignKey(ExamSession, on_delete=models.CASCADE, related_name="submissions")
    question = models.ForeignKey(CodingQuestion, on_delete=models.PROTECT, related_name="submissions")
    language = models.ForeignKey(Language, on_delete=models.PROTECT, related_name="submissions")
    source_code = models.TextField()
    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    verdict = models.CharField(max_length=15, choices=Verdict.choices, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    finished_at = models.DateTimeField(null=True, blank=True)
    passed_count = models.PositiveIntegerField(default=0)
    total_count = models.PositiveIntegerField(default=0)
    passed_weight = models.PositiveIntegerField(default=0)
    total_weight = models.PositiveIntegerField(default=0)
    score = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="Calculated by the server from the weighted test results. Only for completed submissions.",
    )
    max_time_ms = models.PositiveIntegerField(null=True, blank=True)
    max_memory_kb = models.PositiveIntegerField(null=True, blank=True)
    compile_output = models.TextField(blank=True)
    message = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"#{self.pk} {self.get_kind_display()} / {self.question.title}"

    @property
    def is_finished(self):
        return self.status not in self.PENDING_STATUSES

    @property
    def counts_for_scoring(self):
        return self.kind == self.Kind.SUBMIT and self.status == self.Status.COMPLETED


class CaseResult(models.Model):
    """Outcome of one test case for one submission. Snapshots the case so later edits don't rewrite history."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PASSED = "passed", "Passed"
        WRONG_ANSWER = "wrong_answer", "Wrong answer"
        TIME_LIMIT = "time_limit", "Time limit exceeded"
        RUNTIME_ERROR = "runtime_error", "Runtime error"
        COMPILE_ERROR = "compile_error", "Compilation error"
        INTERNAL_ERROR = "internal_error", "Runner internal error"

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="results")
    test_case = models.ForeignKey(QuestionTestCase, on_delete=models.SET_NULL, null=True, blank=True, related_name="results")
    position = models.PositiveIntegerField(default=0)
    is_public = models.BooleanField(default=False)
    weight = models.PositiveIntegerField(default=1)
    input_data = models.TextField(blank=True)
    expected_output = models.TextField(blank=True)
    token = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    stdout = models.TextField(blank=True)
    stderr = models.TextField(blank=True)
    time_ms = models.PositiveIntegerField(null=True, blank=True)
    memory_kb = models.PositiveIntegerField(null=True, blank=True)
    message = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"Case {self.position + 1}: {self.get_status_display()}"
