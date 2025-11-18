import logging
import platform
import shutil

logger = logging.getLogger(__name__)


def get_collector():
    """GPU 브랜드/OS 자동 감지 후 적절한 수집기 인스턴스를 반환합니다.
    실패 시 None 반환.
    """
    # NVIDIA 우선 (pynvml)
    try:
        import pynvml  # noqa: F401
        from .nvidia import NvidiaCollector
        logger.info("GPU 수집기: NVIDIA(pynvml) 감지")
        return NvidiaCollector()
    except Exception as e:
        logger.debug(f"NVIDIA 수집기 비활성화: {e}")

    system = platform.system()

    # AMD Linux (rocm-smi)
    try:
        if system == 'Linux' and shutil.which('rocm-smi'):
            from .amd_linux import AMDLinuxCollector
            logger.info("GPU 수집기: AMD(rocm-smi) 감지")
            return AMDLinuxCollector()
    except Exception as e:
        logger.debug(f"AMD Linux 수집기 비활성화: {e}")

    # AMD Windows (WMI)
    try:
        if system == 'Windows':
            import wmi  # noqa: F401
            from .amd_windows import AMDWindowsCollector
            logger.info("GPU 수집기: AMD(WMI) 감지")
            return AMDWindowsCollector()
    except Exception as e:
        logger.debug(f"AMD Windows 수집기 비활성화: {e}")

    logger.warning("GPU 수집기 감지 실패: GPU 미탐지 또는 선택적 의존성 없음")
    return None
