"""One autonomous cycle, start to finish.

Cycle order (every step is defensive):
  1. Open memory, start a run record.
  2. Reconcile any pending intent left by a crashed/interrupted previous cycle.
  3. Re-read the discussion topic and parse the COURSE-TEAM CONTROL line.
     PAUSED => exit without writing anything.
  4. Read entries + replies, update the seen-set.
  5. Decide. Not posting is a normal, recorded outcome.
  6. If posting: begin intent -> POST -> verify by re-reading -> confirm intent.
  7. Record success/failure; trip the circuit breaker after repeated failures.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from .canvas import (
    CanvasClient,
    CanvasMalformedResponse,
    CanvasPermanentError,
    CanvasTransientError,
)
from .decide import decide
from .memory import Memory, body_hash, utcnow
from .sanitize import html_to_text

log = logging.getLogger("hw3agent.runner")

CONTROL_RUNNING = "RUNNING"
CONTROL_PAUSED = "PAUSED"


@dataclass
class CycleResult:
    outcome: str  # posted | no_action | paused | blocked | error
    reason: str
    posts_made: int = 0
    new_items: int = 0
    canvas_id: str | None = None
    url: str | None = None


def read_control_line(topic: dict) -> tuple[str, str]:
    """Parse the course-team control line at the top of the forum description.

    Fail *closed*: anything we cannot positively read as RUNNING is treated as
    PAUSED, so an unexpected description format cannot be read as permission.
    """
    description = html_to_text(topic.get("message") or topic.get("description") or "")
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if "COURSE-TEAM CONTROL" in upper:
            if CONTROL_PAUSED in upper:
                return CONTROL_PAUSED, line
            if CONTROL_RUNNING in upper:
                return CONTROL_RUNNING, line
            return CONTROL_PAUSED, f"unrecognised control value: {line}"
        # Control line must be at the very top; stop after the first real line.
        return CONTROL_PAUSED, f"no control line at top of description (saw: {line[:80]!r})"
    return CONTROL_PAUSED, "empty forum description"


def reconcile_pending(client: CanvasClient, memory: Memory, cfg) -> list[str]:
    """Resolve intents left pending by an interrupted cycle.

    This is the lost-acknowledgement guard: we re-read the thread and look for
    our own content by normalised body hash. If it is there, Canvas committed
    the write and we confirm locally (no second post). If it is not, the intent
    is abandoned so the normal decision path may try again later.
    """
    notes: list[str] = []
    pending = memory.pending_intents()
    if not pending:
        return notes

    self_id = memory.get_meta("self_user_id") or ""
    entries = client.list_entries(cfg.course_id, cfg.topic_id)

    for intent in pending:
        key = intent["idem_key"]
        want = intent["body_hash"]
        found_id = None

        pool: list[dict] = []
        if intent["action"] == "entry":
            pool = [e for e in entries if str(e.get("user_id")) == str(self_id)]
        else:
            parent = str(intent["target_id"])
            pool = [
                r
                for r in client.list_replies(cfg.course_id, cfg.topic_id, parent)
                if str(r.get("user_id")) == str(self_id)
            ]

        for item in pool:
            text = html_to_text(item.get("message"))
            if body_hash(intent["action"], intent["target_id"], text) == want:
                found_id = str(item.get("id"))
                break

        if found_id:
            memory.confirm_intent(key, found_id, verified=True)
            notes.append(f"recovered intent {key[:8]} -> existing Canvas id {found_id}")
            log.warning(
                "RECOVERY: intent %s was already committed on Canvas as %s; "
                "confirmed locally instead of re-posting",
                key[:8], found_id,
            )
        else:
            memory.abandon_intent(key, "not found on Canvas during reconciliation")
            notes.append(f"abandoned intent {key[:8]} (no matching post on Canvas)")
            log.warning("intent %s not found on Canvas; abandoned", key[:8])
    return notes


def _verify_post(client: CanvasClient, cfg, action: str, target_id, canvas_id: str,
                 expected_hash: str) -> bool:
    """Confirm the contribution was actually saved, by re-reading Canvas."""
    try:
        if action == "entry":
            items = client.list_entries(cfg.course_id, cfg.topic_id)
        else:
            items = client.list_replies(cfg.course_id, cfg.topic_id, target_id)
    except Exception as exc:
        log.warning("verification read failed: %s", exc)
        return False
    for item in items:
        if str(item.get("id")) != str(canvas_id):
            continue
        text = html_to_text(item.get("message"))
        if body_hash(action, target_id, text) == expected_hash:
            return True
        log.warning("post %s found but body hash differs (Canvas may have rewritten HTML)", canvas_id)
        return True  # it exists and is ours; Canvas sanitising HTML is acceptable
    return False


def entry_url(cfg, topic_id: str, entry_id: str | None = None) -> str:
    base = f"{cfg.base_url}/courses/{cfg.course_id}/discussion_topics/{topic_id}"
    return f"{base}#entry-{entry_id}" if entry_id else base


def run_cycle(cfg, *, trigger: str = "cron", client: CanvasClient | None = None) -> CycleResult:
    memory = Memory(cfg.db_path)
    run_id = memory.start_run(trigger)
    client = client or CanvasClient(
        cfg.base_url, cfg.token, timeout=cfg.http_timeout_seconds,
        max_attempts=cfg.http_max_attempts,
    )

    try:
        breaker = memory.breaker_state()
        if breaker.is_open():
            reason = f"circuit breaker open until {breaker.open_until.isoformat()}"
            log.error("STOPPED: %s (last error: %s)", reason, breaker.last_error)
            memory.finish_run(run_id, outcome="blocked", reason=reason)
            return CycleResult("blocked", reason)

        # Identity, cached after the first run.
        self_id = memory.get_meta("self_user_id")
        if not self_id:
            me = client.whoami()
            self_id = str(me["id"])
            memory.set_meta("self_user_id", self_id)
            log.info("identified self as Canvas user id %s", self_id)

        recovery_notes = reconcile_pending(client, memory, cfg)

        # --- mandatory control check before ANY write -----------------------
        topic = client.get_topic(cfg.course_id, cfg.topic_id)
        control, control_line = read_control_line(topic)
        log.info("control line: %s", control_line[:160])
        if control != CONTROL_RUNNING:
            memory.record_success()  # the read path worked; this is not a failure
            memory.finish_run(
                run_id, outcome="paused", reason=control_line[:200],
                detail={"recovery": recovery_notes},
            )
            log.info("PAUSED by course team -- no posting this cycle")
            return CycleResult("paused", control_line[:200])

        entries = client.list_entries(cfg.course_id, cfg.topic_id)
        replies_by_entry: dict[str, list[dict]] = {}
        for entry in entries:
            if entry.get("has_more_replies") or entry.get("recent_replies") is not None \
                    or int(entry.get("reply_count") or 0) > 0:
                replies_by_entry[str(entry["id"])] = client.list_replies(
                    cfg.course_id, cfg.topic_id, entry["id"]
                )
            else:
                replies_by_entry[str(entry["id"])] = []

        decision = decide(
            cfg=cfg, memory=memory, entries=entries,
            replies_by_entry=replies_by_entry, self_id=self_id,
        )

        if not decision.act:
            log.info("DECISION: no post this cycle -- %s", decision.reason)
            for note in decision.skipped[:10]:
                log.info("  skipped: %s", note)
            memory.record_success()
            memory.finish_run(
                run_id, outcome="no_action", reason=decision.reason,
                detail={"skipped": decision.skipped[:20], "recovery": recovery_notes},
            )
            return CycleResult("no_action", decision.reason)

        if cfg.dry_run:
            log.info("DRY RUN -- would %s: %s", decision.action, decision.body[:200])
            memory.record_success()
            memory.finish_run(run_id, outcome="no_action", reason="dry run: " + decision.reason)
            return CycleResult("no_action", "dry run: " + decision.reason)

        # --- two-phase write ------------------------------------------------
        action = "reply" if decision.action == "reply" else "entry"
        key, is_new = memory.begin_intent(
            action, cfg.topic_id, decision.target_id, decision.body
        )
        if not is_new:
            reason = "identical intent already recorded; refusing to duplicate"
            log.warning("IDEMPOTENT STOP: %s (key %s)", reason, key[:8])
            memory.finish_run(run_id, outcome="no_action", reason=reason)
            return CycleResult("no_action", reason)

        expected = body_hash(action, decision.target_id, decision.body)
        log.info("posting %s (intent %s): %s", action, key[:8], decision.body[:120])

        try:
            if action == "reply":
                created = client.create_reply(
                    cfg.course_id, cfg.topic_id, decision.target_id, decision.body
                )
            else:
                created = client.create_entry(cfg.course_id, cfg.topic_id, decision.body)
        except Exception as exc:
            memory.fail_intent(key, str(exc))
            raise

        canvas_id = str(created["id"])
        verified = _verify_post(client, cfg, action, decision.target_id, canvas_id, expected)
        memory.confirm_intent(key, canvas_id, verified=verified)
        memory.record_success()

        url = entry_url(cfg, cfg.topic_id, canvas_id)
        log.info("POSTED %s id=%s verified=%s url=%s", action, canvas_id, verified, url)
        memory.finish_run(
            run_id, outcome="posted", reason=decision.reason, posts_made=1,
            detail={"canvas_id": canvas_id, "verified": verified, "url": url,
                    "recovery": recovery_notes},
        )
        return CycleResult("posted", decision.reason, posts_made=1,
                           canvas_id=canvas_id, url=url)

    except (CanvasTransientError, CanvasPermanentError, CanvasMalformedResponse) as exc:
        state = memory.record_failure(
            str(exc), threshold=cfg.max_consecutive_failures,
            cooldown=timedelta(minutes=cfg.breaker_cooldown_minutes),
        )
        log.error("cycle failed (%d consecutive): %s", state.consecutive_failures, exc)
        if state.is_open():
            log.error("STOPPING: circuit breaker opened until %s", state.open_until)
        memory.finish_run(run_id, outcome="error", reason=str(exc)[:300],
                          detail={"consecutive_failures": state.consecutive_failures})
        return CycleResult("error", str(exc)[:300])
    except Exception as exc:  # unexpected -- still counts toward the breaker
        state = memory.record_failure(
            f"unexpected: {exc}", threshold=cfg.max_consecutive_failures,
            cooldown=timedelta(minutes=cfg.breaker_cooldown_minutes),
        )
        log.exception("unexpected cycle failure (%d consecutive)", state.consecutive_failures)
        memory.finish_run(run_id, outcome="error", reason=f"unexpected: {exc}"[:300])
        return CycleResult("error", f"unexpected: {exc}"[:300])
    finally:
        memory.close()
