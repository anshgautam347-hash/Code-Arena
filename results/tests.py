from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from competitions.models import Competition
from exams import services as exam_services
from exams.models import ExamSession
from judging.models import QuestionTestCase, Submission
from judging.tests import FakeRunner, JudgeTestCase, correct, outcome

from .services import build_standings, format_duration

User = get_user_model()
S = Submission


class ResultsTestCase(JudgeTestCase):
    """Alice, bob, carol and dave can each have begun the exam. Scores are put in directly."""

    def setUp(self):
        super().setUp()
        self.carol = User.objects.create_user("carol", "carol@example.com", "pw-S3cure-123")
        self.dave = User.objects.create_user("dave", "dave@example.com", "pw-S3cure-123")
        self.sessions = {"alice": self.session}
        self.start_time = self.session.started_at

    def player(self, user):
        if user.username not in self.sessions:
            self.sessions[user.username] = self.begun(user, self.start_time)
        return self.sessions[user.username]

    def sub(self, user, question, score, minutes, kind="submit", status="completed"):
        session = self.player(user)
        scored = status == "completed" and kind == "submit"
        return Submission.objects.create(
            session=session, question=question, language=self.python, source_code="code", kind=kind, status=status,
            verdict="accepted" if status == "completed" and Decimal(score) > 0 else ("wrong_answer" if status == "completed" else ""),
            created_at=session.started_at + timedelta(minutes=minutes), passed_count=1 if Decimal(score) > 0 else 0,
            total_count=1, passed_weight=1 if Decimal(score) > 0 else 0, total_weight=1,
            score=Decimal(score) if scored else None, finished_at=session.started_at + timedelta(minutes=minutes),
        )

    def standings(self):
        rows, questions = build_standings(self.competition)
        return {r["username"]: r for r in rows}, rows

    def publish(self):
        self.competition.results_published_at = timezone.now()
        self.competition.save()


# ================================================================ ranking rules
class RankingTests(ResultsTestCase):
    def test_question_score_is_the_best_submission_not_the_last(self):
        for score, minutes in [("5", 10), ("8", 20), ("3", 30)]:
            self.sub(self.alice, self.q1, score, minutes)
        by_name, _ = self.standings()
        self.assertEqual(by_name["alice"]["scores"][self.q1.pk], Decimal("8"))

    def test_total_is_the_sum_over_questions(self):
        self.sub(self.alice, self.q1, "8", 10)
        self.sub(self.alice, self.q2, "6.50", 20)
        by_name, _ = self.standings()
        self.assertEqual(by_name["alice"]["total"], Decimal("14.50"))

    def test_runs_errors_and_unavailable_attempts_do_not_count(self):
        self.sub(self.alice, self.q1, "5", 10)
        self.sub(self.alice, self.q1, "10", 11, kind="run")
        self.sub(self.alice, self.q1, "10", 12, status="error")
        self.sub(self.alice, self.q1, "10", 13, status="runner_unavailable")
        self.sub(self.alice, self.q1, "10", 14, status="running")
        by_name, _ = self.standings()
        self.assertEqual(by_name["alice"]["total"], Decimal("5"))
        self.assertEqual(by_name["alice"]["attempts"], 1)

    def test_higher_total_ranks_first(self):
        self.sub(self.alice, self.q1, "4", 5)
        self.sub(self.bob, self.q1, "9", 50)
        by_name, rows = self.standings()
        self.assertEqual([r["username"] for r in rows], ["bob", "alice"])
        self.assertEqual((by_name["bob"]["rank"], by_name["alice"]["rank"]), (1, 2))

    def test_equal_totals_are_broken_by_time_to_final_score(self):
        # alice improves late (30 min), bob finishes earlier (20 min)
        self.sub(self.alice, self.q1, "5", 5)
        self.sub(self.alice, self.q2, "5", 30)
        self.sub(self.bob, self.q1, "5", 10)
        self.sub(self.bob, self.q2, "5", 20)
        by_name, _ = self.standings()
        self.assertEqual((by_name["bob"]["rank"], by_name["alice"]["rank"]), (1, 2))
        self.assertEqual(by_name["alice"]["finish_seconds"], 30 * 60)
        self.assertEqual(by_name["bob"]["finish_seconds"], 20 * 60)

    def test_later_worse_attempts_do_not_extend_the_finish_time(self):
        self.sub(self.alice, self.q1, "8", 10)
        self.sub(self.alice, self.q1, "2", 55)   # worse, later: not an improvement
        self.sub(self.alice, self.q1, "8", 40)   # equal, later: not an improvement
        by_name, _ = self.standings()
        self.assertEqual(by_name["alice"]["finish_seconds"], 10 * 60)
        self.assertEqual(by_name["alice"]["best"][self.q1.pk]["elapsed"], 10 * 60)

    def test_time_is_measured_from_when_each_participant_began(self):
        late_session = self.begun(self.bob, self.start_time + timedelta(minutes=30))
        self.sessions["bob"] = late_session
        self.sub(self.alice, self.q1, "5", 20)    # 20 min after alice began
        self.sub(self.bob, self.q1, "5", 15)      # 15 min after bob began (but later on the wall clock)
        by_name, _ = self.standings()
        self.assertEqual(by_name["bob"]["rank"], 1)

    def test_equal_time_is_broken_by_fewer_scored_submissions(self):
        self.sub(self.alice, self.q1, "5", 10)
        self.sub(self.bob, self.q1, "0", 2)
        self.sub(self.bob, self.q1, "0", 4)
        self.sub(self.bob, self.q1, "5", 10)
        by_name, _ = self.standings()
        self.assertEqual((by_name["alice"]["rank"], by_name["bob"]["rank"]), (1, 2))
        self.assertEqual((by_name["alice"]["attempts"], by_name["bob"]["attempts"]), (1, 3))

    def test_participants_equal_on_every_rule_share_a_rank(self):
        for user in (self.alice, self.bob, self.carol):
            self.sub(user, self.q1, "5", 10)
        self.sub(self.dave, self.q1, "1", 10)
        by_name, _ = self.standings()
        self.assertEqual([by_name[n]["rank"] for n in ("alice", "bob", "carol", "dave")], [1, 1, 1, 4])

    def test_zero_scorers_are_listed_after_everyone_else(self):
        self.sub(self.alice, self.q1, "0", 5)
        self.sub(self.bob, self.q1, "3", 5)
        self.player(self.carol)  # began, never submitted
        by_name, rows = self.standings()
        self.assertEqual(rows[0]["username"], "bob")
        self.assertEqual(by_name["carol"]["total"], Decimal("0.00"))
        self.assertIsNone(by_name["carol"]["finish_seconds"])
        # Alice tried and scored 0; Carol never submitted. Neither has points, so they share the last rank.
        self.assertEqual((by_name["bob"]["rank"], by_name["alice"]["rank"], by_name["carol"]["rank"]), (1, 2, 2))

    def test_only_participants_who_began_are_listed(self):
        exam_services.create_session(self.bob, self.competition, self.now)  # checked in, never begun
        by_name, _ = self.standings()
        self.assertEqual(list(by_name), ["alice"])

    def test_excluded_participants_have_no_rank_and_do_not_shift_others(self):
        self.sub(self.alice, self.q1, "9", 5)
        self.sub(self.bob, self.q1, "6", 5)
        ExamSession.objects.filter(user=self.alice).update(leaderboard_excluded=True)
        by_name, rows = self.standings()
        self.assertIsNone(by_name["alice"]["rank"])
        self.assertEqual(by_name["bob"]["rank"], 1)
        self.assertEqual(rows[-1]["username"], "alice")

    def test_violation_indicators_are_not_used_for_ranking(self):
        self.sub(self.alice, self.q1, "5", 10)
        self.sub(self.bob, self.q1, "5", 10)
        bob = self.player(self.bob)
        for i in range(6):
            exam_services.record_violation(bob, "tab_hidden", "", self.now + timedelta(seconds=10 * (i + 1)))
        by_name, _ = self.standings()
        self.assertEqual((by_name["alice"]["rank"], by_name["bob"]["rank"]), (1, 1))
        self.assertEqual(by_name["bob"]["violations"], 6)

    def test_format_duration(self):
        self.assertEqual(format_duration(None), "-")
        self.assertEqual(format_duration(65), "1m 05s")
        self.assertEqual(format_duration(3725), "1h 02m 05s")


# ================================================================ participant pages
class ParticipantResultsTests(ResultsTestCase):
    def url(self, name, *args):
        return reverse(f"results:{name}", args=[self.competition.slug, *args])

    def finish_exam(self, user=None):
        exam_services.submit_session(self.player(user or self.alice), self.now)

    def test_login_is_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url("mine")).status_code, 302)

    def test_running_exam_redirects_back_to_the_exam(self):
        response = self.client.get(self.url("mine"))
        self.assertRedirects(response, reverse("exams:exam", args=[self.competition.slug]), fetch_redirect_response=False)

    def test_no_session_redirects_to_check_in(self):
        self.client.force_login(self.bob)
        self.assertRedirects(self.client.get(self.url("mine")), reverse("exams:start", args=[self.competition.slug]), fetch_redirect_response=False)

    def test_scores_and_rank_stay_hidden_until_published(self):
        self.sub(self.alice, self.q1, "8", 10)
        self.sub(self.bob, self.q1, "3", 10)
        self.finish_exam()
        page = self.client.get(self.url("mine"))
        self.assertContains(page, "Not published yet")
        self.assertContains(page, "Question scores")
        self.assertNotContains(page, "8.00")
        self.assertNotContains(page, "RANK")

    def test_published_results_show_scores_and_rank(self):
        self.sub(self.alice, self.q1, "8", 10)
        self.sub(self.alice, self.q2, "2", 12)
        self.sub(self.bob, self.q1, "3", 10)
        self.finish_exam()
        self.publish()
        page = self.client.get(self.url("mine"))
        self.assertContains(page, "10.00")          # total
        self.assertContains(page, "1 of 2")         # rank
        self.assertContains(page, "8.00")
        self.assertContains(page, "View leaderboard")

    def test_history_lists_every_attempt_with_outcomes(self):
        self.sub(self.alice, self.q1, "0", 5)
        self.sub(self.alice, self.q1, "8", 10)
        self.sub(self.alice, self.q1, "0", 12, kind="run")
        self.sub(self.alice, self.q1, "0", 14, status="runner_unavailable")
        self.finish_exam()
        page = self.client.get(self.url("mine")).content.decode()
        self.assertIn("Submission history", page)
        self.assertEqual(page.count('href="/competitions/live-cup/results/submissions/'), 4 + 1)  # 4 rows + best-result link
        self.assertIn("Runner unavailable", page)
        self.assertIn("Accepted", page)

    def test_excluded_participants_are_told_they_are_not_listed(self):
        self.sub(self.alice, self.q1, "8", 10)
        self.finish_exam()
        ExamSession.objects.filter(user=self.alice).update(leaderboard_excluded=True)
        self.publish()
        self.assertContains(self.client.get(self.url("mine")), "not shown on the public leaderboard")

    def test_pending_evaluations_are_collected_when_the_page_opens(self):
        fake = self.use(FakeRunner(correct))
        response = self.send("submit")
        self.assertEqual(Submission.objects.get().status, "running")
        self.finish_exam()
        page = self.client.get(self.url("mine"))
        self.assertEqual(Submission.objects.get().status, "completed")
        self.assertContains(page, "4/4")

    def test_submission_detail_shows_own_code_and_public_cases_only(self):
        self.use(FakeRunner(lambda job: outcome("wrong")))
        self.finish(self.send("submit", code="print('MY-OWN-CODE')"))
        self.finish_exam()
        sub = Submission.objects.get()
        page = self.client.get(self.url("submission", sub.pk))
        self.assertContains(page, "MY-OWN-CODE")
        self.assertContains(page, "Test 1 (public)")
        self.assertContains(page, "Test 2 (private)")
        for secret in ["SECRET-IN-2", "SECRET-IN-3", "SECRET-OUT-2", "SECRET-OUT-3"]:
            self.assertNotContains(page, secret)

    def test_submission_detail_hides_score_until_published(self):
        self.use(FakeRunner(correct))
        self.finish(self.send("submit"))
        self.finish_exam()
        sub = Submission.objects.get()
        self.assertNotContains(self.client.get(self.url("submission", sub.pk)), "SCORE")
        self.publish()
        self.assertContains(self.client.get(self.url("submission", sub.pk)), "10.00")

    def test_other_participants_submissions_return_404(self):
        self.sub(self.alice, self.q1, "8", 10)
        sub = Submission.objects.get()
        self.client.force_login(self.bob)
        self.begun(self.bob)
        self.assertEqual(self.client.get(self.url("submission", sub.pk)).status_code, 404)

    def test_participants_cannot_see_another_persons_results(self):
        self.sub(self.alice, self.q1, "8", 10)
        self.finish_exam()
        self.client.force_login(self.bob)
        self.finish_exam(self.bob)
        page = self.client.get(self.url("mine"))
        self.assertNotContains(page, "alice")
        self.assertContains(page, "You made no submissions")

    def test_finished_page_links_to_results(self):
        self.finish_exam()
        self.assertContains(self.client.get(reverse("exams:exam", args=[self.competition.slug])),
                            self.url("mine"))


# ================================================================ leaderboard
class LeaderboardTests(ResultsTestCase):
    def url(self):
        return reverse("results:leaderboard", args=[self.competition.slug])

    def setUp(self):
        super().setUp()
        self.sub(self.alice, self.q1, "9", 10)
        self.sub(self.bob, self.q1, "4", 10)
        self.sub(self.carol, self.q1, "7", 10)
        self.client.logout()

    def test_hidden_until_results_are_published(self):
        page = self.client.get(self.url())
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "have not been published")
        for name in ["alice", "bob", "carol"]:
            self.assertNotContains(page, name)

    def test_staff_can_preview_before_publishing(self):
        staff = User.objects.create_user("staffer", "s@example.com", "pw-S3cure-123", is_staff=True)
        self.client.force_login(staff)
        page = self.client.get(self.url())
        self.assertContains(page, "Staff preview")
        self.assertContains(page, "alice")

    def test_published_leaderboard_is_ranked_and_public(self):
        self.publish()
        page = self.client.get(self.url())
        self.assertEqual(page.status_code, 200)
        rows = page.context["rows"]
        self.assertEqual([(r["username"], r["rank"]) for r in rows], [("alice", 1), ("carol", 2), ("bob", 3)])
        self.assertContains(page, "9.00")
        self.assertContains(page, "HOW RANKING WORKS")

    def test_private_details_are_not_published(self):
        self.publish()
        page = self.client.get(self.url())
        for private in ["alice@example.com", "bob@example.com", "violation", "Violation"]:
            self.assertNotContains(page, private)

    def test_excluded_participants_are_not_listed(self):
        ExamSession.objects.filter(user=self.alice).update(leaderboard_excluded=True)
        self.publish()
        page = self.client.get(self.url())
        self.assertNotContains(page, "alice")
        self.assertEqual([r["rank"] for r in page.context["rows"]], [1, 2])

    def test_signed_in_participant_is_highlighted(self):
        self.publish()
        self.client.force_login(self.bob)
        page = self.client.get(self.url())
        self.assertContains(page, 'class="me"')
        self.assertContains(page, "(you)")

    def test_draft_competitions_have_no_leaderboard(self):
        self.competition.status = Competition.Status.DRAFT
        self.competition.save()
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_detail_page_links_to_the_leaderboard_only_when_published(self):
        detail = self.competition.get_absolute_url()
        self.assertNotContains(self.client.get(detail), "View leaderboard")
        self.publish()
        self.assertContains(self.client.get(detail), "View leaderboard")

    def test_leaderboard_collects_pending_evaluations(self):
        self.publish()
        self.client.force_login(self.alice)
        self.use(FakeRunner(correct))
        self.send("submit")                      # alice's evaluation is still running
        self.client.logout()
        before = Submission.objects.filter(status="running").count()
        self.client.get(self.url())
        self.assertEqual((before, Submission.objects.filter(status="running").count()), (1, 0))


# ================================================================ administrator review
class AdminResultsTests(ResultsTestCase):
    def setUp(self):
        super().setUp()
        self.admin_user = User.objects.create_superuser("boss", "boss@example.com", "pw-S3cure-123")
        self.sub(self.alice, self.q1, "9", 10)
        self.sub(self.bob, self.q1, "4", 10)
        exam_services.record_violation(self.player(self.bob), "tab_hidden", "", self.now + timedelta(seconds=20))
        self.client.force_login(self.admin_user)
        self.page = f"/admin/competitions/competition/{self.competition.pk}/standings/"

    def test_standings_page_shows_everything_an_administrator_needs(self):
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        for text in ["alice", "bob", "alice@example.com", "9.00", "Publish results", "Download CSV", "Violation indicators", "Session"]:
            self.assertContains(response, text)

    def test_standings_page_requires_staff(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.page).status_code, 302)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(self.page).status_code, 302)

    def test_staff_without_permission_are_refused(self):
        staff = User.objects.create_user("limited", "l@example.com", "pw-S3cure-123", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.page).status_code, 403)

    def test_view_only_staff_cannot_publish(self):
        staff = User.objects.create_user("viewer", "v@example.com", "pw-S3cure-123", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_competition"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.page).status_code, 200)
        self.assertEqual(self.client.post(self.page, {"action": "publish"}).status_code, 403)
        self.competition.refresh_from_db()
        self.assertIsNone(self.competition.results_published_at)

    def test_publish_and_unpublish(self):
        self.client.post(self.page, {"action": "publish"})
        self.competition.refresh_from_db()
        self.assertIsNotNone(self.competition.results_published_at)
        self.assertContains(self.client.get(self.page), "Unpublish results")
        self.client.post(self.page, {"action": "unpublish"})
        self.competition.refresh_from_db()
        self.assertIsNone(self.competition.results_published_at)

    def test_unknown_actions_do_nothing(self):
        self.client.post(self.page, {"action": "delete-everything"})
        self.competition.refresh_from_db()
        self.assertIsNone(self.competition.results_published_at)

    def test_competition_list_actions_and_columns(self):
        listing = self.client.get("/admin/competitions/competition/")
        self.assertContains(listing, "Not published")
        self.assertContains(listing, self.page)
        self.client.post("/admin/competitions/competition/", {"action": "publish_results", "_selected_action": [self.competition.pk]})
        self.competition.refresh_from_db()
        self.assertIsNotNone(self.competition.results_published_at)
        self.client.post("/admin/competitions/competition/", {"action": "unpublish_results", "_selected_action": [self.competition.pk]})
        self.competition.refresh_from_db()
        self.assertIsNone(self.competition.results_published_at)

    def test_csv_export(self):
        response = self.client.get(self.page + "?format=csv")
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("live-cup-standings.csv", response["Content-Disposition"])
        lines = response.content.decode().strip().splitlines()
        self.assertTrue(lines[0].startswith("Rank,Username,Email"))
        self.assertIn("alice@example.com", lines[1])
        self.assertTrue(lines[1].startswith("1,alice"))

    def test_csv_cells_cannot_inject_spreadsheet_formulas(self):
        evil = User.objects.create_user("+evil", "evil@example.com", "pw-S3cure-123")
        self.begun(evil, self.start_time)
        body = self.client.get(self.page + "?format=csv").content.decode()
        self.assertIn("'+evil", body)
        self.assertNotIn(",+evil,", body)

    def test_excluding_from_the_exam_session_admin_hides_from_the_leaderboard(self):
        self.client.post("/admin/exams/examsession/", {"action": "exclude_from_leaderboard", "_selected_action": [self.session.pk]})
        self.assertTrue(ExamSession.objects.get(pk=self.session.pk).leaderboard_excluded)
        self.client.post(self.page, {"action": "publish"})
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("results:leaderboard", args=[self.competition.slug])), "alice")
        self.client.force_login(self.admin_user)
        self.client.post("/admin/exams/examsession/", {"action": "include_in_leaderboard", "_selected_action": [self.session.pk]})
        self.assertFalse(ExamSession.objects.get(pk=self.session.pk).leaderboard_excluded)

    def test_excluded_participants_still_appear_in_admin_standings(self):
        ExamSession.objects.filter(user=self.alice).update(leaderboard_excluded=True)
        page = self.client.get(self.page)
        self.assertContains(page, "alice")
        rows = page.context["rows"]
        self.assertEqual(rows[-1]["username"], "alice")

    def test_session_admin_exposes_the_exclusion_control(self):
        page = self.client.get(f"/admin/exams/examsession/{self.session.pk}/change/")
        self.assertContains(page, "leaderboard_excluded")
