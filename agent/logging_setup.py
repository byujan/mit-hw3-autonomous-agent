"""Logging that is safe to commit: every record passes through a redactor."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

# Anything that looks like a Canvas token (long opaque string, often with a
# numeric prefix and a tilde) is scrubbed even if it is not *our* token.
TOKEN_PATTERNS = [
    re.compile(r"\b\d{3,6}~[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"),
    re.compile(r"(?i)(canvas_api_token\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(access_token=)[^&\s]+"),
]


class RedactingFilter(logging.Filter):
    def __init__(self, extra_secrets: list[str] | None = None) -> None:
        super().__init__()
        self.extra_secrets = [s for s in (extra_secrets or []) if s]

    def _scrub(self, text: str) -> str:
        for secret in self.extra_secrets:
            text = text.replace(secret, "[REDACTED]")
        for pattern in TOKEN_PATTERNS:
            text = pattern.sub(
                lambda m: (m.group(1) + "[REDACTED]") if m.groups() else "[REDACTED]", text
            )
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            record.args = tuple(
                self._scrub(a) if isinstance(a, str) else a for a in record.args
            )
        return True


def setup_logging(log_path: Path, *, secrets: list[str] | None = None,
                  verbose: bool = False) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("hw3agent")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )
    redactor = RedactingFilter(secrets)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    stream_handler.addFilter(redactor)
    logger.addHandler(stream_handler)

    return logger
