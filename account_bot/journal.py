"""A trading journal: your notes, and how each trade turned out.

The point is the loop. /note records WHY you opened a trade, in your own
words, when you opened it. The weekly recap sets that reasoning next to how
the trade ended. After a few months that is the only record here that can
tell you whether your judgment is improving or you have been paid for luck.

Nothing here comes from tastytrade's order history. The alert check observes
open trades four times a day; a trade that stops appearing is recorded as
closed on the day it disappeared, with its profit as LAST SEEN - within a
few hours of the close, not the exact fill. Expiration looks the same as a
close, and is labelled "closed or expired".

Stored as JSON under the archive's data directory, next to the alert log.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from account_bot.market import Snapshot
from account_bot.rules import group_trades
from chain_archiver import calendar as trading_calendar


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def last_trading_day_of_week(day: date) -> bool:
    if not trading_calendar.is_trading_day(day):
        return False
    after = day + timedelta(days=1)
    while not trading_calendar.is_trading_day(after):
        after += timedelta(days=1)
    return _week_start(after) != _week_start(day)


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        self.notes: list[dict] = data.get("notes", [])
        self.open: dict[str, dict] = data.get("open", {})
        self.closed: list[dict] = data.get("closed", [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"notes": self.notes, "open": self.open, "closed": self.closed},
            indent=1, default=str))

    # -- writing ------------------------------------------------------------

    def add_note(self, symbol: str, text: str, when: datetime) -> dict:
        note = {"symbol": symbol.upper().strip(), "text": text.strip(),
                "at": when.isoformat(timespec="minutes")}
        self.notes.append(note)
        self.save()
        return note

    def observe(self, snap: Snapshot) -> list[dict]:
        """Record every open option trade; move the ones that vanished to
        closed. Returns the newly closed."""
        now = {}
        for trade in group_trades(snap.positions):
            result = trade.pnl(snap.mids)
            opened = min((p.opened_on for p in trade.legs if p.opened_on), default=None)
            now[trade.key] = {
                "describe": trade.describe(),
                "underlying": trade.underlying,
                "account": trade.account,
                "opened": opened.isoformat() if opened else None,
                "expires": trade.expires.isoformat(),
                # Keep the last good reading if this check had no quote.
                "pnl": list(result) if result else self.open.get(trade.key, {}).get("pnl"),
                "seen": snap.today.isoformat(),
            }
        gone = []
        for key, record in self.open.items():
            if key not in now:
                gone.append({**record, "closed": snap.today.isoformat()})
        self.closed.extend(gone)
        self.open = now
        self.save()
        return gone

    # -- reading ------------------------------------------------------------

    def notes_for(self, symbol: str, since: str | None) -> list[dict]:
        return [n for n in self.notes if n["symbol"] == symbol
                and (since is None or n["at"][:10] >= since)]

    def recap(self, today: date) -> str:
        start = _week_start(today)
        closed = [c for c in self.closed if c["closed"] >= start.isoformat()]
        lines = [f"**Weekly recap — week of {start:%b %d}**"]

        wins = sum(1 for c in closed if c["pnl"] and c["pnl"][1] > 0)
        losses = sum(1 for c in closed if c["pnl"] and c["pnl"][1] <= 0)
        lines.append(f"{len(closed)} closed or expired · {wins} up · {losses} flat or down"
                     f" · {len(self.open)} still open")

        for c in closed:
            lines.append("")
            lines.append(f"**{c['describe']}** — closed or expired {c['closed']}, "
                         f"{_result(c['pnl'])}")
            for n in self.notes_for(c["underlying"], c.get("opened")):
                lines.append(f"  📝 {n['at'][:10]}: {n['text']}")
        if not closed:
            lines.append("Nothing closed this week.")

        undated = [n for n in self.notes if n["at"][:10] >= start.isoformat()
                   and not any(n["symbol"] == c["underlying"] for c in closed)]
        if undated:
            lines.append("")
            lines.append("**Notes this week on trades still open**")
            for n in undated:
                lines.append(f"  📝 {n['symbol']} {n['at'][:10]}: {n['text']}")
        lines.append("-# Profit is as last seen by the alert check, within a few "
                     "hours of the close - not the exact fill.")
        return "\n".join(lines)


def _result(pnl: list | None) -> str:
    if not pnl:
        return "profit unknown"
    kind, share = pnl
    if kind == "credit":
        return f"last seen at **{share:.0%} of max profit**"
    return f"last seen at **{share:+.0%} return**"
