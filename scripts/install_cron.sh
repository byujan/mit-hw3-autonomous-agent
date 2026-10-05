#!/usr/bin/env bash
# Install the schedule. Default: every 3 hours at :07.
# Usage: ./scripts/install_cron.sh [hours]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOURS="${1:-3}"
ENTRY="7 */$HOURS * * * $REPO_DIR/scripts/run_cycle.sh >> $REPO_DIR/var/cron.log 2>&1"

chmod +x "$REPO_DIR/scripts/run_cycle.sh"
mkdir -p "$REPO_DIR/var"

current="$(crontab -l 2>/dev/null || true)"
if printf '%s\n' "$current" | grep -Fq "$REPO_DIR/scripts/run_cycle.sh"; then
  echo "removing existing entry for this repo"
  current="$(printf '%s\n' "$current" | grep -Fv "$REPO_DIR/scripts/run_cycle.sh")"
fi

printf '%s\n%s\n' "$current" "$ENTRY" | grep -v '^$' | crontab -
echo "installed:"
echo "  $ENTRY"
echo
echo "verify with: crontab -l"
echo "watch with:  tail -f $REPO_DIR/var/cron.log"
