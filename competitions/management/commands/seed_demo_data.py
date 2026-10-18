from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from competitions.models import CodingQuestion, Competition

S = Competition.Status
D = CodingQuestion.Difficulty

# Sample content for local development. It mirrors the three example cards from Stage 1.
DEMO = [
    {
        "slug": "injection-defense",
        "title": "Injection Defense",
        "status": S.COMPLETED,
        "start": timedelta(days=-9), "length": timedelta(minutes=90),
        "description": "Spot the injection. Close the loophole. Build queries that keep untrusted input in its place.",
        "rules": "Work on your own.\nYou may use official documentation.\nYour fix must keep the original behavior for valid input.",
        "questions": [
            ("Replace the string-built query", D.EASY, 10,
             "A function builds a SQL query by pasting a username into a string. Rewrite it with a parameterized query.",
             "A single line containing a username.", "The matching user's id, or NOT FOUND.",
             "1 <= length of username <= 50", "alice", "42"),
            ("Validate at the boundary", D.MEDIUM, 20,
             "A search endpoint passes a sort column straight into ORDER BY. Allow only a fixed list of columns.",
             "A column name.", "ACCEPTED if the column is allowed, otherwise REJECTED.",
             "1 <= length of column name <= 30", "name; DROP TABLE users", "REJECTED"),
        ],
    },
    {
        "slug": "authentication-under-fire",
        "title": "Authentication Under Fire",
        "status": S.ONGOING,
        "start": timedelta(hours=-1), "length": timedelta(hours=2),
        "duration_minutes": 60, "max_violations": 5,
        "description": "A login is only as strong as its weakest link. Find the flaws in an authentication flow.",
        "rules": "Work on your own.\nNo sharing solutions during the competition.",
        "questions": [
            ("Store passwords safely", D.MEDIUM, 20,
             "Passwords are stored as unsalted MD5. Switch to a salted, slow password hash.",
             "A password string.", "The name of the algorithm you used.",
             "1 <= length of password <= 128", "hunter2", "argon2"),
            ("Harden the session cookie", D.EASY, 10,
             "A session cookie is missing important flags. List the flags that should be set.",
             "No input.", "A comma-separated list of cookie flags.", "At most 5 flags", "", "HttpOnly,Secure,SameSite"),
            ("Stop leaking in error messages", D.MEDIUM, 15,
             "The login form tells attackers whether a username exists. Make both failures look the same.",
             "A username and a password.", "The message shown to the user.", "Both fields are non-empty",
             "nobody wrong-password", "Invalid username or password"),
        ],
    },
    {
        "slug": "secure-api-foundations",
        "title": "Secure API Foundations",
        "status": S.UPCOMING,
        "start": timedelta(days=3), "length": timedelta(hours=3),
        "description": "Go beyond the happy path. Protect endpoints, validate requests, and tighten access boundaries.",
        "rules": "Work on your own.\nAnswers must not log sensitive data.",
        "questions": [
            ("Check object ownership", D.HARD, 30,
             "Any logged-in user can read any invoice by id. Add an object-level ownership check.",
             "Two integers: user id and invoice id.", "ALLOWED or DENIED.", "1 <= ids <= 10^6", "7 31", "DENIED"),
        ],
    },
]


class Command(BaseCommand):
    help = "Create sample competitions and questions for local development. Safe to run more than once."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()
        created = 0
        for item in DEMO:
            start = now + item["start"]
            competition, was_created = Competition.objects.get_or_create(
                slug=item["slug"],
                defaults={
                    "title": item["title"], "status": item["status"],
                    "description": item["description"], "rules": item["rules"],
                    "start_time": start, "end_time": start + item["length"],
                    "duration_minutes": item.get("duration_minutes"),
                    "max_violations": item.get("max_violations"),
                },
            )
            created += was_created
            for title, diff, marks, statement, inp, out, cons, s_in, s_out in item["questions"]:
                CodingQuestion.objects.get_or_create(
                    competition=competition, title=title,
                    defaults={
                        "difficulty": diff, "marks": marks, "problem_statement": statement,
                        "input_format": inp, "output_format": out, "constraints": cons,
                        "sample_input": s_in, "sample_output": s_out,
                    },
                )
        self.stdout.write(self.style.SUCCESS(
            f"Demo data ready ({created} new competition(s), {Competition.objects.count()} total)."
        ))
