"""Canvas REST client: stdlib only, retries with exponential backoff + jitter.

Design notes
------------
* Read-only by default. The only write methods are ``create_entry`` and
  ``create_reply`` -- there is deliberately no update/delete capability, so the
  agent cannot edit or remove anyone else's contribution even if instructed to.
* Every request has a timeout and a bounded retry budget.
* 4xx responses (other than 429) are *not* retried: they are permanent.
* Tokens are sent in the Authorization header only, never in a URL or log.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

log = logging.getLogger("hw3agent.canvas")

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class CanvasError(RuntimeError):
    """Base class for Canvas failures."""


class CanvasPermanentError(CanvasError):
    """Not worth retrying (auth, not found, validation)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CanvasTransientError(CanvasError):
    """Worth retrying (timeout, 5xx, rate limit)."""


class CanvasMalformedResponse(CanvasError):
    """Body was not the JSON shape we require."""


@dataclass
class Paused(Exception):
    """Raised/returned when the course-team control line says PAUSED."""

    raw_line: str


class CanvasClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: int = 30,
        max_attempts: int = 4,
        sleep=time.sleep,
        opener=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._opener = opener or urllib.request.build_opener()
        self.request_count = 0

    # ------------------------------------------------------------------ core
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("http") else f"{self.base_url}/api/v1{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

        body = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "mit-hw3-agent/1.0 (+autonomous discussion agent)",
        }
        if form is not None:
            body = urllib.parse.urlencode(form, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        # Writes are at-most-once at the HTTP layer. A dropped connection or a
        # 5xx after a POST may mean Canvas *already committed* the entry, so
        # blindly retrying creates duplicates. Only 429 (definitively rejected,
        # nothing written) is safe to retry on a write; everything else is
        # surfaced and recovered at the cycle level by reconciling the pending
        # intent against Canvas on the next run.
        is_write = method not in {"GET", "HEAD"}
        attempts = 1 if is_write else self.max_attempts

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                self.request_count += 1
                with self._opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    if not raw.strip():
                        return None, resp_headers
                    try:
                        return json.loads(raw), resp_headers
                    except json.JSONDecodeError as exc:
                        raise CanvasMalformedResponse(
                            f"non-JSON body from {method} {_safe(url)}: {raw[:200]!r}"
                        ) from exc
            except urllib.error.HTTPError as exc:
                status = exc.code
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                retry_after = _retry_after(exc.headers)
                # 429 on a write is safe to retry: the request was rejected
                # outright, so nothing was committed.
                if is_write and status == 429 and attempt <= self.max_attempts:
                    last_error = CanvasTransientError(f"HTTP 429 on {_safe(url)}: {detail}")
                    if attempt >= self.max_attempts:
                        raise last_error from exc
                    self._sleep(min(retry_after or 2.0 ** attempt, 60.0))
                    attempts = self.max_attempts
                    continue
                if not is_write and status in RETRY_STATUS:
                    last_error = CanvasTransientError(f"HTTP {status} on {_safe(url)}: {detail}")
                    self._backoff(attempt, retry_after)
                    continue
                raise CanvasPermanentError(
                    f"HTTP {status} on {method} {_safe(url)}: {detail}", status=status
                ) from exc
            except CanvasMalformedResponse:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = CanvasTransientError(f"network error on {_safe(url)}: {exc}")
                self._backoff(attempt, None)
                continue

        raise last_error or CanvasTransientError(f"exhausted retries on {_safe(url)}")

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        if attempt >= self.max_attempts:
            return
        if retry_after is not None:
            delay = min(retry_after, 60.0)
        else:
            delay = min(2.0 ** (attempt - 1), 30.0) + random.uniform(0, 0.75)
        log.warning("retrying in %.1fs (attempt %d/%d)", delay, attempt, self.max_attempts)
        self._sleep(delay)

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        params = dict(params or {})
        params.setdefault("per_page", 50)
        data, headers = self._request("GET", path, params=params)
        pages = 0
        while True:
            if not isinstance(data, list):
                raise CanvasMalformedResponse(f"expected a JSON list from {path}")
            yield from data
            pages += 1
            nxt = _next_link(headers.get("link", ""))
            if not nxt or pages >= 20:
                return
            data, headers = self._request("GET", nxt)

    # ------------------------------------------------------------------ reads
    def whoami(self) -> dict:
        data, _ = self._request("GET", "/users/self")
        if not isinstance(data, dict) or "id" not in data:
            raise CanvasMalformedResponse("/users/self did not return an id")
        return data

    def list_courses(self) -> list[dict]:
        return list(self._paginate("/courses", {"enrollment_state": "active"}))

    def find_discussion_topic(self, course_id: str, name_contains: str) -> dict | None:
        needle = name_contains.lower()
        for topic in self._paginate(f"/courses/{course_id}/discussion_topics"):
            if needle in str(topic.get("title", "")).lower():
                return topic
        return None

    def get_topic(self, course_id: str, topic_id: str) -> dict:
        data, _ = self._request("GET", f"/courses/{course_id}/discussion_topics/{topic_id}")
        if not isinstance(data, dict) or "id" not in data:
            raise CanvasMalformedResponse("discussion topic response missing id")
        return data

    def list_entries(self, course_id: str, topic_id: str) -> list[dict]:
        return list(self._paginate(f"/courses/{course_id}/discussion_topics/{topic_id}/entries"))

    def list_replies(self, course_id: str, topic_id: str, entry_id: str | int) -> list[dict]:
        try:
            return list(
                self._paginate(
                    f"/courses/{course_id}/discussion_topics/{topic_id}/entries/{entry_id}/replies"
                )
            )
        except CanvasPermanentError as exc:
            if exc.status == 404:
                return []
            raise

    # ----------------------------------------------------------------- writes
    def create_entry(self, course_id: str, topic_id: str, message: str) -> dict:
        data, _ = self._request(
            "POST",
            f"/courses/{course_id}/discussion_topics/{topic_id}/entries",
            form={"message": message},
        )
        if not isinstance(data, dict) or "id" not in data:
            raise CanvasMalformedResponse("create_entry response missing id")
        return data

    def create_reply(
        self, course_id: str, topic_id: str, entry_id: str | int, message: str
    ) -> dict:
        data, _ = self._request(
            "POST",
            f"/courses/{course_id}/discussion_topics/{topic_id}/entries/{entry_id}/replies",
            form={"message": message},
        )
        if not isinstance(data, dict) or "id" not in data:
            raise CanvasMalformedResponse("create_reply response missing id")
        return data


# --------------------------------------------------------------------- helpers
def _safe(url: str) -> str:
    """Strip any query string before a URL reaches a log."""
    return url.split("?", 1)[0]


def _retry_after(headers) -> float | None:
    raw = (headers or {}).get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().lstrip("<").rstrip(">")
        if any('rel="next"' in s.replace(" ", "") or "rel=next" in s for s in section[1:]):
            return url
    return None
