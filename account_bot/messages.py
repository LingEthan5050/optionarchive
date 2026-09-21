"""What reaches Discord, and how much of it.

Two tiers, because Discord keeps message history indefinitely and does not
encrypt it end to end.

  * summary() and the rule alerts are POSTED - to the alert channel
    (#options by default), where they stay, readable by anyone who can see
    that channel. They carry symbol, strikes, expiration, days left, and
    profit and day P/L as PERCENTAGES. Dollar amounts appear in a posted
    summary only when SUMMARY_DOLLARS is switched on; never prices or
    account numbers.
  * positions_detail() and balance() are only ever sent as EPHEMERAL replies
    to a slash command: visible to you alone, not written to channel history,
    gone when you dismiss them. Money figures live only here.

If you add a message, decide its tier first.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from account_bot.account import Balance, Position
from account_bot.earnings import affects
from account_bot.rules import group_trades

#: Discord rejects messages over 2000 characters.
LIMIT = 2000

#: Days-left flags, matching the rule-of-thumb alerts: 🔴 at or inside the
#: 21-DTE management point, 🟡 in the week before it.
MANAGE = 21
SOON = 28



def _when(days: int) -> str:
    if days < 0:
        return "expired"
    if days == 0:
        return "**expires today**"
    if days == 1:
        return "**1 day left**"
    return f"{days} days left"


def _flag(days: int, soon: int = SOON, manage: int = MANAGE) -> str:
    if days <= manage:
        return "🔴"
    if days <= soon:
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


def _signed_money(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def summary(
    positions: list[Position],
    mids: dict[str, float],
    balances: list[Balance],
    prior_net_liq: dict[str, float],
    today: date,
    stamp: str,
    dollars: bool,
    soon: int = SOON,
    manage: int = MANAGE,
    upcoming: dict | None = None,
) -> str:
    """The twice-daily summary: day P/L, then each option trade.

    Day P/L is the change in net liquidating value since the previous close,
    summed over the accounts that have a snapshot for it. It is complete in a
    way that adding up open positions is not - a trade closed today still
    counts - but a deposit or withdrawal counts too.

    dollars=False (the posted default) shows percentages only. The ephemeral
    /expiring reply passes True.
    """
    lines = [f"**Options summary — {today:%a %b %d} · {stamp}**"]

    now = {b.account: b.net_liquidating_value for b in balances
           if b.net_liquidating_value is not None}
    common = [a for a in now if a in prior_net_liq]
    before = sum(prior_net_liq[a] for a in common)
    if common and before:
        change = sum(now[a] for a in common) - before
        pct = f"{change / before:+.2%}"
        figure = f"**{_signed_money(change)}** ({pct})" if dollars else f"**{pct}**"
        lines.append(f"Day P/L: {figure} across {len(common)} account"
                     f"{'s' if len(common) != 1 else ''}")
    else:
        lines.append("Day P/L: — (no prior-close snapshot)")

    trades = group_trades(positions)
    if not trades:
        lines.append("No open option positions.")
    for trade in trades:
        days = trade.days_left(today)
        result = trade.pnl(mids)
        profit = f" · {_pct(*result)}" if result else ""
        today_part = ""
        if dollars:
            moved = trade.day_change(mids, today)
            if moved is not None:
                today_part = f" · today {_signed_money(moved)}"
        lines.append(f"{_flag(days, soon, manage)} {trade.describe()} — "
                     f"{_when(days)}{profit}{today_part}{_earnings_mark(trade, upcoming)}")

    lines.append(f"-# 🔴 ≤{manage} days: manage  🟡 ≤{soon} days. Day P/L is the "
                 f"change in net liq since the last close, deposits included. "
                 f"Details: /positions, /balance")
    return "\n".join(lines)


def _pct(kind: str, share: float) -> str:
    if kind == "credit":
        return f"**{share:.0%} of max profit**"
    return f"**{share:+.0%} return**"


def _earnings_mark(trade, upcoming: dict | None) -> str:
    """" · 📅 earnings Oct 20" when a report lands inside the trade's life."""
    e = affects(trade, upcoming or {})
    return f" · 📅 earnings {e.date:%b %d}" if e else ""


def positions_detail(positions: list[Position], today: date,
                     mids: dict[str, float] | None = None,
                     upcoming: dict | None = None) -> str:
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
            lines.append(f"{_flag(days)} {trade.describe()} — {_when(days)}{profit}"
                         f"{_earnings_mark(trade, upcoming)}")
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
            e = (upcoming or {}).get(p.symbol)
            report = f" · 📅 earnings {e.date:%b %d}" if e else ""
            lines.append(f"▪️ {p.symbol} · {p.quantity:+g} sh{entry}{change}{report}")
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
