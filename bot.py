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
    
# FastAPI에서 Bot 인스턴스 접근을 위한 런타임 레지스트리
try:
    from core.runtime import set_bot
except Exception:
    set_bot = None

# 로깅 설정
setup_logging()
logger = logging.getLogger(__name__)

# 인텐트 설정
intents = discord.Intents.default()
intents.message_content = True

# 봇 인스턴스 생성
bot = FuzzyBot(command_prefix=COMMAND_PREFIX, intents=intents)

# 종료 플래그 (이벤트 루프가 생성된 후 설정됨)
_shutdown_event: Optional[asyncio.Event] = None


async def main():
    """봇 메인 실행 함수"""
    global _shutdown_event
    
    # 이벤트 루프가 생성된 후 종료 이벤트 초기화
    _shutdown_event = asyncio.Event()
    
    # 토큰 검증
    if not DISCORD_BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN이 설정되지 않았습니다. TOKEN.env 파일을 확인해주세요.")
        sys.exit(1)
    
    try:
        # 대시보드 서버 시작(선택적)
        if start_dashboard:
            try:
                await start_dashboard(host='localhost', port=8080)
            except Exception as e:
                logger.warning(f"대시보드 시작 실패: {e}")

        async with bot:
            # 런타임 레지스트리에 봇 인스턴스 등록 (대시보드 API에서 사용)
            if set_bot:
                try:
                    set_bot(bot)
                except Exception as e:
                    logger.debug(f"봇 레지스트리 등록 실패: {e}")
            await bot.add_cog(Music(bot))
            await bot.add_cog(TTS(bot))
            await bot.add_cog(YachtDiceGame(bot))
            
            # 봇 시작과 종료 이벤트 대기를 병렬로 처리
            bot_task = asyncio.create_task(bot.start(DISCORD_BOT_TOKEN))
            shutdown_task = asyncio.create_task(_shutdown_event.wait())
            
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
                # 봇이 자체적으로 종료된 경우
                shutdown_task.cancel()
                
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
    # 실행 중인 이벤트 루프에 종료 신호 전달
    try:
        loop = asyncio.get_running_loop()
        if _shutdown_event is not None:
            loop.call_soon_threadsafe(_shutdown_event.set)
    except RuntimeError:
        # 이벤트 루프가 실행 중이 아닌 경우
        if _shutdown_event is not None:
            _shutdown_event.set()


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
