import asyncio
import discord
from core.bot import FuzzyBot
from cogs.music import Music
from cogs.tts import TTS
from GameSystem.YachtDiceGame import YachtDiceGame
from logging_config import setup_logging
from utils.config import DISCORD_BOT_TOKEN, COMMAND_PREFIX

# 로깅 설정
setup_logging()

# 인텐트 설정
intents = discord.Intents.default()
intents.message_content = True

# 봇 인스턴스 생성
bot = FuzzyBot(command_prefix=COMMAND_PREFIX, intents=intents)


async def main():
    async with bot:
        await bot.add_cog(Music(bot))
        await bot.add_cog(TTS(bot))
        await bot.add_cog(YachtDiceGame(bot))
        await bot.start(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
