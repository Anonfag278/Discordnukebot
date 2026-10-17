"""Server-only Discord test runner. Python 3.12+."""
import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
log = logging.getLogger("testbot")


@dataclass(frozen=True)
class Config:
    token: str
    message: str = "the bomb has been dropped"
    count: int = 100

    @classmethod
    def read(cls):
        load_dotenv(BASE / ".env")
        token = os.environ["DISCORD_TOKEN"].strip()
        message = os.getenv("NUKE_MESSAGE", "the bomb has been dropped")
        count = int(os.getenv("NUKE_MESSAGE_COUNT", "100"))
        if not token or token == "replace_with_bot_token":
            raise ValueError("Set DISCORD_TOKEN to your bot token")
        if not message.strip() or len(message) > 2000:
            raise ValueError("NUKE_MESSAGE must contain 1–2000 characters")
        if not 1 <= count <= 100:
            raise ValueError("NUKE_MESSAGE_COUNT must be between 1 and 100")
        return cls(token, message, count)


@dataclass
class Run:
    guild_id: int = 0
    state: str = "running"
    phase: str = "starting"
    renamed_channels: int = 0
    renamed_roles: int = 0
    planned: int = 0
    attempted: int = 0
    failures: int = 0
    error: str = ""
    messages: list[dict] = field(default_factory=list)

    def summary(self):
        return (f"State: {self.state}; phase: {self.phase}\n"
                f"Channels renamed: {self.renamed_channels}; "
                f"roles renamed: {self.renamed_roles}\nMessages sent: {len(self.messages)}/{self.planned}; "
                f"attempted: {self.attempted}; "
                f"failed actions: {self.failures}"
                + (f"\nError: {self.error}" if self.error else ""))


def in_server(interaction):
    return (interaction.guild_id is not None and interaction.guild is not None
            and isinstance(interaction.user, discord.Member))


class ServerTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if not in_server(interaction):
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error):
        log.error("Command failed: %s", type(error).__name__)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("Command failed. See the execution log.", ephemeral=True)


class TestBot(discord.Client):
    def __init__(self, config):
        super().__init__(intents=discord.Intents(guilds=True), allowed_mentions=discord.AllowedMentions.all())
        self.config = config
        self.tree = ServerTree(self)
        self.task = None
        self.starting = False
        self.progress = None
        self.register_commands()

    async def setup_hook(self):
        await self.tree.sync()

    def busy(self):
        return self.starting or (self.task is not None and not self.task.done())

    async def close(self):
        if self.task is not None and not self.task.done():
            if not self.task.cancelling():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await super().close()

    def operation_done(self, task):
        # A task cancelled before its coroutine starts never enters its finally block.
        if task.cancelled():
            self.progress.state = "cancelled"
            self.progress.phase = "finished"
            log.info("Operation cancelled before completion")
        elif task.exception() is not None:
            self.progress.state = "failed"
            self.progress.phase = "finished"
            self.progress.error = type(task.exception()).__name__
            log.error("Operation task failed: %s", self.progress.error)

    def plan(self, guild):
        me = guild.me
        if me is None:
            raise ValueError("Bot guild membership is unavailable; retry after reconnecting")
        channels = sorted((c for c in guild.channels if isinstance(c, (
            discord.TextChannel, discord.VoiceChannel, discord.StageChannel,
            discord.ForumChannel, discord.CategoryChannel
        )) and c.permissions_for(me).manage_channels), key=lambda c: c.id)
        roles = sorted((r for r in guild.roles if not r.is_default() and not r.managed
                        and r < me.top_role and me.guild_permissions.manage_roles), key=lambda r: r.id)
        return channels, roles

    def target_channel(self, interaction):
        channel = interaction.channel
        if (not isinstance(channel, discord.TextChannel)
                or channel.guild.id != interaction.guild_id):
            raise ValueError("Run /nuke in a server text/announcement channel")
        p = channel.permissions_for(interaction.guild.me)
        if not (p.view_channel and p.send_messages):
            raise ValueError("This channel requires View Channel and Send Messages")
        return channel

    def register_commands(self):
        @self.tree.command(name="previewnuke", description="Preview test actions without changing anything")
        @app_commands.guild_only()
        async def preview(interaction: discord.Interaction):
            channels, roles = self.plan(interaction.guild)
            warning = ""
            try:
                self.target_channel(interaction)
            except ValueError as exc:
                warning = f"\nExecution blocked: {exc}"
            await interaction.response.send_message(
                f"Channels to rename: {len(channels)}\nRoles to rename to nuked: {len(roles)}\n"
                f"Messages: {self.config.count}, only in this channel, one second apart.\n"
                f"Mentions disabled. Name changes are permanent.{warning}", ephemeral=True)

        @self.tree.command(name="nuke", description="Confirm and execute the test")
        @app_commands.guild_only()
        async def nuke(interaction: discord.Interaction):
            if self.busy():
                await interaction.response.send_message("An operation is already running.", ephemeral=True)
                return
            await interaction.response.send_message(
                "Confirm permanent channel renames and renaming ALL manageable roles to nuked. "
                f"Send {self.config.count} messages only in this channel, one second apart. Mentions disabled.",
                view=Confirm(self, interaction.user.id, interaction.guild_id), ephemeral=True,
                allowed_mentions=discord.AllowedMentions.all())

        @self.tree.command(name="stopnuke", description="Cancel the running test")
        @app_commands.guild_only()
        async def stop(interaction: discord.Interaction):
            if self.starting:
                await interaction.response.send_message("Confirmation is being acknowledged; retry /stopnuke in a moment.", ephemeral=True)
            elif self.busy():
                if self.progress.guild_id != interaction.guild_id:
                    await interaction.response.send_message("No operation is running in this server.", ephemeral=True)
                    return
                if not self.task.cancelling():
                    self.progress.state = "cancelling"
                    self.task.cancel()
                await interaction.response.send_message("Cancellation requested. Completed changes remain.", ephemeral=True)
            else:
                await interaction.response.send_message("No operation is running.", ephemeral=True)

        @self.tree.command(name="status", description="Show latest operation progress")
        @app_commands.guild_only()
        async def status(interaction: discord.Interaction):
            await interaction.response.send_message(self.progress.summary() if self.progress and self.progress.guild_id == interaction.guild_id else "No recent test in this server.", ephemeral=True)

    async def action(self, label, call):
        # discord.py waits on normal rate-limit buckets. A surfaced 429 is logged
        # and skipped; never blindly retry a message with an uncertain outcome.
        try:
            result = await call()
            log.info("OK %s", label)
            return True, result
        except discord.HTTPException as exc:
            self.progress.failures += 1
            log.warning("FAIL %s HTTP %s code %s", label, exc.status, exc.code)
            return False, None

    async def execute(self, guild, interaction):
        run = self.progress
        try:
            target = self.target_channel(interaction)
            channels, roles = self.plan(guild)
            run.planned = self.config.count
            run.phase = "channel renames"
            for i, channel in enumerate(channels, 1):
                ok, _ = await self.action(f"rename channel {channel.id}", lambda c=channel, n=i: c.edit(name=f"NUKED-{n:03d}", reason=f"Test confirmed by member {interaction.user.id}"))
                run.renamed_channels += int(ok)
                await asyncio.sleep(0.25)
            run.phase = "role changes"
            for role in roles:
                ok, _ = await self.action(f"rename role {role.id}", lambda r=role: r.edit(name="nuked", reason=f"Role rename confirmed by member {interaction.user.id}"))
                run.renamed_roles += int(ok)
                await asyncio.sleep(0.25)
            run.phase = "message test"
            for index in range(self.config.count):
                if index:
                    # Wait after the prior request finishes, including rate-limit waits.
                    await asyncio.sleep(1.0)
                run.attempted += 1
                ok, message = await self.action(f"message channel {target.id}", lambda: target.send(
                    self.config.message, allowed_mentions=discord.AllowedMentions.all()))
                if not ok:
                    # Stop on permission/HTTP failures rather than repeating failed requests.
                    break
                record = {"channel_id": target.id, "message_id": message.id, "url": message.jump_url}
                run.messages.append(record)
                log.info("MESSAGE_RECORD %s", json.dumps(record))
            run.state = "complete with errors" if run.failures else "complete"
        except asyncio.CancelledError:
            run.state = "cancelled"
        except Exception as exc:
            run.state = "failed"
            run.error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            log.error("Operation failed: %s", type(exc).__name__)
        finally:
            run.phase = "finished"
            log.info("SUMMARY %s", run.summary().replace("\n", " | "))
            try:
                await asyncio.wait_for(interaction.followup.send(run.summary(), ephemeral=True), timeout=15)
            except (discord.HTTPException, OSError, TimeoutError):
                log.info("Summary delivery unavailable; use /status")


class Confirm(discord.ui.View):
    def __init__(self, bot, requester_id, guild_id):
        super().__init__(timeout=60)
        self.bot = bot
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.used = False

    @discord.ui.button(label="you sure you wanna drop da bomb?", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.used or self.is_finished():
            await interaction.response.send_message("This confirmation has expired or already been used.", ephemeral=True)
            return
        if (not in_server(interaction) or interaction.guild_id != self.guild_id
                or interaction.user.id != self.requester_id):
            await interaction.response.send_message("Only the member who requested this test can confirm it in the same server.", ephemeral=True)
            return
        if self.bot.busy():
            await interaction.response.send_message("An operation is already running.", ephemeral=True)
            return
        try:
            self.bot.target_channel(interaction)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        # Reserve before awaiting, but acknowledge confirmation before any changes.
        self.bot.starting = True
        self.used = True
        try:
            await interaction.response.edit_message(content="Test started. Use /status or /stopnuke.", view=None)
            self.bot.progress = Run(guild_id=interaction.guild_id, planned=self.bot.config.count)
            self.bot.task = asyncio.create_task(self.bot.execute(interaction.guild, interaction))
            self.bot.task.add_done_callback(self.bot.operation_done)
        finally:
            self.bot.starting = False
            self.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(BASE / "execution.log", encoding="utf-8")])
    logging.getLogger("discord").setLevel(logging.WARNING)
    config = Config.read()
    TestBot(config).run(config.token, log_handler=None)
