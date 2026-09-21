"""Earnings warnings for what you actually hold.

An earnings report is the one scheduled event that can move a stock more
overnight than in weeks of normal trading, and it moves implied volatility
too: IV climbs into the report and usually drops sharply after it (the "IV
crush"). Neither the DTE nor the 50%-profit rule can see it coming, so this
warns separately:

  * an option trade whose underlying reports BEFORE the trade expires - as
    soon as that is known, then again on the last session before the report;
  * a stock position whose company reports within STOCK_WINDOW days.

Dates come live from /market-metrics (via market.Snapshot), which covers
any symbol, not just the archiver's watchlist. Two traps in that data, both
checked against live responses:

  * expected-report-date goes stale: after a report it can keep showing the
    date that just passed. A date before today is treated as unknown.
  * time-of-day (BMO/AMC) is usually missing. When it is, the report is
    assumed to come BEFORE the open - the earliest it could hit - so a
    warning is never a session late.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from account_bot.account import Position
from account_bot.rules import Alert, Trade
from chain_archiver import calendar as trading_calendar

#: How far ahead a stock position's earnings gets a heads-up, in days.
STOCK_WINDOW = 7


@dataclass(frozen=True)
class Earnings:
    symbol: str
    date: date
    #: "BMO" (before market open), "AMC" (after market close), or None.
    when: str | None

    @property
    def timing(self) -> str:
        return {"BMO": "before the open", "AMC": "after the close"}.get(
            self.when, "time not announced")

    def before(self, expiry: date) -> bool:
        """Does the report land while an option expiring `expiry` is alive?
        Options stop trading at the close on expiration day, so a report
        after that close is too late to matter."""
        return self.date < expiry or (self.date == expiry and self.when != "AMC")

    def last_session(self) -> date:
        """The last trading day you can act on before the report hits: the
        report day itself if it comes after the close, otherwise the trading
        day before it."""
        if self.when == "AMC":
            return self.date
        day = self.date - timedelta(days=1)
        while not trading_calendar.is_trading_day(day):
            day -= timedelta(days=1)
        return day


def from_metrics(metrics: dict[str, dict], today: date) -> dict[str, Earnings]:
    """Upcoming earnings from /market-metrics items keyed by symbol (as in
    market.Snapshot.metrics). Stale and missing dates are left out."""
    found = {}
    for symbol, item in metrics.items():
        raw = item.get("earnings") or {}
        stamp = raw.get("expected-report-date")
        if not stamp:
            continue
        day = date.fromisoformat(stamp)
        if day < today:
            continue  # the stale-date trap: a report that already happened
        found[symbol] = Earnings(symbol, day, raw.get("time-of-day"))
    return found


def _effect(trade: Trade) -> str:
    """What an earnings report tends to do to this kind of trade."""
    credit = trade.credit
    if credit is not None and credit > 0:
        return ("Short premium: the IV drop after the report works for you, "
                "a large gap works against you.")
    if credit is not None and credit < 0:
        return "Long premium: the IV drop after the report works against you."
    return "Expect a possible gap in the stock and a drop in IV after the report."


def _when_text(e: Earnings, today: date) -> str:
    if e.date == today:
        return f"reports **today** ({e.timing})"
    if (e.date - today).days == 1:
        return f"reports **tomorrow** ({e.timing})"
    return f"reports {e.date:%a %b %d} ({e.timing})"


def evaluate(trades: list[Trade], stocks: list[Position],
             upcoming: dict[str, Earnings], today: date) -> list[Alert]:
    """Earnings alerts currently due. Keys carry the report date, so a
    rescheduled report warns afresh."""
    alerts = []
    for trade in trades:
        e = upcoming.get(trade.underlying)
        if e is None or not e.before(trade.expires):
            continue
        tag = f"@{e.date.isoformat()}|{trade.key}"
        alerts.append(Alert(
            "earn" + tag,
            f"📅 **{trade.describe()}** — {trade.underlying} {_when_text(e, today)}, "
            f"before this trade expires {trade.expires:%b %d}. {_effect(trade)}",
        ))
        if today >= e.last_session():
            alerts.append(Alert(
                "earnlast" + tag,
                f"⚠️ **{trade.describe()}** — last session before "
                f"{trade.underlying}'s report ({e.timing}, {e.date:%b %d}). "
                f"Anything you want to change should happen today.",
            ))
    for p in stocks:
        e = upcoming.get(p.symbol)
        if e is None or (e.date - today).days > STOCK_WINDOW:
            continue
        tag = f"@{e.date.isoformat()}|{stock_key(p)}"
        alerts.append(Alert(
            "earnstock" + tag,
            f"📅 **{p.symbol}** (shares) — {_when_text(e, today)}. "
            f"Earnings can gap a stock well past a normal day's move.",
        ))
    return alerts


def stock_key(p: Position) -> str:
    return f"{p.account}|{p.symbol}|shares"


def affects(trade: Trade, upcoming: dict[str, Earnings]) -> Earnings | None:
    """The report that lands inside this trade's life, if any - for the
    📅 marker in the summary and /positions."""
    e = upcoming.get(trade.underlying)
    return e if e is not None and e.before(trade.expires) else None
