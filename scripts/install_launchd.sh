#!/usr/bin/env bash
# Install the schedule with launchd (the supported scheduler on modern macOS).
#
#   ./scripts/install_launchd.sh            # every 3 hours
#   ./scripts/install_launchd.sh 300        # every 5 minutes (for demo/testing)
#   ./scripts/install_launchd.sh --uninstall
#
# Why launchd rather than cron: on macOS 10.15+ /usr/sbin/cron requires Full
# Disk Access to be granted manually, and without it the crontab is installed
# but silently never fires. launchd is the documented mechanism, runs under the
# user's GUI session, and catches up missed intervals after sleep.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="edu.mit.hw3.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled $LABEL"
  exit 0
fi

INTERVAL="${1:-10800}"   # seconds; default 3 hours
mkdir -p "$REPO_DIR/var" "$HOME/Library/LaunchAgents"
chmod +x "$REPO_DIR/scripts/run_cycle.sh"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$REPO_DIR/scripts/run_cycle.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>StandardOutPath</key>
    <string>$REPO_DIR/var/cron.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO_DIR/var/cron.log</string>
    <key>RunAtLoad</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "installed $LABEL — every $INTERVAL seconds ($((INTERVAL / 60)) min)"
echo "plist:  $PLIST"
echo "log:    $REPO_DIR/var/cron.log"
echo
echo "status:   launchctl print gui/$(id -u)/$LABEL | head -20"
echo "run now:  launchctl kickstart -p gui/$(id -u)/$LABEL"
echo "remove:   $0 --uninstall"
