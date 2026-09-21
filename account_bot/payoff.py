"""Expiration payoff: max profit, max loss, breakevens, probability of profit.

Works on any set of legs sharing one expiration - verticals, condors,
strangles, butterflies, naked options - without recognising the strategy by
name. At expiration each option is worth its intrinsic value, so the
position's profit is a straight line between strikes. Evaluating it at zero,
at every strike, and past the last strike, and reading the slope beyond
that, gives every number exactly:

  * max profit / max loss: the highest and lowest points, or unlimited when
    the final slope keeps rising (net long calls) or falling (net short
    calls);
  * breakevens: where the line crosses zero, by interpolation;
  * profit zones: the price ranges where it ends above zero.

Probability of profit is the chance the stock finishes inside those zones,
under the lognormal distribution implied by the options' own IV - the same
risk-neutral measure the prices come from, and the way tastytrade computes
its POP. It is a market-implied probability, not a forecast.

Trades whose legs expire on different dates (calendars, diagonals) have no
single expiration payoff, and are reported as not analysable rather than
approximated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from account_bot.rules import Trade


@dataclass(frozen=True)
class Payoff:
    #: Dollars. None means unlimited.
    max_profit: float | None
    #: Dollars, as a positive number. None means unlimited.
    max_loss: float | None
    breakevens: tuple[float, ...]
    #: (low, high) price ranges where the trade ends profitable; high None = no top.
    zones: tuple[tuple[float, float | None], ...]


def _at(trade: Trade, price: float, cost: float) -> float:
    """Profit in dollars if the stock is at `price` on expiration."""
    value = 0.0
    for p in trade.legs:
        intrinsic = max(price - p.strike, 0.0) if p.option_type == "C" else max(p.strike - price, 0.0)
        value += intrinsic * p.quantity * p.multiplier
    return value - cost


def analyse(trade: Trade) -> Payoff | None:
    if len({p.expires for p in trade.legs}) != 1:
        return None
    if any(p.average_open_price is None or not p.strike for p in trade.legs):
        return None
    # What was paid to open: positive for a debit, negative for a credit.
    cost = sum(p.quantity * p.average_open_price * p.multiplier for p in trade.legs)

    strikes = sorted({p.strike for p in trade.legs})
    top = strikes[-1] * 2 + 1  # any point past the last kink; the line is straight after it
    xs = [0.0] + strikes + [top]
    ys = [_at(trade, x, cost) for x in xs]
    # Slope past the last strike: only calls still change value up there.
    slope = sum(p.quantity * p.multiplier for p in trade.legs if p.option_type == "C")

    max_profit = None if slope > 0 else max(ys)
    max_loss = None if slope < 0 else max(0.0, -min(ys))

    crossings = []
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if y0 == 0:
            crossings.append(x0)
        elif y0 * y1 < 0:
            crossings.append(x0 - y0 * (x1 - x0) / (y1 - y0))
    if slope and ys[-1] * slope < 0:  # the line crosses zero beyond `top`
        crossings.append(top - ys[-1] / slope)
    breakevens = tuple(sorted({round(b, 4) for b in crossings if b > 0}))

    # Each stretch between breakevens is wholly profitable or wholly not;
    # test one interior point per stretch.
    edges = [0.0, *breakevens, None]
    zones = []
    for low, high in zip(edges, edges[1:]):
        probe = (low + high) / 2 if high is not None else max(top, low * 2 + 1)
        if _at(trade, probe, cost) > 0:
            zones.append((low, high))
    return Payoff(max_profit, max_loss, breakevens, tuple(zones))


def _above(spot: float, level: float, vol: float, t: float, rate: float, q: float) -> float:
    """P(price at expiration > level) under the risk-neutral lognormal."""
    if level <= 0:
        return 1.0
    d2 = (math.log(spot / level) + (rate - q - 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    return 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))


def probability_of_profit(payoff: Payoff, spot: float, vol: float, t: float,
                          rate: float, q: float = 0.0) -> float | None:
    if vol <= 0 or t <= 0 or spot <= 0:
        return None
    total = 0.0
    for low, high in payoff.zones:
        total += _above(spot, low, vol, t, rate, q) - (
            _above(spot, high, vol, t, rate, q) if high is not None else 0.0)
    return min(max(total, 0.0), 1.0)


def expected_move(spot: float, vol: float, t: float) -> float:
    """One standard deviation of the price at expiration, in dollars - the
    'expected move' quoted on trading screens."""
    return spot * vol * math.sqrt(t)


def sigmas_away(spot: float, strike: float, vol: float, t: float) -> float:
    """How many standard deviations the strike sits from the stock, on the
    log scale the distribution lives on. Positive = that far to travel."""
    return abs(math.log(strike / spot)) / (vol * math.sqrt(t))
