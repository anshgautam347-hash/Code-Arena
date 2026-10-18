from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from .models import CodingQuestion, Competition


def make_competition(slug="demo", status=Competition.Status.UPCOMING, hours=2, **extra):
    start = timezone.now() + timedelta(days=1)
    return Competition.objects.create(
        title=extra.pop("title", slug.replace("-", " ").title()),
        slug=slug,
        description="A description.",
        start_time=start,
        end_time=start + timedelta(hours=hours),
        status=status,
        **extra,
    )


def make_question(competition, title="Q1", marks=10, **extra):
    return CodingQuestion.objects.create(
        competition=competition,
        title=title,
        problem_statement="Fix the bug.",
        input_format="One line.",
        output_format="One line.",
        constraints="n <= 10",
        sample_input="1",
        sample_output="2",
        marks=marks,
        **extra,
    )


# ---------------------------------------------------------------- Stage 1 behavior
class HomepageTests(TestCase):
    def test_homepage_renders(self):
        response = self.client.get(reverse("competitions:home"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "competitions/home.html")
        self.assertTemplateUsed(response, "competitions/base.html")
        self.assertContains(response, "SecureCode")
        self.assertContains(response, "View competitions")
        self.assertContains(response, 'id="competitions"')
        self.assertContains(response, 'id="principles"')
        self.assertContains(response, "Our mission")
        self.assertNotContains(response, "__ARENA_IMAGE__")
        self.assertNotContains(response, "{%")

    def test_home_route(self):
        self.assertEqual(reverse("competitions:home"), "/")
        self.assertEqual(resolve("/").view_name, "competitions:home")

    def test_static_files_exist(self):
        for filename in ["styles.css", "main.js", "arena.jpg", "favicon.svg"]:
            self.assertIsNotNone(finders.find(f"competitions/{filename}"))

    def test_unknown_page_returns_404(self):
        self.assertEqual(self.client.get("/not-a-page/").status_code, 404)

    def test_navigation_links_work_from_any_page(self):
        competition = make_competition("nav")
        for url in ["/", competition.get_absolute_url()]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, 'href="/#competitions"')
                self.assertContains(response, 'href="/#principles"')
                self.assertContains(response, 'class="menu-toggle"')


# ---------------------------------------------------------------- Stage 2: models
class CompetitionModelTests(TestCase):
    def test_str_and_default_status(self):
        competition = make_competition("alpha", title="Alpha")
        self.assertEqual(str(competition), "Alpha")
        self.assertEqual(Competition(title="x").status, Competition.Status.DRAFT)

    def test_created_at_is_set_automatically(self):
        self.assertIsNotNone(make_competition().created_at)

    def test_slug_must_be_unique(self):
        make_competition("same")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_competition("same")

    def test_end_must_be_after_start(self):
        now = timezone.now()
        competition = Competition(
            title="Bad", slug="bad", description="d", start_time=now, end_time=now
        )
        with self.assertRaises(ValidationError) as ctx:
            competition.full_clean()
        self.assertIn("end_time", ctx.exception.message_dict)

    def test_database_rejects_end_before_start(self):
        now = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Competition.objects.create(
                title="Bad", slug="bad", description="d",
                start_time=now, end_time=now - timedelta(hours=1),
            )

    def test_valid_competition_passes_validation(self):
        make_competition().full_clean()

    def test_absolute_url(self):
        self.assertEqual(make_competition("abc").get_absolute_url(), "/competitions/abc/")

    def test_duration_display(self):
        start = timezone.now()
        cases = [(45, "45 min"), (90, "1 h 30 min"), (120, "2 h"), (24 * 60, "1 day"),
                 (72 * 60, "3 days"), (30 * 60, "1 day 6 h")]
        for minutes, expected in cases:
            with self.subTest(minutes=minutes):
                c = Competition(start_time=start, end_time=start + timedelta(minutes=minutes))
                self.assertEqual(c.duration_display, expected)


class CodingQuestionModelTests(TestCase):
    def setUp(self):
        self.competition = make_competition()

    def test_str_relation_and_defaults(self):
        question = make_question(self.competition, "Find the flaw")
        self.assertIn("Find the flaw", str(question))
        self.assertEqual(list(self.competition.questions.all()), [question])
        self.assertEqual(question.difficulty, CodingQuestion.Difficulty.EASY)

    def test_marks_must_be_positive(self):
        question = make_question(self.competition)
        question.marks = 0
        with self.assertRaises(ValidationError) as ctx:
            question.full_clean()
        self.assertIn("marks", ctx.exception.message_dict)

    def test_title_unique_within_competition_only(self):
        make_question(self.competition, "Same")
        make_question(make_competition("other"), "Same")  # allowed in a different competition
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_question(self.competition, "Same")

    def test_deleting_competition_deletes_questions(self):
        make_question(self.competition)
        self.competition.delete()
        self.assertEqual(CodingQuestion.objects.count(), 0)


# ---------------------------------------------------------------- Stage 2: homepage from the database
class HomepageCompetitionsTests(TestCase):
    def test_empty_state_without_competitions(self):
        response = self.client.get("/")
        self.assertContains(response, "No competitions yet.")
        self.assertNotContains(response, 'class="competition-card')

    def test_cards_come_from_database(self):
        competition = make_competition("db-card", title="Database Card")
        make_question(competition, "First", marks=15)
        make_question(competition, "Second", marks=5)
        response = self.client.get("/")
        self.assertContains(response, 'class="competition-card', count=1)
        self.assertContains(response, "Database Card")
        self.assertContains(response, f'href="{competition.get_absolute_url()}"')
        self.assertContains(response, "2 questions")
        self.assertContains(response, "20 marks")
        self.assertContains(response, "<li>First</li>")
        self.assertNotContains(response, "No competitions yet.")
        self.assertNotContains(response, "Injection Defense")  # old hardcoded card is gone

    def test_cards_keep_green_amber_blue_design(self):
        for slug in ["a", "b", "c", "d"]:
            make_competition(slug)
        response = self.client.get("/")
        themes = [c.theme for c in response.context["competitions"]]
        self.assertEqual(themes, ["green", "amber", "blue", "green"])
        for css_class in ["green-card", "amber-card", "blue-card", "challenge-details", "tags"]:
            self.assertContains(response, css_class)

    def test_draft_competitions_are_hidden(self):
        make_competition("secret", status=Competition.Status.DRAFT, title="Secret Draft")
        self.assertNotContains(self.client.get("/"), "Secret Draft")

    def test_ongoing_listed_before_upcoming_and_completed(self):
        make_competition("c", status=Competition.Status.COMPLETED, title="Done One")
        make_competition("u", status=Competition.Status.UPCOMING, title="Soon One")
        make_competition("o", status=Competition.Status.ONGOING, title="Live One")
        titles = [c.title for c in self.client.get("/").context["competitions"]]
        self.assertEqual(titles, ["Live One", "Soon One", "Done One"])

    def test_more_questions_summary(self):
        competition = make_competition("many")
        for n in range(5):
            make_question(competition, f"Q{n}")
        self.assertContains(self.client.get("/"), "and 2 more")

    def test_homepage_query_count_does_not_grow_with_competitions(self):
        for n in range(5):
            make_question(make_competition(f"c{n}"), f"Q{n}")
        with self.assertNumQueries(2):  # competitions + prefetched questions
            self.client.get("/")


# ---------------------------------------------------------------- Stage 2: detail page
class DetailPageTests(TestCase):
    def test_detail_shows_schedule_rules_and_questions(self):
        competition = make_competition(
            "detail", status=Competition.Status.ONGOING, rules="No cheating.\nBe kind."
        )
        make_question(competition, "Fix the query", marks=12)
        response = self.client.get(competition.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "competitions/detail.html")
        self.assertContains(response, "A description.")
        self.assertContains(response, "No cheating.")
        self.assertContains(response, "Fix the query")
        self.assertContains(response, "12 marks")
        self.assertEqual(response.context["total_marks"], 12)
        self.assertNotContains(response, "{%")

    def test_problem_details_hidden_while_ongoing(self):
        competition = make_competition("live", status=Competition.Status.ONGOING)
        make_question(competition, "Hidden Q")
        response = self.client.get(competition.get_absolute_url())
        self.assertContains(response, "Hidden Q")
        self.assertNotContains(response, "Fix the bug.")
        self.assertContains(response, "shown after the competition ends")

    def test_problem_details_shown_when_completed(self):
        competition = make_competition("over", status=Competition.Status.COMPLETED)
        make_question(competition)
        response = self.client.get(competition.get_absolute_url())
        self.assertContains(response, "Fix the bug.")
        self.assertContains(response, "Sample input")

    def test_rules_section_omitted_when_empty(self):
        competition = make_competition("norules")
        self.assertNotContains(self.client.get(competition.get_absolute_url()), "Rules</h2>")

    def test_empty_questions_message(self):
        competition = make_competition("empty")
        self.assertContains(
            self.client.get(competition.get_absolute_url()), "No questions have been added yet"
        )

    def test_unknown_slug_returns_404(self):
        self.assertEqual(self.client.get("/competitions/nope/").status_code, 404)

    def test_draft_is_404_for_public_but_visible_to_staff(self):
        competition = make_competition("draft", status=Competition.Status.DRAFT)
        make_question(competition)
        self.assertEqual(self.client.get(competition.get_absolute_url()).status_code, 404)

        staff = get_user_model().objects.create_user("staff", password="pw", is_staff=True)
        self.client.force_login(staff)
        response = self.client.get(competition.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DRAFT PREVIEW")
        self.assertContains(response, "Fix the bug.")

    def test_description_html_is_escaped(self):
        competition = make_competition("xss")
        competition.description = "<script>alert(1)</script>"
        competition.save()
        response = self.client.get(competition.get_absolute_url())
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")


# ---------------------------------------------------------------- Stage 2: admin
class AdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = get_user_model().objects.create_superuser("boss", "boss@example.com", "pw")
        cls.competition = make_competition("admin-comp", title="Admin Comp")
        cls.question = make_question(cls.competition, "Admin Q")

    def test_admin_requires_login(self):
        response = self.client.get("/admin/competitions/competition/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_admin_pages_load_for_superuser(self):
        self.client.force_login(self.admin_user)
        urls = [
            "/admin/",
            "/admin/competitions/competition/",
            "/admin/competitions/competition/add/",
            f"/admin/competitions/competition/{self.competition.pk}/change/",
            "/admin/competitions/codingquestion/",
            "/admin/competitions/codingquestion/add/",
            f"/admin/competitions/codingquestion/{self.question.pk}/change/",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_admin_search_and_filter(self):
        self.client.force_login(self.admin_user)
        response = self.client.get("/admin/competitions/competition/?q=admin-comp&status=upcoming")
        self.assertContains(response, "Admin Comp")
        response = self.client.get("/admin/competitions/competition/?status=completed")
        self.assertNotContains(response, "Admin Comp")

    def test_admin_rejects_end_before_start(self):
        self.client.force_login(self.admin_user)
        response = self.client.post("/admin/competitions/competition/add/", {
            "title": "Bad Dates", "slug": "bad-dates", "status": "draft",
            "description": "d", "rules": "",
            "start_time_0": "2030-01-02", "start_time_1": "10:00:00",
            "end_time_0": "2030-01-01", "end_time_1": "10:00:00",
            "questions-TOTAL_FORMS": "0", "questions-INITIAL_FORMS": "0",
            "questions-MIN_NUM_FORMS": "0", "questions-MAX_NUM_FORMS": "1000",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "End time must be later than the start time.")
        self.assertFalse(Competition.objects.filter(slug="bad-dates").exists())

    def test_admin_status_actions(self):
        self.client.force_login(self.admin_user)
        self.client.post("/admin/competitions/competition/", {
            "action": "mark_completed", "_selected_action": [self.competition.pk],
        })
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, Competition.Status.COMPLETED)


# ---------------------------------------------------------------- Stage 2: sample data command
class SeedCommandTests(TestCase):
    def test_seed_is_idempotent_and_valid(self):
        call_command("seed_demo_data", stdout=StringIO())
        call_command("seed_demo_data", stdout=StringIO())
        self.assertEqual(Competition.objects.count(), 3)
        for competition in Competition.objects.all():
            competition.full_clean()
        response = self.client.get("/")
        self.assertContains(response, "Injection Defense")
        self.assertContains(response, "Authentication Under Fire")
        self.assertContains(response, "Secure API Foundations")
