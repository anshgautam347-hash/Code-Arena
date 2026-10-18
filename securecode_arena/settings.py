"""Local-only settings (Stage 5): SQLite, admin, participant accounts, exams, code judging and results."""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path):
    """Read KEY=VALUE lines from an optional, git-ignored .env file. Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env_file(BASE_DIR / ".env")
# Public development placeholder, not a production credential.
SECRET_KEY = "stage-1-local-development-only-do-not-use-in-production"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "competitions.apps.CompetitionsConfig",
    "exams.apps.ExamsConfig",
    "judging.apps.JudgingConfig",
    "results",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "securecode_arena.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "securecode_arena.wsgi.application"
ASGI_APPLICATION = "securecode_arena.asgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "exams:login"
LOGIN_REDIRECT_URL = "competitions:home"
LOGOUT_REDIRECT_URL = "competitions:home"

# Exam rules (Stage 3). All enforcement happens on the server.
EXAM_SUBMIT_GRACE_SECONDS = 5      # writes are still accepted this long after the deadline
EXAM_VIOLATION_DEDUPE_SECONDS = 3  # events closer together than this count as one violation
EXAM_CONNECTION_GAP_SECONDS = 90   # silence longer than this is logged for review (not counted)
EXAM_MAX_LOGGED_EVENTS = 200       # cap on stored events per session
EXAM_MAX_CODE_LENGTH = 20000       # maximum characters saved per answer

# --- Code judging (Stage 4) ---
# Code is NEVER run by this project. It is sent to an external Judge0 service. Configure it with
# environment variables (or a git-ignored .env file). With no JUDGE0_URL, submissions are recorded
# as "runner unavailable" and nothing is evaluated or scored: results are never simulated.
JUDGE0_URL = os.environ.get("JUDGE0_URL", "")
JUDGE0_AUTH_TOKEN = os.environ.get("JUDGE0_AUTH_TOKEN", "")                  # secret: keep out of source code
JUDGE0_AUTH_HEADER = os.environ.get("JUDGE0_AUTH_HEADER", "X-Auth-Token")    # RapidAPI uses X-RapidAPI-Key
try:
    JUDGE0_EXTRA_HEADERS = json.loads(os.environ.get("JUDGE0_EXTRA_HEADERS", "{}") or "{}")
except ValueError:
    JUDGE0_EXTRA_HEADERS = {}
JUDGE0_TIMEOUT_SECONDS = 10                  # per HTTP call to the runner
JUDGE_MIN_SECONDS_BETWEEN_SUBMISSIONS = 3    # per participant
JUDGE_MAX_SUBMISSIONS_PER_QUESTION = 60      # runs plus submissions, per participant per question
JUDGE_EVALUATION_TIMEOUT_SECONDS = 120       # give up waiting for a runner result after this long
JUDGE_MAX_OUTPUT_CHARS = 10000               # stored output per test case

# --- Results (Stage 5) ---
LEADERBOARD_MAX_ROWS = 200

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
