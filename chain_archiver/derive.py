"""Greeks and implied volatility, computed from the raw chains.

Never written by the fetcher. This reads `chains` partitions and produces
`derived/greeks` partitions, so a bug in this math can be fixed by deleting
the derived tree and re-running - the archive itself is never at risk.

Model: Black-Scholes with continuous dividend yield for equities and ETFs.
The yield comes from the archived `dividend_rate` divided by spot, which is
why that column is carried in the metrics table.

Where the math is refused
-------------------------
A mid price is only meaningful if there is a real two-sided market behind it.
Rows are left NULL rather than filled with a fabricated number when:

  * bid is zero or missing - there is no bid, so the mid is half the ask
  * spread_pct > 0.5      - the mid is somewhere inside a chasm
  * the option is at or past expiry
  * the price violates its own arbitrage bounds, which no volatility can
    reproduce and which would otherwise drive the solver to a bound

Storing NULL is the honest answer. A garbage IV that looks like a number is
far more dangerous downstream than a missing one.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq

log = logging.getLogger(__name__)

#: Bump when the math changes, so old rows stay identifiable. Stored per row.
MODEL_VERSION = "bs-v1"

#: Brent needs a bracket. Below 0.1% or above 500% vol the quote is noise.
IV_LOWER, IV_UPPER = 0.001, 5.0

#: Refuse a mid whose spread exceeds this fraction of it.
MAX_SPREAD_PCT = 0.5

#: Fallback used only if no rate is supplied. tastytrade publishes the real
#: one at /margin-requirements-public-configuration (fetch.risk_free_rate).
DEFAULT_RISK_FREE_RATE = 0.04

DAYS_PER_YEAR = 365.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def black_scholes(
    spot: float,
    strike: float,
    t: float,
    rate: float,
    dividend_yield: float,
    vol: float,
    is_call: bool,
) -> Greeks:
    """Price and greeks under Black-Scholes with continuous dividend yield.

    theta is per calendar day and vega/rho per 1 percentage point, which is
    how they are quoted on every trading screen. Reporting them per unit-year
    and per unit-vol is technically purer and constantly misread.
    """
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate - dividend_yield + 0.5 * vol * vol) * t) / (
        vol * sqrt_t
    )
    d2 = d1 - vol * sqrt_t

    discount = math.exp(-rate * t)
    carry = math.exp(-dividend_yield * t)
    pdf_d1 = _norm_pdf(d1)

    gamma = carry * pdf_d1 / (spot * vol * sqrt_t)
    vega = spot * carry * pdf_d1 * sqrt_t / 100.0

    if is_call:
        price = spot * carry * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
        delta = carry * _norm_cdf(d1)
        rho = strike * t * discount * _norm_cdf(d2) / 100.0
        theta = (
            -spot * carry * pdf_d1 * vol / (2 * sqrt_t)
            - rate * strike * discount * _norm_cdf(d2)
            + dividend_yield * spot * carry * _norm_cdf(d1)
        ) / DAYS_PER_YEAR
    else:
        price = strike * discount * _norm_cdf(-d2) - spot * carry * _norm_cdf(-d1)
        delta = -carry * _norm_cdf(-d1)
        rho = -strike * t * discount * _norm_cdf(-d2) / 100.0
        theta = (
            -spot * carry * pdf_d1 * vol / (2 * sqrt_t)
            + rate * strike * discount * _norm_cdf(-d2)
            - dividend_yield * spot * carry * _norm_cdf(-d1)
        ) / DAYS_PER_YEAR

    return Greeks(price, delta, gamma, theta, vega, rho)


def implied_vol(
    price: float,
    spot: float,
    strike: float,
    t: float,
    rate: float,
    dividend_yield: float,
    is_call: bool,
) -> float | None:
    """Solve for volatility by Brent's method, or None if it cannot be done.

    Checks arbitrage bounds first. A price outside them is not a hard solve,
    it is an unsolvable one, and bracketing would push Brent onto a bound and
    return a number that looks plausible.
    """
    if t <= 0 or price <= 0 or spot <= 0 or strike <= 0:
        return None

    carry = math.exp(-dividend_yield * t)
    discount = math.exp(-rate * t)
    if is_call:
        lower_bound = max(0.0, spot * carry - strike * discount)
        upper_bound = spot * carry
    else:
        lower_bound = max(0.0, strike * discount - spot * carry)
        upper_bound = strike * discount

    # A hair of tolerance: quotes are rounded to the tick, so a price can sit
    # a cent under intrinsic without being nonsense.
    if price < lower_bound - 0.01 or price > upper_bound:
        return None

    def objective(vol: float) -> float:
        return black_scholes(
            spot, strike, t, rate, dividend_yield, vol, is_call
        ).price - price

    try:
        if objective(IV_LOWER) * objective(IV_UPPER) > 0:
            return None
        return brentq(objective, IV_LOWER, IV_UPPER, xtol=1e-6, maxiter=100)
    except (ValueError, RuntimeError):
        return None


def derive_rows(
    chain_rows: list[dict],
    *,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    dividend_yields: dict[str, float] | None = None,
) -> list[dict]:
    """Turn chain rows into greeks rows matching GREEKS_SCHEMA.

    Every input row produces an output row. Rows the math refuses carry NULL
    analytics rather than being dropped, so `derived` and `chains` stay
    row-for-row aligned and a missing greek is visibly missing.
    """
    yields = dividend_yields or {}
    out: list[dict] = []

    for row in chain_rows:
        bid, ask = row.get("bid"), row.get("ask")
        spot = row.get("underlying_price")
        strike = row.get("strike")
        dte = row.get("dte")

        mid = spread_pct = None
        if bid is not None and ask is not None and (bid + ask) > 0:
            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid if mid > 0 else None

        q = yields.get(row.get("underlying_symbol"), 0.0)
        iv = greeks = None

        usable = (
            mid is not None
            and bid is not None
            and bid > 0
            and spread_pct is not None
            and spread_pct <= MAX_SPREAD_PCT
            and spot is not None
            and strike is not None
            and dte is not None
            and dte > 0
        )
        if usable:
            t = dte / DAYS_PER_YEAR
            is_call = row.get("option_type") == "C"
            iv = implied_vol(mid, spot, strike, t, risk_free_rate, q, is_call)
            if iv is not None:
                greeks = black_scholes(
                    spot, strike, t, risk_free_rate, q, iv, is_call
                )

        out.append(
            {
                "snapshot_ts_utc": row["snapshot_ts_utc"],
                "session": row["session"],
                "occ_symbol": row["occ_symbol"],
                "underlying_symbol": row["underlying_symbol"],
                "expiration_date": row.get("expiration_date"),
                "dte": dte,
                "strike": strike,
                "option_type": row.get("option_type"),
                "mid": mid,
                "implied_vol": iv,
                "delta": greeks.delta if greeks else None,
                "gamma": greeks.gamma if greeks else None,
                "theta": greeks.theta if greeks else None,
                "vega": greeks.vega if greeks else None,
                "rho": greeks.rho if greeks else None,
                "moneyness": (
                    strike / spot if spot and strike and spot > 0 else None
                ),
                "spread_pct": spread_pct,
                "risk_free_rate": risk_free_rate,
                "dividend_yield": q,
                "model_version": MODEL_VERSION,
            }
        )
    return out


def summarize(rows: list[dict]) -> str:
    solved = sum(1 for r in rows if r["implied_vol"] is not None)
    ivs = np.array([r["implied_vol"] for r in rows if r["implied_vol"] is not None])
    if ivs.size == 0:
        return f"0/{len(rows)} solved"
    return (
        f"{solved}/{len(rows)} solved ({100 * solved / len(rows):.1f}%), "
        f"IV median {np.median(ivs):.3f} range [{ivs.min():.3f}, {ivs.max():.3f}]"
    )
