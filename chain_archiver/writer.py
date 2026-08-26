"""Parquet writes: atomic and idempotent.

A killed process must never leave behind a half-written data.parquet that
looks valid to a later reader (section 6). Every write goes to a temp file in
the destination directory, is flushed to disk, and is then renamed into place
- a rename within a directory is atomic, so a reader sees either the previous
snapshot or the new one and never a torn file.

Re-running the same date and session replaces the file rather than appending,
which is what makes reruns safe.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

COMPRESSION = "zstd"
COMPRESSION_LEVEL = 3


def partition_dir(data_dir: Path, dataset: str, day: date, session: str) -> Path:
    """Hive-style partition path, which is what lets DuckDB prune on date."""
    return data_dir / dataset / f"date={day.isoformat()}" / f"session={session}"


def write_partition(
    table: pa.Table, data_dir: Path, dataset: str, day: date, session: str
) -> Path:
    """Write one partition atomically and return the final path."""
    destination = partition_dir(data_dir, dataset, day, session)
    destination.mkdir(parents=True, exist_ok=True)

    final = destination / "data.parquet"
    tmp = destination / "data.parquet.tmp"

    with open(tmp, "wb") as handle:
        pq.write_table(
            table,
            handle,
            compression=COMPRESSION,
            compression_level=COMPRESSION_LEVEL,
        )
        handle.flush()
        os.fsync(handle.fileno())

    # os.replace is atomic on POSIX and on Windows (MoveFileEx with
    # REPLACE_EXISTING), and overwrites cleanly, which is the idempotency
    # requirement in section 1.
    os.replace(tmp, final)

    # The rename is atomic, but on POSIX the directory entry itself is not
    # durable until the directory is fsynced - without this, a power loss can
    # leave the partition missing even though the write returned. Windows
    # cannot open a directory as a file descriptor, so this is POSIX-only.
    if hasattr(os, "O_DIRECTORY"):
        dir_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    log.info(
        "Wrote %s rows to %s (%.1f KiB)",
        table.num_rows,
        final,
        final.stat().st_size / 1024,
    )
    return final


def partition_exists(data_dir: Path, dataset: str, day: date, session: str) -> bool:
    return (partition_dir(data_dir, dataset, day, session) / "data.parquet").exists()
