"""Rule-of-thumb alerts: tastytrade's two best-known management guidelines.

  * Manage at 21 DTE. Close or roll a position with 21 days to go rather than
    riding it into expiration week, where gamma - how fast delta moves with
    the stock - grows sharply and a small move can swing the whole trade. An
    earlier heads-up at 28 DTE gives a week's notice.
  * Take profits at 50% of max profit on short-premium trades. Most of the
    achievable profit is behind you by then, and holding for the rest means
    carrying the full risk for a shrinking reward.

These are published rules of thumb that the alerts remind you of. They are
not a recommendation about any particular position.

Both rules apply to a TRADE, not a leg. An iron condor is four positions to
tastytrade's API but one decision, and a single leg can sit at 90% profit
while the trade is at 20%. Legs are grouped by account and underlying, which
is right for the common structures (verticals, strangles, condors, a short
option against stock). It is wrong for two independent trades on the same
underlying in the same account, which get judged together.

Each alert fires once. Fired keys are kept in a small JSON file, and keys for
trades that are no longer held are dropped, so a later trade on the same
underlying alerts afresh.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from account_bot.account import Position

DEFAULT_DTE_ALERTS = (28, 21)
DEFAULT_PROFIT_TARGET = 0.50


@dataclass
class Trade:
    account: str
    underlying: str
    legs: list[Position] = field(default_factory=list)

    @property
    def key(self) -> str:
        # Stable across runs while the same legs are held.
        legs = ",".join(sorted(p.symbol.replace(" ", "") for p in self.legs))
        return f"{self.account}|{self.underlying}|{legs}"

    @property
    def expires(self) -> date:
        return min(p.expires for p in self.legs)

    def days_left(self, today: date) -> int:
        return (self.expires - today).days

    @property
    def credit(self) -> float | None:
        """What the trade took in when opened, per unit of risk currency.
        Positive for a credit (short premium) trade, negative for a debit."""
        if any(p.average_open_price is None for p in self.legs):
            return None
        # quantity is signed (negative when short), so a short leg's opening
        # value counts as money received.
        return -sum(p.quantity * p.average_open_price * p.multiplier for p in self.legs)

    def cost_to_close(self, mids: dict[str, float]) -> float | None:
        if any(p.symbol not in mids for p in self.legs):
            return None
        return -sum(p.quantity * mids[p.symbol] * p.multiplier for p in self.legs)

    def pnl(self, mids: dict[str, float]) -> tuple[str, float] | None:
        """Profit as a fraction, and which kind - they are not comparable.

        credit: share of max profit captured. Max profit on a short-premium
                trade is the credit itself, so this tops out at 1.0 (100%)
                and is the number tastytrade's 50% rule is stated in.
        debit:  return on what was paid. No ceiling on the upside; -1.0
                (-100%) if the options expire worthless.
        """
        credit, close = self.credit, self.cost_to_close(mids)
        if credit is None or close is None or credit == 0:
            return None
        if credit > 0:
            return "credit", (credit - close) / credit
        paid, worth = -credit, -close
        return "debit", (worth - paid) / paid

    def day_change(self, mids: dict[str, float], today: date) -> float | None:
        """Dollars gained or lost since the previous close (or since entry,
        for a leg opened today). None if any leg lacks a price, so a partial
        number never passes for the whole trade's."""
        total = 0.0
        for p in self.legs:
            base = p.day_baseline(today)
            if base is None or p.symbol not in mids:
                return None
            # quantity is signed, so a short leg gains when its price falls.
            total += (mids[p.symbol] - base) * p.quantity * p.multiplier
        return total

    def profit_share(self, mids: dict[str, float]) -> float | None:
        """Share of max profit, for credit trades only - what the 50% alert
        measures."""
        result = self.pnl(mids)
        return result[1] if result and result[0] == "credit" else None

    def describe(self) -> str:
        """Symbol, expiration and strikes - never a price or an amount."""
        strikes = "/".join(
            f"{p.strike:g}{p.option_type}" for p in
            # Puts then calls, each low strike to high: how a chain reads.
            sorted(self.legs, key=lambda p: (p.option_type != "P", p.strike or 0))
        )
        expiries = sorted({p.expires for p in self.legs})
        when = " & ".join(f"{e:%b %d}" for e in expiries)
        return f"{self.underlying} {when} {strikes}"


@dataclass(frozen=True)
class Alert:
    key: str
    text: str


def group_trades(positions: list[Position]) -> list[Trade]:
    trades: dict[tuple[str, str], Trade] = {}
    for p in positions:
        if not p.is_option:
            continue
        trade = trades.setdefault((p.account, p.underlying),
                                  Trade(p.account, p.underlying))
        trade.legs.append(p)
    return sorted(trades.values(), key=lambda t: t.expires)


def evaluate(
    trades: list[Trade],
    mids: dict[str, float],
    today: date,
    dte_alerts: tuple[int, ...] = DEFAULT_DTE_ALERTS,
    profit_target: float = DEFAULT_PROFIT_TARGET,
) -> list[Alert]:
    """Every alert currently due, fired or not - the caller filters out the
    ones already sent."""
    alerts = []
    tightest_first = sorted(dte_alerts)
    for trade in trades:
        days = trade.days_left(today)
        # Only the tightest threshold already crossed. A trade first seen at
        # 18 DTE should get the 21-DTE alert, not a stale 28-DTE one too;
        # the looser keys are emitted as silent so they never fire later.
        crossed = [t for t in tightest_first if days <= t]
        for threshold in crossed:
            key = f"dte{threshold}|{trade.key}"
            if threshold != crossed[0]:
                alerts.append(Alert(key, ""))
                continue
            if threshold == min(dte_alerts):
                text = (f"🔴 **{trade.describe()}** — {days} days left. "
                        f"tastytrade's guideline: manage (close or roll) at "
                        f"{threshold} DTE. Gamma risk rises quickly from here.")
            else:
                text = (f"🟡 **{trade.describe()}** — {days} days left. "
                        f"The {min(dte_alerts)}-DTE management point is "
                        f"{days - min(dte_alerts)} days away.")
            alerts.append(Alert(key, text))

        share = trade.profit_share(mids)
        if share is not None and share >= profit_target:
            alerts.append(Alert(
                f"profit{int(profit_target * 100)}|{trade.key}",
                f"✅ **{trade.describe()}** — short premium at "
                f"{share:.0%} of max profit. tastytrade's guideline: take "
                f"profits at {profit_target:.0%}.",
            ))
    return alerts


class AlertLog:
    """Which alerts have already fired, persisted as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.fired: set[str] = set(json.loads(path.read_text()))
        except (FileNotFoundError, ValueError):
            self.fired = set()

    def new(self, alerts: list[Alert]) -> list[Alert]:
        return [a for a in alerts if a.key not in self.fired]

    def record(self, alerts: list[Alert], held: set[str]) -> None:
        """Save newly fired keys, and forget any whose holding is gone.

        Every key is "<kind>|<holding key>", where the holding key is a
        Trade.key or earnings.stock_key(). `held` is the set of those for
        everything currently open, so reopening the same structure later is
        a new holding with its own alerts."""
        self.fired.update(a.key for a in alerts)
        self.fired = {k for k in self.fired if k.split("|", 1)[1] in held}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self.fired), indent=1))
