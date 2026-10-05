"""Persistent local memory (SQLite).

Survives cycles, restarts and crashes. Four responsibilities:

1. **Seen-set** -- which entries/replies the agent has already read, so it never
   reprocesses them and never treats its own posts as new input.
2. **Idempotency / two-phase intents** -- before any Canvas write the agent
   records an ``intent`` row keyed by a deterministic idempotency key. If the
   process dies, or the HTTP acknowledgement is lost after Canvas already
   committed the post, the next cycle finds the dangling intent and
   *reconciles* it against Canvas instead of posting again.
3. **Rate limiting** -- a durable ledger of post timestamps enforces the
   three-posts-per-hour ceiling across restarts (an in-memory counter would
   reset and breach the limit).
4. **Circuit breaker** -- consecutive-failure count and a cooldown deadline, so
   repeated failures stop the agent rather than hammering Canvas.

Every write is a single committed transaction; SQLite is opened with WAL so a
kill -9 mid-cycle leaves a consistent database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Everything the agent has read.
CREATE TABLE IF NOT EXISTS seen_items (
    item_type   TEXT NOT NULL,            -- 'entry' | 'reply'
    item_id     TEXT NOT NULL,
    topic_id    TEXT NOT NULL,
    parent_id   TEXT,
    author_id   TEXT,
    is_self     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT,
    summary     TEXT,
    first_seen  TEXT NOT NULL,
    PRIMARY KEY (item_type, item_id)
);

-- Two-phase write log: one row per intended Canvas write.
CREATE TABLE IF NOT EXISTS intents (
    idem_key     TEXT PRIMARY KEY,
    action       TEXT NOT NULL,           -- 'entry' | 'reply'
    topic_id     TEXT NOT NULL,
    target_id    TEXT,                    -- parent entry for replies
    body_hash    TEXT NOT NULL,
    body         TEXT NOT NULL,
    state        TEXT NOT NULL,           -- pending | confirmed | abandoned
    canvas_id    TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Durable ledger of successful posts (rate limiting + audit).
CREATE TABLE IF NOT EXISTS posts (
    canvas_id   TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    topic_id    TEXT NOT NULL,
    target_id   TEXT,
    body_hash   TEXT NOT NULL,
    idem_key    TEXT,
    verified    INTEGER NOT NULL DEFAULT 0,
    posted_at   TEXT NOT NULL
);

-- One row per scheduled cycle: the autonomy audit trail.
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    trigger      TEXT NOT NULL,           -- 'cron' | 'manual'
    outcome      TEXT,                    -- posted | no_action | paused | blocked | error
    reason       TEXT,
    new_items    INTEGER DEFAULT 0,
    posts_made   INTEGER DEFAULT 0,
    detail       TEXT
);

-- Which specific forum item each of our posts was a response to. Canvas
-- replies are flat (you always POST to the parent entry), so "have I already
-- answered this reply?" cannot be derived from the parent id alone.
CREATE TABLE IF NOT EXISTS responses (
    prompt_item_type TEXT NOT NULL,       -- 'entry' | 'reply'
    prompt_item_id   TEXT NOT NULL,
    our_canvas_id    TEXT NOT NULL,
    posted_at        TEXT NOT NULL,
    PRIMARY KEY (prompt_item_type, prompt_item_id)
);

CREATE TABLE IF NOT EXISTS breaker (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    open_until            TEXT,
    last_error            TEXT,
    updated_at            TEXT
);

INSERT OR IGNORE INTO breaker (id, consecutive_failures) VALUES (1, 0);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def body_hash(action: str, target_id: str | None, body: str) -> str:
    """Content fingerprint; normalised so trivial whitespace edits still match."""
    norm = " ".join(body.split()).lower()
    payload = f"{action}|{target_id or ''}|{norm}"
    return hashlib.sha256(payload.encode()).hexdigest()


def idempotency_key(action: str, topic_id: str, target_id: str | None, body: str) -> str:
    return hashlib.sha256(
        f"{topic_id}|{action}|{target_id or ''}|{body_hash(action, target_id, body)}".encode()
    ).hexdigest()[:32]


@dataclass
class BreakerState:
    consecutive_failures: int
    open_until: datetime | None
    last_error: str | None

    def is_open(self, now: datetime | None = None) -> bool:
        if self.open_until is None:
            return False
        return (now or utcnow()) < self.open_until


class Memory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ----------------------------------------------------------- identity
    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # --------------------------------------------------------- seen items
    def is_seen(self, item_type: str, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_items WHERE item_type=? AND item_id=?", (item_type, str(item_id))
        ).fetchone()
        return row is not None

    def mark_seen(
        self,
        item_type: str,
        item_id: str,
        topic_id: str,
        *,
        parent_id: str | None = None,
        author_id: str | None = None,
        is_self: bool = False,
        created_at: str | None = None,
        summary: str | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO seen_items "
                "(item_type,item_id,topic_id,parent_id,author_id,is_self,created_at,summary,first_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    item_type,
                    str(item_id),
                    str(topic_id),
                    str(parent_id) if parent_id else None,
                    str(author_id) if author_id else None,
                    1 if is_self else 0,
                    created_at,
                    (summary or "")[:280],
                    iso(utcnow()),
                ),
            )

    def seen_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM seen_items").fetchone()["n"]

    def have_replied_to(self, entry_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM posts WHERE action='reply' AND target_id=?", (str(entry_id),)
        ).fetchone()
        return row is not None

    def have_posted_hash(self, h: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM posts WHERE body_hash=?", (h,)).fetchone()
        return row is not None

    # ------------------------------------------------- two-phase intents
    def begin_intent(
        self, action: str, topic_id: str, target_id: str | None, body: str
    ) -> tuple[str, bool]:
        """Record the intent to post *before* calling Canvas.

        Returns ``(idem_key, is_new)``. ``is_new=False`` means we already have
        an intent for byte-identical content -- the caller must reconcile rather
        than post again.
        """
        key = idempotency_key(action, topic_id, target_id, body)
        h = body_hash(action, target_id, body)
        now = iso(utcnow())
        existing = self.conn.execute("SELECT state FROM intents WHERE idem_key=?", (key,)).fetchone()
        if existing:
            with self.tx() as c:
                c.execute(
                    "UPDATE intents SET attempts=attempts+1, updated_at=? WHERE idem_key=?",
                    (now, key),
                )
            return key, False
        with self.tx() as c:
            c.execute(
                "INSERT INTO intents "
                "(idem_key,action,topic_id,target_id,body_hash,body,state,attempts,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?, 'pending', 1, ?, ?)",
                (key, action, str(topic_id), str(target_id) if target_id else None, h, body, now, now),
            )
        return key, True

    def confirm_intent(
        self,
        idem_key: str,
        canvas_id: str,
        *,
        verified: bool = False,
        prompt_item_type: str | None = None,
        prompt_item_id: str | None = None,
    ) -> None:
        now = iso(utcnow())
        row = self.conn.execute("SELECT * FROM intents WHERE idem_key=?", (idem_key,)).fetchone()
        if row is None:
            raise KeyError(f"unknown intent {idem_key}")
        with self.tx() as c:
            c.execute(
                "UPDATE intents SET state='confirmed', canvas_id=?, updated_at=? WHERE idem_key=?",
                (str(canvas_id), now, idem_key),
            )
            c.execute(
                "INSERT OR REPLACE INTO posts "
                "(canvas_id,action,topic_id,target_id,body_hash,idem_key,verified,posted_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(canvas_id),
                    row["action"],
                    row["topic_id"],
                    row["target_id"],
                    row["body_hash"],
                    idem_key,
                    1 if verified else 0,
                    now,
                ),
            )
            if prompt_item_type and prompt_item_id:
                c.execute(
                    "INSERT OR REPLACE INTO responses "
                    "(prompt_item_type,prompt_item_id,our_canvas_id,posted_at) VALUES (?,?,?,?)",
                    (prompt_item_type, str(prompt_item_id), str(canvas_id), now),
                )

    def have_responded_to(self, item_type: str, item_id: str) -> bool:
        """Have we already answered this specific entry or reply?"""
        row = self.conn.execute(
            "SELECT 1 FROM responses WHERE prompt_item_type=? AND prompt_item_id=?",
            (item_type, str(item_id)),
        ).fetchone()
        return row is not None

    def fail_intent(self, idem_key: str, error: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE intents SET last_error=?, updated_at=? WHERE idem_key=?",
                (error[:500], iso(utcnow()), idem_key),
            )

    def abandon_intent(self, idem_key: str, reason: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE intents SET state='abandoned', last_error=?, updated_at=? WHERE idem_key=?",
                (reason[:500], iso(utcnow()), idem_key),
            )

    def pending_intents(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM intents WHERE state='pending' ORDER BY created_at"
            ).fetchall()
        )

    def mark_post_verified(self, canvas_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE posts SET verified=1 WHERE canvas_id=?", (str(canvas_id),))

    # -------------------------------------------------------- rate limits
    def posts_in_last(self, window: timedelta) -> int:
        cutoff = iso(utcnow() - window)
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE posted_at >= ?", (cutoff,)
        ).fetchone()["n"]

    def last_post_at(self, action: str | None = None) -> datetime | None:
        if action:
            row = self.conn.execute(
                "SELECT MAX(posted_at) AS t FROM posts WHERE action=?", (action,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT MAX(posted_at) AS t FROM posts").fetchone()
        if not row or not row["t"]:
            return None
        return datetime.fromisoformat(row["t"])

    def all_posts(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM posts ORDER BY posted_at").fetchall())

    # ----------------------------------------------------------- breaker
    def breaker_state(self) -> BreakerState:
        row = self.conn.execute("SELECT * FROM breaker WHERE id=1").fetchone()
        open_until = datetime.fromisoformat(row["open_until"]) if row["open_until"] else None
        return BreakerState(row["consecutive_failures"], open_until, row["last_error"])

    def record_failure(self, error: str, *, threshold: int, cooldown: timedelta) -> BreakerState:
        state = self.breaker_state()
        n = state.consecutive_failures + 1
        open_until = iso(utcnow() + cooldown) if n >= threshold else None
        with self.tx() as c:
            c.execute(
                "UPDATE breaker SET consecutive_failures=?, open_until=?, last_error=?, updated_at=? "
                "WHERE id=1",
                (n, open_until, error[:500], iso(utcnow())),
            )
        return self.breaker_state()

    def record_success(self) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE breaker SET consecutive_failures=0, open_until=NULL, last_error=NULL, "
                "updated_at=? WHERE id=1",
                (iso(utcnow()),),
            )

    # -------------------------------------------------------------- runs
    def start_run(self, trigger: str) -> str:
        run_id = f"{int(time.time())}-{hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:6]}"
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs (run_id, started_at, trigger) VALUES (?,?,?)",
                (run_id, iso(utcnow()), trigger),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        outcome: str,
        reason: str = "",
        new_items: int = 0,
        posts_made: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, outcome=?, reason=?, new_items=?, posts_made=?, "
                "detail=? WHERE run_id=?",
                (
                    iso(utcnow()),
                    outcome,
                    reason[:500],
                    new_items,
                    posts_made,
                    json.dumps(detail or {})[:4000],
                    run_id,
                ),
            )

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        )
