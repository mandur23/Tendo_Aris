import asyncio
import signal
import sys
import logging
import discord
from core.bot import FuzzyBot
from cogs.music import Music
from cogs.tts import TTS
from GameSystem.YachtDiceGame import YachtDiceGame
from logging_config import setup_logging
from utils.config import DISCORD_BOT_TOKEN, COMMAND_PREFIX
from typing import Optional

# 대시보드(FastAPI) 통합
try:
    from dashboard.server import start_dashboard, stop_dashboard
except Exception:
    start_dashboard = None  # 선택적 의존성(대시보드 비활성 시 None)
    stop_dashboard = None

# 로깅 설정
setup_logging()
logger = logging.getLogger(__name__)

# 인텐트 설정
intents = discord.Intents.default()
intents.message_content = True

# 봇 인스턴스 생성
bot = FuzzyBot(command_prefix=COMMAND_PREFIX, intents=intents)


async def main():
    """봇 메인 실행 함수"""
    # 토큰 검증
    if not DISCORD_BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN이 설정되지 않았습니다. TOKEN.env 파일을 확인해주세요.")
        sys.exit(1)
    
    try:
        # 대시보드 서버 시작(선택적)
        if start_dashboard:
            try:
                await start_dashboard(host='0.0.0.0', port=8000)
            except Exception as e:
                logger.warning(f"대시보드 시작 실패: {e}")

        async with bot:
            await bot.add_cog(Music(bot))
            await bot.add_cog(TTS(bot))
            await bot.add_cog(YachtDiceGame(bot))
            await bot.start(DISCORD_BOT_TOKEN)
    except KeyboardInterrupt:
        logger.info("키보드 인터럽트로 봇을 종료합니다.")
    except Exception as e:
        logger.error(f"봇 실행 중 오류 발생: {e}", exc_info=True)
    finally:
        if not bot.is_closed():
            await bot.close()
        # 대시보드 서버 종료(선택적)
        if stop_dashboard:
            try:
                await stop_dashboard()
            except Exception as e:
                logger.debug(f"대시보드 종료 중 오류: {e}")
        logger.info("봇이 정상적으로 종료되었습니다.")


def signal_handler(signum, frame):
    """시그널 핸들러 (Windows에서는 제한적)"""
    logger.info(f"시그널 {signum}을 받았습니다. 봇을 종료합니다.")
    if not bot.is_closed():
        asyncio.create_task(bot.close())
    # 대시보드 서버도 함께 종료 시도
    if 'stop_dashboard' in globals() and stop_dashboard:
        try:
            asyncio.create_task(stop_dashboard())
        except Exception:
            pass


if __name__ == "__main__":
    # Windows에서는 SIGTERM이 없으므로 SIGINT만 처리
    if sys.platform != 'win32':
        signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("프로그램이 종료되었습니다.")
    except Exception as e:
        logger.error(f"프로그램 실행 중 오류: {e}", exc_info=True)
        sys.exit(1)
