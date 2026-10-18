"""Tests for code judging.

IMPORTANT: no test here executes any code. Two kinds of test double stand in for Judge0:

* FakeRunner: has the same interface as Judge0Client and returns exactly the results the
  test scripts. It never computes an answer.
* StubJudge0: a local HTTP server that speaks the subset of the Judge0 batch API the client
  uses, to test request format, base64, batching, headers and error handling.

Neither proves that a real Judge0 server evaluates code correctly. See the README.
"""
import base64
import json
import os
import re
import threading
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from competitions.models import CodingQuestion, Competition
from exams import services as exam_services
from exams.models import ExamSession
from exams.tests import ExamTestCase, at

from . import runner, scoring, services
from .models import CaseResult, Language, QuestionTestCase, Submission
from .runner import Judge0Client, RunnerError, RunnerUnavailable

User = get_user_model()


# ================================================================ test doubles
def outcome(stdout="", status_id=3, stderr="", compile_output="", time_ms=12, memory_kb=2048, message=""):
    return {"status_id": status_id, "stdout": stdout, "stderr": stderr, "compile_output": compile_output,
            "message": message, "time_ms": time_ms, "memory_kb": memory_kb}


class FakeRunner:
    """Scripted stand-in for Judge0Client. Returns what the test tells it to; computes nothing."""

    def __init__(self, responder=None, delay_polls=0, fail_poll=None, fail_submit=None):
        self.responder = responder or (lambda job: outcome(""))
        self.delay_polls = delay_polls
        self.fail_poll = fail_poll
        self.fail_submit = fail_submit
        self.jobs = []
        self.scripted = {}
        self.polls = 0

    def submit_batch(self, jobs):
        if self.fail_submit:
            raise self.fail_submit
        tokens = []
        for job in jobs:
            token = f"fake-{len(self.jobs)}"
            self.jobs.append(job)
            self.scripted[token] = self.responder(job)
            tokens.append(token)
        return tokens

    def get_batch(self, tokens):
        self.polls += 1
        if self.fail_poll:
            raise self.fail_poll
        if self.polls <= self.delay_polls:
            return {t: outcome(status_id=2) for t in tokens}
        return {t: dict(self.scripted[t]) for t in tokens}


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body=None):
        self.server.requests.append({"method": self.command, "path": self.path, "body": body,
                                     "token": self.headers.get("X-Auth-Token"),
                                     "extra": self.headers.get("X-Extra"), "key": self.headers.get("X-RapidAPI-Key")})
        if self.server.mode == "badjson":
            self._send(200, b"<html>not json</html>")
            return True
        if self.server.mode.isdigit():
            self._send(int(self.server.mode), {"error": "scripted"})
            return True
        return False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        if self._record(body):
            return
        subs = body.get("submissions", [])
        start = self.server.counter
        self.server.counter += len(subs)
        self._send(201, [{"token": f"stub-{start + i}"} for i in range(len(subs))])

    def do_GET(self):
        if self._record():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/about":
            return self._send(200, {"version": "1.13.1-stub"})
        if parsed.path == "/languages":
            return self._send(200, [{"id": 71, "name": "Python (3.8.1)"}, {"id": 63, "name": "JavaScript (Node.js 12.14.0)"}])
        tokens = parse_qs(parsed.query).get("tokens", [""])[0].split(",")
        subs = [{
            "token": t, "status_id": 3, "status": {"id": 3, "description": "Accepted"},
            "stdout": base64.b64encode(f"out:{t}\n".encode()).decode(), "stderr": None, "compile_output": None,
            "message": None, "time": "0.012", "memory": 3000,
        } for t in tokens]
        self._send(200, {"submissions": subs})


# ================================================================ the HTTP client against a protocol stub
class Judge0ClientProtocolTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._old_proxy = {k: os.environ.get(k) for k in ("no_proxy", "NO_PROXY")}
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        cls.server.requests, cls.server.counter, cls.server.mode = [], 0, "ok"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for key, value in cls._old_proxy.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDownClass()

    def setUp(self):
        self.server.requests.clear()
        self.server.counter = 0
        self.server.mode = "ok"
        self.client_ = Judge0Client(self.url, token="s3cret", extra_headers={"X-Extra": "yes"}, timeout=5)

    JOB = {"source_code": "print(input())", "language_id": 71, "stdin": "héllo\n", "cpu_time_limit": 2.0, "memory_limit_kb": 131072}

    def test_submit_sends_base64_limits_and_credentials(self):
        tokens = self.client_.submit_batch([self.JOB])
        self.assertEqual(tokens, ["stub-0"])
        request = self.server.requests[0]
        self.assertEqual((request["method"], request["path"]), ("POST", "/submissions/batch?base64_encoded=true"))
        sent = request["body"]["submissions"][0]
        self.assertEqual(base64.b64decode(sent["source_code"]).decode(), "print(input())")
        self.assertEqual(base64.b64decode(sent["stdin"]).decode(), "héllo\n")
        self.assertEqual((sent["language_id"], sent["cpu_time_limit"], sent["memory_limit"]), (71, 2.0, 131072))
        self.assertGreaterEqual(sent["wall_time_limit"], 2.0)
        self.assertNotIn("expected_output", sent)  # expected answers are never sent to the runner
        self.assertEqual((request["token"], request["extra"]), ("s3cret", "yes"))

    def test_large_batches_are_split(self):
        tokens = self.client_.submit_batch([self.JOB] * 45)
        self.assertEqual(len(tokens), 45)
        self.assertEqual(len(set(tokens)), 45)
        self.assertEqual([len(r["body"]["submissions"]) for r in self.server.requests], [20, 20, 5])

    def test_results_are_decoded(self):
        found = self.client_.get_batch(["stub-1"])
        info = found["stub-1"]
        self.assertEqual((info["status_id"], info["stdout"], info["time_ms"], info["memory_kb"]), (3, "out:stub-1\n", 12, 3000))
        self.assertEqual(info["stderr"], "")
        request = self.server.requests[0]
        self.assertIn("base64_encoded=true", request["path"])
        self.assertIn("tokens=stub-1", request["path"])

    def test_custom_auth_header_for_hosted_runners(self):
        Judge0Client(self.url, token="key123", auth_header="X-RapidAPI-Key", timeout=5).about()
        self.assertEqual(self.server.requests[0]["key"], "key123")
        self.assertIsNone(self.server.requests[0]["token"])

    def test_diagnostics(self):
        self.assertEqual(self.client_.about()["version"], "1.13.1-stub")
        self.assertEqual(self.client_.languages()[0]["id"], 71)

    def test_http_errors_map_to_unavailable_or_error(self):
        for code, expected in [("401", RunnerUnavailable), ("403", RunnerUnavailable), ("429", RunnerUnavailable),
                               ("500", RunnerUnavailable), ("503", RunnerUnavailable), ("422", RunnerError)]:
            with self.subTest(code=code):
                self.server.mode = code
                with self.assertRaises(expected) as ctx:
                    self.client_.submit_batch([self.JOB])
                self.assertEqual(isinstance(ctx.exception, RunnerUnavailable), expected is RunnerUnavailable)
                self.assertNotIn("s3cret", str(ctx.exception))  # never leak the credential

    def test_unreadable_reply_is_an_error(self):
        self.server.mode = "badjson"
        with self.assertRaises(RunnerError):
            self.client_.get_batch(["x"])

    def test_unreachable_runner_is_unavailable(self):
        dead = Judge0Client("http://127.0.0.1:9", timeout=2)  # nothing listens on the discard port
        with self.assertRaises(RunnerUnavailable):
            dead.submit_batch([self.JOB])

    @override_settings(JUDGE0_URL="")
    def test_no_client_without_configuration(self):
        self.assertIsNone(runner.get_client())

    @override_settings(JUDGE0_URL="ftp://example.com")
    def test_only_http_urls_are_accepted(self):
        self.assertIsNone(runner.get_client())

    @override_settings(JUDGE0_URL="http://judge.example:2358/", JUDGE0_AUTH_TOKEN="t", JUDGE0_AUTH_HEADER="X-Auth-Token")
    def test_client_is_built_from_settings(self):
        client = runner.get_client()
        self.assertEqual((client.base_url, client.token), ("http://judge.example:2358", "t"))


# ================================================================ shared fixtures
SOLUTION = {  # what a CORRECT program would print for each test input (scripted, not computed)
    "1\n": "2\n", "2\n": "4\n", "SECRET-IN-2\n": "SECRET-OUT-2\n", "SECRET-IN-3\n": "SECRET-OUT-3\n",
}


def correct(job):
    return outcome(SOLUTION[job["stdin"]])


@override_settings(JUDGE_MIN_SECONDS_BETWEEN_SUBMISSIONS=0, JUDGE0_URL="")
class JudgeTestCase(ExamTestCase):
    """Alice has begun an exam. Question q1 has 1 public and 3 private tests (weights 1, 1, 1, 2)."""

    def setUp(self):
        super().setUp()
        self.python = Language.objects.create(name="Python 3", slug="python", judge0_language_id=71,
                                              editor_mode="python", starter_code="# start\n")
        self.add_case(self.q1, "public", "1\n", "2\n", True, 1, 0)
        self.add_case(self.q1, "p1", "2\n", "4\n", False, 1, 1)
        self.add_case(self.q1, "p2", "SECRET-IN-2\n", "SECRET-OUT-2\n", False, 1, 2)
        self.add_case(self.q1, "p3", "SECRET-IN-3\n", "SECRET-OUT-3\n", False, 2, 3)
        self.session = self.begun(self.alice)
        self.client.force_login(self.alice)

    @staticmethod
    def add_case(question, label, stdin, expected, public, weight, position):
        return QuestionTestCase.objects.create(question=question, label=label, input_data=stdin,
                                               expected_output=expected, is_public=public, weight=weight, position=position)

    def jurl(self, name, *args):
        return reverse(f"judging:{name}", args=[self.competition.slug, *args])

    def use(self, fake):
        patcher = mock.patch("judging.runner.get_client", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def send(self, kind, code="print(1)", language="python", question=None, **kwargs):
        question = question or self.q1
        return self.client.post(self.jurl(kind, question.id), {"language": language, "code": code}, **kwargs)

    def finish(self, response):
        """Poll the submission to completion, like the browser does."""
        sub_id = response.json()["submission"]["id"]
        for _ in range(10):
            data = self.client.get(self.jurl("submission", sub_id)).json()["submission"]
            if data["finished"]:
                return data
        return data


# ================================================================ creating and evaluating submissions
class EvaluationTests(JudgeTestCase):
    def test_run_uses_public_cases_only_and_is_not_scored(self):
        fake = self.use(FakeRunner(correct))
        response = self.send("run")
        self.assertEqual(response.status_code, 201)
        self.assertEqual([j["stdin"] for j in fake.jobs], ["1\n"])
        data = self.finish(response)
        self.assertEqual((data["status"], data["verdict"], data["passed"], data["total"]), ("completed", "accepted", 1, 1))
        self.assertIsNone(Submission.objects.get().score)

    def test_submit_evaluates_every_case_and_scores_on_the_server(self):
        fake = self.use(FakeRunner(correct))
        data = self.finish(self.send("submit"))
        self.assertEqual(len(fake.jobs), 4)
        self.assertEqual((data["verdict"], data["passed"], data["total"]), ("accepted", 4, 4))
        submission = Submission.objects.get()
        self.assertEqual(submission.score, Decimal("10.00"))
        self.assertEqual((submission.passed_weight, submission.total_weight), (5, 5))

    def test_partial_credit_follows_test_weights(self):
        def responder(job):  # the weight-2 private case fails
            return outcome("wrong\n") if job["stdin"] == "SECRET-IN-3\n" else correct(job)
        self.use(FakeRunner(responder))
        data = self.finish(self.send("submit"))
        self.assertEqual((data["verdict"], data["passed"], data["total"]), ("wrong_answer", 3, 4))
        self.assertEqual(Submission.objects.get().score, Decimal("6.00"))  # 10 marks * 3/5

    def test_score_is_never_taken_from_the_browser(self):
        self.use(FakeRunner(lambda job: outcome("nope\n")))
        self.client.post(self.jurl("submit", self.q1.id),
                         {"language": "python", "code": "x", "score": "10", "passed": "4", "verdict": "accepted"})
        self.finish_latest()
        submission = Submission.objects.get()
        self.assertEqual((submission.score, submission.verdict), (Decimal("0.00"), "wrong_answer"))

    def finish_latest(self):
        sub = Submission.objects.latest("id")
        services.refresh_submission(sub)

    def test_output_comparison_ignores_trailing_whitespace_only(self):
        def responder(job):
            return outcome({"1\n": "2  \r\n\r\n", "2\n": "4", "SECRET-IN-2\n": "SECRET-OUT-2\n\n\n",
                            "SECRET-IN-3\n": "SECRET-OUT-3\n"}[job["stdin"]])
        self.use(FakeRunner(responder))
        self.assertEqual(self.finish(self.send("submit"))["verdict"], "accepted")

        self.session.submissions.all().delete()
        self.use(FakeRunner(lambda job: outcome("  " + SOLUTION[job["stdin"]])))  # leading spaces matter
        self.assertEqual(self.finish(self.send("submit"))["verdict"], "wrong_answer")

    def test_normalize_output(self):
        self.assertEqual(services.normalize_output("a  \r\nb\t\n\n"), "a\nb")
        self.assertNotEqual(services.normalize_output("a b"), services.normalize_output("ab"))

    def test_runner_receives_language_limits_and_code_but_not_expected_output(self):
        self.q1.time_limit_seconds = Decimal("1.5")
        self.q1.memory_limit_mb = 64
        self.q1.save()
        fake = self.use(FakeRunner(correct))
        self.send("submit", code="print('hi')")
        job = fake.jobs[0]
        self.assertEqual((job["language_id"], job["cpu_time_limit"], job["memory_limit_kb"]), (71, 1.5, 64 * 1024))
        self.assertEqual(job["source_code"], "print('hi')")
        self.assertNotIn("expected_output", job)
        self.assertNotIn("SECRET-OUT", json.dumps(fake.jobs))

    def test_error_verdicts(self):
        cases = [
            (lambda job: outcome(status_id=6, compile_output="syntax error here"), "compile_error"),
            (lambda job: outcome(status_id=11, stderr="Traceback"), "runtime_error"),
            (lambda job: outcome(status_id=5), "time_limit"),
            (lambda job: outcome(status_id=4, stdout="x"), "wrong_answer"),
        ]
        for responder, expected in cases:
            with self.subTest(expected=expected):
                self.session.submissions.all().delete()
                self.use(FakeRunner(responder))
                data = self.finish(self.send("submit"))
                self.assertEqual((data["status"], data["verdict"]), ("completed", expected))
                self.assertEqual(Submission.objects.get().score, Decimal("0.00"))

    def test_compile_error_output_is_shown_to_the_participant(self):
        self.use(FakeRunner(lambda job: outcome(status_id=6, compile_output="line 3: expected ';'")))
        data = self.finish(self.send("run"))
        self.assertIn("expected ';'", data["compile_output"])

    def test_first_failing_test_decides_the_verdict(self):
        def responder(job):
            if job["stdin"] == "2\n":
                return outcome(status_id=5)
            if job["stdin"] == "SECRET-IN-3\n":
                return outcome("bad")
            return correct(job)
        self.use(FakeRunner(responder))
        self.assertEqual(self.finish(self.send("submit"))["verdict"], "time_limit")

    def test_runner_internal_error_is_not_scored(self):
        self.use(FakeRunner(lambda job: outcome(status_id=13, message="internal")))
        data = self.finish(self.send("submit"))
        self.assertEqual((data["status"], data["verdict"]), ("error", ""))
        self.assertIsNone(Submission.objects.get().score)
        self.assertIn("not scored", data["message"])

    def test_polling_until_the_runner_finishes(self):
        self.use(FakeRunner(correct, delay_polls=2))
        response = self.send("submit")
        self.assertEqual(response.json()["submission"]["status"], "running")
        first = self.client.get(self.jurl("submission", response.json()["submission"]["id"])).json()["submission"]
        self.assertFalse(first["finished"])
        self.assertEqual(self.finish(response)["verdict"], "accepted")

    def test_results_survive_polling_twice(self):
        self.use(FakeRunner(correct))
        response = self.send("submit")
        self.finish(response)
        again = self.client.get(self.jurl("submission", response.json()["submission"]["id"])).json()["submission"]
        self.assertEqual(again["passed"], 4)
        self.assertEqual(Submission.objects.get().score, Decimal("10.00"))

    def test_case_results_snapshot_the_tests(self):
        self.use(FakeRunner(correct))
        self.finish(self.send("submit"))
        QuestionTestCase.objects.filter(label="p3").update(expected_output="CHANGED\n", weight=9)
        result = CaseResult.objects.get(position=3)
        self.assertEqual((result.expected_output, result.weight), ("SECRET-OUT-3\n", 2))

    def test_rejudge_uses_the_current_test_cases(self):
        self.use(FakeRunner(correct))
        self.finish(self.send("submit"))
        self.assertEqual(Submission.objects.get().score, Decimal("10.00"))
        QuestionTestCase.objects.filter(label="p3").update(expected_output="SOMETHING ELSE\n")  # the authors fix a test
        submission = Submission.objects.get()
        services.rejudge(submission)
        services.refresh_submission(submission)
        submission.refresh_from_db()
        self.assertEqual((submission.verdict, submission.score), ("wrong_answer", Decimal("6.00")))


# ================================================================ never fake results
class RunnerUnavailableTests(JudgeTestCase):
    def test_no_runner_configured_is_reported_not_faked(self):
        response = self.send("submit")
        self.assertEqual(response.status_code, 201)
        data = response.json()["submission"]
        self.assertEqual((data["status"], data["verdict"], data["cases"]), ("runner_unavailable", "", []))
        self.assertIn("No code runner is configured", data["message"])
        submission = Submission.objects.get()
        self.assertIsNone(submission.score)
        self.assertEqual((submission.passed_count, CaseResult.objects.count()), (0, 0))

    def test_unreachable_runner_is_reported_not_faked(self):
        dead = Judge0Client("http://127.0.0.1:9", timeout=2)
        self.use(dead)
        data = self.send("run").json()["submission"]
        self.assertEqual(data["status"], "runner_unavailable")
        self.assertIn("not available", data["message"])
        self.assertEqual(CaseResult.objects.count(), 0)
        self.assertIsNone(Submission.objects.get().score)

    def test_unavailable_attempts_do_not_count_against_the_participant(self):
        with override_settings(JUDGE_MAX_SUBMISSIONS_PER_QUESTION=1):
            self.send("submit")  # no runner: recorded as unavailable
            self.use(FakeRunner(correct))
            self.assertEqual(self.send("submit").status_code, 201)  # still allowed: the first did not count

    def test_runner_error_on_dispatch_is_not_scored(self):
        self.use(FakeRunner(fail_submit=RunnerError("bad request")))
        data = self.send("submit").json()["submission"]
        self.assertEqual(data["status"], "error")
        self.assertIsNone(Submission.objects.get().score)

    def test_runner_going_away_while_polling_keeps_waiting_then_gives_up_unscored(self):
        self.use(FakeRunner(correct, fail_poll=RunnerUnavailable("down")))
        response = self.send("submit")
        poll = self.client.get(self.jurl("submission", response.json()["submission"]["id"])).json()["submission"]
        self.assertEqual((poll["status"], poll["finished"]), ("running", False))
        submission = Submission.objects.get()
        late = submission.created_at + timedelta(seconds=services._setting("JUDGE_EVALUATION_TIMEOUT_SECONDS", 120) + 5)
        services.refresh_submission(submission, now=late)
        submission.refresh_from_db()
        self.assertEqual(submission.status, "error")
        self.assertIsNone(submission.score)
        self.assertIn("timed out", submission.message)

    def test_overview_reports_whether_a_runner_is_configured(self):
        self.assertFalse(self.client.get(self.jurl("overview")).json()["runner_configured"])
        self.use(FakeRunner())
        self.assertTrue(self.client.get(self.jurl("overview")).json()["runner_configured"])

    def test_submission_that_never_reached_the_runner_is_retried(self):
        fake = self.use(FakeRunner(correct))
        with mock.patch("judging.services.dispatch"):   # simulate a crash right after creating the rows
            response = self.send("submit")
        submission = Submission.objects.get()
        self.assertEqual(submission.status, "queued")
        services.refresh_submission(submission, now=submission.created_at + timedelta(seconds=60), client=fake)
        services.refresh_submission(submission, client=fake)
        submission.refresh_from_db()
        self.assertEqual((submission.status, submission.verdict), ("completed", "accepted"))

    def test_pending_command_collects_abandoned_submissions(self):
        self.use(FakeRunner(correct))
        self.send("submit")  # the participant never polls
        self.assertEqual(Submission.objects.get().status, "running")
        out = StringIO()
        call_command("process_pending_submissions", stdout=out)
        self.assertEqual(Submission.objects.get().status, "completed")
        self.assertIn("0 still pending", out.getvalue())


# ================================================================ rules enforced by the server
class SubmissionRuleTests(JudgeTestCase):
    def test_login_and_csrf_are_required(self):
        self.assertEqual(self.client.get(self.jurl("run", self.q1.id)).status_code, 405)   # POST only
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.alice)
        self.assertEqual(strict.post(self.jurl("run", self.q1.id), {"language": "python", "code": "x"}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.send("run").status_code, 401)

    def test_exam_must_be_in_progress(self):
        self.use(FakeRunner(correct))
        exam_services.submit_session(self.session, self.now)
        response = self.send("submit")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Submission.objects.count(), 0)

    def test_cannot_submit_before_beginning(self):
        self.client.force_login(self.bob)
        pending = exam_services.create_session(self.bob, self.competition, self.now)
        self.assertEqual(pending.status, "pending")
        self.assertEqual(self.send("submit").status_code, 409)

    def test_deadline_is_enforced_with_a_short_grace(self):
        fake = self.use(FakeRunner(correct))
        deadline = self.session.deadline
        ok = self.send("submit")  # normal
        self.assertEqual(ok.status_code, 201)
        self.finish(ok)
        with at(deadline + timedelta(seconds=3)):
            self.assertEqual(self.send("submit").status_code, 201)
        with at(deadline + timedelta(seconds=30)):
            late = self.send("submit")
        self.assertEqual(late.status_code, 409)
        self.assertTrue(late.json()["state"]["ended"])
        self.assertEqual(Submission.objects.count(), 2)

    def test_language_rules(self):
        self.use(FakeRunner(correct))
        Language.objects.create(name="Disabled", slug="disabled", judge0_language_id=1, is_enabled=False)
        other = Competition.objects.create(title="Other", slug="other", description="d", start_time=self.now,
                                           end_time=self.now + timedelta(hours=1), status="ongoing")
        restricted = Language.objects.create(name="Restricted", slug="restricted", judge0_language_id=2)
        restricted.competitions.add(other)
        for slug in ["", "nonsense", "disabled", "restricted"]:
            with self.subTest(slug=slug):
                self.assertEqual(self.send("run", language=slug).status_code, 400)
        restricted.competitions.add(self.competition)
        self.assertEqual(self.send("run", language="restricted").status_code, 201)

    def test_code_limits(self):
        self.use(FakeRunner(correct))
        self.assertEqual(self.send("run", code="   \n").status_code, 400)
        with override_settings(EXAM_MAX_CODE_LENGTH=10):
            self.assertEqual(self.send("run", code="x" * 11).status_code, 413)
        self.assertEqual(Submission.objects.count(), 0)

    def test_question_must_belong_to_the_exam(self):
        other = Competition.objects.create(title="Other", slug="other2", description="d", start_time=self.now,
                                           end_time=self.now + timedelta(hours=1), status="ongoing")
        foreign = self.make_question(other, "Foreign")
        self.add_case(foreign, "x", "", "", True, 1, 0)
        self.assertEqual(self.send("run", question=foreign).status_code, 404)

    def test_questions_without_tests_cannot_be_evaluated(self):
        self.use(FakeRunner(lambda job: outcome("b")))
        self.assertEqual(self.send("submit", question=self.q2).status_code, 409)
        self.add_case(self.q2, "private only", "a", "b", False, 1, 0)
        self.assertEqual(self.send("run", question=self.q2).status_code, 409)       # no public case to run
        self.assertEqual(self.send("submit", question=self.q2).status_code, 201)

    def test_only_one_evaluation_at_a_time(self):
        self.use(FakeRunner(correct, delay_polls=99))
        self.assertEqual(self.send("run").status_code, 201)
        busy = self.send("run")
        self.assertEqual(busy.status_code, 429)
        self.assertEqual(busy.json()["error"], "busy")

    def test_minimum_gap_between_submissions(self):
        self.use(FakeRunner(correct))
        with override_settings(JUDGE_MIN_SECONDS_BETWEEN_SUBMISSIONS=30):
            first = self.send("run")
            self.finish(first)
            again = self.send("run")
            self.assertEqual((again.status_code, again.json()["error"]), (429, "too_fast"))
            with at(timezone.now() + timedelta(seconds=40)):
                self.assertEqual(self.send("run").status_code, 201)

    def test_cap_per_question(self):
        self.use(FakeRunner(correct))
        with override_settings(JUDGE_MAX_SUBMISSIONS_PER_QUESTION=2):
            for _ in range(2):
                self.finish(self.send("run"))
            capped = self.send("run")
            self.assertEqual((capped.status_code, capped.json()["error"]), (429, "limit_reached"))

    def test_participants_cannot_see_each_others_submissions(self):
        self.use(FakeRunner(correct))
        mine = self.send("submit").json()["submission"]["id"]
        self.client.force_login(self.bob)
        self.begun(self.bob)
        self.assertEqual(self.client.get(self.jurl("submission", mine)).status_code, 404)
        history = self.client.get(self.jurl("history")).json()["submissions"]
        self.assertEqual(history, [])
        self.client.force_login(self.alice)
        self.assertEqual(len(self.client.get(self.jurl("history")).json()["submissions"]), 1)

    def test_user_without_a_session_gets_404(self):
        outsider = User.objects.create_user("carol", "carol@example.com", "pw-S3cure-123")
        self.client.force_login(outsider)
        for response in [self.send("run"), self.client.get(self.jurl("overview")), self.client.get(self.jurl("history"))]:
            self.assertEqual(response.status_code, 404)

    def test_history_filters_by_question_and_hides_case_details(self):
        self.use(FakeRunner(correct))
        self.finish(self.send("submit"))
        listed = self.client.get(self.jurl("history") + f"?question={self.q1.id}").json()["submissions"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["cases"], [])
        self.assertEqual(self.client.get(self.jurl("history") + f"?question={self.q2.id}").json()["submissions"], [])


# ================================================================ hidden tests stay hidden
class HiddenTestCaseSecrecyTests(JudgeTestCase):
    def secrets_absent(self, response):
        body = response.content.decode()
        for secret in ["SECRET-IN-2", "SECRET-IN-3", "SECRET-OUT-2", "SECRET-OUT-3"]:
            self.assertNotIn(secret, body, secret)

    def test_private_cases_never_appear_in_any_participant_response(self):
        def leaky(job):  # even if a program echoes private data, it is not shown
            return outcome(f"ECHO {job['stdin']}", stderr=f"ERR {job['stdin']}")
        self.use(FakeRunner(leaky))
        run = self.send("run")
        self.finish(run)            # one evaluation at a time
        submit = self.send("submit")
        self.secrets_absent(run)
        self.secrets_absent(submit)
        sub_id = submit.json()["submission"]["id"]
        self.secrets_absent(self.client.get(self.jurl("submission", sub_id)))
        self.secrets_absent(self.client.get(self.jurl("history")))
        self.secrets_absent(self.client.get(self.jurl("overview")))
        self.secrets_absent(self.client.get(reverse("exams:questions", args=[self.competition.slug])))
        self.secrets_absent(self.client.get(reverse("exams:exam", args=[self.competition.slug])))

    def test_private_case_payload_contains_only_the_status(self):
        self.use(FakeRunner(lambda job: outcome("wrong")))
        data = self.finish(self.send("submit"))
        private = [c for c in data["cases"] if not c["public"]]
        self.assertEqual(len(private), 3)
        for case in private:
            self.assertEqual(set(case), {"index", "public", "status", "status_label", "time_ms"})
        public = [c for c in data["cases"] if c["public"]]
        self.assertEqual(public[0]["input"], "1\n")
        self.assertEqual(public[0]["expected"], "2\n")

    def test_exam_page_and_questions_never_embed_test_data(self):
        page = self.client.get(reverse("exams:exam", args=[self.competition.slug])).content.decode()
        self.assertNotIn("expected_output", page)
        questions = self.client.get(reverse("exams:questions", args=[self.competition.slug])).json()
        self.assertNotIn("test_cases", json.dumps(questions))


# ================================================================ integration with the Stage 3 exam
class ExamIntegrationTests(JudgeTestCase):
    def test_exam_page_loads_the_local_editor_and_controls(self):
        response = self.client.get(reverse("exams:exam", args=[self.competition.slug]))
        for text in ["judging/vendor/codemirror/codemirror.js", "exam-language", "btn-run", "btn-submit-solution", "exam-results"]:
            self.assertContains(response, text)
        self.assertNotContains(response, "cdn")  # the editor is served locally

    def test_exam_config_url_templates_have_the_shape_exam_js_expects(self):
        """exam.js swaps the placeholder id 0 for a real id. These shapes are a contract with that script."""
        response = self.client.get(reverse("exams:exam", args=[self.competition.slug]))
        config = json.loads(re.search(r'<script id="exam-config" type="application/json">(.*?)</script>', response.content.decode(), re.S).group(1))
        urls = config["urls"]
        self.assertRegex(urls["answerTemplate"], r"/exam/answers/0/$")
        self.assertRegex(urls["runTemplate"], r"/exam/solutions/0/run/$")
        self.assertRegex(urls["submitTemplate"], r"/exam/solutions/0/submit/$")
        self.assertRegex(urls["submissionTemplate"], r"/exam/submissions/0/$")
        self.assertTrue(urls["judging"].endswith("/exam/judging/"))
        # and the real URLs those templates stand for resolve
        for key, tail in [("runTemplate", "run/"), ("submitTemplate", "submit/")]:
            self.assertEqual(urls[key].replace("/0/", f"/{self.q1.id}/"), self.jurl(tail.rstrip("/"), self.q1.id))

    def test_languages_are_listed_in_the_order_administrators_choose(self):
        Language.objects.create(name="Alpha", slug="alpha", judge0_language_id=1, position=5)
        Language.objects.filter(pk=self.python.pk).update(position=9)
        Language.objects.create(name="Zed", slug="zed", judge0_language_id=2, position=0)
        slugs = [l["slug"] for l in self.client.get(self.jurl("overview")).json()["languages"]]
        self.assertEqual(slugs, ["zed", "alpha", "python"])  # by position, not alphabetically

    def test_vendored_editor_files_exist(self):
        from django.contrib.staticfiles import finders
        for path in ["codemirror.js", "codemirror.css", "LICENSE", "mode/python.js", "mode/clike.js", "mode/javascript.js"]:
            self.assertIsNotNone(finders.find(f"judging/vendor/codemirror/{path}"), path)

    def test_drafts_remember_the_language(self):
        url = reverse("exams:answer", args=[self.competition.slug, self.q1.id])
        self.client.post(url, {"code": "x = 1", "language": "python"})
        data = self.client.get(reverse("exams:questions", args=[self.competition.slug])).json()
        self.assertEqual((data["questions"][0]["code"], data["questions"][0]["language"]), ("x = 1", "python"))
        self.client.post(url, {"code": "x = 1", "language": "<script>"})   # junk is dropped, not stored
        self.assertEqual(self.session.answers.get().language_slug, "")

    def test_overview_lists_languages_and_progress(self):
        self.use(FakeRunner(correct))
        self.finish(self.send("submit"))
        data = self.client.get(self.jurl("overview")).json()
        self.assertEqual([l["slug"] for l in data["languages"]], ["python"])
        self.assertEqual(data["languages"][0]["mode"], "python")
        self.assertEqual(data["questions"][str(self.q1.id)]["best"]["passed"], 4)
        self.assertEqual(data["questions"][str(self.q1.id)]["submitted"], 1)

    def test_violation_limit_still_ends_the_exam_and_blocks_submissions(self):
        self.use(FakeRunner(correct))
        self.competition.max_violations = 1
        self.competition.save()
        self.session.refresh_from_db()
        with at(self.now + timedelta(seconds=30)):
            self.client.post(reverse("exams:violation", args=[self.competition.slug]), {"kind": "tab_hidden"})
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "auto_submitted")
        self.assertEqual(self.send("submit").status_code, 409)

    def test_results_can_finish_after_the_exam_has_ended(self):
        self.use(FakeRunner(correct, delay_polls=1))
        response = self.send("submit")
        exam_services.submit_session(self.session, self.now)
        data = self.finish(response)  # polling still works for the owner after the exam ended
        self.assertEqual(data["verdict"], "accepted")
        self.assertEqual(Submission.objects.get().score, Decimal("10.00"))


# ================================================================ models, scoring and admin
class ScoringAndModelTests(JudgeTestCase):
    def test_score_for(self):
        cases = [(10, 5, 5, "10.00"), (10, 3, 5, "6.00"), (10, 0, 5, "0.00"), (10, 1, 3, "3.33"),
                 (7, 2, 3, "4.66"), (10, 0, 0, "0.00"), (10, 9, 5, "10.00")]
        for marks, passed, total, expected in cases:
            with self.subTest(case=(marks, passed, total)):
                self.assertEqual(scoring.score_for(marks, passed, total), Decimal(expected))

    def test_never_rounds_above_the_marks(self):
        self.assertLessEqual(scoring.score_for(10, 2, 3) * 3, Decimal("20.00"))

    def test_test_case_validation(self):
        case = QuestionTestCase(question=self.q1, expected_output="x", weight=0)
        with self.assertRaises(ValidationError) as ctx:
            case.full_clean()
        self.assertIn("weight", ctx.exception.message_dict)
        too_big = QuestionTestCase(question=self.q1, expected_output="x" * 100_001, weight=1)
        with self.assertRaises(ValidationError):
            too_big.full_clean()

    def test_execution_limit_validation(self):
        for field, value in [("time_limit_seconds", Decimal("0.4")), ("time_limit_seconds", Decimal("10.5")),
                             ("memory_limit_mb", 8), ("memory_limit_mb", 1024)]:
            with self.subTest(field=field, value=value):
                setattr(self.q1, field, value)
                with self.assertRaises(ValidationError) as ctx:
                    self.q1.full_clean()
                self.assertIn(field, ctx.exception.message_dict)
                self.q1.refresh_from_db()
        self.q1.full_clean()

    def test_language_slug_and_id_are_unique(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Language.objects.create(name="Dup", slug="python", judge0_language_id=71)


class JudgingAdminTests(JudgeTestCase):
    def setUp(self):
        super().setUp()
        self.admin_user = User.objects.create_superuser("boss", "boss@example.com", "pw-S3cure-123")
        self.use(FakeRunner(correct))
        self.finish(self.send("submit", code="print('ADMIN-VISIBLE-CODE')"))
        self.submission = Submission.objects.get()
        self.client.force_login(self.admin_user)

    def test_admin_pages_load(self):
        urls = [
            "/admin/judging/language/", "/admin/judging/language/add/", f"/admin/judging/language/{self.python.pk}/change/",
            "/admin/judging/questiontestcase/", "/admin/judging/submission/",
            f"/admin/judging/submission/{self.submission.pk}/change/",
            f"/admin/competitions/codingquestion/{self.q1.pk}/change/", "/admin/competitions/codingquestion/",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_staff_see_private_cases_on_the_question_page(self):
        page = self.client.get(f"/admin/competitions/codingquestion/{self.q1.pk}/change/")
        self.assertContains(page, "SECRET-IN-2")
        self.assertContains(page, "Execution limits")

    def test_question_list_shows_case_counts(self):
        self.assertContains(self.client.get("/admin/competitions/codingquestion/"), "1 / 3")

    def test_submission_review_shows_code_and_per_test_results(self):
        page = self.client.get(f"/admin/judging/submission/{self.submission.pk}/change/")
        self.assertContains(page, "ADMIN-VISIBLE-CODE")
        self.assertContains(page, "Per-test results")
        self.assertContains(page, "10.00")

    def test_submission_filters_and_search(self):
        self.assertContains(self.client.get("/admin/judging/submission/?q=alice&kind=submit&status=completed"), "alice")
        self.assertNotContains(self.client.get("/admin/judging/submission/?status=error"), "alice</")

    def test_submissions_are_read_only(self):
        self.assertEqual(self.client.get("/admin/judging/submission/add/").status_code, 403)
        response = self.client.post(f"/admin/judging/submission/{self.submission.pk}/change/", {"score": "99"})
        self.assertEqual(response.status_code, 403)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.score, Decimal("10.00"))

    def test_rejudge_action(self):
        QuestionTestCase.objects.filter(label="p3").update(expected_output="DIFFERENT\n")
        self.client.post("/admin/judging/submission/", {"action": "rejudge_selected", "_selected_action": [self.submission.pk]})
        call_command("process_pending_submissions", stdout=StringIO())
        self.submission.refresh_from_db()
        self.assertEqual((self.submission.verdict, self.submission.score), ("wrong_answer", Decimal("6.00")))

    def test_participants_cannot_reach_judging_admin(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get("/admin/judging/submission/").status_code, 302)


class CheckRunnerCommandTests(JudgeTestCase):
    def test_reports_when_nothing_is_configured(self):
        out = StringIO()
        call_command("check_runner", stdout=out)
        self.assertIn("No runner configured", out.getvalue())

    def test_reports_language_mismatches(self):
        class Client:
            base_url, token = "http://x", ""
            def about(self): return {"version": "1.13.1"}
            def languages(self): return [{"id": 71, "name": "Python (3.8.1)"}]
        Language.objects.create(name="Mystery", slug="mystery", judge0_language_id=999)
        out = StringIO()
        with mock.patch("judging.runner.get_client", return_value=Client()):
            call_command("check_runner", stdout=out)
        text = out.getvalue()
        self.assertIn("OK    Python 3", text)
        self.assertIn("MISSING Mystery", text)
        self.assertIn("does not run any code", text)

    def test_reports_an_unreachable_runner(self):
        out = StringIO()
        with mock.patch("judging.runner.get_client", return_value=Judge0Client("http://127.0.0.1:9", timeout=2)):
            call_command("check_runner", stdout=out)
        self.assertIn("NOT REACHABLE", out.getvalue())


class SeedJudgingDemoTests(JudgeTestCase):
    def test_seed_creates_gradable_problems_and_is_idempotent(self):
        call_command("seed_judging_demo", stdout=StringIO())
        call_command("seed_judging_demo", stdout=StringIO())
        competition = Competition.objects.get(slug="judge-warmup")
        self.assertEqual(competition.questions.count(), 3)
        for question in competition.questions.all():
            question.full_clean()
            self.assertGreaterEqual(question.test_cases.filter(is_public=True).count(), 1, question.title)
            self.assertGreaterEqual(question.test_cases.filter(is_public=False).count(), 2, question.title)
        self.assertEqual(set(Language.objects.values_list("slug", flat=True)), {"python", "javascript", "c", "cpp", "java"})
        self.assertEqual(QuestionTestCase.objects.filter(question__competition=competition).count(),
                         sum(q.test_cases.count() for q in competition.questions.all()))


# ================================================================ untrusted code must never run in this project
class NoLocalExecutionGuardTests(SimpleTestCase):
    FORBIDDEN = re.compile(r"\b(subprocess|os\.system|os\.popen|os\.exec\w*|pty|multiprocessing|pickle|marshal)\b|\b(exec|eval|compile)\(")

    def test_application_code_never_executes_submitted_code(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for app in ("judging", "results", "exams"):
            for path in (root / app).rglob("*.py"):
                if "migrations" in path.parts or path.name == "tests.py":
                    continue
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if self.FORBIDDEN.search(line) and not line.strip().startswith("#"):
                        offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "Application code must not execute code: " + "; ".join(offenders))

    def test_only_the_runner_module_talks_to_the_network(self):
        root = Path(__file__).resolve().parent
        for path in root.glob("*.py"):
            if path.name in ("runner.py", "tests.py"):
                continue
            self.assertNotRegex(path.read_text(encoding="utf-8"), r"urllib\.request|import requests|http\.client", path.name)
