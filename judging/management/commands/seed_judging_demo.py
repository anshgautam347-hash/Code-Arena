from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from competitions.models import CodingQuestion, Competition
from judging.models import Language, QuestionTestCase

# IDs below match Judge0 CE v1.13. Other versions use different IDs: run `manage.py check_runner`.
LANGUAGES = [  # listed in this order; the first is the default in the editor
    ("Python 3", "python", 71, "python",
     "import sys\n\n\ndef main():\n    data = sys.stdin.read().split(\"\\n\")\n    # Write your solution here\n    print(data[0])\n\n\nmain()\n"),
    ("JavaScript (Node.js)", "javascript", 63, "text/javascript",
     "const lines = require(\"fs\").readFileSync(0, \"utf8\").split(\"\\n\");\n// Write your solution here\nconsole.log(lines[0]);\n"),
    ("C", "c", 50, "text/x-csrc",
     "#include <stdio.h>\n\nint main(void) {\n    /* Write your solution here */\n    return 0;\n}\n"),
    ("C++", "cpp", 54, "text/x-c++src",
     "#include <bits/stdc++.h>\nusing namespace std;\n\nint main() {\n    // Write your solution here\n    return 0;\n}\n"),
    ("Java", "java", 62, "text/x-java",
     "import java.util.*;\n\npublic class Main {\n    public static void main(String[] args) {\n        Scanner in = new Scanner(System.in);\n        // Write your solution here\n    }\n}\n"),
]

Q = CodingQuestion.Difficulty

# (title, difficulty, marks, statement, input fmt, output fmt, constraints, sample in, sample out, tests)
# tests: (label, public, weight, input, expected)
PROBLEMS = [
    (
        "Escape the HTML", Q.EASY, 20,
        "Untrusted text must be escaped before it is placed in a web page. Read one line and print it with these "
        "five characters replaced:\n& becomes &amp;\n< becomes &lt;\n> becomes &gt;\n\" becomes &quot;\n' becomes &#x27;",
        "One line of text.", "The escaped line.", "0 <= length of the line <= 200", "Tom & Jerry <3", "Tom &amp; Jerry &lt;3",
        [
            ("Ampersand", True, 1, "Tom & Jerry <3\n", "Tom &amp; Jerry &lt;3\n"),
            ("Less-than", True, 1, "a<b\n", "a&lt;b\n"),
            ("Script tag", False, 2, "<script>alert(\"x\")</script>\n", "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;\n"),
            ("Apostrophe", False, 1, "it's\n", "it&#x27;s\n"),
            ("Already escaped", False, 2, "&lt;\n", "&amp;lt;\n"),
            ("Plain text", False, 1, "nothing to do\n", "nothing to do\n"),
        ],
    ),
    (
        "Valid username", Q.MEDIUM, 30,
        "Accounts need safe usernames. Read one line and print VALID if it is 3 to 16 characters long, starts with a "
        "letter, and contains only letters, digits and underscores. Otherwise print INVALID.",
        "One line: the candidate username.", "VALID or INVALID.", "0 <= length of the line <= 100", "alice_01", "VALID",
        [
            ("Normal", True, 1, "alice_01\n", "VALID\n"),
            ("Starts with digit", True, 1, "1abc\n", "INVALID\n"),
            ("Too short", False, 1, "ab\n", "INVALID\n"),
            ("Too long", False, 1, "a_very_long_username_here\n", "INVALID\n"),
            ("Contains space", False, 1, "bad name\n", "INVALID\n"),
            ("Contains dash", False, 1, "x-y\n", "INVALID\n"),
            ("Mixed case", False, 1, "Zed\n", "VALID\n"),
            ("Fifteen chars", False, 1, "abc123456789012\n", "VALID\n"),
        ],
    ),
    (
        "Sum of numbers", Q.EASY, 10,
        "The first line contains n. The second line contains n integers. Print their sum.",
        "Line 1: n. Line 2: n integers.", "A single integer.", "1 <= n <= 100000; each value fits in 32 bits",
        "3\n1 2 3", "6",
        [
            ("Small", True, 1, "3\n1 2 3\n", "6\n"),
            ("Single", False, 1, "1\n5\n", "5\n"),
            ("Negatives", False, 1, "4\n-1 -2 3 10\n", "10\n"),
            ("Large", False, 2, "3\n2000000000 2000000000 2000000000\n", "6000000000\n"),
        ],
    ),
]


class Command(BaseCommand):
    help = (
        "Create the default programming languages and a demo competition with auto-gradable problems "
        "(public and private test cases). Safe to run more than once."
    )

    @transaction.atomic
    def handle(self, *args, **options):
        for position, (name, slug, judge_id, mode, starter) in enumerate(LANGUAGES):
            Language.objects.get_or_create(
                slug=slug,
                defaults={"name": name, "judge0_language_id": judge_id, "editor_mode": mode,
                          "starter_code": starter, "position": position},
            )

        now = timezone.now()
        competition, created = Competition.objects.get_or_create(
            slug="judge-warmup",
            defaults={
                "title": "Judge Warm-up",
                "status": Competition.Status.ONGOING,
                "description": "Three short problems with automatic judging. Write a solution in your preferred language, "
                               "run it against the public examples, then submit it to be scored against all tests, "
                               "including private ones.",
                "rules": "Work on your own.\nOnly solutions sent with Submit solution are scored.\nYour best score on each question counts.",
                "start_time": now - timedelta(minutes=10),
                "end_time": now + timedelta(hours=3),
                "duration_minutes": 60,
            },
        )
        for title, diff, marks, statement, inp, out, cons, s_in, s_out, tests in PROBLEMS:
            question, _ = CodingQuestion.objects.get_or_create(
                competition=competition, title=title,
                defaults={
                    "difficulty": diff, "marks": marks, "problem_statement": statement, "input_format": inp,
                    "output_format": out, "constraints": cons, "sample_input": s_in, "sample_output": s_out,
                },
            )
            if not question.test_cases.exists():
                QuestionTestCase.objects.bulk_create([
                    QuestionTestCase(question=question, label=label, is_public=public, weight=weight,
                                     input_data=inp_, expected_output=exp, position=i)
                    for i, (label, public, weight, inp_, exp) in enumerate(tests)
                ])
        self.stdout.write(self.style.SUCCESS(
            f"Judging demo ready: {Language.objects.count()} language(s); competition 'Judge Warm-up' "
            f"({'created' if created else 'already existed'})."))
        self.stdout.write("Set JUDGE0_URL to evaluate code (see README). Without a runner, submissions are not evaluated.")
