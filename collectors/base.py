from typing import List, Dict, Any


class GPUReading(dict):
    """표준화된 GPU 스냅샷: util%, vram%, temp C, power W, brand/name 등."""
    pass


class GPUCollector:
    """GPU 수집기 인터페이스"""

    def snapshot(self) -> List[GPUReading]:
        """
        GPU 상태 스냅샷 리스트를 반환합니다.
        실패 시 빈 리스트를 반환하거나 예외를 발생시킬 수 있습니다.
        """
        raise NotImplementedError
