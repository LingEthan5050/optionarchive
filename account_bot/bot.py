"""The Discord bot: two private slash commands and a daily expiration reminder.

    python -m account_bot

  /positions   every open position with days left and entry prices
  /balance     net liq, cash and buying power per account, and the total
  /expiring    the daily reminder, on demand

Every command reply is EPHEMERAL - only you see it and it is not saved to the
chat - and the bot answers one Discord user, DISCORD_OWNER_ID, and nobody
else. The only thing it ever posts is the reminder, which carries no money
figures (see messages.py for the two tiers).

The reminder goes out once per NYSE trading day at REMINDER_TIME Eastern
(default 16:30: after the close, and after the 16:05 greeks run), and only
when there is at least one option position to remind you about.

The tastytrade calls are synchronous httpx, so they run in a worker thread;
calling them directly would stall the Discord connection's heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import time

import discord
from discord import app_commands
from discord.ext import tasks

from account_bot import account, messages
from chain_archiver import calendar as trading_calendar
from chain_archiver.auth import TastytradeClient
from chain_archiver.config import Settings

log = logging.getLogger("account_bot")

DEFAULT_REMINDER = time(16, 30)


class AccountBot(discord.Client):
    def __init__(self, settings: Settings, owner_id: int, guild_id: int | None,
                 reminder_at: time) -> None:
        # Guilds only. Slash commands arrive as interactions, and discord.py
        # needs the (unprivileged) guilds intent to keep its state straight;
        # the bot has no reason to see messages, members or presence.
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.reminder_at = reminder_at.replace(tzinfo=account.EASTERN)
        self.tree = app_commands.CommandTree(self)

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
        @self.tree.command(description="Open positions with days left and entry prices")
        async def positions(interaction: discord.Interaction) -> None:
            async def build():
                held = await self.fetch()
                return messages.positions_detail(held, account.today_eastern())
            await self.reply(interaction, build)

        @self.tree.command(description="Balances and buying power (only you see this)")
        async def balance(interaction: discord.Interaction) -> None:
            async def build():
                return messages.balance(await self.fetch(balances=True))
            await self.reply(interaction, build)

        @self.tree.command(description="Options sorted by days to expiration")
        async def expiring(interaction: discord.Interaction) -> None:
            async def build():
                held = await self.fetch()
                return (messages.reminder(held, account.today_eastern())
                        or "No open option positions.")
            await self.reply(interaction, build)

        # A guild sync is instant; a global one can take up to an hour to
        # appear, which looks exactly like the bot being broken.
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        self.daily = tasks.loop(time=self.reminder_at)(self.send_reminder)
        self.daily.before_loop(self.wait_until_ready)
        self.daily.start()

    async def on_ready(self) -> None:
        log.info("Connected as %s; reminder at %s ET", self.user,
                 self.reminder_at.strftime("%H:%M"))

    async def send_reminder(self) -> None:
        today = account.today_eastern()
        if not trading_calendar.is_trading_day(today):
            return
        try:
            held = await self.fetch()
        except Exception:  # noqa: BLE001
            log.exception("Reminder: could not fetch positions")
            return
        text = messages.reminder(held, today)
        if text is None:
            log.info("Reminder: no option positions, nothing sent")
            return
        try:
            owner = self.get_user(self.owner_id) or await self.fetch_user(self.owner_id)
            for part in messages.chunk(text):
                await owner.send(part)
            log.info("Reminder sent: %d option position(s)",
                     sum(p.is_option for p in held))
        except discord.HTTPException:
            # Usually: you and the bot share no server, or DMs from server
            # members are switched off in your privacy settings.
            log.exception("Reminder: could not DM the owner")


def _reminder_time() -> time:
    raw = os.environ.get("REMINDER_TIME", "").strip()
    if not raw:
        return DEFAULT_REMINDER
    hour, minute = raw.split(":")
    return time(int(hour), int(minute))


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

    bot = AccountBot(settings, int(owner), int(guild) if guild.isdigit() else None,
                     _reminder_time())
    bot.run(token, log_handler=None)
    return 0
