from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from competitions.models import CodingQuestion, Competition

from . import services
from .models import Answer, ExamSession, Violation
from .services import ExamError

User = get_user_model()
GOOD_PASSWORD = "S3cure-pass-123"


def at(moment):
    """Make the server clock return `moment` (for timer tests)."""
    return mock.patch("django.utils.timezone.now", return_value=moment)


# A fast hasher keeps the suite quick. Production uses Django's default (PBKDF2).
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ExamTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.competition = Competition.objects.create(
            title="Live Cup", slug="live-cup", description="d",
            start_time=self.now - timedelta(hours=1), end_time=self.now + timedelta(hours=2),
            status=Competition.Status.ONGOING, duration_minutes=60,
        )
        self.q1 = self.make_question(self.competition, "Q1", "SECRET-STATEMENT-1")
        self.q2 = self.make_question(self.competition, "Q2", "SECRET-STATEMENT-2")
        self.alice = User.objects.create_user("alice", "alice@example.com", GOOD_PASSWORD)
        self.bob = User.objects.create_user("bob", "bob@example.com", GOOD_PASSWORD)

    @staticmethod
    def make_question(competition, title, statement="Fix it."):
        return CodingQuestion.objects.create(
            competition=competition, title=title, problem_statement=statement,
            input_format="i", output_format="o", constraints="c",
            sample_input="1", sample_output="2", marks=10,
        )

    def url(self, name, *args, slug=None):
        return reverse(f"exams:{name}", args=[slug or self.competition.slug, *args])

    def begun(self, user=None, when=None):
        """A session that has already begun at `when` (default: now)."""
        when = when or self.now
        session = services.create_session(user or self.alice, self.competition, when)
        return services.begin_session(session, when)

    def pending(self, user=None):
        return services.create_session(user or self.alice, self.competition, self.now)

    def post(self, name, data=None, when=None, *args):
        if when is None:
            return self.client.post(self.url(name, *args), data or {})
        with at(when):
            return self.client.post(self.url(name, *args), data or {})


# ================================================================ accounts
class AuthenticationTests(ExamTestCase):
    def test_register_creates_account_and_signs_in(self):
        response = self.client.post(reverse("exams:register"), {
            "username": "carol", "email": "carol@example.com",
            "password1": GOOD_PASSWORD, "password2": GOOD_PASSWORD,
        })
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        user = User.objects.get(username="carol")
        self.assertEqual(user.email, "carol@example.com")
        self.assertFalse(user.is_staff)
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_register_rejects_duplicate_email_ignoring_case(self):
        response = self.client.post(reverse("exams:register"), {
            "username": "carol", "email": "ALICE@example.com",
            "password1": GOOD_PASSWORD, "password2": GOOD_PASSWORD,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertFalse(User.objects.filter(username="carol").exists())

    def test_register_rejects_weak_or_mismatched_passwords(self):
        for p1, p2 in [("12345678", "12345678"), (GOOD_PASSWORD, "different-pass-999")]:
            with self.subTest(p1=p1):
                response = self.client.post(reverse("exams:register"), {
                    "username": "dave", "email": "dave@example.com", "password1": p1, "password2": p2,
                })
                self.assertEqual(response.status_code, 200)
                self.assertFalse(User.objects.filter(username="dave").exists())

    def test_register_requires_email(self):
        response = self.client.post(reverse("exams:register"), {
            "username": "erin", "password1": GOOD_PASSWORD, "password2": GOOD_PASSWORD,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="erin").exists())

    def test_register_ignores_offsite_next_url(self):
        response = self.client.post(reverse("exams:register"), {
            "username": "frank", "email": "frank@example.com", "next": "https://evil.example/",
            "password1": GOOD_PASSWORD, "password2": GOOD_PASSWORD,
        })
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_login_logout_flow(self):
        response = self.client.post(reverse("exams:login"), {"username": "alice", "password": GOOD_PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)
        # Logging out needs POST so a link on another site cannot sign you out.
        self.assertEqual(self.client.get(reverse("exams:logout")).status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)
        self.client.post(reverse("exams:logout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_rejects_wrong_password(self):
        response = self.client.post(reverse("exams:login"), {"username": "alice", "password": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_honours_safe_next_only(self):
        good = self.client.post(reverse("exams:login"), {
            "username": "alice", "password": GOOD_PASSWORD, "next": self.url("start")})
        self.assertEqual(good["Location"], self.url("start"))
        self.client.post(reverse("exams:logout"))
        bad = self.client.post(reverse("exams:login"), {
            "username": "alice", "password": GOOD_PASSWORD, "next": "https://evil.example/"})
        self.assertEqual(bad["Location"], "/")

    def test_exam_pages_require_login(self):
        for name in ["start", "exam"]:
            with self.subTest(name=name):
                response = self.client.get(self.url(name))
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("exams:login")))

    def test_exam_api_returns_401_json_when_signed_out(self):
        for name, method in [("state", "get"), ("begin", "post"), ("questions", "get"),
                             ("violation", "post"), ("submit", "post")]:
            with self.subTest(name=name):
                response = getattr(self.client, method)(self.url(name if name != "violation" else "violation"))
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"], "auth")
        self.assertEqual(self.client.post(self.url("answer", self.q1.id)).status_code, 401)

    def test_navigation_shows_account_state(self):
        self.assertContains(self.client.get("/"), "Sign in")
        self.client.force_login(self.alice)
        response = self.client.get("/")
        self.assertContains(response, "alice")
        self.assertContains(response, "Log out")
        self.assertNotContains(response, "Register")

    def test_post_endpoints_enforce_csrf(self):
        self.pending()
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.alice)
        response = strict.post(self.url("begin"), {"fullscreen": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ExamSession.objects.get().status, ExamSession.Status.PENDING)


# ================================================================ eligibility and check-in
class EligibilityTests(ExamTestCase):
    def check(self, user=None, when=None):
        return services.check_eligibility(user or self.alice, self.competition, when or self.now)

    def test_open_competition_is_eligible(self):
        self.assertTrue(self.check().ok)

    def test_status_must_be_ongoing(self):
        cases = {
            Competition.Status.UPCOMING: "not_open",
            Competition.Status.COMPLETED: "closed",
            Competition.Status.DRAFT: "unavailable",
        }
        for status, code in cases.items():
            with self.subTest(status=status):
                self.competition.status = status
                self.competition.save()
                result = self.check()
                self.assertFalse(result.ok)
                self.assertEqual(result.code, code)

    def test_time_window_is_enforced_even_if_status_is_ongoing(self):
        before = self.check(when=self.competition.start_time - timedelta(minutes=1))
        after = self.check(when=self.competition.end_time)
        self.assertEqual((before.ok, before.code), (False, "not_started"))
        self.assertEqual((after.ok, after.code), (False, "closed"))

    def test_competition_needs_questions(self):
        self.competition.questions.all().delete()
        self.assertEqual(self.check().code, "no_questions")

    def test_one_attempt_per_participant(self):
        session = self.begun()
        services.submit_session(session, self.now)
        self.assertEqual(self.check().code, "already_attempted")
        self.assertTrue(self.check(self.bob).ok)

    def test_unfinished_session_may_resume(self):
        self.pending()
        self.assertTrue(self.check().ok)

    def test_start_page_shows_monitoring_notice(self):
        self.client.force_login(self.alice)
        response = self.client.get(self.url("start"))
        self.assertEqual(response.status_code, 200)
        for text in ["Monitoring notice", "What is monitored", "What is not monitored",
                     "cannot stop you", "not proof", "No camera or microphone"]:
            self.assertContains(response, text)

    def test_start_page_states_the_automatic_submission_policy(self):
        self.client.force_login(self.alice)
        self.assertContains(self.client.get(self.url("start")), "does not submit automatically")
        self.competition.max_violations = 4
        self.competition.save()
        self.assertContains(self.client.get(self.url("start")), "after <strong>4</strong>")

    def test_start_requires_accepting_the_notice(self):
        self.client.force_login(self.alice)
        response = self.client.post(self.url("start"), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You must accept the monitoring notice")
        self.assertFalse(ExamSession.objects.exists())

    def test_start_creates_one_session_and_is_idempotent(self):
        self.client.force_login(self.alice)
        for _ in range(2):
            response = self.client.post(self.url("start"), {"accept": "on"})
            self.assertRedirects(response, self.url("exam"), fetch_redirect_response=False)
        session = ExamSession.objects.get()
        self.assertEqual((session.user, session.competition), (self.alice, self.competition))
        self.assertEqual(session.status, ExamSession.Status.PENDING)
        self.assertIsNone(session.started_at)

    def test_ineligible_participant_cannot_create_a_session(self):
        self.competition.status = Competition.Status.UPCOMING
        self.competition.save()
        self.client.force_login(self.alice)
        response = self.client.post(self.url("start"), {"accept": "on"})
        self.assertContains(response, "not opened yet")
        self.assertFalse(ExamSession.objects.exists())

    def test_draft_competition_has_no_exam_page(self):
        self.competition.status = Competition.Status.DRAFT
        self.competition.save()
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(self.url("start")).status_code, 404)

    def test_detail_page_links_to_exam_only_while_ongoing(self):
        self.assertContains(self.client.get(self.competition.get_absolute_url()), "Enter exam")
        self.competition.status = Competition.Status.UPCOMING
        self.competition.save()
        self.assertNotContains(self.client.get(self.competition.get_absolute_url()), "Enter exam")


# ================================================================ starting and the timer
class BeginAndTimerTests(ExamTestCase):
    def test_begin_requires_fullscreen_assertion(self):
        self.pending()
        self.client.force_login(self.alice)
        response = self.post("begin", {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "fullscreen_required")
        self.assertEqual(ExamSession.objects.get().status, ExamSession.Status.PENDING)

    def test_begin_sets_deadline_on_the_server(self):
        self.pending()
        self.client.force_login(self.alice)
        # Client-supplied times and durations are ignored.
        response = self.post("begin", {"fullscreen": "1", "deadline": "2099-01-01T00:00:00Z",
                                       "seconds_remaining": "999999"}, when=self.now)
        self.assertEqual(response.status_code, 200)
        session = ExamSession.objects.get()
        self.assertEqual(session.status, ExamSession.Status.IN_PROGRESS)
        self.assertEqual(session.started_at, self.now)
        self.assertEqual(session.deadline, self.now + timedelta(minutes=60))
        self.assertEqual(response.json()["state"]["seconds_remaining"], 3600)

    def test_deadline_is_capped_by_competition_end(self):
        self.competition.duration_minutes = 600
        self.competition.save()
        self.assertEqual(self.begun().deadline, self.competition.end_time)

    def test_without_time_limit_exam_runs_until_end_time(self):
        self.competition.duration_minutes = None
        self.competition.save()
        self.assertEqual(self.begun().deadline, self.competition.end_time)

    def test_begin_is_idempotent_and_cannot_reset_the_clock(self):
        session = self.begun()
        later = self.now + timedelta(minutes=10)
        services.begin_session(session, later)
        session.refresh_from_db()
        self.assertEqual(session.started_at, self.now)
        self.assertEqual(session.deadline, self.now + timedelta(minutes=60))

    def test_cannot_begin_after_competition_closed(self):
        session = self.pending()
        with self.assertRaises(ExamError):
            services.begin_session(session, self.competition.end_time + timedelta(minutes=1))
        session.refresh_from_db()
        self.assertEqual(session.status, ExamSession.Status.EXPIRED)
        self.assertEqual(session.end_reason, "never_started")

    def test_state_endpoint_reports_server_remaining_time(self):
        self.begun()
        self.client.force_login(self.alice)
        with at(self.now + timedelta(minutes=45)):
            data = self.client.get(self.url("state")).json()["state"]
        self.assertEqual(data["seconds_remaining"], 15 * 60)
        self.assertFalse(data["ended"])

    def test_session_expires_after_deadline_plus_grace(self):
        session = self.begun()
        deadline = session.deadline
        self.client.force_login(self.alice)
        with at(deadline + timedelta(seconds=3)):  # inside the grace period
            data = self.client.get(self.url("state")).json()["state"]
        self.assertEqual((data["status"], data["seconds_remaining"]), ("in_progress", 0))
        with at(deadline + timedelta(seconds=30)):
            data = self.client.get(self.url("state")).json()["state"]
        self.assertTrue(data["ended"])
        session.refresh_from_db()
        self.assertEqual(session.status, ExamSession.Status.EXPIRED)
        self.assertEqual(session.end_reason, "time_expired")
        self.assertEqual(session.ended_at, deadline)

    def test_closing_the_competition_ends_running_exams(self):
        session = self.begun()
        self.competition.status = Competition.Status.COMPLETED
        self.competition.save()
        services.sync_session(ExamSession.objects.get(pk=session.pk), self.now)
        session.refresh_from_db()
        self.assertEqual(session.status, ExamSession.Status.EXPIRED)
        self.assertEqual(session.end_reason, "competition_closed")

    def test_sweep_command_closes_overdue_sessions(self):
        self.competition.start_time = self.now - timedelta(hours=5)
        self.competition.save()
        overdue = self.begun(self.alice, when=self.now - timedelta(hours=2))  # deadline was an hour ago
        fresh = self.begun(self.bob)
        out = StringIO()
        with at(self.now):
            call_command("finalize_expired_sessions", stdout=out)
        overdue.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(overdue.status, ExamSession.Status.EXPIRED)
        self.assertEqual(fresh.status, ExamSession.Status.IN_PROGRESS)
        self.assertIn("Closed 1", out.getvalue())


# ================================================================ pages
class ExamPageTests(ExamTestCase):
    def test_exam_page_redirects_to_check_in_without_a_session(self):
        self.client.force_login(self.alice)
        self.assertRedirects(self.client.get(self.url("exam")), self.url("start"), fetch_redirect_response=False)

    def test_exam_page_never_embeds_question_text(self):
        self.client.force_login(self.alice)
        for prepare in (self.pending, lambda: services.begin_session(ExamSession.objects.get(), self.now)):
            prepare()
            response = self.client.get(self.url("exam"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'id="exam-config"')
            self.assertContains(response, "Enter fullscreen")
            self.assertNotContains(response, "SECRET-STATEMENT-1")
            self.assertIn("no-store", response["Cache-Control"])

    def test_exam_page_hides_site_navigation(self):
        self.pending()
        self.client.force_login(self.alice)
        response = self.client.get(self.url("exam"))
        self.assertNotContains(response, 'class="site-header"')
        self.assertContains(response, "exam-bar")

    def test_finished_page_after_submit(self):
        session = self.begun()
        services.save_answer(session, self.q1.id, "print(1)", self.now)
        services.submit_session(session, self.now)
        self.client.force_login(self.alice)
        response = self.client.get(self.url("exam"))
        self.assertTemplateUsed(response, "exams/finished.html")
        self.assertContains(response, "Your exam was submitted")
        self.assertContains(response, "1 of 2")

    def test_static_assets_exist(self):
        self.assertIsNotNone(finders.find("exams/exam.js"))
        self.assertIsNotNone(finders.find("competitions/styles.css"))


# ================================================================ questions, answers, submission rules
class QuestionAndSubmissionTests(ExamTestCase):
    def test_questions_are_hidden_until_the_exam_begins(self):
        self.pending()
        self.client.force_login(self.alice)
        response = self.client.get(self.url("questions"))
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("SECRET", response.content.decode())

    def test_questions_available_while_in_progress(self):
        self.begun()
        self.client.force_login(self.alice)
        data = self.client.get(self.url("questions")).json()
        self.assertEqual([q["title"] for q in data["questions"]], ["Q1", "Q2"])
        self.assertEqual(data["questions"][0]["problem_statement"], "SECRET-STATEMENT-1")

    def test_save_answer_creates_then_updates(self):
        self.begun()
        self.client.force_login(self.alice)
        self.assertEqual(self.post("answer", {"code": "v1"}, None, self.q1.id).status_code, 200)
        self.post("answer", {"code": "v2"}, None, self.q1.id)
        self.assertEqual(Answer.objects.count(), 1)
        self.assertEqual(Answer.objects.get().code, "v2")
        data = self.client.get(self.url("questions")).json()
        self.assertEqual(data["questions"][0]["code"], "v2")

    def test_answer_length_is_limited(self):
        self.begun()
        self.client.force_login(self.alice)
        with override_settings(EXAM_MAX_CODE_LENGTH=10):
            response = self.post("answer", {"code": "x" * 11}, None, self.q1.id)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(Answer.objects.exists())

    def test_cannot_answer_a_question_from_another_competition(self):
        other = Competition.objects.create(
            title="Other", slug="other", description="d", start_time=self.now, end_time=self.now + timedelta(hours=1),
            status=Competition.Status.ONGOING)
        foreign = self.make_question(other, "Foreign")
        self.begun()
        self.client.force_login(self.alice)
        response = self.post("answer", {"code": "x"}, None, foreign.id)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Answer.objects.exists())

    def test_cannot_save_before_beginning(self):
        self.pending()
        self.client.force_login(self.alice)
        self.assertEqual(self.post("answer", {"code": "x"}, None, self.q1.id).status_code, 409)

    def test_saving_allowed_within_grace_but_not_after(self):
        session = self.begun()
        self.client.force_login(self.alice)
        within = self.post("answer", {"code": "late but ok"}, session.deadline + timedelta(seconds=3), self.q1.id)
        self.assertEqual(within.status_code, 200)
        after = self.post("answer", {"code": "too late"}, session.deadline + timedelta(seconds=30), self.q1.id)
        self.assertEqual(after.status_code, 409)
        self.assertTrue(after.json()["state"]["ended"])
        self.assertEqual(Answer.objects.get().code, "late but ok")

    def test_submit_ends_the_exam_and_locks_answers(self):
        self.begun()
        self.client.force_login(self.alice)
        self.post("answer", {"code": "final"}, None, self.q1.id)
        response = self.post("submit")
        self.assertEqual(response.json()["state"]["status"], "submitted")
        session = ExamSession.objects.get()
        self.assertEqual(session.end_reason, "submitted_by_participant")
        self.assertIsNotNone(session.ended_at)
        self.assertEqual(self.post("answer", {"code": "sneaky edit"}, None, self.q1.id).status_code, 409)
        self.assertEqual(Answer.objects.get().code, "final")
        self.assertEqual(self.client.get(self.url("questions")).status_code, 409)

    def test_submit_twice_is_harmless(self):
        self.begun()
        self.client.force_login(self.alice)
        self.post("submit")
        ended = ExamSession.objects.get().ended_at
        self.assertEqual(self.post("submit").status_code, 200)
        self.assertEqual(ExamSession.objects.get().ended_at, ended)

    def test_cannot_submit_before_beginning(self):
        self.pending()
        self.client.force_login(self.alice)
        self.assertEqual(self.post("submit").status_code, 409)
        self.assertEqual(ExamSession.objects.get().status, ExamSession.Status.PENDING)

    def test_submit_after_deadline_is_recorded_as_expired_not_submitted(self):
        session = self.begun()
        self.client.force_login(self.alice)
        self.post("submit", {}, session.deadline + timedelta(minutes=5))
        session.refresh_from_db()
        self.assertEqual(session.status, ExamSession.Status.EXPIRED)

    def test_participants_cannot_see_or_change_each_others_data(self):
        alice_session = self.begun(self.alice)
        services.save_answer(alice_session, self.q1.id, "ALICE-PRIVATE-CODE", self.now)

        # Bob has no session: every endpoint refuses, and nothing from Alice leaks.
        self.client.force_login(self.bob)
        for name in ["state", "questions"]:
            response = self.client.get(self.url(name))
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("ALICE-PRIVATE-CODE", response.content.decode())
        self.assertEqual(self.post("answer", {"code": "bob was here"}, None, self.q1.id).status_code, 404)
        self.assertEqual(self.post("submit").status_code, 404)
        self.assertEqual(self.post("violation", {"kind": "tab_hidden"}).status_code, 404)

        # With his own session Bob only ever sees his own answers.
        self.begun(self.bob)
        data = self.client.get(self.url("questions")).json()
        self.assertEqual(data["questions"][0]["code"], "")
        self.post("answer", {"code": "BOB-CODE"}, None, self.q1.id)
        alice_session.refresh_from_db()
        self.assertEqual(alice_session.status, ExamSession.Status.IN_PROGRESS)
        self.assertEqual(Answer.objects.get(session=alice_session).code, "ALICE-PRIVATE-CODE")
        self.assertEqual(Answer.objects.get(session__user=self.bob).code, "BOB-CODE")

    def test_exam_urls_contain_no_session_identifier(self):
        for name in ["state", "begin", "questions", "violation", "submit", "exam", "start"]:
            self.assertNotRegex(self.url(name), r"session")


# ================================================================ violation logging
class ViolationTests(ExamTestCase):
    def test_reported_events_are_stored_with_server_timestamp(self):
        self.begun()
        self.client.force_login(self.alice)
        when = self.now + timedelta(seconds=30)
        response = self.post("violation", {"kind": "tab_hidden", "detail": "Tab hidden"}, when)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["recorded"])
        self.assertEqual(response.json()["state"]["violations_counted"], 1)
        violation = Violation.objects.get()
        self.assertEqual(violation.kind, "tab_hidden")
        self.assertEqual(violation.occurred_at, when)
        self.assertTrue(violation.counted)

    def test_all_supported_event_types_are_accepted(self):
        session = self.begun()
        for i, kind in enumerate(["tab_hidden", "fullscreen_exit", "focus_lost", "navigation_attempt"]):
            services.record_violation(session, kind, "", self.now + timedelta(seconds=10 * (i + 1)))
        self.assertEqual(session.violations.count(), 4)

    def test_unknown_or_server_only_event_types_are_rejected(self):
        self.begun()
        self.client.force_login(self.alice)
        for kind in ["", "made_up", "connection_gap"]:
            with self.subTest(kind=kind):
                self.assertEqual(self.post("violation", {"kind": kind}).status_code, 400)
        self.assertFalse(Violation.objects.exists())

    def test_client_cannot_decide_whether_an_event_counts(self):
        self.begun()
        self.client.force_login(self.alice)
        self.post("violation", {"kind": "tab_hidden", "counted": "false", "violations_counted": "0"})
        self.assertTrue(Violation.objects.get().counted)

    def test_burst_of_events_counts_once(self):
        session = self.begun()
        t = self.now + timedelta(minutes=1)
        services.record_violation(session, "focus_lost", "", t)
        services.record_violation(session, "tab_hidden", "", t + timedelta(seconds=1))
        services.record_violation(session, "fullscreen_exit", "", t + timedelta(seconds=2))
        self.assertEqual(session.violations.count(), 3)      # all logged for review
        self.assertEqual(session.counted_violations(), 1)    # but only one counts
        services.record_violation(session, "tab_hidden", "", t + timedelta(seconds=30))
        self.assertEqual(session.counted_violations(), 2)

    def test_detail_text_is_truncated(self):
        session = self.begun()
        services.record_violation(session, "tab_hidden", "x" * 500, self.now)
        self.assertEqual(len(Violation.objects.get().detail), 200)

    def test_event_storage_is_capped(self):
        session = self.begun()
        with override_settings(EXAM_MAX_LOGGED_EVENTS=2):
            for i in range(5):
                services.record_violation(session, "tab_hidden", "", self.now + timedelta(seconds=10 * (i + 1)))
        self.assertEqual(session.violations.count(), 2)

    def test_events_are_rejected_when_exam_is_not_running(self):
        self.client.force_login(self.alice)
        self.pending()
        self.assertEqual(self.post("violation", {"kind": "tab_hidden"}).status_code, 409)
        session = ExamSession.objects.get()
        services.begin_session(session, self.now)
        services.submit_session(session, self.now)
        self.assertEqual(self.post("violation", {"kind": "tab_hidden"}).status_code, 409)
        self.assertFalse(Violation.objects.exists())

    def test_no_automatic_submission_without_a_threshold(self):
        session = self.begun()
        for i in range(8):
            services.record_violation(session, "tab_hidden", "", self.now + timedelta(seconds=10 * (i + 1)))
        session.refresh_from_db()
        self.assertEqual(session.status, ExamSession.Status.IN_PROGRESS)
        self.assertEqual(session.counted_violations(), 8)

    def test_automatic_submission_at_the_threshold(self):
        self.competition.max_violations = 3
        self.competition.save()
        session = self.begun()
        self.client.force_login(self.alice)
        services.save_answer(session, self.q1.id, "kept", self.now)
        for i in range(2):
            self.post("violation", {"kind": "tab_hidden"}, self.now + timedelta(seconds=10 * (i + 1)))
        self.assertEqual(ExamSession.objects.get().status, ExamSession.Status.IN_PROGRESS)
        response = self.post("violation", {"kind": "fullscreen_exit"}, self.now + timedelta(seconds=40))
        state = response.json()["state"]
        self.assertTrue(state["ended"])
        self.assertEqual(state["status"], "auto_submitted")
        session.refresh_from_db()
        self.assertEqual(session.end_reason, "violation_limit")
        self.assertEqual(Answer.objects.get().code, "kept")
        self.assertEqual(self.post("answer", {"code": "more"}, None, self.q1.id).status_code, 409)

    def test_burst_does_not_trigger_automatic_submission(self):
        self.competition.max_violations = 2
        self.competition.save()
        session = self.begun()
        t = self.now + timedelta(minutes=1)
        for kind, offset in [("focus_lost", 0), ("tab_hidden", 1), ("fullscreen_exit", 2)]:
            services.record_violation(session, kind, "", t + timedelta(seconds=offset))
        session.refresh_from_db()
        self.assertEqual(session.status, ExamSession.Status.IN_PROGRESS)

    def test_long_silence_is_logged_for_review_but_never_counted(self):
        session = self.begun()
        self.client.force_login(self.alice)
        with at(self.now + timedelta(seconds=300)):
            self.client.get(self.url("state"))
        gap = Violation.objects.get()
        self.assertEqual(gap.kind, "connection_gap")
        self.assertFalse(gap.counted)
        self.assertEqual(session.counted_violations(), 0)
        with at(self.now + timedelta(seconds=310)):  # contact resumed: no second gap
            self.client.get(self.url("state"))
        self.assertEqual(Violation.objects.count(), 1)

    def test_connection_gap_cannot_auto_submit_an_exam(self):
        self.competition.max_violations = 1
        self.competition.save()
        session = self.begun()
        self.client.force_login(self.alice)
        with at(self.now + timedelta(seconds=300)):
            self.client.get(self.url("state"))
        session.refresh_from_db()
        self.assertEqual(session.status, ExamSession.Status.IN_PROGRESS)


# ================================================================ administrator review
class AdminReviewTests(ExamTestCase):
    def setUp(self):
        super().setUp()
        self.admin_user = User.objects.create_superuser("boss", "boss@example.com", GOOD_PASSWORD)
        self.session = self.begun()
        services.save_answer(self.session, self.q1.id, "ADMIN-CAN-SEE-THIS", self.now)
        services.record_violation(self.session, "tab_hidden", "Tab hidden", self.now + timedelta(minutes=2))
        services.submit_session(self.session, self.now + timedelta(minutes=5))

    def test_admin_pages_require_staff(self):
        self.client.force_login(self.alice)  # a participant, not staff
        self.assertEqual(self.client.get("/admin/exams/examsession/").status_code, 302)

    def test_review_pages_load(self):
        self.client.force_login(self.admin_user)
        violation = Violation.objects.get()
        answer = Answer.objects.get()
        urls = [
            "/admin/exams/examsession/",
            f"/admin/exams/examsession/{self.session.pk}/change/",
            "/admin/exams/violation/",
            f"/admin/exams/violation/{violation.pk}/change/",
            "/admin/exams/answer/",
            f"/admin/exams/answer/{answer.pk}/change/",
            f"/admin/competitions/competition/{self.competition.pk}/change/",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_session_detail_shows_answers_and_violation_timeline(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(f"/admin/exams/examsession/{self.session.pk}/change/")
        self.assertContains(response, "ADMIN-CAN-SEE-THIS")
        self.assertContains(response, "Tab or window hidden")
        self.assertContains(response, "not proof of misconduct")

    def test_changelist_search_and_filters(self):
        self.client.force_login(self.admin_user)
        self.assertContains(self.client.get("/admin/exams/examsession/?q=alice"), "alice")
        self.assertNotContains(self.client.get("/admin/exams/examsession/?q=nobody-here"), "alice@")
        self.assertContains(self.client.get("/admin/exams/examsession/?status=submitted"), "alice")
        self.assertNotContains(self.client.get("/admin/exams/examsession/?status=expired"), "alice@")

    def test_sessions_cannot_be_created_by_hand(self):
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get("/admin/exams/examsession/add/").status_code, 403)
        self.assertEqual(self.client.get("/admin/exams/violation/add/").status_code, 403)

    def test_only_review_fields_are_editable(self):
        self.client.force_login(self.admin_user)
        self.client.post(f"/admin/exams/examsession/{self.session.pk}/change/", {
            "review_status": "flagged", "review_notes": "Looked at the timeline.",
            "status": "in_progress", "deadline": "2099-01-01 00:00:00",
            "violations-TOTAL_FORMS": "0", "violations-INITIAL_FORMS": "0",
            "violations-MIN_NUM_FORMS": "0", "violations-MAX_NUM_FORMS": "0",
            "answers-TOTAL_FORMS": "0", "answers-INITIAL_FORMS": "0",
            "answers-MIN_NUM_FORMS": "0", "answers-MAX_NUM_FORMS": "0",
        })
        self.session.refresh_from_db()
        self.assertEqual(self.session.review_status, "flagged")
        self.assertEqual(self.session.review_notes, "Looked at the timeline.")
        self.assertEqual(self.session.status, ExamSession.Status.SUBMITTED)   # unchanged
        self.assertLess(self.session.deadline, self.now + timedelta(days=1))  # unchanged

    def test_bulk_review_actions(self):
        self.client.force_login(self.admin_user)
        self.client.post("/admin/exams/examsession/", {
            "action": "mark_cleared", "_selected_action": [self.session.pk]})
        self.session.refresh_from_db()
        self.assertEqual(self.session.review_status, ExamSession.Review.CLEARED)

    def test_list_counts_match_the_data(self):
        admin_obj = admin.site._registry[ExamSession]
        self.client.force_login(self.admin_user)
        response = self.client.get("/admin/exams/examsession/")
        row = response.context["cl"].result_list[0]
        self.assertEqual(admin_obj.counted_violations_col(row), 1)
        self.assertEqual(admin_obj.events_col(row), 1)

    def test_users_with_sessions_cannot_be_deleted_by_accident(self):
        from django.db.models import ProtectedError
        with self.assertRaises(ProtectedError):
            self.alice.delete()
