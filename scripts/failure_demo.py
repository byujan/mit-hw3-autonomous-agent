#!/usr/bin/env python3
"""Reproducible failure-injection demo -- the recovery evidence.

Runs entirely against an in-memory fake Canvas, so it touches no real forum
and needs no token. It injects a *lost acknowledgement*: Canvas commits the
reply, then the connection drops before the agent reads the response. The
agent must not post a second copy on the next cycle.

    python3 scripts/failure_demo.py

Writes a transcript to var/failure_demo.md for submission.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from agent.memory import Memory  # noqa: E402
from agent.runner import run_cycle  # noqa: E402
from test_agent import SUBSTANTIVE, FakeCanvas, client_for, make_cfg  # noqa: E402

out: list[str] = []


def say(line: str = "") -> None:
    print(line)
    out.append(line)


def snapshot(cfg, label: str) -> None:
    mem = Memory(cfg.db_path)
    posts = mem.all_posts()
    pending = mem.pending_intents()
    say(f"  local memory after {label}:")
    say(f"    confirmed posts : {len(posts)}")
    say(f"    pending intents : {len(pending)}")
    for p in pending:
        say(f"      - {p['idem_key'][:12]} state={p['state']} attempts={p['attempts']}")
    for p in posts:
        say(f"      + canvas_id={p['canvas_id']} verified={bool(p['verified'])}")
    mem.close()


def main() -> int:
    say("# Failure injection: lost acknowledgement after a committed write")
    say()
    say("Scenario: the agent POSTs a reply. Canvas saves it. The connection then")
    say("drops before the agent can read the HTTP response, so the agent cannot")
    say("know whether its write landed. A naive agent re-posts and duplicates.")
    say()

    with tempfile.TemporaryDirectory() as tmp:
        cfg = make_cfg(tmp)
        fake = FakeCanvas()
        entry = fake.add_entry(SUBSTANTIVE)
        eid = str(entry["id"])
        say(f"Forum seeded with one substantive entry from another agent (id {eid}).")
        say()

        say("## Cycle 1 -- acknowledgement is swallowed mid-write")
        fake.swallow_ack = True
        r1 = run_cycle(cfg, trigger="manual", client=client_for(fake, cfg))
        say(f"  cycle outcome   : {r1.outcome} ({r1.reason[:90]})")
        say(f"  replies ON CANVAS: {len(fake.replies.get(eid, []))}  <-- Canvas did commit it")
        snapshot(cfg, "cycle 1")
        say()
        say("  The intent is still 'pending': locally the agent has no proof of the write.")
        say()

        say("## Cycle 2 -- recovery with a healthy connection")
        fake.swallow_ack = False
        r2 = run_cycle(cfg, trigger="manual", client=client_for(fake, cfg))
        say(f"  cycle outcome   : {r2.outcome} ({r2.reason[:90]})")
        say(f"  replies ON CANVAS: {len(fake.replies.get(eid, []))}  <-- still ONE, no duplicate")
        snapshot(cfg, "cycle 2")
        say()

        dup_free = len(fake.replies.get(eid, [])) == 1
        say("## Result")
        say()
        say("  The reconciler re-read the thread, matched the orphaned intent to the")
        say("  already-committed reply by normalised body hash, and confirmed it")
        say("  locally instead of posting again. Completed work was preserved and no")
        say("  duplicate was created.")
        say()
        say(f"  duplicate-free: {'PASS' if dup_free else 'FAIL'}")

        say()
        say("## Cycle 3 -- repeated hard failures must stop the agent")
        for _ in range(cfg.max_consecutive_failures):
            for _ in range(6):
                fake.queue_fault("/discussion_topics/448963", 500)
            res = run_cycle(cfg, trigger="manual", client=client_for(fake, cfg))
            say(f"  outcome: {res.outcome} -- {res.reason[:80]}")
        mem = Memory(cfg.db_path)
        state = mem.breaker_state()
        mem.close()
        say(f"  breaker: failures={state.consecutive_failures} open={state.is_open()}")
        blocked = run_cycle(cfg, trigger="manual", client=client_for(fake, cfg))
        say(f"  next cycle refuses to run: {blocked.outcome} ({blocked.reason[:70]})")
        stopped = blocked.outcome == "blocked"
        say()
        say(f"  stopping-rule: {'PASS' if stopped else 'FAIL'}")

        report = REPO / "var" / "failure_demo.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nwrote {report}")
        return 0 if (dup_free and stopped) else 1


if __name__ == "__main__":
    raise SystemExit(main())
