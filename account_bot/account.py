"""Read-only views of the tastytrade accounts: positions and balances.

This is the only module in the repo that touches account data rather than
market data. It reuses the archiver's client and credential, which is
registered with the `read` scope - enough to see balances and positions,
never enough to place or cancel an order.

Nothing here logs a value. Callers get dataclasses; what reaches Discord, and
how much of it, is decided in messages.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from chain_archiver.auth import TastytradeClient

EASTERN = ZoneInfo("America/New_York")

#: OCC option symbol: root padded to six, YYMMDD, C/P, strike x 1000 in eight
#: digits. The fallback when a position arrives without expires-at.
OCC = re.compile(
    r"^(?P<root>[A-Z0-9./]{1,6})\s*(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$"
)


@dataclass(frozen=True)
class Account:
    number: str
    #: Type plus the last four digits - enough to tell accounts apart without
    #: putting the full number into a chat message.
    label: str


@dataclass(frozen=True)
class Position:
    account: str
    symbol: str
    underlying: str
    instrument_type: str
    #: Signed: negative when short.
    quantity: float
    average_open_price: float | None
    multiplier: float
    expires: date | None = None
    strike: float | None = None
    option_type: str | None = None

    @property
    def is_option(self) -> bool:
        return self.expires is not None

    def days_left(self, today: date) -> int | None:
        """Calendar days to expiration, the way DTE is quoted everywhere."""
        return (self.expires - today).days if self.expires else None


@dataclass(frozen=True)
class Balance:
    account: str
    net_liquidating_value: float | None
    cash_balance: float | None
    derivative_buying_power: float | None
    equity_buying_power: float | None


def _num(value: object) -> float | None:
    # The API sends decimals as strings ("1234.56") to avoid float rounding.
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def accounts(client: TastytradeClient) -> list[Account]:
    """Every open account, in the order tastytrade lists them."""
    found = []
    for item in client.get("/customers/me/accounts").get("items", []):
        acc = item["account"]
        if acc.get("is-closed"):
            continue
        number = acc["account-number"]
        kind = acc.get("nickname") or acc.get("account-type-name") or "Account"
        found.append(Account(number, f"{kind} …{number[-4:]}"))
    return found


def _option_fields(raw: dict) -> tuple[date | None, float | None, str | None]:
    match = OCC.match(raw.get("symbol", ""))
    strike = int(match["strike"]) / 1000 if match else None
    option_type = match["cp"] if match else None

    expires = None
    if raw.get("expires-at"):
        # expires-at is a UTC timestamp at the close; the expiration DATE is
        # the Eastern one, which UTC can push to the following day.
        stamp = datetime.fromisoformat(raw["expires-at"].replace("Z", "+00:00"))
        expires = stamp.astimezone(EASTERN).date()
    elif match:
        expires = datetime.strptime(match["exp"], "%y%m%d").date()
    return expires, strike, option_type


def positions(client: TastytradeClient, accts: list[Account]) -> list[Position]:
    found = []
    for acct in accts:
        items = client.get(f"/accounts/{acct.number}/positions").get("items", [])
        for raw in items:
            quantity = _num(raw.get("quantity")) or 0.0
            if raw.get("quantity-direction") == "Short":
                quantity = -abs(quantity)
            is_option = "Option" in (raw.get("instrument-type") or "")
            expires, strike, option_type = (
                _option_fields(raw) if is_option else (None, None, None)
            )
            found.append(Position(
                account=acct.label,
                symbol=raw.get("symbol", ""),
                underlying=raw.get("underlying-symbol") or raw.get("symbol", ""),
                instrument_type=raw.get("instrument-type", ""),
                quantity=quantity,
                average_open_price=_num(raw.get("average-open-price")),
                multiplier=_num(raw.get("multiplier")) or 1.0,
                expires=expires,
                strike=strike,
                option_type=option_type,
            ))
    return found


def balances(client: TastytradeClient, accts: list[Account]) -> list[Balance]:
    found = []
    for acct in accts:
        data = client.get(f"/accounts/{acct.number}/balances")
        # Arrives as a one-element list under "items" rather than as the
        # object the docs describe; accept either.
        raw = (data.get("items") or [data])[0]
        found.append(Balance(
            account=acct.label,
            net_liquidating_value=_num(raw.get("net-liquidating-value")),
            cash_balance=_num(raw.get("cash-balance")),
            derivative_buying_power=_num(raw.get("derivative-buying-power")),
            equity_buying_power=_num(raw.get("equity-buying-power")),
        ))
    return found


def today_eastern() -> date:
    return datetime.now(EASTERN).date()
