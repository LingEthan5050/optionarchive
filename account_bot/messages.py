"""What reaches Discord, and how much of it.

Two tiers, because Discord keeps message history indefinitely and does not
encrypt it end to end.

  * reminder()  is POSTED - it lands in your DMs and stays there. It carries
    the minimum that makes it useful: symbol, expiration, side, days left. No
    dollar amounts, no prices, no account numbers.
  * positions_detail() and balance() are only ever sent as EPHEMERAL replies
    to a slash command: visible to you alone, not written to channel history,
    gone when you dismiss them. Money figures live only here.

If you add a message, decide its tier first.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from account_bot.account import Balance, Position

#: Discord rejects messages over 2000 characters.
LIMIT = 2000

#: Days-left values that get called out in the reminder.
URGENT = 3
SOON = 7


def _contract(p: Position) -> str:
    strike = f"{p.strike:g}" if p.strike is not None else "?"
    side = "short" if p.quantity < 0 else "long"
    qty = abs(p.quantity)
    return (f"{p.underlying} {p.expires:%b %d} {strike}{p.option_type or ''} "
            f"· {side} {qty:g}")


def _when(days: int) -> str:
    if days < 0:
        return "expired"
    if days == 0:
        return "**expires today**"
    if days == 1:
        return "**1 day left**"
    return f"{days} days left"


def _flag(days: int) -> str:
    if days <= URGENT:
        return "🔴"
    if days <= SOON:
        return "🟡"
    return "▫️"


def chunk(text: str) -> list[str]:
    """Split on line boundaries so no message exceeds Discord's limit."""
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > LIMIT:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts or [""]


def reminder(positions: list[Position], today: date) -> str | None:
    """The daily POSTED summary: option positions by days left. None when
    there are no options, so the bot stays quiet rather than posting noise."""
    options = sorted(
        (p for p in positions if p.is_option), key=lambda p: p.expires
    )
    if not options:
        return None

    urgent = sum(1 for p in options if p.days_left(today) <= URGENT)
    head = f"**Open options — {today:%a %b %d}**"
    if urgent:
        head += f"  ·  {urgent} within {URGENT} days"
    lines = [head]
    for p in options:
        days = p.days_left(today)
        lines.append(f"{_flag(days)} {_contract(p)} — {_when(days)}")
    lines.append("-# 🔴 ≤3 days  🟡 ≤7 days. Prices and balances: /positions, /balance")
    return "\n".join(lines)


def positions_detail(positions: list[Position], today: date) -> str:
    """EPHEMERAL only: every position, with entry prices."""
    if not positions:
        return "No open positions."
    by_account: dict[str, list[Position]] = defaultdict(list)
    for p in positions:
        by_account[p.account].append(p)

    lines = []
    for account, held in by_account.items():
        lines.append(f"**{account}**")
        options = sorted((p for p in held if p.is_option), key=lambda p: p.expires)
        others = sorted((p for p in held if not p.is_option), key=lambda p: p.symbol)
        for p in options:
            entry = (f" · opened {p.average_open_price:,.2f}"
                     if p.average_open_price is not None else "")
            lines.append(f"{_flag(p.days_left(today))} {_contract(p)} — "
                         f"{_when(p.days_left(today))}{entry}")
        for p in others:
            entry = (f" @ {p.average_open_price:,.2f}"
                     if p.average_open_price is not None else "")
            lines.append(f"▪️ {p.symbol} · {p.quantity:+g} sh{entry}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _money(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "—"


def balance(balances: list[Balance]) -> str:
    """EPHEMERAL only: per-account balances and the total."""
    if not balances:
        return "No open accounts."
    lines = []
    for b in balances:
        lines.append(f"**{b.account}**")
        lines.append(f"Net liq {_money(b.net_liquidating_value)} · "
                     f"cash {_money(b.cash_balance)}")
        lines.append(f"Option BP {_money(b.derivative_buying_power)} · "
                     f"stock BP {_money(b.equity_buying_power)}")
        lines.append("")
    total = sum(b.net_liquidating_value or 0 for b in balances)
    lines.append(f"**Total net liq {_money(total)}**")
    lines.append("-# Only you can see this. It isn't saved to the chat.")
    return "\n".join(lines)
