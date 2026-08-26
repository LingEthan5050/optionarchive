#!/bin/bash
# Install the archiver's launchd timers on macOS.
#
#   ./deploy/install-macos.sh
#
# Idempotent: re-run it after moving the repo or changing the Python path.
# Uninstall with ./deploy/uninstall-macos.sh

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$REPO/logs"

AM_LABEL="com.chainarchiver.am"
PM_LABEL="com.chainarchiver.pm"

echo "Repo:   $REPO"

# -- preflight ----------------------------------------------------------

if [ ! -x "$PYTHON" ]; then
    echo "ERROR: $PYTHON not found. Create the venv first:" >&2
    echo "  uv venv && uv pip install -e '.[dev,schedule]'" >&2
    exit 1
fi

if [ ! -f "$REPO/.env" ]; then
    echo "ERROR: $REPO/.env not found. Copy .env.example and fill it in." >&2
    exit 1
fi

# The credential file must not be group- or world-readable.
chmod 600 "$REPO/.env"
echo "Locked: .env is now 0600"

# launchd's StartCalendarInterval uses the machine's local timezone, so the
# 09:45 / 15:45 targets are only correct if this Mac is on Eastern time.
TZ_NAME="$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"
if [ "$TZ_NAME" != "America/New_York" ]; then
    echo "WARNING: system timezone is '$TZ_NAME', not America/New_York." >&2
    echo "         launchd fires on local time, so the snapshot targets will" >&2
    echo "         be wrong. Fix with:" >&2
    echo "         sudo systemsetup -settimezone America/New_York" >&2
fi

mkdir -p "$AGENTS" "$LOGS"

# -- write the agents ---------------------------------------------------
# No weekday filtering here on purpose. launchd fires unconditionally and
# chain_archiver.calendar decides whether the firing should do anything, so
# holidays and early closes live in one place instead of two.
#
# The pm agent fires twice: 12:45 covers early-close days (13:00 close) and
# 15:45 covers normal ones. The guard makes whichever is wrong a no-op, so
# neither launchd nor this script needs to know the NYSE calendar.

write_agent() {
    local label="$1" session="$2" intervals="$3"
    cat > "$AGENTS/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>chain_archiver.cli</string>
        <string>snapshot</string>
        <string>--session</string>
        <string>$session</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$REPO</string>

    <key>StartCalendarInterval</key>
    <array>
$intervals
    </array>

    <key>RunAtLoad</key>
    <false/>

    <key>StandardOutPath</key>
    <string>$LOGS/$session.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGS/$session.log</string>
</dict>
</plist>
PLIST
    echo "Wrote:  $AGENTS/$label.plist"
}

interval() {
    printf '        <dict><key>Hour</key><integer>%s</integer><key>Minute</key><integer>%s</integer></dict>' "$1" "$2"
}

write_agent "$AM_LABEL" am "$(interval 9 45)"
write_agent "$PM_LABEL" pm "$(interval 12 45)
$(interval 15 45)"

# -- load ---------------------------------------------------------------

for label in "$AM_LABEL" "$PM_LABEL"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
    echo "Loaded: $label"
done

echo
echo "Installed. Verify with:"
echo "  launchctl list | grep chainarchiver"
echo
echo "IMPORTANT: a sleeping Mac misses snapshots. Disable sleep with:"
echo "  sudo pmset -a sleep 0 disablesleep 1"
echo "and enable 'Start up automatically after a power failure' in"
echo "System Settings > Energy Saver."
echo
echo "Test the wiring now without waiting for a trigger:"
echo "  $PYTHON -m chain_archiver.cli snapshot --session pm --dry-run --force --symbols SPY"
