"""Position greeks: what each trade and the whole portfolio are exposed to.

Two numbers carry most of a portfolio's story, and they are the two tastytrade
traders watch:

  * THETA per day, in dollars - what time decay alone adds to (short premium)
    or takes from (long premium) the account each calendar day, other things
    equal.
  * BETA-WEIGHTED DELTA, in SPY shares - the whole portfolio's directional
    exposure restated as one position in SPY. Each position's delta (in
    shares of its own underlying) is converted to dollars of exposure, scaled
    by the underlying's beta to SPY, and divided by SPY's price. "+40" means
    the portfolio moves roughly like owning 40 shares of SPY.

Greeks come from the archiver's own Black-Scholes model (chain_archiver.
derive), with IV solved from the live mid - the same math the archive uses,
so the bot and the archive never disagree about what a delta is. Theta is
per calendar day, as derive.py defines it.

What is left out rather than guessed:
  * VIX options, which price off VIX futures rather than the spot index, so
    Black-Scholes on spot gives confident, wrong numbers.
  * any leg whose IV cannot be solved (no quote, a price outside arbitrage
    bounds, or at expiry) - its whole trade is skipped and named, so a total
    never silently omits part of a position.
  * positions whose underlying has no beta, from the beta-weighted total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time

from account_bot.account import EASTERN, Position
from account_bot.market import BENCHMARK, Snapshot
from account_bot.rules import Trade, group_trades
from chain_archiver.derive import black_scholes, implied_vol

#: Priced off futures, not spot. See the module docstring.
NOT_MODELLED = {"VIX"}

SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass(frozen=True)
class Exposure:
    #: Share-equivalents of the underlying (100 = like owning 100 shares).
    delta: float
    #: Dollars per calendar day from time decay.
    theta: float


def years_to_expiry(expires: date, now: datetime) -> float:
    """Time to the 16:00 ET close on expiration day, in years. Measured to
    the hour rather than in whole days, because theta on the last few days is
    dominated by exactly that difference."""
    close = datetime.combine(expires, time(16, 0), EASTERN)
    return max((close - now).total_seconds(), 0.0) / SECONDS_PER_YEAR


def dividend_yield(snap: Snapshot, symbol: str) -> float:
    spot = snap.spots.get(symbol)
    dividend = snap.metric(symbol, "dividend-rate-per-share")
    return dividend / spot if dividend and spot else 0.0


def leg_iv(p: Position, snap: Snapshot, now: datetime) -> float | None:
    """The leg's implied volatility, solved from its live mid."""
    if p.underlying in NOT_MODELLED:
        return None
    spot = snap.spots.get(p.underlying)
    mid = snap.mids.get(p.symbol)
    t = years_to_expiry(p.expires, now)
    if not spot or mid is None or t <= 0 or not p.strike:
        return None
    return implied_vol(mid, spot, p.strike, t, snap.rate,
                       dividend_yield(snap, p.underlying), p.option_type == "C")


def trade_iv(t: Trade, snap: Snapshot, now: datetime) -> float | None:
    """The trade's volatility for probability and expected-move estimates:
    the mean of its legs' solved IVs, else the underlying's IV index."""
    solved = [v for v in (leg_iv(p, snap, now) for p in t.legs) if v is not None]
    if solved:
        return sum(solved) / len(solved)
    return snap.metric(t.underlying, "implied-volatility-index")


def leg(p: Position, snap: Snapshot, now: datetime) -> Exposure | None:
    vol = leg_iv(p, snap, now)
    if vol is None:
        return None
    spot = snap.spots[p.underlying]
    t = years_to_expiry(p.expires, now)
    is_call = p.option_type == "C"
    g = black_scholes(spot, p.strike, t, snap.rate, dividend_yield(snap, p.underlying),
                      vol, is_call)
    size = p.quantity * p.multiplier  # signed: short legs flip both greeks
    return Exposure(delta=g.delta * size, theta=g.theta * size)


def trade(t: Trade, snap: Snapshot, now: datetime) -> Exposure | None:
    parts = [leg(p, snap, now) for p in t.legs]
    if any(part is None for part in parts):
        return None
    return Exposure(sum(p.delta for p in parts), sum(p.theta for p in parts))


@dataclass
class Portfolio:
    theta: float = 0.0
    #: Beta-weighted delta in SPY shares; None if SPY could not be priced.
    beta_delta: float | None = 0.0
    #: Holdings left out of the totals, with the reason.
    skipped: list[str] = field(default_factory=list)


def portfolio(snap: Snapshot, now: datetime) -> Portfolio:
    result = Portfolio()
    spy = snap.spots.get(BENCHMARK)
    if not spy:
        result.beta_delta = None

    def weigh(symbol: str, delta_shares: float) -> None:
        if result.beta_delta is None:
            return
        beta = 1.0 if symbol == BENCHMARK else snap.metric(symbol, "beta")
        spot = snap.spots.get(symbol)
        if beta is None or not spot:
            result.skipped.append(f"{symbol} (no beta)")
            return
        result.beta_delta += delta_shares * spot * beta / spy

    for t in group_trades(snap.positions):
        exposure = trade(t, snap, now)
        if exposure is None:
            why = "not modelled" if t.underlying in NOT_MODELLED else "no IV"
            result.skipped.append(f"{t.underlying} ({why})")
            continue
        result.theta += exposure.theta
        weigh(t.underlying, exposure.delta)
    for p in snap.positions:
        if not p.is_option:
            weigh(p.symbol, p.quantity)  # a share has delta 1
    return result
