"""Configuration, loaded entirely from the environment.

No secret ever appears in source, in the repo, or in a log line. The Canvas
token is read from the environment once and held only in memory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = REPO_ROOT / "var"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - config error path
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass
class Config:
    # --- Canvas access -----------------------------------------------------
    base_url: str = "https://canvas.mit.edu"
    token: str = ""
    course_id: str = ""
    topic_id: str = ""

    # --- Local state -------------------------------------------------------
    state_dir: Path = field(default_factory=lambda: DEFAULT_STATE_DIR)

    # --- Automation controls ----------------------------------------------
    max_posts_per_hour: int = 3
    max_posts_per_cycle: int = 1
    max_consecutive_failures: int = 5
    breaker_cooldown_minutes: int = 180
    new_thread_min_gap_hours: int = 24
    http_timeout_seconds: int = 30
    http_max_attempts: int = 4

    # --- Behaviour ---------------------------------------------------------
    dry_run: bool = False
    llm_backend: str = "hermes"  # hermes | template | none
    llm_timeout_seconds: int = 240
    agent_label: str = "byujan-hw3-agent"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "memory.db"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "agent.log"

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> "Config":
        cfg = cls(
            base_url=os.environ.get("CANVAS_BASE_URL", "https://canvas.mit.edu").rstrip("/"),
            token=os.environ.get("CANVAS_API_TOKEN", ""),
            course_id=os.environ.get("CANVAS_COURSE_ID", "").strip(),
            topic_id=os.environ.get("CANVAS_TOPIC_ID", "").strip(),
            state_dir=Path(os.environ.get("AGENT_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser(),
            max_posts_per_hour=_env_int("AGENT_MAX_POSTS_PER_HOUR", 3),
            max_posts_per_cycle=_env_int("AGENT_MAX_POSTS_PER_CYCLE", 1),
            max_consecutive_failures=_env_int("AGENT_MAX_CONSECUTIVE_FAILURES", 5),
            breaker_cooldown_minutes=_env_int("AGENT_BREAKER_COOLDOWN_MINUTES", 180),
            new_thread_min_gap_hours=_env_int("AGENT_NEW_THREAD_MIN_GAP_HOURS", 24),
            http_timeout_seconds=_env_int("AGENT_HTTP_TIMEOUT", 30),
            http_max_attempts=_env_int("AGENT_HTTP_MAX_ATTEMPTS", 4),
            dry_run=_env_bool("AGENT_DRY_RUN", False),
            llm_backend=os.environ.get("AGENT_LLM_BACKEND", "hermes").strip().lower(),
            llm_timeout_seconds=_env_int("AGENT_LLM_TIMEOUT", 240),
            agent_label=os.environ.get("AGENT_LABEL", "byujan-hw3-agent").strip(),
        )

        missing = []
        if require_token and not cfg.token:
            missing.append("CANVAS_API_TOKEN")
        if require_token and not cfg.course_id:
            missing.append("CANVAS_COURSE_ID")
        if require_token and not cfg.topic_id:
            missing.append("CANVAS_TOPIC_ID")
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". See .env.example; never hard-code these."
            )

        if cfg.max_posts_per_hour > 3:
            # Hard ceiling from the assignment: no more than three posts per hour.
            cfg.max_posts_per_hour = 3

        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    def redact(self, text: str) -> str:
        """Defensive scrub so a token can never reach a log file."""
        if self.token and self.token in text:
            text = text.replace(self.token, "[REDACTED-CANVAS-TOKEN]")
        return text
