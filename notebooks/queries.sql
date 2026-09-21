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


-- ======================================================================
-- Earnings IV crush
-- ======================================================================
-- The archive's two daily snapshots straddle an earnings report almost
-- exactly: 15:45 catches the last pre-event surface, 09:45 the first
-- post-event one, with nothing in between to confound the move.
--
-- Three properties of the earnings fields, all verified against live data
-- on 2026-09-15, shape everything below.
--
-- 1. earnings_time_of_day is usually NULL - 3 of 34 symbols had it, and no
--    row carried 'BMO' at all. So the event's position within a day is
--    normally unknown and the window has to bracket both cases: pre = the
--    last pm snapshot on or before D-1, post = the first am snapshot on or
--    after D+1. That costs one extra session of drift versus a tight AMC
--    window, so window_kind below reports which one was used. Do not pool
--    tight and wide events without checking that column.
--
-- 2. earnings_expected_report_date goes stale. 5 of 19 populated dates were
--    already in the past at capture time, so the field is not reliably the
--    NEXT report. Every query here therefore keeps a date only from
--    snapshots taken on or before it, which is also the honest thing to do
--    for anything predictive: it uses the date as it was believed then.
--
-- 3. implied_volatility_index is a DECIMAL (0.158 = 15.8%) while the
--    historical_volatility_* columns are PERCENTAGE POINTS - the trap in
--    schema.py section 4.2. Less obviously, _index_rank and _percentile are
--    ALSO decimals, 0-1, not 0-100. The *100 below is deliberate.
--
-- Snapshots are resolved by "<= / >= a date" rather than by exact match so
-- that a holiday or a weekend between the window edge and the event does
-- not silently drop the event.


-- E1. Inventory: which earnings events can actually be measured yet.
-- Run this first. Until an event has a snapshot on both sides it is not
-- analyzable, and early on that is every event.
WITH events AS (
    SELECT symbol,
           earnings_expected_report_date AS edate,
           max(earnings_time_of_day)     AS tod
    FROM 'data/metrics/**/*.parquet'
    WHERE earnings_expected_report_date IS NOT NULL
      AND earnings_expected_report_date >= snapshot_ts_utc::DATE
    GROUP BY 1, 2
),
windows AS (
    SELECT symbol, edate, tod,
           CASE WHEN tod = 'AMC' THEN edate ELSE edate - INTERVAL 1 DAY END::DATE AS pre_on_or_before,
           CASE WHEN tod = 'BMO' THEN edate ELSE edate + INTERVAL 1 DAY END::DATE AS post_on_or_after,
           CASE WHEN tod IS NULL THEN 'wide (time of day unknown)'
                ELSE 'tight (' || tod || ')' END AS window_kind
    FROM events
)
SELECT * FROM windows ORDER BY edate, symbol;


-- E2. The crush, measured on the 30-day constant-maturity IV index.
-- Works with the metrics layer alone - no derived greeks needed - so this
-- is the one that becomes useful first.
WITH events AS (
    SELECT symbol,
           earnings_expected_report_date AS edate,
           max(earnings_time_of_day)     AS tod
    FROM 'data/metrics/**/*.parquet'
    WHERE earnings_expected_report_date IS NOT NULL
      AND earnings_expected_report_date >= snapshot_ts_utc::DATE
    GROUP BY 1, 2
),
windows AS (
    SELECT symbol, edate, tod,
           CASE WHEN tod = 'AMC' THEN edate ELSE edate - INTERVAL 1 DAY END::DATE AS pre_on_or_before,
           CASE WHEN tod = 'BMO' THEN edate ELSE edate + INTERVAL 1 DAY END::DATE AS post_on_or_after,
           CASE WHEN tod IS NULL THEN 'wide' ELSE 'tight' END AS window_kind
    FROM events
),
pre AS (
    SELECT w.symbol, w.edate, max(m.snapshot_ts_utc) AS pre_ts
    FROM windows w
    JOIN 'data/metrics/**/*.parquet' m
      ON m.symbol = w.symbol
     AND m.session = 'pm'
     AND m.snapshot_ts_utc::DATE <= w.pre_on_or_before
    GROUP BY 1, 2
),
post AS (
    SELECT w.symbol, w.edate, min(m.snapshot_ts_utc) AS post_ts
    FROM windows w
    JOIN 'data/metrics/**/*.parquet' m
      ON m.symbol = w.symbol
     AND m.session = 'am'
     AND m.snapshot_ts_utc::DATE >= w.post_on_or_after
    GROUP BY 1, 2
)
SELECT
    w.symbol,
    w.edate,
    w.window_kind,
    pre.pre_ts,
    post.post_ts,
    round(a.implied_volatility_index * 100, 2)                              AS ivx_pre_pct,
    round(b.implied_volatility_index * 100, 2)                              AS ivx_post_pct,
    round((b.implied_volatility_index - a.implied_volatility_index) * 100, 2) AS crush_vol_pts,
    round(100.0 * (b.implied_volatility_index / nullif(a.implied_volatility_index, 0) - 1), 1) AS crush_pct,
    round(a.implied_volatility_index_rank * 100, 1)                         AS ivr_pre_pct
FROM windows w
JOIN pre  ON pre.symbol  = w.symbol AND pre.edate  = w.edate
JOIN post ON post.symbol = w.symbol AND post.edate = w.edate
JOIN 'data/metrics/**/*.parquet' a ON a.symbol = w.symbol AND a.snapshot_ts_utc = pre.pre_ts
JOIN 'data/metrics/**/*.parquet' b ON b.symbol = w.symbol AND b.snapshot_ts_utc = post.post_ts
ORDER BY w.edate DESC, w.symbol;


-- E3. The crush measured where it is largest: the at-the-money contracts of
-- the first expiration that contains the event. The 30-day index in E2 is a
-- blend across expirations and badly understates this - front-week ATM IV
-- routinely halves overnight while the index moves a few points.
--
-- Two things worth knowing before trusting a row.
--  * The derived layer solves IV for roughly 75% of contracts (74.8% on
--    2026-09-15). Deep OTM strikes with no bid never solve, so the
--    implied_vol IS NOT NULL filter is load-bearing: without it the ATM
--    pick can land on an unsolved contract and return NULL.
--  * The expiration is pinned from the PRE snapshot and reused for POST, so
--    this compares one expiration with itself. Letting each side choose its
--    own nearest expiry silently compares different contracts across the
--    event, which is the classic way this analysis goes wrong.
--
-- Requires the derived layer: run `archiver derive --date <D>` first, or the
-- glob matches nothing and DuckDB raises an IO error.
WITH events AS (
    SELECT symbol,
           earnings_expected_report_date AS edate,
           max(earnings_time_of_day)     AS tod
    FROM 'data/metrics/**/*.parquet'
    WHERE earnings_expected_report_date IS NOT NULL
      AND earnings_expected_report_date >= snapshot_ts_utc::DATE
    GROUP BY 1, 2
),
windows AS (
    SELECT symbol, edate, tod,
           CASE WHEN tod = 'AMC' THEN edate ELSE edate - INTERVAL 1 DAY END::DATE AS pre_on_or_before,
           CASE WHEN tod = 'BMO' THEN edate ELSE edate + INTERVAL 1 DAY END::DATE AS post_on_or_after
    FROM events
),
pre AS (
    SELECT w.symbol, w.edate, max(m.snapshot_ts_utc) AS ts
    FROM windows w
    JOIN 'data/metrics/**/*.parquet' m
      ON m.symbol = w.symbol AND m.session = 'pm'
     AND m.snapshot_ts_utc::DATE <= w.pre_on_or_before
    GROUP BY 1, 2
),
post AS (
    SELECT w.symbol, w.edate, min(m.snapshot_ts_utc) AS ts
    FROM windows w
    JOIN 'data/metrics/**/*.parquet' m
      ON m.symbol = w.symbol AND m.session = 'am'
     AND m.snapshot_ts_utc::DATE >= w.post_on_or_after
    GROUP BY 1, 2
),
-- The expiration that contains the event, chosen once, from the pre side.
front AS (
    SELECT p.symbol, p.edate, min(g.expiration_date) AS expiry
    FROM pre p
    JOIN 'data/derived/greeks/**/*.parquet' g
      ON g.underlying_symbol = p.symbol
     AND g.snapshot_ts_utc   = p.ts
     AND g.expiration_date  >= p.edate
     AND g.implied_vol IS NOT NULL
    GROUP BY 1, 2
),
legs AS (
    SELECT symbol, edate, 'pre'  AS leg, ts FROM pre
    UNION ALL
    SELECT symbol, edate, 'post' AS leg, ts FROM post
),
-- moneyness is strike/spot (derive.py), so ATM is nearest 1.0. One contract
-- per side per option type, then averaged: the call/put pair is a cheap
-- straddle IV that is less sensitive to a single bad quote than either leg.
atm AS (
    SELECT l.symbol, l.edate, l.leg, f.expiry, g.option_type, g.implied_vol, g.dte
    FROM legs l
    JOIN front f ON f.symbol = l.symbol AND f.edate = l.edate
    JOIN 'data/derived/greeks/**/*.parquet' g
      ON g.underlying_symbol = l.symbol
     AND g.snapshot_ts_utc   = l.ts
     AND g.expiration_date   = f.expiry
     AND g.implied_vol IS NOT NULL
    QUALIFY row_number() OVER (
        PARTITION BY l.symbol, l.edate, l.leg, g.option_type
        ORDER BY abs(g.moneyness - 1)
    ) = 1
),
straddle AS (
    SELECT symbol, edate, leg, expiry,
           avg(implied_vol) AS atm_iv,
           count(*)         AS legs_used,
           min(dte)         AS dte
    FROM atm
    GROUP BY 1, 2, 3, 4
)
SELECT
    a.symbol,
    a.edate,
    a.expiry,
    a.dte                                                       AS dte_at_pre,
    a.legs_used                                                 AS pre_legs,
    b.legs_used                                                 AS post_legs,
    round(a.atm_iv * 100, 2)                                    AS atm_iv_pre_pct,
    round(b.atm_iv * 100, 2)                                    AS atm_iv_post_pct,
    round((b.atm_iv - a.atm_iv) * 100, 2)                       AS crush_vol_pts,
    round(100.0 * (b.atm_iv / nullif(a.atm_iv, 0) - 1), 1)      AS crush_pct
FROM straddle a
JOIN straddle b
  ON b.symbol = a.symbol AND b.edate = a.edate AND b.expiry = a.expiry
WHERE a.leg = 'pre' AND b.leg = 'post'
ORDER BY a.edate DESC, a.symbol;
