"""Offline checks: python -m unittest -v test_main"""
import asyncio
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import main


def interaction(guild_id=123, user_id=7):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    channel = MagicMock(spec=discord.TextChannel)
    channel.guild.id = guild_id
    channel.id = 456
    channel.permissions_for.return_value = discord.Permissions(view_channel=True, send_messages=True)
    channel.send = AsyncMock(return_value=NS(id=789, jump_url="test-link"))
    return NS(guild_id=guild_id, guild=NS(me=object()), user=member, channel=channel,
              response=NS(send_message=AsyncMock(), edit_message=AsyncMock()),
              followup=NS(send=AsyncMock()))


class ConfigTests(unittest.TestCase):
    def test_defaults_and_custom_message(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN": "dummy"}, clear=True), patch.object(main, "load_dotenv"):
            self.assertEqual(main.Config.read().count, 100)
            with patch.dict(os.environ, {"NUKE_MESSAGE": "custom text", "NUKE_MESSAGE_COUNT": "5"}):
                self.assertEqual(main.Config.read(), main.Config("dummy", "custom text", 5))

    def test_invalid_values(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN": "dummy"}, clear=True), patch.object(main, "load_dotenv"):
            for key, value in [("NUKE_MESSAGE_COUNT", "0"), ("NUKE_MESSAGE_COUNT", "101"),
                               ("NUKE_MESSAGE_COUNT", "abc"), ("NUKE_MESSAGE", " "),
                               ("NUKE_MESSAGE", "x" * 2001), ("DISCORD_TOKEN", "replace_with_bot_token")]:
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                    with self.assertRaises(ValueError):
                        main.Config.read()


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = main.TestBot(main.Config("dummy", "@everyone <@123> test", 3))
        self.i = interaction()
        self.bot.progress = main.Run(guild_id=123)

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_startup_and_global_commands(self):
        self.assertTrue(callable(self.bot.run))
        self.assertEqual({c.name for c in self.bot.tree.get_commands()},
                         {"nuke", "previewnuke", "status", "stopnuke"})
        self.bot.tree.sync = AsyncMock()
        await self.bot.setup_hook()
        self.bot.tree.sync.assert_awaited_once_with()

    async def test_paced_current_channel_only_and_no_mentions(self):
        self.bot.plan = lambda guild: ([], [])
        events = []
        async def send(*args, **kwargs):
            self.assertEqual(args[0], self.bot.config.message)
            self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
            events.append("send")
            return NS(id=789, jump_url="test-link")
        async def sleep(delay):
            self.assertEqual(delay, 1.0)
            events.append("wait")
        self.i.channel.send.side_effect = send
        with patch.object(main.asyncio, "sleep", side_effect=sleep):
            await self.bot.execute(self.i.guild, self.i)
        self.assertEqual(events, ["send", "wait", "send", "wait", "send"])
        self.assertEqual(self.bot.progress.planned, 3)
        self.assertEqual(len(self.bot.progress.messages), 3)
        self.assertTrue(all(m["channel_id"] == 456 for m in self.bot.progress.messages))

    async def test_http_failure_stops_further_messages(self):
        self.bot.plan = lambda guild: ([], [])
        for status in (403, 429):
            self.bot.progress = main.Run(guild_id=123)
            self.i.channel.send.reset_mock()
            self.i.channel.send.side_effect = discord.HTTPException(NS(status=status, reason="test"), "test")
            await self.bot.execute(self.i.guild, self.i)
            self.i.channel.send.assert_awaited_once()
            self.assertEqual(self.bot.progress.state, "complete with errors")
            self.assertEqual(self.bot.progress.failures, 1)

    async def test_cancel_during_pacing(self):
        self.bot.plan = lambda guild: ([], [])
        waiting = asyncio.Event()
        async def sleep(delay):
            waiting.set()
            await asyncio.Event().wait()
        with patch.object(main.asyncio, "sleep", side_effect=sleep):
            self.bot.task = asyncio.create_task(self.bot.execute(self.i.guild, self.i))
            await waiting.wait()
            self.bot.task.cancel()
            await self.bot.task
        self.assertEqual(self.i.channel.send.await_count, 1)
        self.assertEqual(self.bot.progress.state, "cancelled")

    async def test_renames_roles_without_deleting(self):
        role = NS(id=9, edit=AsyncMock(), delete=AsyncMock())
        channel = NS(id=8, edit=AsyncMock())
        self.bot.plan = lambda guild: ([channel], [role])
        with patch.object(main.asyncio, "sleep", new=AsyncMock()):
            await self.bot.execute(self.i.guild, self.i)
        role.edit.assert_awaited_once_with(name="nuked", reason="Role rename confirmed by member 7")
        role.delete.assert_not_called()
        channel.edit.assert_awaited_once()

    async def test_confirmation_requester_and_server_bound(self):
        view = main.Confirm(self.bot, 7, 123)
        await view.confirm.callback(interaction(user_id=8))
        await view.confirm.callback(interaction(guild_id=999))
        self.assertFalse(view.used)
        self.assertIsNone(self.bot.task)

    async def test_acknowledgement_before_execution_and_single_use(self):
        self.bot.execute = AsyncMock()
        async def ack(**kwargs):
            self.assertTrue(self.bot.busy())
            self.bot.execute.assert_not_called()
        self.i.response.edit_message.side_effect = ack
        view = main.Confirm(self.bot, 7, 123)
        await view.confirm.callback(self.i)
        await self.bot.task
        await view.confirm.callback(self.i)
        self.bot.execute.assert_awaited_once()

    async def test_failed_ack_does_not_execute(self):
        self.i.response.edit_message.side_effect = OSError("test")
        with self.assertRaises(OSError):
            await main.Confirm(self.bot, 7, 123).confirm.callback(self.i)
        self.assertIsNone(self.bot.task)
        self.assertFalse(self.bot.busy())

    async def test_cross_server_stop_and_status(self):
        self.bot.task = asyncio.create_task(asyncio.Event().wait())
        other = interaction(guild_id=999)
        await self.bot.tree.get_command("stopnuke").callback(other)
        self.assertEqual(self.bot.task.cancelling(), 0)
        await self.bot.tree.get_command("status").callback(other)
        self.assertEqual(other.response.send_message.call_args.args[0], "No recent test in this server.")

    async def test_invalid_target_prevents_all_changes(self):
        self.i.channel.permissions_for.return_value = discord.Permissions.none()
        self.bot.plan = MagicMock()
        await self.bot.execute(self.i.guild, self.i)
        self.bot.plan.assert_not_called()
        self.assertEqual(self.bot.progress.state, "failed")

    async def test_members_allowed_and_dm_rejected(self):
        self.assertTrue(main.in_server(self.i))
        self.i.guild = None
        self.assertFalse(main.in_server(self.i))

    async def test_cancel_before_start(self):
        self.bot.task = asyncio.create_task(self.bot.execute(self.i.guild, self.i))
        self.bot.task.add_done_callback(self.bot.operation_done)
        self.bot.task.cancel()
        await asyncio.gather(self.bot.task, return_exceptions=True)
        self.assertEqual(self.bot.progress.state, "cancelled")


if __name__ == "__main__":
    unittest.main()
