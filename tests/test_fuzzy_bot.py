"""FuzzyBot: 유사 명령어 매칭은 접두사로 시작한 메시지에서만 수행되는지 테스트."""
import asyncio

import discord
from discord.ext import commands

import core.bot as core_bot
from core.bot import FuzzyBot


class FakeCtx:
    def __init__(self, prefix, invoked_with):
        self.prefix = prefix
        self.invoked_with = invoked_with
        self.command = None


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content, delete_after=None):
        self.sent.append(content)


class FakeMessage:
    def __init__(self):
        self.channel = FakeChannel()


def _make_bot():
    bot = FuzzyBot(command_prefix="!", intents=discord.Intents.none())

    @bot.command(name="재생")
    async def play(ctx):
        pass

    @bot.command(name="정지")
    async def stop(ctx):
        pass

    return bot


def _run(monkeypatch, ctx):
    async def fake_super_get_context(self, message, *, cls=commands.Context):
        return ctx

    monkeypatch.setattr(commands.Bot, "get_context", fake_super_get_context)

    calls = {"n": 0}
    real_extract_one = core_bot.process.extractOne

    def counting_extract_one(*args, **kwargs):
        calls["n"] += 1
        return real_extract_one(*args, **kwargs)

    monkeypatch.setattr(core_bot.process, "extractOne", counting_extract_one)

    async def main():
        bot = _make_bot()
        message = FakeMessage()
        result = await bot.get_context(message)
        return result, message, calls["n"]

    return asyncio.run(main())


def test_일반_채팅은_퍼지_매칭을_하지_않는다(monkeypatch):
    ctx, message, calls = _run(monkeypatch, FakeCtx(prefix=None, invoked_with=None))
    assert calls == 0
    assert ctx.command is None and message.channel.sent == []


def test_오타_명령어는_제안한다(monkeypatch):
    ctx, message, calls = _run(monkeypatch, FakeCtx(prefix="!", invoked_with="재생해"))
    assert calls == 1
    assert ctx.command is None
    assert message.channel.sent and "재생" in message.channel.sent[0]


def test_거의_같은_명령어는_자동_실행된다(monkeypatch):
    ctx, message, calls = _run(monkeypatch, FakeCtx(prefix="!", invoked_with="재생"))
    assert ctx.command is not None and ctx.command.name == "재생"
