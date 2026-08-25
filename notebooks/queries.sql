-- Saved DuckDB queries worth keeping.
-- Run against the archive in place; no import step, no server.
--
--   duckdb
--   .read notebooks/queries.sql
--
-- Paths are relative to the repo root.


-- A single chain slice: the 40-50 DTE window most premium-selling research
-- lives in.
SELECT expiration_date, strike, option_type, bid, ask, open_interest
FROM 'data/chains/**/*.parquet'
WHERE underlying_symbol = 'SPY'
  AND session = 'pm'
  AND dte BETWEEN 40 AND 50
ORDER BY expiration_date, strike;


-- IV rank history for one symbol. This is the series that tells you whether
-- high-IVR entries actually worked out.
SELECT snapshot_ts_utc, implied_volatility_index_rank
FROM 'data/metrics/**/*.parquet'
WHERE symbol = 'SPY' AND session = 'pm'
ORDER BY 1;


-- Coverage check, and the cheap version of the Phase 3 heatmap. Any date
-- where a symbol is missing or its row count collapsed is a silent hole.
SELECT
    snapshot_ts_utc::DATE     AS trade_date,
    session,
    underlying_symbol,
    count(*)                  AS contracts,
    count(bid)                AS quoted,
    round(100.0 * count(bid) / count(*), 1) AS pct_quoted
FROM 'data/chains/**/*.parquet'
GROUP BY 1, 2, 3
ORDER BY 1 DESC, 3;


-- Days where a symbol is absent entirely. Empty result is the healthy answer.
WITH days AS (
    SELECT DISTINCT snapshot_ts_utc::DATE AS trade_date, session
    FROM 'data/chains/**/*.parquet'
),
symbols AS (
    SELECT DISTINCT underlying_symbol FROM 'data/chains/**/*.parquet'
),
present AS (
    SELECT DISTINCT snapshot_ts_utc::DATE AS trade_date, session, underlying_symbol
    FROM 'data/chains/**/*.parquet'
)
SELECT d.trade_date, d.session, s.underlying_symbol
FROM days d
CROSS JOIN symbols s
LEFT JOIN present p
       ON p.trade_date = d.trade_date
      AND p.session = d.session
      AND p.underlying_symbol = s.underlying_symbol
WHERE p.underlying_symbol IS NULL
ORDER BY 1 DESC, 3;


-- Spread cost by DTE bucket. The slippage published backtests assume away.
-- Note this computes a mid inline rather than reading one: the raw layer
-- deliberately does not store it (section 1).
SELECT
    underlying_symbol,
    CASE
        WHEN dte <= 7   THEN '0-7'
        WHEN dte <= 30  THEN '8-30'
        WHEN dte <= 60  THEN '31-60'
        WHEN dte <= 120 THEN '61-120'
        ELSE '120+'
    END AS dte_bucket,
    count(*) AS contracts,
    round(median((ask - bid) / ((ask + bid) / 2)) * 100, 2) AS median_spread_pct
FROM 'data/chains/**/*.parquet'
WHERE bid > 0 AND ask > 0 AND session = 'pm'
GROUP BY 1, 2
ORDER BY 1, 2;


-- Term structure for one snapshot: front vs back month, straight off the
-- metrics endpoint's per-expiration IV is not archived, so this uses the
-- chain's ATM contracts as a proxy.
SELECT
    expiration_date,
    dte,
    min(abs(strike - underlying_price)) AS atm_distance
FROM 'data/chains/**/*.parquet'
WHERE underlying_symbol = 'SPY'
  AND snapshot_ts_utc::DATE = current_date
  AND session = 'pm'
GROUP BY 1, 2
ORDER BY 2;


-- Archive size and span. Useful for sanity-checking storage growth against
-- the 300-600 MB/year estimate.
SELECT
    min(snapshot_ts_utc)::DATE AS first_day,
    max(snapshot_ts_utc)::DATE AS last_day,
    count(DISTINCT snapshot_ts_utc) AS snapshots,
    count(*) AS total_rows
FROM 'data/chains/**/*.parquet';
