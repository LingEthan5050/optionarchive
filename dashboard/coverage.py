"""Coverage heatmap - the ops view.

    streamlit run dashboard/coverage.py

This is the one dashboard page that does not have to earn its place by being
asked for three times, because it answers a question SQL answers badly: is
anything missing? The failure mode it exists to catch is the silent hole -
a single symbol failing for weeks while every run reports success and the
row counts look broadly fine.

Everything else stays in notebooks. Do not add a page here to answer
something a SELECT already answers.

Read-only: an in-memory connection over the parquet files. Nothing this page
does can modify the archive.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from chain_archiver import calendar as trading_calendar  # noqa: E402
from chain_archiver.config import WATCHLIST  # noqa: E402

st.set_page_config(page_title="Archive coverage", layout="wide")

#: A partition holding less than this share of the median row count is
#: present but suspect - a symbol probably failed inside a successful run.
THIN_PARTITION = 0.60


@st.cache_data(ttl=300)
def load(data_dir: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    root = Path(data_dir)
    glob = (root / "chains" / "**" / "*.parquet").as_posix()
    if not any((root / "chains").rglob("*.parquet")):
        return pd.DataFrame(), pd.DataFrame()

    con = duckdb.connect()
    per_partition = con.sql(f"""
        SELECT snapshot_ts_utc::DATE AS trade_date,
               session,
               count(*) AS rows,
               count(DISTINCT underlying_symbol) AS symbols,
               count(bid) * 1.0 / count(*) AS quoted_share
        FROM read_parquet('{glob}')
        GROUP BY 1, 2
    """).df()
    per_symbol = con.sql(f"""
        SELECT snapshot_ts_utc::DATE AS trade_date,
               session,
               underlying_symbol AS symbol,
               count(*) AS rows
        FROM read_parquet('{glob}')
        GROUP BY 1, 2, 3
    """).df()

    # DuckDB hands back a datetime64 column, so trade_date arrives as
    # pandas.Timestamp. Those never compare equal to datetime.date, which
    # would silently mark every captured day as missing - the ops view
    # crying wolf about the exact thing it exists to detect.
    for frame in (per_partition, per_symbol):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date

    return per_partition, per_symbol


def trading_days(start: date, end: date) -> list[date]:
    days, cursor = [], start
    while cursor <= end:
        if trading_calendar.is_trading_day(cursor):
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


st.title("Archive coverage")

default_dir = os.environ.get("ARCHIVE_DATA_DIR") or str(REPO / "data")
data_dir = st.sidebar.text_input("Archive path", default_dir)
lookback = st.sidebar.slider("Days back", 7, 180, 45)

partitions, symbols = load(data_dir)

if partitions.empty:
    st.warning(f"No chain partitions under {data_dir}. Nothing captured yet.")
    st.stop()

end = date.today()
start = end - timedelta(days=lookback)
expected = trading_days(start, end)
median_rows = partitions["rows"].median()

# -- the grid ------------------------------------------------------------
# Every expected trading day appears whether or not it was captured, which
# is the entire point: a missing day has no row of its own to show up in.

records = []
seen = {(r.trade_date, r.session): r for r in partitions.itertuples()}
for day in expected:
    for session in ("am", "pm"):
        hit = seen.get((day, session))
        if hit is None:
            state = "missing"
            rows = 0
        elif hit.rows < median_rows * THIN_PARTITION:
            state = "thin"
            rows = hit.rows
        else:
            state = "ok"
            rows = hit.rows
        records.append(
            {"date": day, "session": session, "state": state, "rows": rows}
        )

grid = pd.DataFrame(records)

captured = (grid["state"] != "missing").sum()
total = len(grid)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Expected snapshots", total)
c2.metric("Captured", captured, delta=f"{captured / total:.0%}")
c3.metric("Missing", int((grid["state"] == "missing").sum()))
c4.metric("Thin", int((grid["state"] == "thin").sum()))

st.subheader("Snapshot coverage")
st.caption(
    "Only NYSE trading days are shown - weekends and holidays are not gaps. "
    f"'Thin' means under {THIN_PARTITION:.0%} of the median row count, which "
    "is how a symbol failing inside an otherwise successful run looks."
)

pivot = grid.pivot(index="session", columns="date", values="rows")
styles = grid.pivot(index="session", columns="date", values="state")


def paint(_: object) -> pd.DataFrame:
    colours = {
        "missing": "background-color: #b3261e; color: white",
        "thin": "background-color: #e0a300; color: black",
        "ok": "background-color: #1f7a3f; color: white",
    }
    return styles.map(lambda s: colours.get(s, ""))


st.dataframe(pivot.style.apply(paint, axis=None).format("{:,.0f}"), height=140)

# -- per symbol ----------------------------------------------------------
# The grid above stays green when one symbol quietly drops out, because the
# other thirty-three keep the row count healthy. This is where that shows.

st.subheader("Per-symbol coverage")

watch = [s.symbol for s in WATCHLIST]
snapshots = symbols.groupby(["trade_date", "session"]).ngroups
present = symbols.groupby("symbol")[["trade_date"]].count().rename(
    columns={"trade_date": "snapshots"}
)
per_symbol = pd.DataFrame({"symbol": watch}).merge(
    present, left_on="symbol", right_index=True, how="left"
).fillna({"snapshots": 0})
per_symbol["coverage"] = per_symbol["snapshots"] / max(snapshots, 1)
per_symbol = per_symbol.sort_values("coverage")

incomplete = per_symbol[per_symbol["coverage"] < 1.0]
if incomplete.empty:
    st.success(f"All {len(watch)} symbols present in every one of the "
               f"{snapshots} snapshots.")
else:
    st.error(f"{len(incomplete)} symbol(s) missing from some snapshots.")

st.dataframe(
    per_symbol.style.format({"snapshots": "{:,.0f}", "coverage": "{:.0%}"}),
    height=400,
)

with st.expander("Recent partitions"):
    st.dataframe(
        partitions.sort_values(["trade_date", "session"], ascending=False)
        .style.format({"rows": "{:,.0f}", "quoted_share": "{:.1%}"})
    )
