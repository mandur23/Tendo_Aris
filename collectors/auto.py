import logging
import platform
import shutil
from typing import List, Dict

from .base import GPUCollector, GPUReading

logger = logging.getLogger(__name__)

# 감지 진단 정보 저장소 (현재 프로세스 내)
_detect_reasons: List[str] = []
_active_collector_name: str = ""


class NullCollector(GPUCollector):
    """GPU 미탐지/의존성 부재 환경을 위한 폴백 수집기."""

    def snapshot(self) -> List[GPUReading]:
        return []


def get_collector() -> GPUCollector:
    """GPU 브랜드/OS 자동 감지 후 적절한 수집기 인스턴스를 반환합니다.
    감지 실패 또는 선택적 의존성 부재 시 NullCollector를 반환합니다.
    """
    # NVIDIA 우선 (pynvml)
    try:
        import pynvml  # noqa: F401
        from .nvidia import NvidiaCollector
        logger.info("GPU 수집기: NVIDIA(pynvml) 감지")
        col = NvidiaCollector()
        _detect_reasons.append("NVIDIA: pynvml 사용")
        global _active_collector_name
        _active_collector_name = "NvidiaCollector"
        return col
    except Exception as e:
        _detect_reasons.append(f"NVIDIA: 사용 불가 - {e}")
        logger.debug(f"NVIDIA 수집기 비활성화: {e}")

    system = platform.system()

    # AMD Linux (rocm-smi)
    try:
        if system == 'Linux' and shutil.which('rocm-smi'):
            from .amd_linux import AMDLinuxCollector
            logger.info("GPU 수집기: AMD(rocm-smi) 감지")
            col = AMDLinuxCollector()
            _detect_reasons.append("AMD Linux: rocm-smi 사용")
            _active_collector_name = "AMDLinuxCollector"
            return col
        else:
            if system != 'Linux':
                _detect_reasons.append(f"AMD Linux: 사용 불가 - OS가 Linux가 아님({system})")
            elif not shutil.which('rocm-smi'):
                _detect_reasons.append("AMD Linux: 사용 불가 - rocm-smi 미설치")
    except Exception as e:
        _detect_reasons.append(f"AMD Linux: 사용 불가 - {e}")
        logger.debug(f"AMD Linux 수집기 비활성화: {e}")

    # AMD Windows (WMI)
    try:
        if system == 'Windows':
            import wmi  # noqa: F401
            from .amd_windows import AMDWindowsCollector
            logger.info("GPU 수집기: AMD(WMI) 감지")
            col = AMDWindowsCollector()
            _detect_reasons.append("AMD Windows: WMI 사용")
            _active_collector_name = "AMDWindowsCollector"
            return col
        else:
            _detect_reasons.append(f"AMD Windows: 사용 불가 - OS가 Windows가 아님({system})")
    except Exception as e:
        _detect_reasons.append(f"AMD Windows: 사용 불가 - {e}")
        logger.debug(f"AMD Windows 수집기 비활성화: {e}")

    # 미탐지 또는 선택적 의존성 없음: 폴백 수집기로 대체하고 정보 로그만 남김
    logger.info("GPU 수집기 감지 실패: GPU 미탐지 또는 선택적 의존성 없음 -> NullCollector 사용")
    _active_collector_name = "NullCollector"
    if not _detect_reasons:
        _detect_reasons.append("어떤 수집기도 활성화되지 않았습니다. GPU 미탐지 또는 선택적 의존성 없음")
    return NullCollector()


def get_gpu_detect_diagnostics() -> Dict[str, object]:
    """GPU 수집기 감지 과정의 요약 진단 정보를 반환합니다.

    Returns:
        {
          'active': str,          # 선택된 수집기 이름 (예: 'NvidiaCollector', 'NullCollector')
          'reasons': List[str],   # 감지 실패/성공 로그의 요약 문자열 리스트
        }
    """
    return {
        "active": _active_collector_name or "",
        "reasons": list(_detect_reasons),
    }
