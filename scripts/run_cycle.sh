#!/usr/bin/env bash
# One scheduled cycle. This is what cron/launchd invokes -- no human in the loop.
#
# Install (cron, every 3 hours at :07 to avoid round-hour contention):
#   crontab -e
#   7 */3 * * * /Users/you/path/HW3/scripts/run_cycle.sh >> /Users/you/path/HW3/var/cron.log 2>&1
#
# The script is deliberately defensive: it refuses to run without a token,
# serialises cycles with a lock so two overlapping runs cannot double-post,
# and always exits 0 for transient trouble so cron does not mail-spam.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR" || exit 1

STATE_DIR="${AGENT_STATE_DIR:-$REPO_DIR/var}"
mkdir -p "$STATE_DIR"

# Load secrets from .env (never committed). Keep them out of argv and logs.
if [[ -f "$REPO_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +a
fi

if [[ -z "${CANVAS_API_TOKEN:-}" ]]; then
  echo "$(date -u +%FT%TZ) FATAL CANVAS_API_TOKEN not set; refusing to run" >&2
  exit 1
fi

PYTHON="${AGENT_PYTHON:-python3}"

# Single-flight: a stuck cycle must not overlap with the next one. macOS has no
# flock(1), so use a PID-file guard that works everywhere.
PIDFILE="$STATE_DIR/cycle.pid"
if [[ -f "$PIDFILE" ]]; then
  old_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "$(date -u +%FT%TZ) SKIP another cycle is running (pid $old_pid)" >&2
    exit 0
  fi
  echo "$(date -u +%FT%TZ) clearing stale pidfile (pid ${old_pid:-unknown})" >&2
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

echo "$(date -u +%FT%TZ) starting cycle"
# Proves to the agent that this cycle came from the scheduler, not a human
# shell. `python -m agent run --trigger cron` alone is recorded as manual.
export AGENT_INVOKED_BY=cron
"$PYTHON" -m agent run --trigger cron
rc=$?
echo "$(date -u +%FT%TZ) cycle finished rc=$rc"

# rc=1 means a handled failure (already recorded + breaker counted). Don't let
# cron treat it as a crash loop.
exit 0
