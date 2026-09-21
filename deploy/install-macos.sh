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
DERIVE_LABEL="com.chainarchiver.derive"
BACKUP_LABEL="com.chainarchiver.backup"

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
for label in "$AM_LABEL" "$PM_LABEL" "$DERIVE_LABEL" "$BACKUP_LABEL"; do
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

# Render CLI arguments as plist <string> elements.
cli_args() {
    for a in "$@"; do printf '        <string>%s</string>\n' "$a"; done
}

write_agent() {
    local label="$1" logname="$2" args="$3" intervals="$4"
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
$args
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
    <string>$LOGS/$logname.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGS/$logname.log</string>
</dict>
</plist>
PLIST
    echo "Wrote:  $AGENTS/$label.plist"
}

# The backup agent runs a shell script, not the CLI, and needs the remote in
# its environment: launchd agents inherit nothing from your shell.
write_backup_agent() {
    local remote="$1"
    cat > "$AGENTS/$BACKUP_LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$BACKUP_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO/deploy/backup.sh</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>ARCHIVE_REMOTE</key>
        <string>$remote</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>$REPO</string>

    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>19</integer><key>Minute</key><integer>0</integer></dict>
    </array>

    <key>RunAtLoad</key>
    <false/>

    <key>StandardOutPath</key>
    <string>$LOGS/backup.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGS/backup.log</string>
</dict>
</plist>
PLIST
    echo "Wrote:  $AGENTS/$BACKUP_LABEL.plist"
}

interval() {
    printf '        <dict><key>Hour</key><integer>%s</integer><key>Minute</key><integer>%s</integer></dict>' "$1" "$2"
}

write_agent "$AM_LABEL" am "$(cli_args snapshot --session am)" "$(interval 9 45)"
write_agent "$PM_LABEL" pm "$(cli_args snapshot --session pm)" "$(interval 12 45)
$(interval 15 45)"

# Greeks, once after each capture rather than once nightly. `derive` with no
# --date takes only the single latest (date, session) partition (cli.py
# run_derive), so a single evening run would derive the pm snapshot and leave
# every am snapshot without greeks forever. Running after each capture also
# means the derived layer is queryable within minutes instead of at day end.
# On a non-trading day this re-derives the most recent partition: a few
# seconds of CPU writing a file identical to the one already there.
write_agent "$DERIVE_LABEL" derive "$(cli_args derive)" "$(interval 10 5)
$(interval 16 5)"

# -- load ---------------------------------------------------------------

# Sunday 19:00. Installed only when a remote exists: an agent that fails
# every week trains you to ignore the log it fails into. ARCHIVE_REMOTE comes
# from the environment or .env, whichever is set.
# `|| true`: preflight guarantees .env exists today, but under `set -e` a
# missing file here would kill the install at the last step instead of
# quietly meaning "no remote configured".
ARCHIVE_REMOTE="${ARCHIVE_REMOTE:-$(sed -n 's/^ARCHIVE_REMOTE=//p' "$REPO/.env" 2>/dev/null | tr -d '"' | head -1 || true)}"
LABELS="$AM_LABEL $PM_LABEL $DERIVE_LABEL"
if [ -n "$ARCHIVE_REMOTE" ]; then
    if command -v rclone >/dev/null; then
        write_backup_agent "$ARCHIVE_REMOTE"
        LABELS="$LABELS $BACKUP_LABEL"
    else
        echo "WARNING: ARCHIVE_REMOTE is set but rclone is missing; skipping the"
        echo "         backup agent. Install it with: brew install rclone"
    fi
else
    echo
    echo "NOTE: ARCHIVE_REMOTE is unset, so no backup is scheduled. The archive"
    echo "      cannot be backfilled - a lost day is lost permanently. Set up a"
    echo "      remote with 'rclone config', add ARCHIVE_REMOTE to .env, and"
    echo "      re-run this script."
fi

for label in $LABELS; do
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
