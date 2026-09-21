"""Look things up in the archive the bot runs next to.

The archiver has captured /market-metrics for its watchlist twice a day since
it started, which is exactly the history the API will not give back: what IV
rank was on the day you opened a trade. tastytrade's premium-selling approach
leans on selling when IV rank is high, so "IVR now vs at entry" is the
simplest check of whether an entry followed that.

Coverage is whatever the archive holds - its watchlist symbols, from its
first capture - so a lookup can come back empty, and the caller says so
rather than guessing.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import pyarrow.parquet as pq

#: How many calendar days before the entry date to fall back through, so an
#: entry on a day the archive missed (or a weekend fill) still finds the most
#: recent earlier reading.
LOOKBACK_DAYS = 5


@lru_cache(maxsize=512)
def _read(path: str, symbol: str) -> float | None:
    table = pq.read_table(path, columns=["symbol", "implied_volatility_index_rank"],
                          filters=[("symbol", "=", symbol)])
    values = [v for v in table.column("implied_volatility_index_rank").to_pylist()
              if v is not None]
    return values[0] if values else None


def ivr_on(archive: Path, symbol: str, day: date) -> tuple[float, date] | None:
    """IV rank (0-1) for `symbol` as of `day`, and the date it was read on.

    On the entry day itself the morning capture comes first - closest to
    most fills - and on earlier days the afternoon one, the latest reading
    that was actually available before the trade.
    """
    for back in range(LOOKBACK_DAYS + 1):
        when = day - timedelta(days=back)
        sessions = ("am", "pm") if back == 0 else ("pm", "am")
        for session in sessions:
            part = archive / "metrics" / f"date={when.isoformat()}" / f"session={session}" / "data.parquet"
            if part.exists():
                value = _read(str(part), symbol)
                if value is not None:
                    return value, when
    return None
