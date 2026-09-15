# Option Chain Archiver

Daily snapshots of option chain state and market metrics from the tastytrade
API, persisted as partitioned Parquet.

Option chain history is expensive to buy and impossible to backfill, so this is
built to be boring and to start running before anything else in the project
exists. It writes files. It does not place orders, manage positions, or analyze
anything.

**Status: Phases 1-4 complete.** Capture, calendar guard, run log, healthcheck,
verify, greeks and the coverage dashboard are all in. Phase 5 is deliberately
not built - it is the "as earned" tier. See
[Where this stops](#where-this-stops) for what is deliberately not built yet.

## Setup

```bash
uv venv --python 3.14
uv pip install -e ".[dev]"
```

Commands below use the Windows interpreter path. On macOS and Linux substitute
`.venv/bin/python` for `.venv/Scripts/python.exe`, or activate the venv and
use the `archiver` console script directly.

Register a tastytrade OAuth application with the **`read` scope only**. It
should be structurally incapable of placing a trade; do not add `trade` scope
"just in case." Then:

```bash
cp .env.example .env
```

Fill in `TT_CLIENT_SECRET` and `TT_REFRESH_TOKEN`. `.env` is gitignored from
the first commit and must never be pasted into a chat window.

## Secrets

`.env` holds the client secret and refresh token. Three things protect it:

1. **Gitignored** from the first commit, so it cannot be staged accidentally.
2. **Locked down** to the owning user. The default ACL on a Windows user folder
   grants `BUILTIN\Users:(M)` - any local account could read and modify it.
   Restrict it with:

   ```
   icacls .env /inheritance:r /grant:r "%USERNAME%:(R,W)"
   ```

3. **A pre-commit hook** in `.githooks/` that blocks staging `.env`, any
   JWT-shaped string, or a populated `TT_` credential. `core.hooksPath` is local
   config and is not cloned, so enable it once per checkout:

   ```
   git config core.hooksPath .githooks
   ```

Scope matters less than it sounds. A `read` token cannot trade, but it can read
every account, balance, position and transaction on the login - tastytrade has
no scope narrower than `read`. Treat the file as account credentials.

Refresh tokens never expire, so a leaked one stays valid until revoked. Revoke
at Manage > My Profile > API > OAuth Applications > Manage > revoke the grant,
then Create Grant for a replacement.

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
| `--force` | Bypass the trading-day and session-time guards. |

Other commands:

```bash
archiver derive --all        # greeks for every partition (backfills history)
archiver verify              # sanity-check written partitions
archiver status              # recent runs, failures, coverage
```

`derive`, `verify` and `status` read only what is on disk, so they run on a
machine that has the archive but no credentials.

Exit codes: `0` success, `1` partial or total failure worth alerting on,
`2` bad credentials or missing configuration.

A run outside its session window, or on a weekend or holiday, logs a skip and
exits **0** - it is a correct outcome, not a failure, and treating it as one
would make every weekend look like an outage. Use `--force` for ad-hoc runs.

## Deployment (macOS)

Two launchd agents, installed by:

```bash
./deploy/install-macos.sh
```

It writes `~/Library/LaunchAgents/com.chainarchiver.{am,pm}.plist`, locks
`.env` to 0600, warns if the system timezone is not `America/New_York`, and
loads both agents. Idempotent - re-run after moving the repo. Remove with
`./deploy/uninstall-macos.sh`.

The agents carry no weekday or holiday logic. launchd fires on a fixed clock
and `chain_archiver.calendar` decides whether that firing should do anything,
so the NYSE calendar lives in exactly one place. The pm agent fires **twice**,
at 12:45 and 15:45: on a normal day the 12:45 firing is out of window and
becomes a no-op, and on an early-close day the 15:45 firing is. Neither the
plist nor the installer needs to know which days are which.

Four things that will silently cost you snapshots. The installer refuses the
first and warns about the rest:

- **Privacy-protected folders.** macOS blocks background jobs from reading
  `~/Desktop`, `~/Documents`, `~/Downloads` and iCloud Drive. A repo there works
  perfectly when run by hand and fails every scheduled run with "Operation not
  permitted". Clone into `~/optionarchive`.
- **A sleeping Mac misses runs.** `sudo pmset -a sleep 0 autorestart 1` - the
  second flag brings it back after a power failure.
- **No auto-login, no runs.** These are LaunchAgents, which only run while a
  user is logged in. After any restart nothing happens until someone logs in,
  so enable automatic login. It is unavailable with FileVault on, in which case
  the healthcheck is what tells you the Mac is sitting at the login screen.
- **launchd uses local time.** The 09:45 / 15:45 targets are only correct if
  the machine is on Eastern.

Logs land in `logs/` (gitignored). Check the agents with:

```bash
launchctl list | grep chainarchiver
```

## Reading the data

The archive is Parquet on disk. DuckDB queries it in place, no import step:

```bash
duckdb -c "SELECT underlying_symbol, count(*) FROM 'data/chains/**/*.parquet' GROUP BY 1"
```

For a browser UI rather than a terminal, DuckDB ships one - no separate app
to build:

```bash
python notebooks/ui.py
```

That registers the partitions as views named `chains` and `metrics` and opens
DuckDB's notebook-style UI, with schema browsing, autocomplete and result
grids. The connection is in-memory and the views read the parquet directly, so
nothing typed into the UI can modify the archive.

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
- **Strikes:** within `strike_pct` of spot, calibrated per symbol to roughly
  2.5 standard deviations at 120 DTE.

A single fixed strike band is not comparable across symbols. Measured against
live IV, ±35% is **5.1 sd on TLT but 0.7 sd on VIX** - so the high-IV names an
IV-rank screener actually surfaces were the most truncated, and VIX was cut off
at strike 20 against a listed range reaching 200.

The calibration only ever widens; 0.35 stays the floor. These were measured in
a low-vol regime (SPY IV 15%), and every band shrinks in sd terms when IV
triples - narrowing now would be precisely the vol-event regret. Over-wide
costs disk, too-narrow costs data permanently.

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
- Index underlyings (SPX, VIX, NDX, RUT) must be requested under `index=`
  rather than `equity=`; set `is_index=True` on the `SymbolSpec`. Each returns
  two roots - an AM-settled monthly (`SPX`) and a PM-settled weekly (`SPXW`) -
  which is why `settlement_type` is a column.
- The risk-free rate is published, unauthenticated, at
  `/margin-requirements-public-configuration`. `fetch.fetch_risk_free_rate()`
  reads it — that resolves the open question about where the rate comes from,
  and it is a better default than a config constant.

Contracts that return no quote are kept with NULL quote columns. The fact that
a contract existed and did not quote is itself data; dropping those rows would
make coverage look better than it was.

## Derived greeks

`archiver derive` reads `chains` and writes `derived/greeks`. Never written by
the fetcher, so a bug in the math is fixed by deleting the derived tree and
re-running - the archive itself is never at risk.

Black-Scholes with continuous dividend yield, IV solved by Brent's method
bracketed to [0.001, 5.0]. `risk_free_rate`, `dividend_yield` and
`model_version` are stored per row, so recomputing history later never
requires guessing what assumptions a given version used. The rate comes from
tastytrade's own published figure.

Roughly 72% of rows solve. The rest are left **NULL rather than filled with a
fabricated number**, because a garbage IV that looks like a number is far more
dangerous downstream than a missing one. Rows are refused when the bid is
zero, `spread_pct > 0.5`, the contract is at expiry, or the price violates its
own arbitrage bounds.

Two known limitations, both deliberate:

- Equity and ETF options are **American**; this prices them as European. The
  error is concentrated in deep ITM puts with early-exercise value. Index
  options (SPX, NDX, RUT) are genuinely European, so they are exact.
- Solved ATM IV runs 1-3 vol points below tastytrade's `implied_volatility_index`.
  That is expected, not an error: their index is a variance-swap-style
  calculation across the whole strike strip, and equity skew lifts it above ATM.

## Monitoring

The failure mode that matters is silent death - the job stops and you notice in
March. Set `HEALTHCHECK_URL` in `.env`; the run pings it on success and
`/fail` on failure, and the service alerts when a ping does not arrive.
Silence is the alarm. Unset, the job runs unmonitored rather than refusing to
run.

`data/runs.db` records every run and every per-symbol failure. It answers the
questions Parquet cannot: which symbol has been failing all week, when wall
time started creeping, which days have no run at all.

```bash
streamlit run dashboard/coverage.py
```

The coverage heatmap is the ops view, and the one page exempt from the
three-times rule - it catches the silent hole where a single symbol fails for
weeks while every run reports success. Only NYSE trading days are shown, so
weekends never read as gaps. Everything else stays in notebooks.

## Backup

```bash
ARCHIVE_REMOTE=b2:my-bucket/optionarchive ./deploy/backup.sh
```

Copy, not sync-with-delete: a local mistake must not propagate to the backup.
The local copy stays authoritative so analysis never pays egress.

## Where this stops

Phase 5 is deliberately unbuilt, per the "as earned" rule:

- **More dashboard pages.** A question earns a page after being asked three
  times. Until then it is a notebook query.
- **The morning screener.** Wants real IV-rank history behind it first.
- **DXLink streaming** for vendor greeks. `streamer_symbol` is stored from day
  one so v2 can subscribe without a migration, and vendor greeks will land in
  `derived/greeks_dxlink/` alongside the computed ones rather than replacing
  them - having both is how you check your model against theirs.

Also not built: `watchlist.yaml`. The universe still lives in `config.py`,
which is version controlled and typed, and has not yet been painful enough to
warrant a config file.
