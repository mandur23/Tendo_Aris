import asyncio
import os
import sys
import time
import logging

# torch 보다 먼저 onnxruntime 을 import 해 DLL 을 선점한다 (Windows 에서 native DLL 충돌 방지).
try:
    if sys.platform == "win32":
        # Python 3.8+ Windows: onnxruntime DLL 디렉터리를 검색 경로에 명시적으로 추가
        try:
            import importlib.util
            from pathlib import Path as _Path
            _ort_spec = importlib.util.find_spec("onnxruntime")
            if _ort_spec and _ort_spec.submodule_search_locations:
                _ort_dir = _Path(list(_ort_spec.submodule_search_locations)[0])
                for _dll_dir in [_ort_dir, _ort_dir / "capi"]:
                    if _dll_dir.exists():
                        os.add_dll_directory(str(_dll_dir))
        except Exception:
            pass
    import onnxruntime as _onnxruntime_preload  # noqa: F401
except Exception as _onnx_e:  # pragma: no cover
    print(f"[startup] onnxruntime 선제 로드 실패(Supertonic 만 영향): {_onnx_e}")

from logging_config import setup_logging
from core.launcher import main
from core.shutdown_handler import setup_signal_handlers
from core.watchdog import (
    can_schedule_restart,
    get_restart_backoff_seconds,
    is_restart_requested,
    record_restart_scheduled,
    restart_process,
)

# 깊은 task tree 에서 Task.cancel 자식 전파가 RecursionError 를 일으키는 경우에 대한 안전망.
sys.setrecursionlimit(3000)

setup_logging()
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    setup_signal_handlers()

    try:
        asyncio.run(main())
        # 워치독 또는 !재시작 명령어가 재시작을 요청한 경우 프로세스를 다시 실행
        if is_restart_requested():
            if can_schedule_restart():
                backoff = get_restart_backoff_seconds()
                if backoff > 0:
                    logger.warning(f"재시작 백오프 {backoff:.0f}초 대기 후 프로세스를 재실행합니다.")
                    time.sleep(backoff)
                record_restart_scheduled()
                restart_process()
            else:
                logger.error("재시작 한도를 초과해 프로세스를 종료합니다.")
                sys.exit(1)
    except KeyboardInterrupt:
        logger.info("프로그램이 종료되었습니다.")
    except ValueError as e:
        logger.error(f"설정 오류: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"프로그램 실행 중 오류: {e}", exc_info=True)
        sys.exit(1)
