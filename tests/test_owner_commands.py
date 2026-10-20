"""공유 자원을 변경하는 명령의 실제 discord.py 권한 검사."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from cogs.music import Music
from cogs.tts import TTS
from core.bot import FuzzyBot
from utils.discord_utils import ConfirmView


@pytest.mark.parametrize("command", [
    Music.restart_bot, Music.exit_bot,
    TTS.tts_add_rvc_model, TTS.tts_remove_rvc_model, TTS.tts_auto_register_rvc,
], ids=["restart", "shutdown", "rvc-add", "rvc-remove", "rvc-scan"])
def test_global_commands_reject_guild_admin_and_allow_bot_owner(command):
    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none(), owner_id=1)
        ctx = SimpleNamespace(
            bot=bot, command=None,
            author=SimpleNamespace(id=2, guild_permissions=SimpleNamespace(administrator=True, manage_guild=True)),
        )
        with pytest.raises(commands.NotOwner):
            await command.can_run(ctx)
        ctx.author.id = 1
        assert await command.can_run(ctx)

    asyncio.run(scenario())


def test_not_owner_error_has_clear_message():
    async def scenario():
        bot = FuzzyBot(command_prefix="!", intents=discord.Intents.none(), owner_id=1)
        ctx = SimpleNamespace(send=AsyncMock())
        await bot.on_command_error(ctx, commands.NotOwner())
        assert "봇 소유자" in ctx.send.call_args.args[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("deferred", [False, True])
def test_confirmation_write_failure_reports_error(deferred):
    async def scenario():
        response = SimpleNamespace(is_done=lambda: deferred, send_message=AsyncMock(), edit_message=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1), response=response,
            followup=SimpleNamespace(send=AsyncMock()), message=None,
        )
        view = ConfirmView(author_id=1, on_confirm=AsyncMock(side_effect=OSError("disk full")))
        await view._confirm_callback(interaction)
        send = interaction.followup.send if deferred else response.send_message
        assert "완료하지 못했어요" in send.call_args.args[0]
        assert send.call_args.kwargs["ephemeral"] is True

    asyncio.run(scenario())
