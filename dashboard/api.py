import logging
import time
from pathlib import Path
from typing import Any, Dict

import psutil
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from collectors.auto import get_collector

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
def summary():
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

    _last["ts"] = now
    _last["data"] = data
    return JSONResponse(data)
