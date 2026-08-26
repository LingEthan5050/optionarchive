#!/bin/bash
# Remove the archiver's launchd timers.
set -euo pipefail
AGENTS="$HOME/Library/LaunchAgents"
for label in com.chainarchiver.am com.chainarchiver.pm; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    echo "Removed: $label"
done
echo "Snapshots will no longer run. The archive in data/ is untouched."
