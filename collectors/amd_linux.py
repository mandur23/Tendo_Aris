import json
import logging
import shutil
import subprocess
from typing import List

logger = logging.getLogger(__name__)


class AMDLinuxCollector:
    def __init__(self):
        if not shutil.which('rocm-smi'):
            raise RuntimeError('rocm-smi 명령을 찾을 수 없습니다. ROCm이 설치되어 있는지 확인하세요.')

    def _run_json(self, args, timeout: float = 0.7):
        try:
            p = subprocess.run(
                ['rocm-smi', *args, '--json'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError('rocm-smi 호출 타임아웃')
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip())
        try:
            return json.loads(p.stdout)
        except Exception as e:
            raise RuntimeError(f'rocm-smi JSON 파싱 실패: {e}')

    def snapshot(self) -> List[dict]:
        # 다양한 버전 호환 위해 한 번에 여러 메트릭 요청
        try:
            data = self._run_json(['--showuse', '--showmemuse', '--showtemp', '--showpower'])
        except Exception as e:
            logger.debug(f"AMDLinuxCollector 스냅샷 실패: {e}")
            return []

        out: List[dict] = []
        cards = []
        if isinstance(data, dict):
            # 일부 버전은 'card' 키 사용
            cards = data.get('card', []) or data.get('cards', [])
        elif isinstance(data, list):
            cards = data
        for idx, info in enumerate(cards):
            try:
                util = float(info.get('GPU use (%)', info.get('GPU use (%) ', 0)) or 0)
                vram_used = int(info.get('VRAM use (B)', 0) or 0)
                vram_total = int(info.get('VRAM total (B)', 0) or 0)
                vram_pct = round(100.0 * vram_used / vram_total, 1) if vram_total else 0.0
                temp = info.get('Temperature (Sensor edge) (C)')
                if temp is None:
                    temp = info.get('Temperature (Sensor edge) (Celsius)')
                power = info.get('Average Graphics Package Power (W)') or info.get('GPU power (W)')
                name = info.get('Card series') or info.get('CardSKU') or f'AMD GPU {idx}'
                out.append({
                    'index': idx,
                    'name': name,
                    'util': util,
                    'vram_percent': vram_pct,
                    'vram_used': vram_used,
                    'vram_total': vram_total,
                    'temp': float(temp) if temp is not None else None,
                    'power': float(power) if power is not None else None,
                    'brand': 'AMD',
                })
            except Exception as e:
                logger.debug(f"AMDLinuxCollector 카드 파싱 실패 idx={idx}: {e}")
        return out
