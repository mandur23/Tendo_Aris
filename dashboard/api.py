import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
from fastapi import FastAPI, Query, Body
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from collectors.auto import get_collector, get_gpu_detect_diagnostics
from core.runtime import get_bot
from utils.config import COMMAND_PREFIX

logger = logging.getLogger(__name__)

app = FastAPI()

# 정적 파일 제공 (public/index.html)
PUBLIC_DIR = Path(__file__).resolve().parent.parent / 'public'
if PUBLIC_DIR.exists():
    app.mount('/', StaticFiles(directory=str(PUBLIC_DIR), html=True), name='static')
else:
    logger.warning(f"대시보드 정적 디렉토리 없음: {PUBLIC_DIR}")

# GPU 수집기 (선택적)
GPU = None
try:
    GPU = get_collector()
except Exception as e:
    logger.warning(f"GPU 수집기 초기화 실패: {e}")

# 간단 캐시로 과도한 호출 방지(1초)
_last: Dict[str, Any] = {"ts": 0, "data": None}


@app.get('/api/summary')
def summary(diag: int = Query(0, description="1이면 GPU 감지 진단 정보를 포함")):
    now = time.time()
    if _last["data"] is not None and now - _last["ts"] < 0.8:
        return JSONResponse(_last["data"])  # 최근 결과 재사용

    # CPU/RAM/디스크/네트워크는 가벼운 psutil 사용
    try:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
    except Exception as e:
        logger.error(f"시스템 메트릭 수집 실패: {e}")
        cpu = 0.0
        class M:  # 간단한 대체 구조
            percent = 0.0
            used = 0
            total = 0
        mem = M()

    disks = []
    try:
        for p in psutil.disk_partitions():
            try:
                u = psutil.disk_usage(p.mountpoint)
                disks.append({"mount": p.mountpoint, "percent": u.percent})
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"디스크 수집 실패: {e}")

    try:
        net = psutil.net_io_counters()
        net_data = {"bytes_sent": getattr(net, 'bytes_sent', 0), "bytes_recv": getattr(net, 'bytes_recv', 0)}
    except Exception:
        net_data = {"bytes_sent": 0, "bytes_recv": 0}

    gpus = []
    if GPU:
        try:
            g = GPU.snapshot()
            gpus = g if isinstance(g, list) else [g]
        except Exception as e:
            logger.debug(f"GPU 스냅샷 실패: {e}")

    data = {
        "cpu": {"percent": cpu},
        "ram": {"percent": getattr(mem, 'percent', 0.0), "used": getattr(mem, 'used', 0), "total": getattr(mem, 'total', 0)},
        "gpu": gpus,
        "disk": disks[:2],
        "net": net_data,
        "ts": int(now),
    }

    # GPU 감지 실패 원인 진단 정보 포함 조건
    try:
        if diag == 1 or not gpus:
            data["gpu_diag"] = get_gpu_detect_diagnostics()
    except Exception as e:
        logger.debug(f"GPU 진단 정보 생성 실패: {e}")

    _last["ts"] = now
    _last["data"] = data
    return JSONResponse(data)


# --- 추가: 시스템 로그 Tail ---
@app.get('/api/logs')
def get_logs(lines: int = Query(200, ge=1, le=2000)):
    """logs/yacht_bot.log의 마지막 N줄을 반환합니다."""
    log_path = Path(__file__).resolve().parent.parent / 'logs' / 'yacht_bot.log'
    out: List[str] = []
    try:
        if log_path.exists():
            with log_path.open('r', encoding='utf-8', errors='ignore') as f:
                buf = f.readlines()
                out = [s.rstrip('\n') for s in buf[-lines:]]
    except Exception as e:
        logger.debug(f"로그 읽기 실패: {e}")
    return JSONResponse({"lines": out})


def _get_active_player():
    """현재 활성 Music 플레이어 하나를 반환합니다. 없으면 None"""
    try:
        bot = get_bot()
        if not bot:
            return None
        music_cog = bot.get_cog('Music')
        if not music_cog:
            return None
        players = getattr(music_cog, 'players', {})
        if not players:
            return None
        # 임의로 첫 번째 플레이어 선택
        return next(iter(players.values()))
    except Exception as e:
        logger.debug(f"플레이어 탐색 실패: {e}")
        return None


@app.get('/api/music/status')
async def music_status():
    """현재 재생 중인 곡과 큐 정보(최대 5개)를 반환합니다."""
    player = _get_active_player()
    current: Optional[Dict[str, Any]] = None
    queue_preview: List[Dict[str, Any]] = []
    guild_id = None
    if player is not None:
        try:
            guild_id = getattr(player, 'guild', None).id if getattr(player, 'guild', None) else None
            src = getattr(player, 'current', None)
            if isinstance(src, dict):
                current = {
                    'title': src.get('title'),
                    'url': src.get('webpage_url') or src.get('url'),
                    'duration': src.get('duration')
                }
            # 큐 미리보기 (비파괴적으로 내부 큐를 확인)
            q = getattr(player, 'queue', None)
            if q is not None and hasattr(q, '_queue'):
                # asyncio.Queue 내부
                raw = list(q._queue)  # type: ignore[attr-defined]
                for item in raw[:5]:
                    if isinstance(item, dict):
                        queue_preview.append({
                            'title': item.get('title'),
                            'url': item.get('webpage_url') or item.get('url')
                        })
                    else:
                        # 아직 미추출 URL 문자열
                        queue_preview.append({'title': str(item), 'url': str(item)})
        except Exception as e:
            logger.debug(f"음악 상태 수집 실패: {e}")
    return JSONResponse({
        'guild_id': guild_id,
        'current': current,
        'queue': queue_preview
    })


@app.post('/api/music/enqueue')
async def music_enqueue(payload: Dict[str, Any] = Body(...)):
    """현재 활성 길드의 음악 채널에 !play 명령으로 곡을 추가합니다.
    요청 본문: {"query": "유튜브 URL 또는 검색어"}
    """
    query = (payload or {}).get('query')
    if not query or not isinstance(query, str):
        return JSONResponse({"ok": False, "error": "query가 필요합니다."}, status_code=400)

    player = _get_active_player()
    if player is None:
        return JSONResponse({"ok": False, "error": "활성화된 음악 세션이 없습니다. 디스코드에서 먼저 재생을 시작하세요."}, status_code=409)

    try:
        channel = getattr(player, 'channel', None)
        bot = get_bot()
        if channel is None or bot is None:
            return JSONResponse({"ok": False, "error": "재생 채널을 확인할 수 없습니다."}, status_code=500)
        # 디스코드 채널에 명령어 메시지를 보내 봇이 자체적으로 처리하도록 함
        content = f"{COMMAND_PREFIX}play {query}"
        await channel.send(content)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error(f"enqueue 실패: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
