"""SQLite run and failure log.

The archive itself cannot tell you why a partition is thin or missing - the
rows that were never fetched leave no trace in Parquet. This does: every run
records what it attempted, what it got, and what broke.

SQLite rather than a log file because the questions are queries: which symbol
has been failing all week, when did the wall time start creeping, which days
have no run at all.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_utc    TEXT    NOT NULL,
    finished_at_utc   TEXT,
    trade_date        TEXT    NOT NULL,
    session           TEXT    NOT NULL,
    symbols_attempted INTEGER NOT NULL DEFAULT 0,
    symbols_ok        INTEGER NOT NULL DEFAULT 0,
    chain_rows        INTEGER NOT NULL DEFAULT 0,
    metric_rows       INTEGER NOT NULL DEFAULT 0,
    wall_seconds      REAL,
    exit_code         INTEGER,
    dry_run           INTEGER NOT NULL DEFAULT 0,
    note              TEXT
);

CREATE TABLE IF NOT EXISTS failures (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL REFERENCES runs(id),
    symbol   TEXT    NOT NULL,
    error    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_date    ON runs(trade_date, session);
CREATE INDEX IF NOT EXISTS idx_fail_symbol  ON failures(symbol);
"""


class RunLog:
    """Append-only record of snapshot runs. Never raises into the caller.

    A logging failure must not be able to take down a capture - the snapshot
    is the valuable thing, and losing it because the bookkeeping broke would
    be exactly backwards.
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "runs.db"
        self.run_id: int | None = None
        self._started = datetime.now(timezone.utc)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as con:
                con.executescript(SCHEMA)
        except sqlite3.Error as exc:
            log.warning("Run log unavailable at %s: %s", self.path, exc)
            self.path = None  # type: ignore[assignment]

    def _connect(self) -> sqlite3.Connection | None:
        if self.path is None:
            return None
        try:
            return sqlite3.connect(self.path)
        except sqlite3.Error as exc:
            log.warning("Run log write failed: %s", exc)
            return None

    def start(self, trade_date: date, session: str, dry_run: bool) -> None:
        con = self._connect()
        if con is None:
            return
        try:
            with closing(con):
                cur = con.execute(
                    "INSERT INTO runs (started_at_utc, trade_date, session, dry_run)"
                    " VALUES (?, ?, ?, ?)",
                    (self._started.isoformat(), trade_date.isoformat(), session,
                     int(dry_run)),
                )
                self.run_id = cur.lastrowid
                con.commit()
        except sqlite3.Error as exc:
            log.warning("Run log start failed: %s", exc)

    def finish(
        self,
        *,
        attempted: int,
        ok: int,
        chain_rows: int,
        metric_rows: int,
        exit_code: int,
        failures: list[tuple[str, str]],
        note: str | None = None,
    ) -> None:
        con = self._connect()
        if con is None or self.run_id is None:
            return
        finished = datetime.now(timezone.utc)
        try:
            with closing(con):
                con.execute(
                    "UPDATE runs SET finished_at_utc=?, symbols_attempted=?,"
                    " symbols_ok=?, chain_rows=?, metric_rows=?, wall_seconds=?,"
                    " exit_code=?, note=? WHERE id=?",
                    (
                        finished.isoformat(), attempted, ok, chain_rows,
                        metric_rows, (finished - self._started).total_seconds(),
                        exit_code, note, self.run_id,
                    ),
                )
                con.executemany(
                    "INSERT INTO failures (run_id, symbol, error) VALUES (?, ?, ?)",
                    [(self.run_id, s, e) for s, e in failures],
                )
                con.commit()
        except sqlite3.Error as exc:
            log.warning("Run log finish failed: %s", exc)


def recent_runs(data_dir: Path, limit: int = 20) -> list[sqlite3.Row]:
    path = data_dir / "runs.db"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as con:
        con.row_factory = sqlite3.Row
        return con.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def recent_failures(data_dir: Path, limit: int = 20) -> list[sqlite3.Row]:
    path = data_dir / "runs.db"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as con:
        con.row_factory = sqlite3.Row
        return con.execute(
            "SELECT r.trade_date, r.session, f.symbol, f.error"
            " FROM failures f JOIN runs r ON r.id = f.run_id"
            " ORDER BY f.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
