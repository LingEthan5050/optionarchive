#!/bin/bash
# Weekly sync of the archive to object storage.
#
#   ./deploy/backup.sh
#
# The local copy stays authoritative: analysis reads local Parquet, so you
# never pay egress to query your own data. This is disaster recovery only.
#
# Set up the remote once with `rclone config` (B2 or S3 Glacier Instant are
# both a few dollars a month at this volume), then set ARCHIVE_REMOTE.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${ARCHIVE_DATA_DIR:-$REPO/data}"
REMOTE="${ARCHIVE_REMOTE:-}"

if [ -z "$REMOTE" ]; then
    echo "ERROR: set ARCHIVE_REMOTE, e.g. b2:my-bucket/optionarchive" >&2
    exit 1
fi
if ! command -v rclone >/dev/null; then
    echo "ERROR: rclone not installed (brew install rclone)" >&2
    exit 1
fi

echo "Syncing $DATA -> $REMOTE"

# Deliberately NOT --delete-during. A local mistake should not propagate to
# the backup; orphaned remote files cost pennies, lost history is forever.
# runs.db is excluded: it is operational bookkeeping, not archive data, and
# syncing a live SQLite file mid-write is a good way to back up a torn one.
rclone copy "$DATA" "$REMOTE" \
    --exclude "runs.db" \
    --exclude "*.tmp" \
    --transfers 8 \
    --progress \
    --stats-one-line

echo "Done. Verify with: rclone size $REMOTE"
