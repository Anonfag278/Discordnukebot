# Discord test bot

Python 3.12+ and discord.py. Any member with slash-command access can invoke the bot. Confirmation is single-use, expires after 60 seconds, and is restricted to the requester in the same server.

## Setup

From this folder in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Set your bot token and message options:

```dotenv
DISCORD_TOKEN=your_bot_token
NUKE_MESSAGE="the bomb has been dropped"
NUKE_MESSAGE_COUNT=100
```

The count must be 1–100; text must contain 1–2000 characters. Messages are sent only in the channel where the command is invoked. The bot waits one second after each completed send before attempting the next; Discord rate limits can extend the wait. All mentions are disabled, including user, role, @everyone, and @here mentions in custom text.

Old ENABLE_MASS_MENTIONS, TEST_GUILD_ID, NOTIFICATION_CHANNEL_ID, NUKE_GIF_URL, and TEST_ROLE_IDS settings are ignored. This version sends text, with no GIF uploads or simulated events. The previously supplied nuke.gif remains in the folder as an unused asset. Keep your .env private.

Start or restart the bot after changing settings:

```powershell
.\.venv\Scripts\python.exe main.py
```

## Invitation

In the Discord Developer Portal, select Guild Install and both bot and applications.commands scopes. Grant View Channels, Send Messages, Manage Channels, and Manage Roles. Place the bot's role above the ordinary roles you want renamed. No privileged gateway intents are needed. Commands sync globally; registration may take time. The token must be a bot token, never a user token.

## Commands and actions

- /previewnuke: Shows counts of channel/role renames and planned messages, plus any target-channel permission problem.
- /nuke: Asks “you sure you wanna drop da bomb?” before starting.
- /status: Shows the latest operation if it belongs to this server.
- /stopnuke: Cancels remaining actions in this server, including during the one-second wait.

A confirmed run checks the invoking text/announcement channel first. It then renames manageable server channels to numbered NUKED names and ordinary roles below the bot to exactly nuked. Managed roles and @everyone are skipped. No roles are deleted. It then sends the configured message count in the invoking channel only. Channel and role changes affect the server; only the message workload is limited to one channel.

One operation can run at a time across the process. Other servers cannot see or stop its operation. Progress is memory-only and keeps only the most recent run. Stop does not undo completed changes, and an in-flight request may finish. There is no recovery functionality.

The message sequence stops at the first surfaced HTTP failure, including 403/429. discord.py handles normal rate-limit waits internally. The bot adds no retries. Summaries show planned, attempted, sent, and failed counts. Each acknowledged message is recorded as MESSAGE_RECORD in execution.log, with its channel ID, message ID, and link. Summary delivery is ephemeral; /status remains available if the interaction expires.

## Offline checks

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_main
```

Tests mock Discord requests and verify current-channel targeting, one-second waits, mention suppression, cancellation, configuration limits, confirmation, and server isolation. They do not log in or change a server. Live testing requires your own bot token and server.
