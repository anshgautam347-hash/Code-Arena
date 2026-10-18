"""Client for an external Judge0 code-execution service.

Untrusted code is NEVER executed by this project. It is sent over HTTP to Judge0,
which runs it in its own isolated sandbox. This module uses only the standard
library, so no extra dependency is needed.

If the runner is not configured or cannot be reached, callers get RunnerUnavailable.
There is deliberately no local fallback and no simulated output.
"""
import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

# Judge0 status ids
IN_QUEUE, PROCESSING, ACCEPTED, WRONG_ANSWER, TIME_LIMIT, COMPILE_ERROR = 1, 2, 3, 4, 5, 6
RUNTIME_ERRORS = {7, 8, 9, 10, 11, 12, 14}
INTERNAL_ERROR = 13
PENDING_IDS = {IN_QUEUE, PROCESSING}

FIELDS = "token,status_id,status,stdout,stderr,compile_output,message,time,memory"


class RunnerError(Exception):
    """The runner answered, but something about the request or response was wrong."""


class RunnerUnavailable(RunnerError):
    """The runner is not configured, cannot be reached, or refused us (credentials, rate limit)."""


def _b64(text):
    return base64.b64encode((text or "").encode("utf-8")).decode("ascii")


def _unb64(value):
    if value in (None, ""):
        return ""
    try:
        return base64.b64decode(value).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return str(value)


class Judge0Client:
    BATCH_LIMIT = 20  # Judge0's default maximum submissions per batch call

    def __init__(self, base_url, token="", auth_header="X-Auth-Token", extra_headers=None, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.auth_header = auth_header or "X-Auth-Token"
        self.extra_headers = dict(extra_headers or {})
        self.timeout = timeout

    # ------------------------------------------------------------ HTTP
    def _request(self, method, path, params=None, body=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", **self.extra_headers}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers[self.auth_header] = self.token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RunnerUnavailable("The code runner rejected our credentials.") from exc
            if exc.code == 429:
                raise RunnerUnavailable("The code runner is rate limiting requests.") from exc
            if exc.code >= 500:
                raise RunnerUnavailable(f"The code runner reported an error ({exc.code}).") from exc
            raise RunnerError(f"The code runner rejected the request ({exc.code}).") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise RunnerUnavailable("The code runner could not be reached.") from exc
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except ValueError as exc:
            raise RunnerError("The code runner sent an unreadable response.") from exc

    # ------------------------------------------------------------ submissions
    def submit_batch(self, jobs):
        """Queue jobs for execution. Returns one token per job, in order.

        Each job: source_code, language_id, stdin, cpu_time_limit (seconds), memory_limit_kb.
        """
        tokens = []
        for start in range(0, len(jobs), self.BATCH_LIMIT):
            chunk = jobs[start:start + self.BATCH_LIMIT]
            payload = {"submissions": [self._job_payload(job) for job in chunk]}
            reply = self._request("POST", "/submissions/batch", {"base64_encoded": "true"}, payload)
            if not isinstance(reply, list) or len(reply) != len(chunk):
                raise RunnerError("The code runner returned an unexpected reply to a batch.")
            for item in reply:
                token = item.get("token") if isinstance(item, dict) else None
                if not token:
                    raise RunnerError("The code runner refused one of the test runs.")
                tokens.append(token)
        return tokens

    @staticmethod
    def _job_payload(job):
        cpu = float(job["cpu_time_limit"])
        return {
            "source_code": _b64(job["source_code"]),
            "language_id": int(job["language_id"]),
            "stdin": _b64(job.get("stdin", "")),
            "cpu_time_limit": cpu,
            "wall_time_limit": min(max(cpu * 3, 2.0), 20.0),
            "memory_limit": int(job["memory_limit_kb"]),
        }

    def get_batch(self, tokens):
        """Fetch current results. Returns {token: info} where info is a plain dict."""
        found = {}
        for start in range(0, len(tokens), self.BATCH_LIMIT):
            chunk = tokens[start:start + self.BATCH_LIMIT]
            params = {"tokens": ",".join(chunk), "base64_encoded": "true", "fields": FIELDS}
            reply = self._request("GET", "/submissions/batch", params)
            items = reply.get("submissions") if isinstance(reply, dict) else None
            if not isinstance(items, list):
                raise RunnerError("The code runner returned an unexpected reply to a status request.")
            for item in items:
                if not isinstance(item, dict) or not item.get("token"):
                    continue
                status = item.get("status") or {}
                status_id = item.get("status_id", status.get("id"))
                if status_id is None:
                    continue
                found[item["token"]] = {
                    "status_id": int(status_id),
                    "stdout": _unb64(item.get("stdout")),
                    "stderr": _unb64(item.get("stderr")),
                    "compile_output": _unb64(item.get("compile_output")),
                    "message": _unb64(item.get("message")),
                    "time_ms": _seconds_to_ms(item.get("time")),
                    "memory_kb": _to_int(item.get("memory")),
                }
        return found

    # ------------------------------------------------------------ diagnostics (used by check_runner)
    def about(self):
        return self._request("GET", "/about")

    def languages(self):
        reply = self._request("GET", "/languages")
        return reply if isinstance(reply, list) else []


def _seconds_to_ms(value):
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_client():
    """The configured runner, or None when no runner is configured."""
    url = (getattr(settings, "JUDGE0_URL", "") or "").strip()
    if not url:
        return None
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        logger.error("JUDGE0_URL must start with http:// or https://")
        return None
    return Judge0Client(
        url,
        token=getattr(settings, "JUDGE0_AUTH_TOKEN", ""),
        auth_header=getattr(settings, "JUDGE0_AUTH_HEADER", "X-Auth-Token"),
        extra_headers=getattr(settings, "JUDGE0_EXTRA_HEADERS", {}),
        timeout=getattr(settings, "JUDGE0_TIMEOUT_SECONDS", 10),
    )
