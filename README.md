# SecureCode Arena — Stages 4 and 5

A Django project. Stages 1 to 3 (landing page, competition management, participant accounts and the
monitored exam environment) are unchanged. This release adds:

- **Stage 4:** test cases, a real code editor, evaluation through an **external, isolated Judge0
  service**, submission tracking, execution-status handling and server-side scoring.
- **Stage 5:** participant results, question-wise scores, submission history, leaderboards, tie-breaking
  rules, publishing controls and administrator review.

## Read this first: what is and is not verified

Judge0 needs Docker and could not run in the environment where this was built, so **this project has
never been run against a real Judge0 server.** Everything around it was tested.

| Verified (tests actually run) | Not verified |
| --- | --- |
| 239 automated tests pass, including the Stage 1 to 3 tests | A real Judge0 server (any version) |
| The runner client's HTTP format, base64, batching, auth headers and error mapping, against a local stub that imitates the Judge0 batch API as documented | That Judge0's real replies match that stub in every detail |
| The whole pipeline with **real code execution** (Python and Node) through a throwaway Judge0-style server: correct, partial, timeout, crash, outage and recovery, scoring, ranking, publishing and CSV | Compiled languages (C, C++, Java) and every seeded language ID |
| The exam screen logic and the real CodeMirror editor, driven in a simulated DOM against the live server | How the pages look, real fullscreen, real tab switching, mobile layout, any real browser |
| Upgrading a real Stage 3 database with exam data: nothing lost | Behaviour under many simultaneous participants |

The throwaway server used for the live checks is **not included** and is not safe for untrusted code.
Before a real event, run `check_runner` (below) and then submit a few solutions yourself in a real browser.

## Run it on Windows (PowerShell)

Open the folder containing `manage.py` in VS Code, then **Terminal > New Terminal**. Skip the first two
lines if `.venv` already exists.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py seed_demo_data
.\.venv\Scripts\python.exe manage.py seed_judging_demo
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test
.\.venv\Scripts\python.exe manage.py runserver
```

Then open http://127.0.0.1:8000/ and http://127.0.0.1:8000/admin/.

**Upgrading an existing database:** keep your `db.sqlite3` and run `manage.py migrate`. It applies
`competitions.0003`, `exams.0002` and `judging.0001`. Nothing is deleted or rewritten: existing questions get a
2.0 s / 128 MB limit, results start unpublished, and nobody is excluded from the leaderboard. No new Python
dependencies were added (`requirements.txt` is unchanged).

`seed_judging_demo` creates five languages and a demo competition, **Judge Warm-up**, with three real
problems and public and private test cases. (The Stage 2 demo questions are descriptive and cannot be
auto-graded, so they were left alone.)

## Connect a code runner

Untrusted code is never run by Django. It is sent to a separate **Judge0** server. Settings come from
environment variables, or from a git-ignored `.env` file (copy `.env.example`). **Never put a token in source code.**

| Setting | Meaning |
| --- | --- |
| `JUDGE0_URL` | Base URL, for example `http://localhost:2358`. Empty means no runner. |
| `JUDGE0_AUTH_TOKEN` | Secret sent with each request, if your runner needs one. |
| `JUDGE0_AUTH_HEADER` | Header name for the token. Default `X-Auth-Token`. |
| `JUDGE0_EXTRA_HEADERS` | Optional JSON object of additional headers. |

In PowerShell, for the current window only:

```powershell
$env:JUDGE0_URL = "http://localhost:2358"
.\.venv\Scripts\python.exe manage.py check_runner
.\.venv\Scripts\python.exe manage.py runserver
```

Options (check Judge0's own documentation, as I could not test either on Windows):

1. **Self-hosted Judge0 CE** with Docker (https://github.com/judge0/judge0). Best for privacy and cost. Its
   sandbox needs a compatible Linux/cgroup setup; Docker Desktop with WSL 2 has been reported to need extra
   configuration, so read Judge0's deployment notes first.
2. **A hosted Judge0** (for example through RapidAPI). Free tiers are rate limited, and **participants' code
   leaves your server**, so consider that before using it for real people. A typical configuration is
   `JUDGE0_URL=https://judge0-ce.p.rapidapi.com`, `JUDGE0_AUTH_HEADER=X-RapidAPI-Key`,
   `JUDGE0_AUTH_TOKEN=<your key>` and `JUDGE0_EXTRA_HEADERS={"X-RapidAPI-Host":"judge0-ce.p.rapidapi.com"}`. Confirm the
   values with your provider.

`manage.py check_runner` reports whether the runner is reachable and compares the **Judge0 language IDs** stored
in the admin with the ones the runner offers. The seeded IDs (Python 71, JavaScript 63, C 50, C++ 54, Java 62)
follow Judge0 CE v1.13 as documented; IDs differ between versions, so fix any `MISSING` line in
**Admin > Languages**. `check_runner` does not run any code.

### If the runner is missing or down

Results are **never simulated.** A submission is saved with the status *Runner unavailable*, no verdict, no
test results and no score. The participant sees a clear message, their code stays saved, the attempt does not
count against their limits or ranking, and they can try again. With no runner configured, the exam screen
says so and disables Run and Submit.

## Administrator guide

**Languages** (Admin > Judging > Languages): name, slug, Judge0 ID, editor highlighting, starter code, order
(the first language is the default), enable or disable, and optionally restrict to certain competitions.

**Questions and test cases:** open a coding question. Set **Execution limits** (time per test 0.5 to 10 s,
memory 16 to 512 MB) and add **Test cases**. Each has input, expected output, a **weight** and a **public** flag.
- *Public* cases are shown to participants and are what **Run** uses.
- *Private* cases are used only when **Submitting** and their input, expected output and output are never shown.
- A question's marks are split by weight: passing weight 3 of 5 on a 10-mark question scores 6.00.

Output is compared ignoring line-ending style, trailing spaces and trailing blank lines. Anything else must match.

**Reviewing submissions** (Admin > Judging > Submissions): filter, search, open one to see the code, verdict,
per-test results, timings and score. These are read-only. The action **Re-evaluate selected** reruns chosen
submissions against the *current* test cases (use it after correcting a wrong test), then run
`manage.py process_pending_submissions` or open a results page to collect the results.

**Standings and publishing** (Admin > Competitions > *Standings*): everyone who began, with per-question
scores, time to final score, scored submissions, violation indicators, review status and exclusion. Buttons
**Publish results** and **Unpublish results**, and **Download CSV** (cells are protected against spreadsheet
formula injection). The list actions on Competitions do the same for several at once.

**Excluding a participant:** on their exam session, tick *Leaderboard excluded* (or use the list action) and put
the reason in the review notes. Their submissions are kept and they stay visible to you.

## Participant experience

1. Check in, accept the notice, enter fullscreen, begin (Stage 3, unchanged).
2. Pick a question and a language, write code in the editor, press **Run public tests** for practice.
3. Press **Submit solution** (click twice to confirm) to be scored against all tests. You see the verdict
   and how many tests passed. Public cases show details; private ones show only pass or fail.
4. **Only solutions sent with Submit solution are scored.** Saved drafts are not graded when the exam ends.
5. After the exam: **Your results** shows question-wise scores, rank and full submission history. Scores,
   rank and the leaderboard stay hidden until the organizers publish.

## Scoring and ranking rules

1. A question's score is the participant's **best** scored submission (earliest if equal). The total is the sum.
2. Higher total ranks first.
3. Tie: less **time to final score** (from when they began to their last improving submission).
4. Tie: fewer scored submissions.
5. Equal on all three share a rank (1, 1, 3). Participants with no points share the last rank.

Runs, runner-unavailable attempts and evaluation errors never count. Browser-event violation indicators are
**not** used for ranking, because they are indicators for human review. Use the exclusion control after review.
The leaderboard shows usernames only (never emails), and is public once published.

## Security design

- **No code execution in Django.** A test fails the build if the application modules use `subprocess`, `exec`,
  `eval`, `os.system` and similar, or if anything but `judging/runner.py` talks to the network.
- **Server-side everything that matters:** deadlines, rate limits (one evaluation at a time, 3 s gap, 60 per
  question), language checks, scoring and ranking. The browser's claims are ignored (a test posts a fake score).
- **Hidden tests stay hidden:** private inputs go to the runner (it has to run them) but are never included in any
  page or JSON sent to participants, and private expected outputs are never sent to participants **or** to Judge0
  (outputs are compared in Django).
- **Ownership:** a participant can only read or poll their own submissions (others get 404).
- **Output is untrusted:** the exam screen inserts program output as text, never HTML (tested with hostile output).
- The runner is a trust boundary: its isolation, resource enforcement and patching are Judge0's job.

## Known limitations

- A program can still print the private input it receives to its own output; that output is not shown to the
  participant, but the safest policy is to keep real secrets out of test data.
- Rate limiting is basic and per participant. A full hardening pass (Stage 6) is not done.
- The editor traps the Tab key for indentation, as most code editors do. There is no custom-input run.
- Submissions started before the exam ends may finish after it; they are collected when a results page opens, or by
  `manage.py process_pending_submissions`.
- SQLite is fine for local use but is not suited to many simultaneous writers.
- Browser monitoring remains a set of indicators and cannot prevent cheating (see the Stage 3 notes).
- Not done: deployment (Stage 7), email or password reset, plagiarism checks.

## Other commands

```powershell
.\.venv\Scripts\python.exe manage.py check_runner                  # runner reachability and language IDs
.\.venv\Scripts\python.exe manage.py process_pending_submissions   # collect unfinished evaluations
.\.venv\Scripts\python.exe manage.py finalize_expired_sessions     # close overdue exam sessions
```

## Files

New apps: `judging/` (languages, test cases, submissions, the Judge0 client, evaluation, admin, commands, vendored
CodeMirror 5.65.18 under its MIT licence) and `results/` (standings, results pages, leaderboard, admin standings).

Changed (additive): `securecode_arena/settings.py` and `urls.py`; `competitions` (question limits, results
publication field, admin integration, detail page link, styles appended); `exams` (answer language, leaderboard
exclusion, exam screen markup, `exam.js`, finished page). Existing migrations are untouched; new ones are
`competitions.0003`, `exams.0002`, `judging.0001`.

## Local development only

`DEBUG=True` and the development `SECRET_KEY` are for local use only. `db.sqlite3` and `.env` are git-ignored.

Stage 5 stops here. Stage 6 (hardening) and Stage 7 (deployment) begin only after explicit approval.
