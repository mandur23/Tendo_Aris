import asyncio
import logging
from typing import Optional

from uvicorn import Config, Server

from .api import app

logger = logging.getLogger(__name__)

_server: Optional[Server] = None
_task: Optional[asyncio.Task] = None


async def start_dashboard(host: str = '127.0.0.1', port: int = 8000):
    """Uvicorn 서버를 현재 이벤트 루프의 백그라운드 태스크로 시작합니다."""
    global _server, _task
    if _server is not None:
        return
    cfg = Config(app=app, host=host, port=port, loop='asyncio', lifespan='on', log_level='warning')
    _server = Server(cfg)

    async def _run():
        try:
            await _server.serve()
        except Exception as e:
            logger.error(f"대시보드 서버 실행 오류: {e}")
        finally:
            # 종료 시 참조 정리
            _cleanup()

    _task = asyncio.create_task(_run(), name="dashboard-uvicorn")
    logger.info(f"대시보드 서버 시작: http://{host}:{port}")


def _cleanup():
    global _server, _task
    _server = None
    if _task is not None:
        if not _task.done():
            _task.cancel()
        _task = None


async def stop_dashboard():
    """서버가 실행 중이면 우아하게 종료합니다."""
    global _server, _task
    if _server is None:
        return
    try:
        _server.should_exit = True
        # 태스크 종료를 잠시 대기
        if _task is not None:
            try:
                await asyncio.wait_for(_task, timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("대시보드 서버 종료 타임아웃")
    finally:
        _cleanup()
