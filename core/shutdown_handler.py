"""봇 종료 및 시그널 핸들러 관리"""
import asyncio
import signal
import sys
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 종료 플래그 (이벤트 루프가 생성된 후 설정됨)
_shutdown_event: Optional[asyncio.Event] = None


def get_shutdown_event() -> asyncio.Event:
    """종료 이벤트를 가져옵니다. 없으면 생성합니다."""
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event


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


def setup_signal_handlers():
    """시그널 핸들러를 설정합니다."""
    # Windows에서는 SIGTERM이 없으므로 SIGINT만 처리
    if sys.platform != 'win32':
        signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

