"""Partition sanity checks.

Run in the same job right after writing, so a bad partition is caught while
you still remember what happened, not sixty days later in a notebook.

The checks are deliberately cheap and specific. Each one corresponds to a way
the pipeline has a plausible route to being wrong: a filter that silently
matched nothing, a chain endpoint returning the same root twice, a crossed
market, a spot price read from the wrong instrument.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq

from chain_archiver.writer import partition_dir

log = logging.getLogger(__name__)

#: A spot price that moved more than this against the previous session is
#: more likely a bad read than a real move, and is worth a human look.
MAX_SPOT_MOVE = 0.20

#: Below this share of the expected symbols, the partition is incomplete
#: enough to be misleading.
MIN_SYMBOL_COVERAGE = 0.80

#: Sizeless crossed quotes are normal on expiring contracts. Above this share
#: of the partition they are not, and something upstream has degraded.
MAX_CROSSED_SHARE = 0.001


@dataclass
class VerifyResult:
    day: date
    session: str
    ok: bool = True
    rows: int = 0
    symbols: int = 0
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.problems.append(message)


def _previous_spots(data_dir: Path, day: date, session: str) -> dict[str, float]:
    """Spot prices from the most recent earlier partition, for drift checks."""
    root = data_dir / "chains"
    if not root.exists():
        return {}
    earlier = sorted(
        p for p in root.glob("date=*/session=*/data.parquet")
        if p.parent.parent.name < f"date={day.isoformat()}"
    )
    if not earlier:
        return {}
    table = pq.read_table(
        earlier[-1], columns=["underlying_symbol", "underlying_price"]
    )
    return {
        row["underlying_symbol"]: row["underlying_price"]
        for row in table.to_pylist()
        if row["underlying_price"] is not None
    }


def verify_partition(
    data_dir: Path, day: date, session: str, expected_symbols: int | None = None
) -> VerifyResult:
    result = VerifyResult(day=day, session=session)

    path = partition_dir(data_dir, "chains", day, session) / "data.parquet"
    if not path.exists():
        result.fail(f"no chains partition at {path}")
        return result

    table = pq.read_table(path)
    result.rows = table.num_rows

    if result.rows == 0:
        result.fail("partition has zero rows")
        return result

    rows = table.to_pylist()

    # 1. Duplicate contracts. A repeated occ_symbol means a root was walked
    #    twice, and every downstream aggregate would silently double-count.
    occ = [r["occ_symbol"] for r in rows]
    duplicates = len(occ) - len(set(occ))
    if duplicates:
        result.fail(f"{duplicates} duplicate occ_symbol rows")

    # 2. Crossed markets, split by whether anyone is actually there.
    #
    #    A crossed quote with size on both sides is a genuine anomaly. A
    #    crossed quote with no size is usually a dead contract on expiry day
    #    carrying a stale residual ask, which is ordinary market microstructure
    #    and not worth failing a 126,000-row partition over. Failing on those
    #    would make the exit code cry wolf and ping /fail on good captures.
    crossed_live = crossed_stale = 0
    for r in rows:
        if r["bid"] is None or r["ask"] is None or r["bid"] <= r["ask"]:
            continue
        if (r["bid_size"] or 0) > 0 and (r["ask_size"] or 0) > 0:
            crossed_live += 1
        else:
            crossed_stale += 1

    if crossed_live:
        result.fail(f"{crossed_live} rows crossed with size on both sides")
    if crossed_stale:
        share = crossed_stale / result.rows
        message = f"{crossed_stale} crossed rows with no size ({share:.3%})"
        if share > MAX_CROSSED_SHARE:
            result.fail(message + " - above tolerance")
        else:
            result.notes.append(message)

    # 3. Symbol coverage.
    symbols = {r["underlying_symbol"] for r in rows}
    result.symbols = len(symbols)
    if expected_symbols:
        coverage = len(symbols) / expected_symbols
        if coverage < MIN_SYMBOL_COVERAGE:
            result.fail(
                f"only {len(symbols)}/{expected_symbols} symbols present "
                f"({coverage:.0%})"
            )

    # 4. Spot drift against the previous session.
    previous = _previous_spots(data_dir, day, session)
    current = {
        r["underlying_symbol"]: r["underlying_price"]
        for r in rows if r["underlying_price"] is not None
    }
    for symbol, spot in sorted(current.items()):
        before = previous.get(symbol)
        if not before or before <= 0:
            continue
        move = abs(spot - before) / before
        if move > MAX_SPOT_MOVE:
            result.fail(
                f"{symbol} spot moved {move:.0%} "
                f"({before:.2f} -> {spot:.2f}) since the previous partition"
            )

    # 5. Quote coverage - informational. Some contracts legitimately do not
    #    quote, but a collapse in coverage means the quote endpoint degraded.
    quoted = sum(1 for r in rows if r["bid"] is not None)
    share = quoted / result.rows
    result.notes.append(f"{quoted:,}/{result.rows:,} contracts quoted ({share:.1%})")
    if share < 0.5:
        result.fail(f"only {share:.1%} of contracts have quotes")

    metrics_path = partition_dir(data_dir, "metrics", day, session) / "data.parquet"
    if not metrics_path.exists():
        result.notes.append("no metrics partition")
    else:
        result.notes.append(
            f"{pq.read_table(metrics_path).num_rows} metric rows"
        )

    return result
