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
from account_bot.rules import group_trades

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


def _pct(kind: str, share: float) -> str:
    if kind == "credit":
        return f"**{share:.0%} of max profit**"
    return f"**{share:+.0%} return**"


def positions_detail(positions: list[Position], today: date,
                     mids: dict[str, float] | None = None) -> str:
    """EPHEMERAL only: every trade with its profit, legs and entry prices.

    Option legs are grouped into trades (rules.group_trades), because profit
    only means something for the whole structure. A credit trade shows the
    share of max profit captured - the number tastytrade's 50% rule uses -
    and a debit trade shows its return on what was paid. The two are not the
    same scale: the first tops out at 100%, the second has no ceiling.
    """
    mids = mids or {}
    if not positions:
        return "No open positions."
    by_account: dict[str, list[Position]] = defaultdict(list)
    for p in positions:
        by_account[p.account].append(p)

    lines = []
    for account, held in by_account.items():
        lines.append(f"**{account}**")
        for trade in group_trades(held):
            days = trade.days_left(today)
            result = trade.pnl(mids)
            profit = f" · {_pct(*result)}" if result else " · profit —"
            lines.append(f"{_flag(days)} {trade.describe()} — {_when(days)}{profit}")
            for leg in sorted(trade.legs, key=lambda p: (p.option_type != "P", p.strike or 0)):
                side = "short" if leg.quantity < 0 else "long"
                entry = (f" · opened {leg.average_open_price:,.2f}"
                         if leg.average_open_price is not None else "")
                now = f" · now {mids[leg.symbol]:,.2f}" if leg.symbol in mids else ""
                lines.append(f"  └ {side} {abs(leg.quantity):g} "
                             f"{leg.strike:g}{leg.option_type}{entry}{now}")
        for p in sorted((p for p in held if not p.is_option), key=lambda p: p.symbol):
            entry = (f" @ {p.average_open_price:,.2f}"
                     if p.average_open_price is not None else "")
            change = ""
            if p.symbol in mids and p.average_open_price:
                move = (mids[p.symbol] - p.average_open_price) / p.average_open_price
                # A short stock position profits when the price falls.
                change = f" · **{move * (1 if p.quantity > 0 else -1):+.1%}**"
            lines.append(f"▪️ {p.symbol} · {p.quantity:+g} sh{entry}{change}")
        lines.append("")
    lines.append("-# Credit trades: share of max profit. Debit trades and "
                 "stock: return on cost. Live mid prices.")
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
