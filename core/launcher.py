"""봇 실행 및 초기화 로직"""
import asyncio
import sys
import logging
import discord
from core.bot import FuzzyBot
from core.shutdown_handler import get_shutdown_event
from cogs.music import Music
from cogs.tts import TTS
from cogs.chat_ai import ChatAI
from GameSystem.YachtDiceGame import YachtDiceGame
from utils.config import DISCORD_BOT_TOKEN, COMMAND_PREFIX
from utils.db_utils import close_db_pool

logger = logging.getLogger(__name__)


def create_bot() -> FuzzyBot:
    """봇 인스턴스를 생성합니다."""
    intents = discord.Intents.default()
    intents.message_content = True
    return FuzzyBot(command_prefix=COMMAND_PREFIX, intents=intents)


async def load_cogs(bot: FuzzyBot):
    """모든 Cog를 봇에 로드합니다."""
    await bot.add_cog(Music(bot))
    await bot.add_cog(TTS(bot))
    await bot.add_cog(ChatAI(bot))
    await bot.add_cog(YachtDiceGame(bot))


async def cleanup_bot(bot: FuzzyBot):
    """봇 종료 시 정리 작업을 수행합니다."""
    # DB 연결 풀 정리 (봇 종료 전)
    try:
        # bot.get_cog()를 사용하여 이미 로드된 Music Cog 가져오기
        music_cog = bot.get_cog('Music')
        if music_cog:
            await music_cog.cog_unload()
    except Exception as e:
        logger.debug(f"봇 종료 시 Cog 언로드 중 오류 (무시됨): {e}")
    
    # DB 연결 풀 명시적 종료
    await close_db_pool()
    
    # 봇 종료
    if not bot.is_closed():
        try:
            await bot.close()
        except Exception as e:
            logger.debug(f"봇 종료 중 오류 (무시됨): {e}")


async def run_bot(bot: FuzzyBot):
    """봇을 실행합니다."""
    shutdown_event = get_shutdown_event()
    
    # 토큰 검증
    if not DISCORD_BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN이 설정되지 않았습니다. TOKEN.env 파일을 확인해주세요.")
        raise ValueError("DISCORD_BOT_TOKEN이 설정되지 않았습니다.")
    
    try:
        async with bot:
            await load_cogs(bot)
            
            # 봇 시작과 종료 이벤트 대기를 병렬로 처리
            bot_task = asyncio.create_task(bot.start(DISCORD_BOT_TOKEN))
            shutdown_task = asyncio.create_task(shutdown_event.wait())
            
            # 둘 중 하나가 완료되면 종료
            done, pending = await asyncio.wait(
                [bot_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # 종료 신호를 받은 경우 봇 종료
            if shutdown_task in done:
                if not bot.is_closed():
                    await bot.close()
                # 봇 태스크 취소
                if not bot_task.done():
                    bot_task.cancel()
                    try:
                        await bot_task
                    except asyncio.CancelledError:
                        pass
            else:
                # 봇이 자체적으로 종료된 경우: bot_task 결과를 회수해 예외 누락 방지
                if not shutdown_task.done():
                    shutdown_task.cancel()
                    try:
                        await shutdown_task
                    except asyncio.CancelledError:
                        pass
                await bot_task
                
    except KeyboardInterrupt:
        logger.info("키보드 인터럽트로 봇을 종료합니다.")
    except Exception as e:
        logger.error(f"봇 실행 중 오류 발생: {e}", exc_info=True)
        raise
    finally:
        if 'bot' in locals():
            await cleanup_bot(bot)
        logger.info("봇이 정상적으로 종료되었습니다.")


async def main():
    """봇 메인 실행 함수"""
    bot = create_bot()
    await run_bot(bot)

