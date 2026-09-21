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
from datetime import date, datetime

from account_bot.account import EASTERN, Balance, Position
from account_bot.earnings import affects, from_metrics
from account_bot import greeks, history
from account_bot.market import Snapshot
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


def _portfolio_line(pf: greeks.Portfolio) -> list[str]:
    """Theta per day and beta-weighted delta, plus anything left out."""
    parts = [f"Θ **{_signed_money(pf.theta)}/day**"]
    if pf.beta_delta is not None:
        parts.append(f"β-weighted Δ **{pf.beta_delta:+.0f} SPY sh**")
    lines = ["Portfolio: " + " · ".join(parts)]
    if pf.skipped:
        lines.append(f"-# Not in these totals: {', '.join(pf.skipped)}")
    return lines


def summary(
    snap: Snapshot,
    stamp: str,
    dollars: bool,
    soon: int = SOON,
    manage: int = MANAGE,
    now: datetime | None = None,
) -> str:
    """The twice-daily summary: day P/L, then each option trade.

    Day P/L is the change in net liquidating value since the previous close,
    summed over the accounts that have a snapshot for it. It is complete in a
    way that adding up open positions is not - a trade closed today still
    counts - but a deposit or withdrawal counts too.

    dollars=False (the posted default) shows percentages only. The ephemeral
    /expiring reply passes True.
    """
    positions, mids, today = snap.positions, snap.mids, snap.today
    balances, prior_net_liq = snap.balances, snap.prior_net_liq
    upcoming = from_metrics(snap.metrics, today)
    lines = [f"**Options summary — {today:%a %b %d} · {stamp}**"]

    current = {b.account: b.net_liquidating_value for b in balances
               if b.net_liquidating_value is not None}
    common = [a for a in current if a in prior_net_liq]
    before = sum(prior_net_liq[a] for a in common)
    if common and before:
        change = sum(current[a] for a in common) - before
        pct = f"{change / before:+.2%}"
        figure = f"**{_signed_money(change)}** ({pct})" if dollars else f"**{pct}**"
        lines.append(f"Day P/L: {figure} across {len(common)} account"
                     f"{'s' if len(common) != 1 else ''}")
    else:
        lines.append("Day P/L: — (no prior-close snapshot)")

    if dollars:
        # Theta is a dollar figure, so it follows the same gate as day P/L.
        lines += _portfolio_line(greeks.portfolio(snap, now or datetime.now(EASTERN)))

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


def _ivr_text(trade, snap: Snapshot) -> str | None:
    """"IVR 28% (entry 41%)". IV rank arrives as a decimal (0.28 = 28%) - the
    scale trap the archive's schema warns about."""
    now = snap.metric(trade.underlying, "implied-volatility-index-rank")
    if now is None:
        return None
    text = f"IVR {now:.0%}"
    opened = min((p.opened_on for p in trade.legs if p.opened_on), default=None)
    found = history.ivr_on(snap.archive, trade.underlying, opened) if (
        snap.archive and opened) else None
    if found:
        value, on = found
        text += f" (entry {value:.0%}" + (f", as of {on:%b %d}" if on != opened else "") + ")"
    else:
        text += " (entry n/a)"
    return text


def positions_detail(snap: Snapshot, now: datetime | None = None,
                     notes=None) -> str:
    """EPHEMERAL only: every trade with its profit, legs and entry prices.

    Option legs are grouped into trades (rules.group_trades), because profit
    only means something for the whole structure. A credit trade shows the
    share of max profit captured - the number tastytrade's 50% rule uses -
    and a debit trade shows its return on what was paid. The two are not the
    same scale: the first tops out at 100%, the second has no ceiling.
    """
    positions, mids, today = snap.positions, snap.mids, snap.today
    upcoming = from_metrics(snap.metrics, today)
    now = now or datetime.now(EASTERN)
    if not positions:
        return "No open positions."
    by_account: dict[str, list[Position]] = defaultdict(list)
    for p in positions:
        by_account[p.account].append(p)

    lines = _portfolio_line(greeks.portfolio(snap, now)) + [""]
    for account, held in by_account.items():
        lines.append(f"**{account}**")
        for trade in group_trades(held):
            days = trade.days_left(today)
            result = trade.pnl(mids)
            profit = f" · {_pct(*result)}" if result else " · profit —"
            lines.append(f"{_flag(days)} {trade.describe()} — {_when(days)}{profit}"
                         f"{_earnings_mark(trade, upcoming)}")
            detail = []
            exposure = greeks.trade(trade, snap, now)
            if exposure is not None:
                detail.append(f"Δ {exposure.delta:+.0f} sh · Θ {_signed_money(exposure.theta)}/day")
            ivr = _ivr_text(trade, snap)
            if ivr:
                detail.append(ivr)
            if detail:
                lines.append("  " + " · ".join(detail))
            if notes is not None:
                opened = min((p.opened_on for p in trade.legs if p.opened_on), default=None)
                for n in notes.notes_for(trade.underlying,
                                         opened.isoformat() if opened else None)[-2:]:
                    lines.append(f"  📝 {n['at'][:10]}: {n['text']}")
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
    lines.append("-# Credit trades: share of max profit. Debit trades and stock: "
                 "return on cost. Δ in shares of the underlying; β-weighted Δ as "
                 "SPY shares. Θ: dollars per day from time decay. IVR at entry "
                 "comes from the archive, where it covers the symbol and date. "
                 "Live mids.")
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


#: How to use each slash command. help_text() takes the command names and
#: descriptions from the live command tree, so it cannot list a command that
#: doesn't exist; this adds the example and what to expect. A command
#: registered without an entry here fails the help test rather than showing
#: up bare.
USAGE = {
    "positions": ("/positions",
                  "Every trade: days left, profit %, Δ/Θ, IV rank now vs entry, your notes. "
                  "Opens with portfolio Θ per day and β-weighted Δ."),
    "balance": ("/balance", "Net liq, cash and buying power for each account, and the total."),
    "expiring": ("/expiring",
                 "The summary right now, in dollars: day P/L, portfolio greeks, each trade."),
    "note": ("/note symbol:SPY note:selling premium, IVR 60",
             "Save why you opened a trade. Shows under it in /positions and in the recap."),
    "recap": ("/recap", "This week's closed trades with their last-seen profit and your notes."),
    "alerttest": ("/alerttest", "Posts a test message where alerts go, to check the channel works."),
    "help": ("/help", "This list."),
}


def help_text(commands: list[tuple[str, str]], summary_times: list[str],
              alert_times: list[str], channel: str, soon: int, manage: int,
              profit_target: float, tested_buffer: float) -> str:
    """EPHEMERAL: every command with an example, then what runs on its own."""
    lines = ["**Options bot — commands**",
             "-# Replies are only visible to you and aren't saved to the chat.", ""]
    # In USAGE's order - most used first - with anything unlisted at the end.
    rank = {name: i for i, name in enumerate(USAGE)}
    for name, description in sorted(commands, key=lambda c: (rank.get(c[0], len(rank)), c[0])):
        example, detail = USAGE.get(name, (f"/{name}", description))
        lines.append(f"`{example}`")
        lines.append(f"  {detail}")
    lines += [
        "",
        f"**Automatic** (NYSE trading days, Eastern time)",
        f"• **Summary** {', '.join(summary_times)} in #{channel}: day P/L and each "
        f"trade by days left (percentages; dollars via /expiring).",
        f"• **Alerts** checked {', '.join(alert_times)} in #{channel}, each sent once:",
        f"  🟡 {soon} days to expiration · 🔴 {manage} days: manage (close or roll)",
        f"  ✅ {profit_target:.0%} of max profit on a credit trade",
        f"  🎯 stock within {tested_buffer:.0%} of, or through, a short strike",
        f"  📅 earnings before a trade expires, or on shares within a week · "
        f"⚠️ last session before the report",
        "• **Weekly recap** on the week's last trading day at 16:15, by DM.",
    ]
    return "\n".join(lines)

