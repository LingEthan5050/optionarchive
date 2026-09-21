"""One snapshot of everything the bot knows, fetched in a single session.

Positions come from account.py; this adds the market side - live mids for
every leg and share position, the underlying's price, its /market-metrics
row (beta, IV rank, dividend, earnings), SPY for beta-weighting, and the
risk-free rate - so the summary, /positions and the alert checks all look at
the same numbers taken at the same moment rather than each fetching its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from account_bot import account
from chain_archiver import derive as greeks_math
from chain_archiver.auth import TastytradeClient
from chain_archiver.fetch import fetch_metrics, fetch_option_quotes, fetch_risk_free_rate

#: Underlyings that quote as an index rather than a stock, so their level is
#: requested with the `index` parameter. Their options quote like any other.
INDEXES = {"SPX", "NDX", "RUT", "VIX", "XSP", "DJX"}

BENCHMARK = "SPY"


@dataclass
class Snapshot:
    today: date
    positions: list = field(default_factory=list)
    #: Live mid for every option leg and share position, by symbol.
    mids: dict[str, float] = field(default_factory=dict)
    #: Live price of every underlying held, plus the benchmark.
    spots: dict[str, float] = field(default_factory=dict)
    #: Raw /market-metrics item per underlying (and the benchmark).
    metrics: dict[str, dict] = field(default_factory=dict)
    rate: float = greeks_math.DEFAULT_RISK_FREE_RATE
    balances: list = field(default_factory=list)
    prior_net_liq: dict[str, float] = field(default_factory=dict)
    #: The archiver's data directory, for history lookups (IV rank at entry).
    archive: Path | None = None

    def metric(self, symbol: str, key: str) -> float | None:
        value = self.metrics.get(symbol, {}).get(key)
        try:
            return float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None


def _mid(quote: dict) -> float | None:
    try:
        if quote.get("mid") not in (None, ""):
            return float(quote["mid"])
        return (float(quote["bid"]) + float(quote["ask"])) / 2
    except (KeyError, TypeError, ValueError):
        # An index level has no two-sided market; take its last price.
        try:
            return float(quote["last"])
        except (KeyError, TypeError, ValueError):
            return None


def _quote_many(client: TastytradeClient, kind: str, symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    data = client.get("/market-data/by-type", params={kind: ",".join(symbols)})
    found = {}
    for item in data.get("items") or []:
        price = _mid(item)
        if item.get("symbol") and price is not None:
            found[item["symbol"]] = price
    return found


def gather(client: TastytradeClient, *, balances: bool = False,
           prior_day: date | None = None) -> Snapshot:
    today = account.today_eastern()
    accts = account.accounts(client)
    held = account.positions(client, accts)
    snap = Snapshot(today=today, positions=held)

    options = [p.symbol for p in held if p.is_option]
    for symbol, quote in (fetch_option_quotes(client, options) if options else {}).items():
        price = _mid(quote)
        if price is not None:
            snap.mids[symbol] = price

    underlyings = {p.underlying for p in held} | {BENCHMARK}
    stocks = sorted(u for u in underlyings if u not in INDEXES)
    snap.spots.update(_quote_many(client, "equity", stocks))
    snap.spots.update(_quote_many(client, "index", sorted(underlyings & INDEXES)))
    # A share position's mid is its underlying's price.
    for p in held:
        if not p.is_option and p.symbol in snap.spots:
            snap.mids[p.symbol] = snap.spots[p.symbol]

    snap.metrics = {item["symbol"]: item
                    for item in fetch_metrics(client, sorted(underlyings))
                    if item.get("symbol")}
    rate = fetch_risk_free_rate(client)
    if rate is not None:
        snap.rate = rate

    if balances:
        snap.balances = account.balances(client, accts)
    if prior_day is not None:
        snap.prior_net_liq = account.prior_net_liq(client, accts, prior_day)
    return snap
