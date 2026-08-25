"""PyArrow schemas — the single source of truth for what lands on disk.

Nothing computed lives here. mid, greeks, moneyness and spread_pct are the
derived layer's business (§1, §4.3), so a bug in that math can never corrupt
the archive.

A row that does not fit these types is a hard failure, not something to
coerce quietly: the whole point of the raw layer is that it is exactly what
the API returned.
"""

from __future__ import annotations

import pyarrow as pa

TIMESTAMP = pa.timestamp("us", tz="UTC")

#: One row per contract per snapshot (§4.1).
CHAINS_SCHEMA = pa.schema(
    [
        pa.field("snapshot_ts_utc", TIMESTAMP, nullable=False),
        pa.field("session", pa.string(), nullable=False),
        pa.field("underlying_symbol", pa.string(), nullable=False),
        pa.field("underlying_price", pa.float64()),
        pa.field("occ_symbol", pa.string(), nullable=False),
        pa.field("streamer_symbol", pa.string()),
        pa.field("expiration_date", pa.date32(), nullable=False),
        pa.field("dte", pa.int16()),
        pa.field("strike", pa.float64(), nullable=False),
        pa.field("option_type", pa.string(), nullable=False),
        pa.field("bid", pa.float64()),
        pa.field("ask", pa.float64()),
        pa.field("bid_size", pa.int32()),
        pa.field("ask_size", pa.int32()),
        pa.field("last", pa.float64()),
        pa.field("volume", pa.int64()),
        pa.field("open_interest", pa.int64()),
        pa.field("multiplier", pa.int32()),
        pa.field("expiration_type", pa.string()),
        pa.field("settlement_type", pa.string()),
        pa.field("is_index_option", pa.bool_()),
    ]
)

#: One row per underlying per snapshot (§4.2).
METRICS_SCHEMA = pa.schema(
    [
        pa.field("snapshot_ts_utc", TIMESTAMP, nullable=False),
        pa.field("session", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("implied_volatility_index", pa.float64()),
        pa.field("implied_volatility_index_rank", pa.float64()),
        pa.field("implied_volatility_percentile", pa.float64()),
        pa.field("implied_volatility_index_5_day_change", pa.float64()),
        pa.field("historical_volatility_30_day", pa.float64()),
        pa.field("historical_volatility_60_day", pa.float64()),
        pa.field("iv_hv_30_day_difference", pa.float64()),
        pa.field("liquidity_rating", pa.int32()),
        pa.field("liquidity_rank", pa.float64()),
        pa.field("beta", pa.float64()),
        pa.field("corr_spy_3month", pa.float64()),
        pa.field("earnings_expected_report_date", pa.date32()),
        pa.field("earnings_time_of_day", pa.string()),
        pa.field("dividend_next_date", pa.date32()),
        pa.field("dividend_rate", pa.float64()),
    ]
)

SCHEMAS = {"chains": CHAINS_SCHEMA, "metrics": METRICS_SCHEMA}


def build_table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    """Build a table against a fixed schema.

    Raises pyarrow.ArrowInvalid / ArrowTypeError on any mismatch, which the
    caller must let propagate — see the module docstring.
    """
    return pa.Table.from_pylist(rows, schema=schema)
