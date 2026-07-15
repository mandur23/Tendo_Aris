"""봇 실행 및 초기화 로직"""
import asyncio
import sys
import logging
import discord
from core.bot import FuzzyBot
from core.shutdown_handler import get_shutdown_event
from core.watchdog import connection_watchdog
from core.web_admin import WebAdmin
from cogs.music import Music
from cogs.tts import TTS
from cogs.chat_ai import ChatAI
from cogs.trpg import TRPG
from cogs.trpg_party import TRPGParty
from cogs.trpg_world import TRPGWorld
from GameSystem.YachtDiceGame import YachtDiceGame
from GameSystem.BlackjackGame import BlackjackGame
from utils.config import DISCORD_BOT_TOKEN, COMMAND_PREFIX
from utils.db_utils import close_db_pool
from utils.file_utils import backup_data_dir

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
    await bot.add_cog(TRPG(bot))
    await bot.add_cog(TRPGParty(bot))
    await bot.add_cog(TRPGWorld(bot))
    await bot.add_cog(YachtDiceGame(bot))
    await bot.add_cog(BlackjackGame(bot))


async def cleanup_bot(bot: FuzzyBot):
    """봇 종료 시 정리 작업을 수행합니다."""
    # 웹 관리자 대시보드 종료
    web_admin = getattr(bot, 'web_admin', None)
    if web_admin:
        try:
            await web_admin.stop()
        except Exception as e:
            logger.debug(f"웹 관리자 대시보드 종료 중 오류 (무시됨): {e}")

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

    # 데이터(세이브·플레이리스트·설정) 일일 백업 — 실패해도 시작을 막지 않는다.
    await asyncio.to_thread(backup_data_dir)

    try:
        async with bot:
            await load_cogs(bot)

            # 웹 관리자 대시보드 시작 (WEB_ADMIN_PASSWORD 설정 시)
            bot.web_admin = WebAdmin(bot)
            await bot.web_admin.start()

            # 봇 시작과 종료 이벤트 대기를 병렬로 처리
            bot_task = asyncio.create_task(bot.start(DISCORD_BOT_TOKEN))
            shutdown_task = asyncio.create_task(shutdown_event.wait())
            watchdog_task = asyncio.create_task(connection_watchdog(bot))

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

            # 워치독 태스크 정리
            if not watchdog_task.done():
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
                
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

