#!/bin/bash
# Install the archiver's launchd timers on macOS.
#
#   ./deploy/install-macos.sh
#   ./deploy/install-macos.sh --force   # take over a schedule installed elsewhere
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

FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        *) echo "usage: $0 [--force]" >&2; exit 2 ;;
    esac
done

echo "Repo:   $REPO"

# -- preflight ----------------------------------------------------------

# A second checkout - a dev copy, a restore from backup - installs under the
# same two labels as the live one. Without this check, running the installer
# from it silently repoints the 09:45/12:45/15:45 schedule at the wrong tree:
# no error, nothing in the output, and you find out when half-finished code
# runs against the market instead of the code you deployed.
for label in "$AM_LABEL" "$PM_LABEL"; do
    plist="$AGENTS/$label.plist"
    [ -f "$plist" ] || continue
    installed="$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "$plist" 2>/dev/null || true)"
    [ -n "$installed" ] && [ "$installed" != "$REPO" ] || continue
    if [ "$FORCE" -eq 1 ]; then
        echo "WARNING: taking $label over from $installed (--force)."
    else
        echo "ERROR: $label is already installed from a different checkout:" >&2
        echo "         installed: $installed" >&2
        echo "         this one:  $REPO" >&2
        echo "       Installing from here would repoint the live schedule at this" >&2
        echo "       checkout. If that is really what you want:" >&2
        echo "         $0 --force" >&2
        exit 1
    fi
done

# macOS privacy protection (TCC) blocks background processes from reading
# Desktop, Documents, Downloads and iCloud Drive. Everything works when you
# run it by hand in Terminal - which has been granted access - and then every
# scheduled run dies with "Operation not permitted". Refuse rather than
# install something that will fail silently at 09:45.
case "$REPO" in
    "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*|"$HOME/Library/Mobile Documents"*)
        echo "ERROR: the repo is under a privacy-protected folder:" >&2
        echo "         $REPO" >&2
        echo "       launchd jobs cannot read it and every scheduled run would fail." >&2
        echo "       Move it somewhere unprotected, e.g.:" >&2
        echo "         mv \"$REPO\" ~/optionarchive" >&2
        exit 1
        ;;
esac

if [ ! -x "$PYTHON" ]; then
    echo "ERROR: $PYTHON not found. Create the venv first:" >&2
    echo "  uv venv --python 3.14 && uv pip install -e '.[dev]'" >&2
    exit 1
fi

# Import the whole CLI once, so a missing dependency fails here in front of
# you rather than inside launchd at 09:45 where nobody is watching.
if ! "$PYTHON" -c "import chain_archiver.cli" 2>/dev/null; then
    echo "ERROR: chain_archiver does not import cleanly. Reinstall with:" >&2
    echo "  uv pip install -e '.[dev]'" >&2
    "$PYTHON" -c "import chain_archiver.cli" >&2 || true
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
TZ_NAME="$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||' || true)"
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
    # bootout returns before launchd has finished unloading; bootstrapping
    # straight after races it and fails with "Input/output error".
    sleep 1
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
    echo "Loaded: $label"
done

# -- things this script will not change for you -------------------------

echo
echo "Installed. Verify with:"
echo "  launchctl list | grep chainarchiver"

if [ "$(pmset -g | awk '/^ *sleep /{print $2}')" != "0" ]; then
    echo
    echo "WARNING: this Mac is set to sleep, and a sleeping Mac misses snapshots."
    echo "  sudo pmset -a sleep 0 autorestart 1"
    echo "  (autorestart brings it back up after a power failure)"
fi

if [ -z "$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null)" ]; then
    echo
    echo "WARNING: automatic login is off."
    echo "  These are LaunchAgents: they run only while you are logged in. After a"
    echo "  power cut or update restart, nothing runs until someone logs in."
    echo "  Enable it in System Settings > Users & Groups > Automatically log in."
    echo "  (Unavailable while FileVault is on - then the healthcheck is what"
    echo "  tells you the Mac rebooted and is sitting at the login screen.)"
fi

echo
echo "Test the wiring now without waiting for a trigger:"
echo "  $PYTHON -m chain_archiver.cli snapshot --session pm --dry-run --force --symbols SPY"
