"""REST calls and row assembly.

Every value written here is either verbatim from the API or a structural fact
about where it appeared in the response (which side of a strike a contract sat
on, which root it belonged to). Nothing is computed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Iterable, Iterator

from chain_archiver.auth import ApiError, TastytradeClient
from chain_archiver.config import METRICS_CHUNK_SIZE, QUOTE_CHUNK_SIZE, SymbolSpec

log = logging.getLogger(__name__)

#: Expiration type that denotes a standard monthly, which is what the
#: expiration filter keeps out past max_dte (section 6).
MONTHLY_EXPIRATION_TYPE = "Regular"


# -- coercion ------------------------------------------------------------
# The API sends numbers as JSON strings ("1.23"). These convert without
# inventing values: anything absent, blank or unparseable becomes NULL.


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    parsed = _f(value)
    return None if parsed is None else int(parsed)


def _d(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# -- endpoints -----------------------------------------------------------


def fetch_underlying_price(client: TastytradeClient, spec: SymbolSpec) -> float | None:
    """Mid of the underlying at snapshot time.

    Falls back to the bid/ask midpoint and then to last, because index
    underlyings quote a level rather than a two-sided market.
    """
    param = "index" if spec.is_index else "equity"
    data = client.get("/market-data/by-type", params={param: spec.symbol})
    items = data.get("items") or []
    if not items:
        return None

    quote = items[0]
    mid = _f(quote.get("mid"))
    if mid is not None:
        return mid
    bid, ask = _f(quote.get("bid")), _f(quote.get("ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return _f(quote.get("last"))


def fetch_nested_chain(client: TastytradeClient, symbol: str) -> list[dict]:
    """The nested chain, which is far more compact than the flat form.

    Returns one item per root, so adjusted roots (SPY1) arrive alongside the
    standard one and each carries its own shares-per-contract.
    """
    quoted = symbol.replace("/", "%2F")
    data = client.get(f"/option-chains/{quoted}/nested")
    return data.get("items") or []


def fetch_option_quotes(
    client: TastytradeClient, occ_symbols: list[str]
) -> dict[str, dict]:
    """Quotes keyed by OCC symbol.

    /market-data/by-type caps at 100 symbols across all instrument types, so
    this chunks. Contracts the endpoint declines to return are simply absent
    from the result and end up as NULL quote columns downstream.
    """
    quotes: dict[str, dict] = {}
    for chunk in _chunks(occ_symbols, QUOTE_CHUNK_SIZE):
        data = client.get(
            "/market-data/by-type", params={"equity-option": ",".join(chunk)}
        )
        for item in data.get("items") or []:
            symbol = item.get("symbol")
            if symbol:
                quotes[symbol] = item
    return quotes


def fetch_metrics(client: TastytradeClient, symbols: Iterable[str]) -> list[dict]:
    """Raw /market-metrics items for the given symbols.

    Batched, because the endpoint is built for it. If a batch fails - one bad
    symbol is enough to reject the whole request - this retries the members
    individually so a single unsupported symbol cannot cost the others their
    metrics.
    """
    wanted = list(symbols)
    items: list[dict] = []

    for chunk in _chunks(wanted, METRICS_CHUNK_SIZE):
        try:
            data = client.get("/market-metrics", params={"symbols": ",".join(chunk)})
            items.extend(data.get("items") or [])
        except ApiError as exc:
            log.warning("Batched metrics failed (%s); falling back per symbol", exc)
            for symbol in chunk:
                try:
                    data = client.get("/market-metrics", params={"symbols": symbol})
                    items.extend(data.get("items") or [])
                except ApiError as inner:
                    log.warning("Metrics failed for %s: %s", symbol, inner)
    return items


def fetch_risk_free_rate(client: TastytradeClient) -> float | None:
    """tastytrade publishes the rate it uses; this endpoint needs no auth.

    Not written by the archiver - the derived layer stores it per row
    (section 4.3). Exposed here so that step has a source that is not a
    config constant.
    """
    data = client.get("/margin-requirements-public-configuration")
    return _f(data.get("risk-free-rate"))


# -- row assembly --------------------------------------------------------


def _keep_expiration(expiration: dict, spec: SymbolSpec) -> bool:
    dte = _i(expiration.get("days-to-expiration"))
    if dte is None:
        return False
    if dte <= spec.max_dte:
        return True
    return (
        expiration.get("expiration-type") == MONTHLY_EXPIRATION_TYPE
        and dte <= spec.monthly_max_dte
    )


def select_contracts(
    chain_items: list[dict], spec: SymbolSpec, underlying_price: float
) -> list[dict]:
    """Flatten the nested chain down to the contracts worth archiving.

    Applies the expiration and strike filters from section 6. Each returned
    dict carries everything the chain endpoint knows about the contract; the
    quote columns are filled in later.
    """
    low = underlying_price * (1 - spec.strike_pct)
    high = underlying_price * (1 + spec.strike_pct)

    sides = (
        ("C", "call", "call-streamer-symbol"),
        ("P", "put", "put-streamer-symbol"),
    )

    contracts: list[dict] = []
    for root in chain_items:
        multiplier = _i(root.get("shares-per-contract"))
        underlying_symbol = root.get("underlying-symbol") or spec.symbol

        for expiration in root.get("expirations") or []:
            if not _keep_expiration(expiration, spec):
                continue

            expiration_date = _d(expiration.get("expiration-date"))
            if expiration_date is None:
                continue

            for strike_entry in expiration.get("strikes") or []:
                strike = _f(strike_entry.get("strike-price"))
                if strike is None or not (low <= strike <= high):
                    continue

                for option_type, occ_key, streamer_key in sides:
                    occ_symbol = strike_entry.get(occ_key)
                    if not occ_symbol:
                        continue
                    contracts.append(
                        {
                            "underlying_symbol": underlying_symbol,
                            "occ_symbol": occ_symbol,
                            "streamer_symbol": strike_entry.get(streamer_key),
                            "expiration_date": expiration_date,
                            "dte": _i(expiration.get("days-to-expiration")),
                            "strike": strike,
                            "option_type": option_type,
                            "multiplier": multiplier,
                            "expiration_type": expiration.get("expiration-type"),
                            "settlement_type": expiration.get("settlement-type"),
                            "is_index_option": spec.is_index,
                        }
                    )
    return contracts


def build_chain_rows(
    contracts: list[dict],
    quotes: dict[str, dict],
    *,
    snapshot_ts: datetime,
    session: str,
    underlying_price: float | None,
) -> list[dict]:
    """Merge contract facts with quotes into rows matching CHAINS_SCHEMA.

    Contracts with no quote are kept with NULL quote columns: the fact that a
    contract existed and did not quote is itself data, and dropping it would
    make coverage look better than it was.
    """
    rows = []
    for contract in contracts:
        quote = quotes.get(contract["occ_symbol"], {})
        rows.append(
            {
                "snapshot_ts_utc": snapshot_ts,
                "session": session,
                "underlying_symbol": contract["underlying_symbol"],
                "underlying_price": underlying_price,
                "occ_symbol": contract["occ_symbol"],
                "streamer_symbol": contract["streamer_symbol"],
                "expiration_date": contract["expiration_date"],
                "dte": contract["dte"],
                "strike": contract["strike"],
                "option_type": contract["option_type"],
                "bid": _f(quote.get("bid")),
                "ask": _f(quote.get("ask")),
                "bid_size": _i(quote.get("bid-size")),
                "ask_size": _i(quote.get("ask-size")),
                "last": _f(quote.get("last")),
                "volume": _i(quote.get("volume")),
                "open_interest": _i(quote.get("open-interest")),
                "multiplier": contract["multiplier"],
                "expiration_type": contract["expiration_type"],
                "settlement_type": contract["settlement_type"],
                "is_index_option": contract["is_index_option"],
            }
        )
    return rows


def build_metrics_rows(
    items: list[dict], *, snapshot_ts: datetime, session: str
) -> list[dict]:
    """Normalize /market-metrics items into METRICS_SCHEMA rows.

    Three things the API does that the schema does not: rank and percentile
    arrive as strings, the earnings fields are nested one level down, and the
    dividend rate is named dividend-rate-per-share.
    """
    rows = []
    for item in items:
        earnings = item.get("earnings") or {}
        rows.append(
            {
                "snapshot_ts_utc": snapshot_ts,
                "session": session,
                "symbol": item.get("symbol"),
                "implied_volatility_index": _f(item.get("implied-volatility-index")),
                "implied_volatility_index_rank": _f(
                    item.get("implied-volatility-index-rank")
                ),
                "implied_volatility_percentile": _f(
                    item.get("implied-volatility-percentile")
                ),
                "implied_volatility_index_5_day_change": _f(
                    item.get("implied-volatility-index-5-day-change")
                ),
                "historical_volatility_30_day": _f(
                    item.get("historical-volatility-30-day")
                ),
                "historical_volatility_60_day": _f(
                    item.get("historical-volatility-60-day")
                ),
                "iv_hv_30_day_difference": _f(item.get("iv-hv-30-day-difference")),
                "liquidity_rating": _i(item.get("liquidity-rating")),
                "liquidity_rank": _f(item.get("liquidity-rank")),
                "beta": _f(item.get("beta")),
                "corr_spy_3month": _f(item.get("corr-spy-3month")),
                "earnings_expected_report_date": _d(
                    earnings.get("expected-report-date")
                ),
                "earnings_time_of_day": earnings.get("time-of-day"),
                "dividend_next_date": _d(item.get("dividend-next-date")),
                "dividend_rate": _f(item.get("dividend-rate-per-share")),
            }
        )
    return rows
