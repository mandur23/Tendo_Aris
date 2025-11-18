import logging
from typing import List


logger = logging.getLogger(__name__)


class NvidiaCollector:
    def __init__(self):
        import pynvml
        self.nvml = pynvml
        try:
            self.nvml.nvmlInit()
        except Exception as e:
            logger.warning(f"pynvml 초기화 실패: {e}")
            raise

    def snapshot(self) -> List[dict]:
        out: List[dict] = []
        try:
            count = self.nvml.nvmlDeviceGetCount()
        except Exception as e:
            logger.debug(f"NVML 장치 수 조회 실패: {e}")
            return out
        for i in range(count):
            try:
                h = self.nvml.nvmlDeviceGetHandleByIndex(i)
                util = self.nvml.nvmlDeviceGetUtilizationRates(h)
                mem = self.nvml.nvmlDeviceGetMemoryInfo(h)
                try:
                    name = self.nvml.nvmlDeviceGetName(h).decode()
                except Exception:
                    name = f"NVIDIA GPU {i}"
                try:
                    temp = self.nvml.nvmlDeviceGetTemperature(h, self.nvml.NVML_TEMPERATURE_GPU)
                except Exception:
                    temp = None
                try:
                    power = self.nvml.nvmlDeviceGetPowerUsage(h) / 1000.0
                except Exception:
                    power = None
                vram_percent = round(100.0 * mem.used / mem.total, 1) if getattr(mem, 'total', 0) else 0.0
                out.append({
                    "index": i,
                    "name": name,
                    "util": getattr(util, 'gpu', 0),
                    "vram_percent": vram_percent,
                    "vram_used": int(getattr(mem, 'used', 0) or 0),
                    "vram_total": int(getattr(mem, 'total', 0) or 0),
                    "temp": temp,
                    "power": power,
                    "brand": "NVIDIA",
                })
            except Exception as e:
                logger.debug(f"NVML 스냅샷 실패(index={i}): {e}")
        return out
