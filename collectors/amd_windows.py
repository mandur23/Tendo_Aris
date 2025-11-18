import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class AMDWindowsCollector:
    """Windows WMI 기반 AMD GPU 수집기.
    - Util/VRAM: WMI GPUPerformanceCounters
    - Temp/Power: 선택적으로 LibreHardwareMonitor HTTP JSON으로 보강(없으면 None)
    """

    def __init__(self, lhm_url: Optional[str] = None):
        try:
            import wmi  # type: ignore
        except Exception as e:
            raise RuntimeError(f"wmi 모듈 필요: {e}")
        self._wmi = wmi.WMI(namespace='root\\CIMV2')
        self._lhm_url = lhm_url

    def _util(self) -> float:
        try:
            engines = self._wmi.Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine()
            total = 0.0
            for e in engines:
                name = getattr(e, 'Name', '') or ''
                util = float(getattr(e, 'UtilizationPercentage', 0) or 0)
                if 'engtype_3D' in name or 'engtype_Compute' in name:
                    total += util
            # 일부 시스템에서 복수 엔진 합산이 100 초과할 수 있어 클램프
            return max(0.0, min(100.0, total))
        except Exception as e:
            logger.debug(f"WMI Util 조회 실패: {e}")
            return 0.0

    def _vram(self):
        try:
            mems = self._wmi.Win32_PerfFormattedData_GPUPerformanceCounters_GPUAdapterMemory()
            if not mems:
                return 0.0, None, None
            m = mems[0]
            used = int(getattr(m, 'DedicatedUsage', 0) or 0)
            total = int(getattr(m, 'DedicatedLimit', 0) or 0)
            pct = round(100.0 * used / total, 1) if total else 0.0
            return pct, used, total
        except Exception as e:
            logger.debug(f"WMI VRAM 조회 실패: {e}")
            return 0.0, None, None

    def _temp_power_from_lhm(self):
        if not self._lhm_url:
            return None, None
        try:
            import requests
            r = requests.get(self._lhm_url, timeout=0.5)
            j = r.json()
            temp, power = None, None

            def walk(n):
                nonlocal temp, power
                for s in n.get('Sensors', []) or []:
                    if 'GPU Core' in s.get('Name', '') and s.get('Type') == 'Temperature':
                        temp = s.get('Value')
                    if 'GPU Core' in s.get('Name', '') and s.get('Type') == 'Power':
                        power = s.get('Value')
                for c in n.get('Children', []) or []:
                    walk(c)

            walk(j)
            return temp, power
        except Exception as e:
            logger.debug(f"LibreHardwareMonitor 조회 실패: {e}")
            return None, None

    def snapshot(self) -> List[dict]:
        util = self._util()
        vram_pct, used, total = self._vram()
        temp, power = self._temp_power_from_lhm()
        return [{
            'index': 0,
            'name': 'AMD GPU',
            'util': util,
            'vram_percent': vram_pct,
            'vram_used': used,
            'vram_total': total,
            'temp': temp,
            'power': power,
            'brand': 'AMD',
        }]
