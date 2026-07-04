"""봇 연결 상태 감시(워치독) 및 오프라인 알림/자동 재시작

TOKEN.env 의 다음 설정을 사용합니다:
- OFFLINE_ALERT_WEBHOOK_URL: 오프라인 알림용 Discord 웹훅 (선택)
- OFFLINE_STARTUP_GRACE_SECONDS: 시작 후 연결이 안 될 때 재시작까지 대기 시간 (초)
- OFFLINE_RESTART_SECONDS: 오프라인 지속 시 자동 재시작 기준 시간 (초)
- AUTO_RESTART_ON_OFFLINE: 오프라인 자동 재시작 활성화 여부
"""
import asyncio
import logging
import os
import subprocess
import sys
import time

import aiohttp

from utils.config import (
    AUTO_RESTART_ON_OFFLINE,
    OFFLINE_ALERT_WEBHOOK_URL,
    OFFLINE_RESTART_SECONDS,
    OFFLINE_STARTUP_GRACE_SECONDS,
)

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30

# 재시작 요청 플래그: 이벤트 루프 종료 후 bot.py 에서 확인해 프로세스를 다시 실행한다.
_restart_requested = False


def request_restart() -> None:
    """봇 종료 후 프로세스 재시작을 예약합니다."""
    global _restart_requested
    _restart_requested = True


def is_restart_requested() -> bool:
    return _restart_requested


def restart_process() -> None:
    """현재 스크립트를 같은 인자로 새 프로세스로 실행하고 종료합니다.

    이벤트 루프가 완전히 종료된 뒤(bot.py 메인 스코프)에서만 호출해야 합니다.
    """
    logger.warning("프로세스를 재시작합니다...")
    subprocess.Popen([sys.executable] + sys.argv, cwd=os.getcwd())
    sys.exit(0)


async def _send_webhook_alert(message: str) -> None:
    """오프라인 알림 웹훅으로 메시지를 전송합니다. (설정된 경우에만)"""
    if not OFFLINE_ALERT_WEBHOOK_URL:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await session.post(OFFLINE_ALERT_WEBHOOK_URL, json={'content': message})
    except Exception as e:
        logger.warning(f"오프라인 알림 웹훅 전송 실패: {e}")


async def connection_watchdog(bot) -> None:
    """연결 상태를 주기적으로 확인해 오프라인 알림 및 자동 재시작을 수행합니다.

    - 시작 후 OFFLINE_STARTUP_GRACE_SECONDS 안에 한 번도 연결되지 못하면 알림/재시작.
    - 연결된 적이 있으나 OFFLINE_RESTART_SECONDS 이상 오프라인이 지속되면 알림/재시작.
    """
    start_time = time.monotonic()
    state = {'offline_since': None, 'ever_connected': False}

    async def _on_ready():
        state['ever_connected'] = True
        state['offline_since'] = None

    async def _on_resumed():
        state['offline_since'] = None

    async def _on_disconnect():
        if state['offline_since'] is None:
            state['offline_since'] = time.monotonic()

    bot.add_listener(_on_ready, 'on_ready')
    bot.add_listener(_on_resumed, 'on_resumed')
    bot.add_listener(_on_disconnect, 'on_disconnect')

    try:
        while not bot.is_closed():
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            if bot.is_closed():
                return

            now = time.monotonic()

            # 1) 시작 후 아직 한 번도 연결되지 못한 경우
            if not state['ever_connected']:
                if now - start_time >= OFFLINE_STARTUP_GRACE_SECONDS:
                    elapsed = int(now - start_time)
                    logger.error(f"시작 후 {elapsed}초 동안 Discord에 연결하지 못했습니다.")
                    await _send_webhook_alert(
                        f"⚠️ 아리스 봇이 시작 후 {elapsed}초 동안 Discord에 연결하지 못했어요."
                    )
                    if AUTO_RESTART_ON_OFFLINE:
                        logger.warning("자동 재시작을 예약하고 봇을 종료합니다.")
                        request_restart()
                        await bot.close()
                    return
                continue

            # 2) 연결된 적이 있으나 오프라인이 지속되는 경우
            offline_since = state['offline_since']
            if offline_since is None:
                continue

            offline_for = now - offline_since
            if offline_for >= OFFLINE_RESTART_SECONDS:
                logger.error(f"{int(offline_for)}초 동안 오프라인 상태입니다.")
                await _send_webhook_alert(
                    f"⚠️ 아리스 봇이 {int(offline_for)}초 동안 오프라인 상태예요."
                )
                if AUTO_RESTART_ON_OFFLINE:
                    logger.warning("자동 재시작을 예약하고 봇을 종료합니다.")
                    request_restart()
                    await bot.close()
                return
    except asyncio.CancelledError:
        pass
    finally:
        for listener, name in (
            (_on_ready, 'on_ready'),
            (_on_resumed, 'on_resumed'),
            (_on_disconnect, 'on_disconnect'),
        ):
            try:
                bot.remove_listener(listener, name)
            except Exception:
                pass
