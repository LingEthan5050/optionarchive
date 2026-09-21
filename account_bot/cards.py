"""/positions as Discord embeds: a card per trade instead of a wall of text.

Same numbers as messages.positions_detail() - that stays as the plain-text
version and the one the tests pin down - laid out for reading at a glance:

  * a Portfolio card: theta per day, beta-weighted delta, trade count;
  * one card per option trade, its edge coloured by where it stands against
    the 28/21-DTE rule, with profit, greeks, IV rank and earnings as
    labelled columns, the legs as an aligned table, and your latest note;
  * one Shares card, as an aligned table.

Colour is never the only signal: the same 🔴/🟡 flag and the days left are
in each card's title, for anyone who can't tell the edges apart.
"""

from __future__ import annotations

from datetime import datetime

import discord

from account_bot import greeks, history, payoff
from account_bot.account import EASTERN
from account_bot.earnings import affects, from_metrics
from account_bot.market import Snapshot
from account_bot.messages import MANAGE, SOON, _flag, _signed_money
from account_bot.rules import Trade, group_trades

#: Edge colours: the reference palette's red and yellow for the two DTE
#: states, and a quiet neutral for everything else so the flagged ones stand
#: out. Titles carry the same 🔴/🟡, so none of these is the only signal.
EDGE = {"🔴": 0xE34948, "🟡": 0xEDA100, "▫️": 0x5B6573}
PORTFOLIO_EDGE = 0x2A78D6

#: Discord's per-message limits: 10 embeds and 6000 characters across them.
MAX_EMBEDS = 10
MAX_CHARS = 6000


def _profit(trade: Trade, mids: dict[str, float]) -> str:
    result = trade.pnl(mids)
    if result is None:
        return "—"
    kind, share = result
    return f"**{share:.0%}** of max" if kind == "credit" else f"**{share:+.0%}** return"


def _legs_table(trade: Trade, mids: dict[str, float]) -> str:
    """Monospace, so opened / now line up down the column."""
    rows = []
    for p in sorted(trade.legs, key=lambda p: (p.option_type != "P", p.strike or 0)):
        side = "short" if p.quantity < 0 else "long"
        leg = f"{abs(p.quantity):g} {p.strike:g}{p.option_type}"
        opened = f"{p.average_open_price:.2f}" if p.average_open_price is not None else "—"
        now = f"{mids[p.symbol]:.2f}" if p.symbol in mids else "—"
        rows.append((side, leg, opened, now))
    width = max(len(r[1]) for r in rows)
    lines = [f"{'':5} {'':{width}}  {'open':>6}  {'now':>6}"]
    lines += [f"{s:5} {l:{width}}  {o:>6}  {n:>6}" for s, l, o, n in rows]
    return "```\n" + "\n".join(lines) + "\n```"


def _ivr(trade: Trade, snap: Snapshot) -> str | None:
    now = snap.metric(trade.underlying, "implied-volatility-index-rank")
    if now is None:
        return None
    opened = min((p.opened_on for p in trade.legs if p.opened_on), default=None)
    found = history.ivr_on(snap.archive, trade.underlying, opened) if (
        snap.archive and opened) else None
    return f"{now:.0%} · entry {found[0]:.0%}" if found else f"{now:.0%} · entry n/a"


def _when(days: int) -> str:
    if days <= 0:
        return "**expires today**"
    return "**1 day left**" if days == 1 else f"**{days} days left**"


def _money(value: float | None) -> str:
    return "unlimited" if value is None else f"${value:,.0f}"


def _covered(trade: Trade, snap: Snapshot) -> bool:
    """Short calls with at least as many shares behind them in the same
    account: the payoff math sees only the options, so without this a
    covered call would read as unlimited risk."""
    short_calls = -sum(p.quantity * p.multiplier for p in trade.legs
                       if p.option_type == "C" and p.quantity < 0)
    shares = sum(p.quantity for p in snap.positions
                 if not p.is_option and p.symbol == trade.underlying
                 and p.account == trade.account)
    return short_calls > 0 and shares >= short_calls


def _underlying_line(trade: Trade, snap: Snapshot) -> str:
    spot = snap.spots.get(trade.underlying)
    if spot is None:
        return ""
    prev = snap.prev_close.get(trade.underlying)
    change = f" ({(spot - prev) / prev:+.1%} today)" if prev else ""
    return f"\n{trade.underlying} **{spot:,.2f}**{change}"


def trade_card(trade: Trade, snap: Snapshot, now: datetime, upcoming: dict,
               notes=None, soon: int = SOON, manage: int = MANAGE) -> discord.Embed:
    days = trade.days_left(snap.today)
    flag = _flag(days, soon, manage)
    expiries = " & ".join(f"{e:%b %d}" for e in sorted({p.expires for p in trade.legs}))
    strikes = " / ".join(f"{p.strike:g}{p.option_type}" for p in
                         sorted(trade.legs, key=lambda p: (p.option_type != "P", p.strike or 0)))
    card = discord.Embed(
        title=f"{flag} {trade.underlying} · {expiries}",
        description=f"{strikes}  ·  {_when(days)}{_underlying_line(trade, snap)}",
        colour=EDGE[flag],
    )
    spot = snap.spots.get(trade.underlying)
    vol = greeks.trade_iv(trade, snap, now)
    t = greeks.years_to_expiry(trade.expires, now)
    shape = payoff.analyse(trade)

    # Row 1: how it's doing, and how far through its life it is.
    dollars = trade.open_pnl(snap.mids)
    card.add_field(name="Profit", value=_profit(trade, snap.mids)
                   + (f"\n{_signed_money(dollars)}" if dollars is not None else ""))
    opened = min((p.opened_on for p in trade.legs if p.opened_on), default=None)
    if opened and trade.expires > opened:
        held, life = (snap.today - opened).days, (trade.expires - opened).days
        card.add_field(name="Time", value=f"held {held} of {life} days\n**{held / life:.0%}** of its life")
    else:
        card.add_field(name="Time", value="—")
    pop = (payoff.probability_of_profit(shape, spot, vol, t, snap.rate,
                                        greeks.dividend_yield(snap, trade.underlying))
           if shape and spot and vol else None)
    card.add_field(name="Prob. of profit", value=f"≈ **{pop:.0%}**" if pop is not None else "—")

    # Row 2: the shape of the risk.
    if shape:
        loss = "covered by shares" if shape.max_loss is None and _covered(trade, snap) \
            else _money(shape.max_loss)
        card.add_field(name="Max profit / loss", value=f"{_money(shape.max_profit)} / {loss}")
        card.add_field(name="Breakeven", value=" · ".join(f"{b:,.2f}" for b in shape.breakevens) or "—")
    else:
        card.add_field(name="Max profit / loss", value="n/a · legs expire on different dates")
        card.add_field(name="Breakeven", value="—")
    if spot and vol and t > 0:
        move = payoff.expected_move(spot, vol, t)
        card.add_field(name="Expected move", value=f"±${move:,.2f} ({move / spot:.1%})\nby expiration")
    else:
        card.add_field(name="Expected move", value="—")

    # Row 3: exposure and context.
    exposure = greeks.trade(trade, snap, now)
    card.add_field(name="Δ · Θ per day", value=(
        f"{exposure.delta:+.0f} sh · {_signed_money(exposure.theta)}" if exposure else "—"))
    card.add_field(name="IV rank", value=_ivr(trade, snap) or "—")
    report = affects(trade, upcoming)
    card.add_field(name="📅 Earnings", value=(f"{report.date:%a %b %d}\n{report.timing}"
                                              if report else "none before expiry"))

    # How much room each short strike has - the tested-side check.
    shorts = [p for p in trade.legs if p.quantity < 0 and p.strike]
    if shorts and spot and vol and t > 0:
        room = []
        for p in sorted(shorts, key=lambda p: (p.option_type != "P", p.strike)):
            gap = (spot - p.strike) / spot if p.option_type == "P" else (p.strike - spot) / spot
            if abs(gap) < 0.0005:
                room.append(f"short {p.strike:g}{p.option_type}: **at the money**")
                continue
            state = "OTM" if gap > 0 else "**ITM**"
            room.append(f"short {p.strike:g}{p.option_type}: {abs(gap):.1%} {state} · "
                        f"{payoff.sigmas_away(spot, p.strike, vol, t):.1f}σ")
        card.add_field(name="Room to short strikes", value="\n".join(room), inline=False)

    card.add_field(name="Legs", value=_legs_table(trade, snap.mids), inline=False)
    if notes is not None:
        mine = notes.notes_for(trade.underlying, opened.isoformat() if opened else None)
        if mine:
            latest = mine[-1]
            card.add_field(name="📝 Your note",
                           value=f"{latest['text'][:900]}\n*{latest['at'][:10]}*",
                           inline=False)
    card.set_footer(text=f"{trade.account}  ·  probability is market-implied, from the options' IV")
    return card


def portfolio_card(snap: Snapshot, now: datetime, trade_count: int) -> discord.Embed:
    pf = greeks.portfolio(snap, now)
    card = discord.Embed(title="Portfolio", colour=PORTFOLIO_EDGE)

    current = {b.account: b.net_liquidating_value for b in snap.balances
               if b.net_liquidating_value is not None}
    card.add_field(name="Net liq", value=f"**${sum(current.values()):,.0f}**" if current else "—")
    common = [a for a in current if a in snap.prior_net_liq]
    before = sum(snap.prior_net_liq[a] for a in common)
    if common and before:
        change = sum(current[a] for a in common) - before
        card.add_field(name="Day P/L", value=f"**{_signed_money(change)}**\n{change / before:+.2%}")
    else:
        card.add_field(name="Day P/L", value="—")
    open_parts = [t.open_pnl(snap.mids) for t in group_trades(snap.positions)]
    open_parts += [(snap.mids[p.symbol] - p.average_open_price) * p.quantity
                   for p in snap.positions if not p.is_option
                   and p.symbol in snap.mids and p.average_open_price is not None]
    known = [v for v in open_parts if v is not None]
    card.add_field(name="Open P/L", value=f"**{_signed_money(sum(known))}**" if known else "—")

    card.add_field(name="Θ per day", value=f"**{_signed_money(pf.theta)}**")
    card.add_field(name="β-weighted Δ", value=(
        f"**{pf.beta_delta:+.0f}** SPY sh" if pf.beta_delta is not None else "—"))
    card.add_field(name="Option trades", value=f"**{trade_count}**")
    footer = ("Day P/L: net liq vs last close, deposits included. Open P/L: since entry, "
              "at live mids. Θ: dollars/day from time decay. β-weighted Δ: the portfolio as SPY shares.")
    if pf.skipped:
        footer += f"  Not in greeks: {', '.join(pf.skipped)}."
    card.set_footer(text=footer)
    return card


def shares_card(snap: Snapshot) -> discord.Embed | None:
    held = sorted((p for p in snap.positions if not p.is_option),
                  key=lambda p: (p.account, p.symbol))
    if not held:
        return None
    rows = [("", "qty", "entry", "now", "P/L", "$ P/L", "today", "")]
    for p in held:
        now = snap.mids.get(p.symbol)
        entry = p.average_open_price
        pct = dollars = today = "—"
        if now is not None and entry:
            side = 1 if p.quantity > 0 else -1
            pct = f"{(now - entry) / entry * side:+.1%}"
            dollars = f"{(now - entry) * p.quantity:+,.0f}"
        prev = snap.prev_close.get(p.symbol)
        if now is not None and prev:
            today = f"{(now - prev) / prev:+.1%}"
        rows.append((p.symbol, f"{p.quantity:+g}", f"{entry:.2f}" if entry else "—",
                     f"{now:.2f}" if now is not None else "—", pct, dollars, today,
                     p.account[-5:]))
    widths = [max(len(r[i]) for r in rows) for i in range(8)]
    lines = ["  ".join(r[0].ljust(widths[0]) if i == 0 else
                       (c.rjust(widths[i]) if i < 7 else c)
                       for i, c in enumerate(r)).rstrip() for r in rows]
    return discord.Embed(title="Shares", colour=PORTFOLIO_EDGE,
                         description="```\n" + "\n".join(lines) + "\n```")


def positions_cards(snap: Snapshot, now: datetime | None = None, notes=None,
                    soon: int = SOON, manage: int = MANAGE) -> list[discord.Embed]:
    now = now or datetime.now(EASTERN)
    upcoming = from_metrics(snap.metrics, snap.today)
    trades = group_trades(snap.positions)
    if not snap.positions:
        return [discord.Embed(title="No open positions", colour=PORTFOLIO_EDGE)]
    cards = [portfolio_card(snap, now, len(trades))]
    # Most urgent first: the trade you need to look at leads.
    cards += [trade_card(t, snap, now, upcoming, notes, soon, manage)
              for t in sorted(trades, key=lambda t: t.expires)]
    shares = shares_card(snap)
    if shares:
        cards.append(shares)
    cards[-1].set_footer(text=((cards[-1].footer.text + "  ·  ") if cards[-1].footer.text else "")
                         + "Only you can see this. Live mid prices.")
    return cards


def batches(cards: list[discord.Embed]) -> list[list[discord.Embed]]:
    """Group cards into messages within Discord's per-message limits."""
    out, current, size = [], [], 0
    for card in cards:
        if current and (len(current) == MAX_EMBEDS or size + len(card) > MAX_CHARS):
            out.append(current)
            current, size = [], 0
        current.append(card)
        size += len(card)
    if current:
        out.append(current)
    return out
