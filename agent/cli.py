"""Command-line entry point.

    python -m agent bootstrap   # verify token, discover course/topic ids
    python -m agent run         # one autonomous cycle (what cron calls)
    python -m agent status      # memory + rate limit + breaker + run history
    python -m agent evidence    # markdown evidence report for submission
    python -m agent reset-breaker
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from .canvas import CanvasClient
from .config import Config, ConfigError
from .logging_setup import setup_logging
from .memory import Memory
from .runner import entry_url, read_control_line, run_cycle

FORUM_NAME = "Homework 3: Agent Discussion Forum"


def _load_dotenv() -> None:
    """Load .env next to the repo root if present (never committed)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def cmd_bootstrap(args) -> int:
    cfg = Config.from_env(require_token=False)
    if not cfg.token:
        print("CANVAS_API_TOKEN is not set. Put it in .env (chmod 600) or export it.",
              file=sys.stderr)
        return 2
    setup_logging(cfg.log_path, secrets=[cfg.token], verbose=args.verbose)
    client = CanvasClient(cfg.base_url, cfg.token, timeout=cfg.http_timeout_seconds,
                          max_attempts=cfg.http_max_attempts)

    me = client.whoami()
    print(f"authenticated as Canvas user id {me['id']} ({me.get('name', '?')})")

    course_id = cfg.course_id
    topic_id = cfg.topic_id
    if not course_id:
        print("discovering course containing the agent forum...")
        for course in client.list_courses():
            topic = client.find_discussion_topic(str(course["id"]), FORUM_NAME)
            if topic:
                course_id, topic_id = str(course["id"]), str(topic["id"])
                print(f"  found in course {course_id}: {course.get('name')}")
                break
    elif not topic_id:
        topic = client.find_discussion_topic(course_id, FORUM_NAME)
        if topic:
            topic_id = str(topic["id"])

    if not (course_id and topic_id):
        print(f"could not locate '{FORUM_NAME}'. Set CANVAS_COURSE_ID/CANVAS_TOPIC_ID manually.",
              file=sys.stderr)
        return 1

    topic = client.get_topic(course_id, topic_id)
    control, line = read_control_line(topic)
    print(f"forum: {topic.get('title')}")
    print(f"url:   {cfg.base_url}/courses/{course_id}/discussion_topics/{topic_id}")
    print(f"control: {control}  ({line[:120]})")
    print(f"entries visible: {len(client.list_entries(course_id, topic_id))}")

    mem = Memory(cfg.db_path)
    mem.set_meta("self_user_id", str(me["id"]))
    mem.set_meta("course_id", course_id)
    mem.set_meta("topic_id", topic_id)
    mem.close()
    print(f"\nAdd these to .env:\n  CANVAS_COURSE_ID={course_id}\n  CANVAS_TOPIC_ID={topic_id}")
    return 0


def cmd_run(args) -> int:
    cfg = Config.from_env()
    setup_logging(cfg.log_path, secrets=[cfg.token], verbose=args.verbose)
    result = run_cycle(cfg, trigger=args.trigger)
    print(json.dumps({
        "outcome": result.outcome, "reason": result.reason,
        "posts_made": result.posts_made, "url": result.url,
    }, indent=2))
    return 0 if result.outcome in {"posted", "no_action", "paused"} else 1


def cmd_status(args) -> int:
    cfg = Config.from_env(require_token=False)
    mem = Memory(cfg.db_path)
    breaker = mem.breaker_state()
    posts = mem.all_posts()
    print(f"state db:        {cfg.db_path}")
    print(f"items seen:      {mem.seen_count()}")
    print(f"posts total:     {len(posts)}  (last hour: {mem.posts_in_last(timedelta(hours=1))}"
          f"/{cfg.max_posts_per_hour})")
    print(f"pending intents: {len(mem.pending_intents())}")
    print(f"breaker:         failures={breaker.consecutive_failures} "
          f"open={'yes until ' + breaker.open_until.isoformat() if breaker.is_open() else 'no'}")
    if breaker.last_error:
        print(f"last error:      {breaker.last_error[:140]}")
    print("\nrecent runs:")
    for run in mem.recent_runs(15):
        print(f"  {run['started_at']}  {run['trigger']:<6} {str(run['outcome']):<9} "
              f"posts={run['posts_made']} new={run['new_items']}  {(run['reason'] or '')[:72]}")
    if posts:
        print("\nposts:")
        for p in posts:
            print(f"  {p['posted_at']}  {p['action']:<5} id={p['canvas_id']:<10} "
                  f"verified={bool(p['verified'])}  {entry_url(cfg, p['topic_id'], p['canvas_id'])}")
    mem.close()
    return 0


def cmd_evidence(args) -> int:
    """Emit the markdown evidence block required by the deliverables."""
    cfg = Config.from_env(require_token=False)
    mem = Memory(cfg.db_path)
    runs = mem.recent_runs(100)
    posts = mem.all_posts()
    lines = ["# Activity evidence", ""]
    lines.append(f"Total scheduled cycles recorded: {len(runs)}")
    counts: dict[str, int] = {}
    for r in runs:
        counts[str(r["outcome"])] = counts.get(str(r["outcome"]), 0) + 1
    lines.append(f"Outcomes: {counts}")
    lines.append("")
    lines.append("| started (UTC) | trigger | outcome | posts | new items | reason |")
    lines.append("|---|---|---|---|---|---|")
    for r in reversed(runs):
        lines.append(
            f"| {r['started_at']} | {r['trigger']} | {r['outcome']} | {r['posts_made']} "
            f"| {r['new_items']} | {(r['reason'] or '')[:90].replace('|', '/')} |"
        )
    lines += ["", "## Posted contributions (links)", ""]
    for p in posts:
        lines.append(
            f"- `{p['action']}` id {p['canvas_id']} verified={bool(p['verified'])} — "
            f"{entry_url(cfg, p['topic_id'], p['canvas_id'])}"
        )
    if not posts:
        lines.append("_none yet_")
    mem.close()
    out = "\n".join(lines)
    if args.output:
        Path(args.output).write_text(out + "\n", encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(out)
    return 0


def cmd_reset_breaker(args) -> int:
    cfg = Config.from_env(require_token=False)
    mem = Memory(cfg.db_path)
    mem.record_success()
    mem.close()
    print("circuit breaker reset")
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m agent", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("bootstrap", help="verify token and discover ids").set_defaults(func=cmd_bootstrap)
    p_run = sub.add_parser("run", help="execute one autonomous cycle")
    p_run.add_argument("--trigger", default="cron", choices=["cron", "manual"])
    p_run.set_defaults(func=cmd_run)
    sub.add_parser("status", help="show memory and control state").set_defaults(func=cmd_status)
    p_ev = sub.add_parser("evidence", help="write the markdown evidence report")
    p_ev.add_argument("-o", "--output")
    p_ev.set_defaults(func=cmd_evidence)
    sub.add_parser("reset-breaker").set_defaults(func=cmd_reset_breaker)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
