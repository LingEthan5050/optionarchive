"""Data explorer - the archive itself, not just whether it arrived.

    streamlit run dashboard/coverage.py     # this page appears in the sidebar

The coverage page answers "is anything missing?". This one answers "what
did the market look like?": one symbol at one snapshot, its chain, its smile
and term structure, and how its volatility has moved across every snapshot
so far. It earned its place the README's way - by being asked for.

Read-only, like everything under dashboard/: an in-memory connection over
the parquet files, so nothing on this page can modify the archive.

Three traps this page handles rather than passes on to whoever reads it.
  * Scales. implied_volatility_index, _rank and _percentile are decimals
    (0.158 = 15.8%); historical_volatility_* are already percentage points.
    Everything shown here is converted to percent exactly once, below.
  * earnings_expected_report_date goes stale and can name a report that has
    already happened. A date before the snapshot is shown as unknown rather
    than as the next earnings.
  * SPX lists AM- and PM-settled contracts on the same expiration date, so
    an expiration is identified by date AND settlement, or the chain table
    shows two rows for every strike.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from chain_archiver.config import WATCHLIST  # noqa: E402

st.set_page_config(page_title="Data explorer", layout="wide")

EASTERN = ZoneInfo("America/New_York")

#: Categorical slots 1 and 2 of the reference palette, validated as a pair in
#: both modes (worst CVD dE 24.7 light / 26.8 dark). Dark mode uses its own
#: steps rather than the light hexes on a dark surface.
PALETTE = {
    "light": {"calls": "#2a78d6", "puts": "#eb6834", "ink": "#52514e"},
    "dark": {"calls": "#3987e5", "puts": "#d95926", "ink": "#c3c2b7"},
}


def sql_path(path: Path) -> str:
    """A path as a SQL string literal. The archive path is typed into the
    sidebar, so a quote in it must not end the literal early."""
    return "'" + path.as_posix().replace("'", "''") + "'"


# -- loading ---------------------------------------------------------------


@st.cache_data(ttl=300)
def snapshots(data_dir: str) -> list[tuple[date, str]]:
    """Every (date, session) with a chains partition, newest first."""
    found = []
    for part in Path(data_dir, "chains").glob("date=*/session=*/data.parquet"):
        day = date.fromisoformat(part.parent.parent.name.removeprefix("date="))
        found.append((day, part.parent.name.removeprefix("session=")))
    # am before pm within a day, then newest day first
    return sorted(found, key=lambda s: (s[0], s[1] == "pm"), reverse=True)


@st.cache_data(ttl=300)
def chain(data_dir: str, day: date, session: str, symbol: str) -> pd.DataFrame:
    """One symbol's chain at one snapshot, joined to its derived greeks when
    that partition exists."""
    part = f"date={day.isoformat()}/session={session}/data.parquet"
    chains_file = Path(data_dir, "chains", part)
    greeks_file = Path(data_dir, "derived", "greeks", part)
    con = duckdb.connect()
    if greeks_file.exists():
        query = f"""
            SELECT c.*, g.mid, g.implied_vol, g.delta, g.gamma, g.theta,
                   g.vega, g.moneyness, g.spread_pct
            FROM read_parquet({sql_path(chains_file)}) c
            LEFT JOIN read_parquet({sql_path(greeks_file)}) g USING (occ_symbol)
            WHERE c.underlying_symbol = ?
        """
    else:
        query = f"""
            SELECT *, NULL::DOUBLE AS mid, NULL::DOUBLE AS implied_vol,
                   NULL::DOUBLE AS delta, NULL::DOUBLE AS gamma,
                   NULL::DOUBLE AS theta, NULL::DOUBLE AS vega,
                   NULL::DOUBLE AS moneyness, NULL::DOUBLE AS spread_pct
            FROM read_parquet({sql_path(chains_file)})
            WHERE underlying_symbol = ?
        """
    return con.execute(query, [symbol]).df()


@st.cache_data(ttl=300)
def metrics_at(data_dir: str, day: date, session: str) -> pd.DataFrame:
    part = Path(data_dir, "metrics", f"date={day.isoformat()}",
                f"session={session}", "data.parquet")
    if not part.exists():
        return pd.DataFrame()
    return duckdb.connect().execute(
        f"SELECT * FROM read_parquet({sql_path(part)})"
    ).df()


@st.cache_data(ttl=300)
def history(data_dir: str, symbol: str) -> pd.DataFrame:
    """The symbol's metrics at every snapshot, with spot from the chain."""
    metrics_glob = Path(data_dir, "metrics", "**", "*.parquet")
    chains_glob = Path(data_dir, "chains", "**", "*.parquet")
    return duckdb.connect().execute(f"""
        WITH spot AS (
            SELECT snapshot_ts_utc, any_value(underlying_price) AS spot
            FROM read_parquet({sql_path(chains_glob)})
            WHERE underlying_symbol = ?
            GROUP BY 1
        )
        SELECT m.snapshot_ts_utc, m.session,
               m.implied_volatility_index * 100      AS ivx_pct,
               m.implied_volatility_index_rank * 100 AS ivr_pct,
               m.historical_volatility_30_day        AS hv30_pct,
               s.spot
        FROM read_parquet({sql_path(metrics_glob)}) m
        LEFT JOIN spot s USING (snapshot_ts_utc)
        WHERE m.symbol = ?
        ORDER BY 1
    """, [symbol, symbol]).df()


# -- chart helpers -----------------------------------------------------------


def colours() -> dict[str, str]:
    theme = getattr(st.context, "theme", None)
    return PALETTE["dark" if getattr(theme, "type", None) == "dark" else "light"]


def line_with_hover(
    df: pd.DataFrame,
    x: alt.X,
    y: alt.Y,
    tooltip: list,
    colour: alt.Color | alt.value,
    x_field: str,
) -> alt.LayerChart:
    """A 2px line with a crosshair: hovering anywhere snaps to the nearest x,
    draws a rule there and reveals the points and their tooltip."""
    ink = colours()["ink"]
    nearest = alt.selection_point(
        nearest=True, on="pointerover", fields=[x_field], empty=False,
        clear="pointerout",
    )
    base = alt.Chart(df).encode(x=x, y=y, color=colour)
    line = base.mark_line(strokeWidth=2)
    points = base.mark_point(size=64, filled=True).encode(
        opacity=alt.condition(nearest, alt.value(1), alt.value(0)),
        tooltip=tooltip,
    )
    # Invisible, wider hit targets so hovering doesn't need pixel aim.
    hit = alt.Chart(df).mark_point(size=400, opacity=0).encode(
        x=x, y=y, tooltip=tooltip,
    ).add_params(nearest)
    rule = alt.Chart(df).mark_rule(color=ink, strokeDash=[3, 3]).encode(
        x=x
    ).transform_filter(nearest)
    return alt.layer(line, rule, points, hit)


def show(chart: alt.TopLevelMixin, height: int = 280) -> None:
    st.altair_chart(
        chart.properties(height=height).configure_axis(gridOpacity=0.35),
        width="stretch",
    )


# -- page --------------------------------------------------------------------

st.title("Data explorer")

default_dir = os.environ.get("ARCHIVE_DATA_DIR") or str(REPO / "data")
data_dir = st.sidebar.text_input("Archive path", default_dir)

available = snapshots(data_dir)
if not available:
    st.warning(f"No chain partitions under {data_dir}. Nothing captured yet.")
    st.stop()

symbols = [s.symbol for s in WATCHLIST]
symbol = st.sidebar.selectbox("Symbol", symbols, index=symbols.index("SPY"))
day, session = st.sidebar.selectbox(
    "Snapshot",
    available,
    format_func=lambda s: f"{s[0]:%a %Y-%m-%d} · {s[1]}",
)

df = chain(data_dir, day, session, symbol)
if df.empty:
    st.warning(f"{symbol} has no rows in the {day} {session} snapshot.")
    st.stop()

metrics = metrics_at(data_dir, day, session)
row = metrics[metrics["symbol"] == symbol].iloc[0] if not metrics.empty and (
    metrics["symbol"] == symbol).any() else None

spot = float(df["underlying_price"].dropna().iloc[0])
taken = pd.Timestamp(df["snapshot_ts_utc"].iloc[0]).tz_convert(EASTERN)
st.caption(f"{symbol} · snapshot taken {taken:%a %Y-%m-%d %H:%M} ET · "
           f"{len(df):,} contracts")

# -- headline numbers ----------------------------------------------------------

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Spot", f"{spot:,.2f}")
if row is not None:
    c2.metric("IV index", f"{row['implied_volatility_index'] * 100:.1f}%")
    c3.metric("IV rank", f"{row['implied_volatility_index_rank'] * 100:.0f}%")
    c4.metric("HV 30-day", f"{row['historical_volatility_30_day']:.1f}%")
    earnings = row["earnings_expected_report_date"]
    upcoming = pd.notna(earnings) and pd.Timestamp(earnings).date() >= day
    c5.metric(
        "Next earnings",
        f"{pd.Timestamp(earnings):%b %d}" if upcoming else "—",
        help=None if upcoming else
        "No upcoming date on record. tastytrade sometimes keeps reporting the "
        "last date after it passes, so a past date is not shown as next.",
    )

if df["implied_vol"].isna().all():
    st.info("Greeks haven't been derived for this snapshot yet, so IV and "
            "delta are blank. They're computed a few minutes after each capture.")

# -- expiration and chain -------------------------------------------------------

st.subheader("Option chain")

expiries = (
    df.groupby(["expiration_date", "settlement_type"], dropna=False)["dte"]
    .first().reset_index().sort_values(["expiration_date", "settlement_type"])
)
doubled = expiries["expiration_date"].duplicated(keep=False)
expiries["label"] = [
    f"{e:%Y-%m-%d} · {d} DTE" + (f" · {s}-settled" if dbl else "")
    for e, d, s, dbl in zip(expiries["expiration_date"], expiries["dte"],
                            expiries["settlement_type"], doubled)
]
default = int((expiries["dte"] - 30).abs().argmin())

left, right = st.columns([3, 2])
choice = left.selectbox("Expiration", expiries.index, index=default,
                        format_func=lambda i: expiries.loc[i, "label"])
band = right.slider("Strikes within ± % of spot", 2, 35, 10)

exp = expiries.loc[choice]
leg = df[(df["expiration_date"] == exp["expiration_date"])
         & (df["settlement_type"] == exp["settlement_type"])]
leg = leg[(leg["strike"] - spot).abs() <= spot * band / 100]

side_cols = {"bid": "Bid", "ask": "Ask", "mid": "Mid", "implied_vol": "IV",
             "delta": "Delta", "volume": "Vol", "open_interest": "OI"}


def side(kind: str, prefix: str) -> pd.DataFrame:
    part = leg[leg["option_type"] == kind].set_index("strike")[list(side_cols)]
    part = part.rename(columns={k: f"{prefix} {v}" for k, v in side_cols.items()})
    part[f"{prefix} IV"] = part[f"{prefix} IV"] * 100
    return part


table = side("C", "Call").join(side("P", "Put"), how="outer").sort_index()
table.index.name = "Strike"
table = table.reset_index()

call_cols = [f"Call {v}" for v in side_cols.values()]
put_cols = [f"Put {v}" for v in side_cols.values()]
table = table[list(reversed(call_cols)) + ["Strike"] + put_cols]
atm = int((table["Strike"] - spot).abs().argmin()) if len(table) else None


def mark_atm(r: pd.Series) -> list[str]:
    return ["font-weight: 700; background-color: rgba(128,128,128,0.18)"
            if r.name == atm else "" for _ in r]


fmt = {c: "{:,.2f}" for c in table.columns if c.endswith(("Bid", "Ask", "Mid"))}
fmt |= {c: "{:.1f}%" for c in table.columns if c.endswith("IV")}
fmt |= {c: "{:+.2f}" for c in table.columns if c.endswith("Delta")}
fmt |= {c: "{:,.0f}" for c in table.columns if c.endswith(("Vol", "OI"))}
fmt["Strike"] = "{:,.2f}"

# Streamlit draws a missing number as a grey "None", whatever the Styler's
# na_rep says, which reads like a bug. The chain is always in strike order,
# so formatting to text costs nothing - no one sorts it by clicking.
shown = table.copy()
for col, pattern in fmt.items():
    shown[col] = [pattern.format(v) if pd.notna(v) else "—" for v in table[col]]

st.caption(f"Calls on the left, puts on the right. The bold row is the strike "
           f"closest to spot ({spot:,.2f}). A dash in IV or delta means the "
           f"solver couldn't find one, usually because there was no bid.")
st.dataframe(
    shown.style.apply(mark_atm, axis=1),
    hide_index=True, width="stretch", height=440,
    column_config={c: st.column_config.TextColumn(alignment="right")
                   for c in shown.columns},
)

# -- smile and term structure -----------------------------------------------------

pal = colours()
kind_colour = alt.Color(
    "side:N",
    scale=alt.Scale(domain=["Calls", "Puts"], range=[pal["calls"], pal["puts"]]),
    legend=alt.Legend(title=None, orient="top", direction="horizontal"),
)

smile = leg.dropna(subset=["implied_vol"]).assign(
    side=lambda d: d["option_type"].map({"C": "Calls", "P": "Puts"}),
    iv_pct=lambda d: d["implied_vol"] * 100,
)

g1, g2 = st.columns(2)

with g1:
    st.markdown(f"**Volatility smile** · {exp['label']}")
    if smile.empty:
        st.caption("No solved IVs for this expiration.")
    else:
        body = line_with_hover(
            smile,
            alt.X("strike:Q", title="Strike", scale=alt.Scale(zero=False)),
            alt.Y("iv_pct:Q", title="Implied vol (%)", scale=alt.Scale(zero=False)),
            [alt.Tooltip("side:N", title="Side"),
             alt.Tooltip("strike:Q", title="Strike", format=",.2f"),
             alt.Tooltip("iv_pct:Q", title="IV %", format=".1f"),
             alt.Tooltip("delta:Q", title="Delta", format="+.2f")],
            kind_colour,
            "strike",
        )
        spot_rule = alt.Chart(pd.DataFrame({"spot": [spot]})).mark_rule(
            color=pal["ink"], strokeWidth=1
        ).encode(x="spot:Q")
        spot_label = alt.Chart(pd.DataFrame({"spot": [spot], "t": ["spot"]})).mark_text(
            align="left", dx=4, dy=-6, color=pal["ink"], fontSize=11
        ).encode(x="spot:Q", y=alt.value(0), text="t:N")
        # Direct labels at each line's right end, in ink rather than the
        # series colour - identity comes from the line beside the word.
        ends = smile.loc[smile.groupby("side")["strike"].idxmax()]
        end_labels = alt.Chart(ends).mark_text(
            align="left", dx=6, color=pal["ink"], fontSize=11
        ).encode(x="strike:Q", y="iv_pct:Q", text="side:N")
        show(alt.layer(body, spot_rule, spot_label, end_labels))

with g2:
    st.markdown("**Term structure** · at-the-money IV by expiration")
    solved = df.dropna(subset=["implied_vol"]).assign(
        gap=lambda d: (d["strike"] - spot).abs()
    )
    if solved.empty:
        st.caption("No solved IVs in this snapshot.")
    else:
        # The call and put nearest spot in each expiration, averaged: a cheap
        # straddle IV, steadier than either leg on its own.
        nearest = solved.loc[
            solved.groupby(["expiration_date", "settlement_type", "option_type"])["gap"].idxmin()
        ]
        term = (
            nearest.groupby(["expiration_date", "settlement_type"])
            .agg(dte=("dte", "first"), atm_iv=("implied_vol", "mean"))
            .reset_index()
        )
        term["atm_iv_pct"] = term["atm_iv"] * 100
        term["expiry"] = pd.to_datetime(term["expiration_date"]).dt.strftime("%Y-%m-%d")
        if term["expiration_date"].duplicated(keep=False).any():
            # Two settlements on one date would zig-zag the line; keep the
            # PM-settled series, which carries the weeklies.
            term = term[term["settlement_type"] != "AM"] if (
                term["settlement_type"] == "PM").any() else term
        show(line_with_hover(
            term,
            alt.X("dte:Q", title="Days to expiration"),
            alt.Y("atm_iv_pct:Q", title="ATM implied vol (%)", scale=alt.Scale(zero=False)),
            [alt.Tooltip("expiry:N", title="Expiration"),
             alt.Tooltip("dte:Q", title="DTE"),
             alt.Tooltip("atm_iv_pct:Q", title="ATM IV %", format=".1f")],
            alt.value(pal["calls"]),
            "dte",
        ))

# -- history ------------------------------------------------------------------------

st.subheader(f"{symbol} across every snapshot")
hist = history(data_dir, symbol)
if len(hist) < 2:
    st.caption("History needs at least two snapshots.")
else:
    hist["when"] = pd.to_datetime(hist["snapshot_ts_utc"]).dt.tz_convert(EASTERN)
    hist["label"] = hist["when"].dt.strftime("%a %b %d %H:%M")
    # Snapshots are evenly spaced rather than placed on a calendar axis: a
    # time axis draws a straight line across every weekend and holiday, which
    # reads as the market drifting while it was closed.
    hist["tick"] = hist["when"].dt.strftime("%b %d ") + hist["session"]
    st.caption(f"{len(hist)} snapshots so far. Each measure gets its own chart "
               f"because they're on different scales.")
    h1, h2, h3 = st.columns(3)
    for col, field, title, fmt_ in (
        (h1, "ivx_pct", "IV index (%)", ".1f"),
        (h2, "ivr_pct", "IV rank (%)", ".0f"),
        (h3, "spot", "Spot", ",.2f"),
    ):
        with col:
            st.markdown(f"**{title}**")
            show(line_with_hover(
                hist,
                alt.X("tick:O", title=None, sort=None,
                      axis=alt.Axis(labelAngle=-45, labelOverlap=True)),
                alt.Y(f"{field}:Q", title=None, scale=alt.Scale(zero=False)),
                [alt.Tooltip("label:N", title="Snapshot"),
                 alt.Tooltip(f"{field}:Q", title=title, format=fmt_)],
                alt.value(pal["calls"]),
                "tick",
            ), height=220)

# -- every symbol at this snapshot ------------------------------------------------

st.subheader("All symbols at this snapshot")
if metrics.empty:
    st.caption("No metrics partition for this snapshot.")
else:
    board = pd.DataFrame({
        "Symbol": metrics["symbol"],
        "IV index %": metrics["implied_volatility_index"] * 100,
        "IV rank %": metrics["implied_volatility_index_rank"] * 100,
        "IV pctile %": metrics["implied_volatility_percentile"] * 100,
        "HV30 %": metrics["historical_volatility_30_day"],
        # Both sides in percentage points before subtracting.
        "IV − HV30": metrics["implied_volatility_index"] * 100
                      - metrics["historical_volatility_30_day"],
        "Earnings": [
            f"{pd.Timestamp(e):%Y-%m-%d}"
            if pd.notna(e) and pd.Timestamp(e).date() >= day else "—"
            for e in metrics["earnings_expected_report_date"]
        ],
        "Beta": metrics["beta"],
    }).sort_values("IV rank %", ascending=False)
    st.caption("Click a column header to sort. Earnings shows a dash where no "
               "upcoming date is on record.")
    # Numeric columns stay numeric here, unlike the chain, because this table
    # exists to be sorted by clicking a header.
    num = st.column_config.NumberColumn
    st.dataframe(
        board, hide_index=True, width="stretch", height=420,
        column_config={
            "IV index %": num(format="%.1f"), "IV rank %": num(format="%.0f"),
            "IV pctile %": num(format="%.0f"), "HV30 %": num(format="%.1f"),
            "IV − HV30": num(format="%+.1f"), "Beta": num(format="%.2f"),
            "Earnings": st.column_config.TextColumn(alignment="right"),
        },
    )
