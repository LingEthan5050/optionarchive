"""The Discord bot: private slash commands, a twice-daily summary and alerts.

    python -m account_bot

  /positions   every open trade with profit %, days left and entry prices
  /balance     net liq, cash and buying power per account, and the total
  /expiring    the summary right now, with dollar amounts (only you see it)
  /alerttest   post a test message where alerts go

Every command reply is EPHEMERAL - only you see it and it is not saved to the
chat - and the bot answers one Discord user, DISCORD_OWNER_ID, and nobody
else. What it posts unprompted - the summary and the alerts - goes to the
ALERT_CHANNEL text channel (default #options), falling back to a DM if that
channel is missing, and carries no money figures (see messages.py). Anyone
who can read that channel can read those posts.

Rule-of-thumb alerts (rules.py) are checked four times per trading day and
posted once each: 28 and 21 DTE, and 50% of max profit on short premium.

The summary - day P/L and every option trade by days left - is posted on
NYSE trading days at SUMMARY_TIMES Eastern (default 10:00 and 16:00). It
shows percentages only unless SUMMARY_DOLLARS is on, because the channel it
posts to may be readable by others; /expiring shows the dollar version to
you alone.

The tastytrade calls are synchronous httpx, so they run in a worker thread;
calling them directly would stall the Discord connection's heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

from account_bot import account, messages, rules
from chain_archiver import calendar as trading_calendar
from chain_archiver.auth import TastytradeClient
from chain_archiver.config import Settings
from chain_archiver.fetch import fetch_option_quotes

log = logging.getLogger("account_bot")

#: When the summary is posted, Eastern: half an hour after the open, once
#: the first half hour's wide spreads have settled, and at the close.
DEFAULT_SUMMARY_TIMES = (time(10, 0), time(16, 0))

#: When the rule-of-thumb alerts are checked, Eastern. Inside market hours so
#: an alert arrives while you can still act on it; the first check leaves the
#: open's wide spreads fifteen minutes to settle before quotes are trusted.
ALERT_CHECKS = (time(9, 45), time(12, 0), time(14, 0), time(15, 30))


class AccountBot(discord.Client):
    def __init__(self, settings: Settings, owner_id: int, guild_id: int | None,
                 summary_times: tuple[time, ...] = DEFAULT_SUMMARY_TIMES,
                 dte_alerts: tuple[int, ...] = rules.DEFAULT_DTE_ALERTS,
                 profit_target: float = rules.DEFAULT_PROFIT_TARGET,
                 alert_channel: str = "options",
                 summary_dollars: bool = False) -> None:
        # Guilds only. Slash commands arrive as interactions, and discord.py
        # needs the (unprivileged) guilds intent to keep its state straight;
        # the bot has no reason to see messages, members or presence.
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.summary_times = tuple(t.replace(tzinfo=account.EASTERN)
                                   for t in summary_times)
        self.summary_dollars = summary_dollars
        self.tree = app_commands.CommandTree(self)
        self.dte_alerts = dte_alerts
        self.profit_target = profit_target
        self.alert_channel = alert_channel
        self.alert_log = rules.AlertLog(settings.data_dir / "bot" / "alerts.json")

    # -- tastytrade, off the event loop -----------------------------------

    async def fetch(self, *, balances: bool = False):
        def work():
            client = TastytradeClient(self.settings)
            try:
                accts = account.accounts(client)
                if balances:
                    return account.balances(client, accts)
                return account.positions(client, accts)
            finally:
                client.close()
        return await asyncio.to_thread(work)

    async def fetch_with_mids(self) -> tuple[list, dict[str, float]]:
        def work():
            client = TastytradeClient(self.settings)
            try:
                held = account.positions(client, account.accounts(client))
                return held, _mids(client, held)
            finally:
                client.close()
        return await asyncio.to_thread(work)

    async def fetch_summary(self) -> dict:
        """Everything the summary needs, in one authenticated session."""
        def work():
            client = TastytradeClient(self.settings)
            try:
                accts = account.accounts(client)
                held = account.positions(client, accts)
                return {
                    "positions": held,
                    "mids": _mids(client, held),
                    "balances": account.balances(client, accts),
                    "prior": account.prior_net_liq(
                        client, accts, _previous_trading_day(account.today_eastern())),
                }
            finally:
                client.close()
        return await asyncio.to_thread(work)

    def render_summary(self, data: dict, stamp: str, dollars: bool) -> str:
        return messages.summary(
            data["positions"], data["mids"], data["balances"], data["prior"],
            account.today_eastern(), stamp, dollars,
            soon=max(self.dte_alerts), manage=min(self.dte_alerts),
        )

    # -- replies ----------------------------------------------------------

    async def reply(self, interaction: discord.Interaction, build) -> None:
        """Owner check, then an ephemeral reply built from fresh data."""
        if interaction.user.id != self.owner_id:
            log.warning("Refused /%s from user %s", interaction.command.name,
                        interaction.user.id)
            await interaction.response.send_message(
                "This bot is private.", ephemeral=True)
            return
        # Deferring buys up to 15 minutes; tastytrade takes a few seconds,
        # past Discord's 3-second window for an immediate answer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await build()
        except Exception as exc:  # noqa: BLE001 - surface it, don't crash
            log.exception("/%s failed", interaction.command.name)
            text = f"Couldn't reach tastytrade ({type(exc).__name__}). Try again shortly."
        for part in messages.chunk(text):
            await interaction.followup.send(part, ephemeral=True)

    # -- lifecycle -----------------------------------------------------------

    async def setup_hook(self) -> None:
        @self.tree.command(description="Open trades with profit %, days left and entry prices")
        async def positions(interaction: discord.Interaction) -> None:
            async def build():
                held, mids = await self.fetch_with_mids()
                return messages.positions_detail(held, account.today_eastern(), mids)
            await self.reply(interaction, build)

        @self.tree.command(description="Balances and buying power (only you see this)")
        async def balance(interaction: discord.Interaction) -> None:
            async def build():
                return messages.balance(await self.fetch(balances=True))
            await self.reply(interaction, build)

        @self.tree.command(description="Summary now: day P/L and options by days left")
        async def expiring(interaction: discord.Interaction) -> None:
            async def build():
                data = await self.fetch_summary()
                now = datetime.now(account.EASTERN)
                return self.render_summary(data, f"{now:%H:%M} ET", dollars=True)
            await self.reply(interaction, build)

        @self.tree.command(description="Post a test message where alerts go")
        async def alerttest(interaction: discord.Interaction) -> None:
            async def build():
                channel = self.find_alert_channel()
                sent = await self.post("🔔 Test: rule-of-thumb alerts and the "
                                       "daily summary will appear here.")
                if not sent:
                    return "Couldn't post the test anywhere - check bot.log."
                return (f"Posted in {channel.mention}." if channel else
                        f"No #{self.alert_channel} channel found, so it went to your DMs.")
            await self.reply(interaction, build)

        # A guild sync is instant; a global one can take up to an hour to
        # appear, which looks exactly like the bot being broken.
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        self.daily = tasks.loop(time=list(self.summary_times))(self.send_summary)
        self.daily.before_loop(self.wait_until_ready)
        self.daily.start()

        checks = [c.replace(tzinfo=account.EASTERN) for c in ALERT_CHECKS]
        self.alerts = tasks.loop(time=checks)(self.check_alerts)
        self.alerts.before_loop(self.wait_until_ready)
        self.alerts.start()

    async def on_ready(self) -> None:
        log.info("Connected as %s; summaries at %s ET (%s)", self.user,
                 ", ".join(f"{t:%H:%M}" for t in self.summary_times),
                 "with dollar amounts" if self.summary_dollars else "percentages only")
        if self.find_alert_channel():
            log.info("Alerts and summaries post in #%s", self.alert_channel)
        else:
            log.warning("No #%s channel found; alerts will be DM'd instead",
                        self.alert_channel)

    async def send_summary(self) -> None:
        today = account.today_eastern()
        if not trading_calendar.is_trading_day(today):
            return
        try:
            data = await self.fetch_summary()
        except Exception:  # noqa: BLE001
            log.exception("Summary: could not fetch account data")
            return
        now = datetime.now(account.EASTERN)
        stamp = f"{now:%H:%M} ET" + (" · close" if now.hour >= 16 else "")
        if await self.post(self.render_summary(data, stamp, self.summary_dollars)):
            log.info("Summary sent: %d option trade(s)",
                     len(rules.group_trades(data["positions"])))

    async def dm_owner(self, text: str) -> bool:
        try:
            owner = self.get_user(self.owner_id) or await self.fetch_user(self.owner_id)
            for part in messages.chunk(text):
                await owner.send(part)
            return True
        except discord.HTTPException:
            # Usually: you and the bot share no server, or DMs from server
            # members are switched off in your privacy settings.
            log.exception("Could not DM the owner")
            return False

    def find_alert_channel(self) -> discord.TextChannel | None:
        guild = self.get_guild(self.guild_id) if self.guild_id else None
        if guild is None:
            return None
        return discord.utils.get(guild.text_channels, name=self.alert_channel)

    async def post(self, text: str) -> bool:
        """Post an automated message to the alert channel, or DM the owner if
        the channel is missing or refuses the bot. An alert that silently
        goes nowhere is worse than one that arrives somewhere unexpected."""
        channel = self.find_alert_channel()
        if channel is not None:
            try:
                for part in messages.chunk(text):
                    await channel.send(part)
                return True
            except discord.HTTPException:
                log.exception("Could not post in #%s; falling back to DM",
                              self.alert_channel)
        else:
            log.warning("No #%s channel in the server; falling back to DM",
                        self.alert_channel)
        return await self.dm_owner(text)

    async def check_alerts(self) -> None:
        """Rule-of-thumb alerts: 28/21 DTE and 50% of max profit."""
        today = account.today_eastern()
        if not trading_calendar.is_trading_day(today):
            return
        try:
            held, mids = await self.fetch_with_mids()
        except Exception:  # noqa: BLE001
            log.exception("Alerts: could not fetch positions")
            return
        trades = rules.group_trades(held)
        due = rules.evaluate(trades, mids, today, self.dte_alerts, self.profit_target)
        new = self.alert_log.new(due)
        loud = [a for a in new if a.text]
        if loud:
            body = "\n".join(a.text for a in loud)
            body += "\n-# A rule-of-thumb reminder, not a recommendation."
            if not await self.post(body):
                return  # not recorded, so the next check tries again
        # Record even when nothing was loud: silent keys and pruning of
        # closed trades still need saving.
        self.alert_log.record(new, trades)
        log.info("Alerts: %d trade(s) checked, %d alert(s) sent", len(trades), len(loud))


def _mids(client: TastytradeClient, held: list) -> dict[str, float]:
    """Live mid prices for every option and stock position held."""
    symbols = [p.symbol for p in held if p.is_option]
    quotes = fetch_option_quotes(client, symbols) if symbols else {}
    stocks = sorted({p.symbol for p in held if p.instrument_type == "Equity"})
    if stocks:
        data = client.get("/market-data/by-type", params={"equity": ",".join(stocks)})
        for item in data.get("items") or []:
            if item.get("symbol"):
                quotes[item["symbol"]] = item
    mids = {}
    for symbol, q in quotes.items():
        try:
            mids[symbol] = (float(q["mid"]) if q.get("mid") not in (None, "")
                            else (float(q["bid"]) + float(q["ask"])) / 2)
        except (KeyError, TypeError, ValueError):
            continue
    return mids


def _previous_trading_day(day: date) -> date:
    day -= timedelta(days=1)
    while not trading_calendar.is_trading_day(day):
        day -= timedelta(days=1)
    return day


def _times(raw: str, default: tuple[time, ...]) -> tuple[time, ...]:
    """"10:00,16:00" -> (time(10, 0), time(16, 0))."""
    parsed = []
    for part in raw.split(","):
        if part.strip():
            hour, minute = part.strip().split(":")
            parsed.append(time(int(hour), int(minute)))
    return tuple(parsed) or default


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Quiet the per-request httpx lines: they name account URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = Settings.from_env()  # also loads .env into os.environ
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    owner = os.environ.get("DISCORD_OWNER_ID", "").strip()
    guild = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not token or not owner.isdigit():
        log.error("Set DISCORD_BOT_TOKEN and DISCORD_OWNER_ID in .env "
                  "(see .env.example).")
        return 1

    dte = tuple(int(x) for x in os.environ.get("ALERT_DTE", "").split(",") if x.strip()) \
        or rules.DEFAULT_DTE_ALERTS
    target = float(os.environ.get("PROFIT_TARGET") or rules.DEFAULT_PROFIT_TARGET)

    channel = os.environ.get("ALERT_CHANNEL", "").strip().lstrip("#") or "options"

    times = _times(os.environ.get("SUMMARY_TIMES", ""), DEFAULT_SUMMARY_TIMES)
    dollars = os.environ.get("SUMMARY_DOLLARS", "").strip().lower() in ("1", "true", "yes", "on")

    bot = AccountBot(settings, int(owner), int(guild) if guild.isdigit() else None,
                     times, dte, target, channel, dollars)
    bot.run(token, log_handler=None)
    return 0
