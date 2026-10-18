from django.contrib import admin
from django.forms import Textarea
from django.db import models

from .models import QuestionTestCase


class TestCaseInline(admin.StackedInline):
    """Test cases edited on the coding question's own page. Private cases are staff-only."""

    model = QuestionTestCase
    extra = 0
    fields = (("label", "is_public", "weight", "position"), "input_data", "expected_output")
    formfield_overrides = {models.TextField: {"widget": Textarea(attrs={"rows": 4, "cols": 80, "style": "font-family:monospace"})}}
    verbose_name_plural = "Test cases (private cases are never shown to participants)"
    classes = ("collapse",)
