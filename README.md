# Option Chain Archiver

Daily snapshots of option chain state and market metrics from the tastytrade
API, persisted as partitioned Parquet.

Option chain history is expensive to buy and impossible to backfill, so this is
built to be boring and to start running before anything else in the project
exists. It writes files. It does not place orders, manage positions, or analyze
anything.

**Status: Phase 1.** Capture works and runs manually. See
[Where this stops](#where-this-stops) for what is deliberately not built yet.

## Setup

```bash
uv venv --python 3.14
uv pip install -e ".[dev]"
```

Register a tastytrade OAuth application with the **`read` scope only**. It
should be structurally incapable of placing a trade; do not add `trade` scope
"just in case." Then:

```bash
cp .env.example .env
```

Fill in `TT_CLIENT_SECRET` and `TT_REFRESH_TOKEN`. `.env` is gitignored from
the first commit and must never be pasted into a chat window.

## Running a snapshot

Validate credentials and the full fetch path without writing anything:

```bash
.venv/Scripts/python.exe -m chain_archiver.cli snapshot --session pm --dry-run
```

Capture for real:

```bash
.venv/Scripts/python.exe -m chain_archiver.cli snapshot --session pm
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--session {am,pm}` | Required. Labels the partition and the rows. |
| `--dry-run` | Fetch and validate against the schema, write nothing. |
| `--symbols SPY,QQQ` | Override the watchlist for an ad-hoc run. |
| `--data-dir PATH` | Override `ARCHIVE_DATA_DIR`. |
| `-v` | Debug logging, including retries and token refreshes. |

Exit codes: `0` success, `1` partial or total failure worth alerting on,
`2` bad credentials or missing configuration.

## Reading the data

The archive is Parquet on disk. DuckDB queries it in place, no import step:

```bash
duckdb -c "SELECT underlying_symbol, count(*) FROM 'data/chains/**/*.parquet' GROUP BY 1"
```

Saved queries live in [notebooks/queries.sql](notebooks/queries.sql), including
a coverage check that catches the silent-hole failure mode where one symbol
fails for weeks while runs look healthy.

From Python:

```python
import duckdb
df = duckdb.sql("""
    SELECT snapshot_ts_utc, implied_volatility_index_rank
    FROM 'data/metrics/**/*.parquet'
    WHERE symbol = 'SPY' AND session = 'pm'
    ORDER BY 1
""").df()
```

**Access is read-only.** Notebooks must never write into `data/`. If a notebook
can write to the archive, eventually one will.

## Layout

```
data/
  chains/date=2026-08-24/session=pm/data.parquet     one row per contract
  metrics/date=2026-08-24/session=pm/data.parquet    one row per underlying
```

Partitioned Hive-style, zstd level 3. `date` is the US/Eastern trading date,
not the UTC date.

Writes are atomic: the table goes to `data.parquet.tmp`, is fsynced, then
renamed into place. A killed process cannot leave a half-written partition that
looks valid. Re-running the same date and session overwrites cleanly rather
than appending, so reruns are safe.

The chain layer stores exactly what the API returned. `mid`, greeks, moneyness
and `spread_pct` are all absent by design — they belong to `derived/`, so a bug
in that math can never corrupt the archive.

## Filters

Applied per symbol in `config.py`:

- **Expirations:** everything out to 120 DTE, plus monthlies (`Regular`) out to
  365. The far tail is illiquid and inflates storage for no analytical value.
- **Strikes:** within ±35% of spot.

A symbol whose underlying quote fails is failed outright rather than archived
unfiltered — without spot there is no strike filter, and a giant unfiltered
chain would quietly poison the partition.

## Notes on the API

Verified against the tastytrade REST API, and worth knowing because several of
these are not what you would guess:

- `/market-data/by-type` caps at **100 symbols across all instrument types** per
  request. Quotes are chunked accordingly.
- `implied-volatility-index-rank` and `implied-volatility-percentile` come back
  as **strings**, not numbers. Normalized to `float64` on ingest.
- `implied_volatility_index` is a **decimal** (`0.158`), but
  `historical_volatility_30_day`, `historical_volatility_60_day` and
  `iv_hv_30_day_difference` are **percentage points** (`12.63`). Stored as
  returned. Rescale before comparing: `ivx * 100 - hv30`.
- Earnings fields are **nested** under `earnings`, and the dividend rate is
  named `dividend-rate-per-share`. Both flattened to match the schema.
- The nested chain returns **one item per root**, so adjusted roots (`SPY1`)
  arrive alongside the standard one, each with its own `shares-per-contract`.
  Hence `multiplier` is read per root rather than assumed to be 100.
- Index underlyings (SPX, VIX, NDX) must be requested under `index=` rather
  than `equity=`; set `is_index=True` on the `SymbolSpec`.
- The risk-free rate is published, unauthenticated, at
  `/margin-requirements-public-configuration`. `fetch.fetch_risk_free_rate()`
  reads it — that resolves the open question about where the rate comes from,
  and it is a better default than a config constant.

Contracts that return no quote are kept with NULL quote columns. The fact that
a contract existed and did not quote is itself data; dropping those rows would
make coverage look better than it was.

## Where this stops

Phase 1 is capture only. Deliberately not built yet:

- **Trading-day guard.** There is no calendar check, so a manual run on a
  Saturday will happily write a partition. `pandas_market_calendars` is already
  declared under the `schedule` extra for Phase 2.
- **Run log.** No `runs.db`; per-symbol results print to the log instead.
- **Scheduling**, healthcheck ping, `verify`, backup sync, and the coverage
  heatmap.
- **`derive`.** No greeks, no IV solving. `streamer_symbol` is stored from day
  one so DXLink can subscribe in v2 without a migration.

Retries (3 attempts, exponential backoff with jitter, on 429 and 5xx only),
per-symbol isolation, atomic writes and idempotency are in already, because
they are listed as non-negotiable and each is cheap.
