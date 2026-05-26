"""Supertonic TTS 유틸리티 (공식 PyPI 패키지 `supertonic` 사용)

Supertonic v3 는 한국어(`ko`)를 포함한 31개 언어 + `na`(언어 미지정) 폴백을 지원하며,
ONNX Runtime 기반의 on-device 합성을 수행한다. 첫 실행 시 약 305MB 의 모델을 자동으로
Hugging Face Hub 에서 받는다.

Discord 봇의 TTS 흐름에 맞춰 다음을 제공한다.
- `check_supertonic_available()`            : 라이브러리(설치된 supertonic) 가용 여부
- `check_supertonic_model_exists()`         : 모델 자산이 디스크에 있는지 여부
- `download_supertonic_model()`             : 모델 자산을 미리 받기
- `text_to_speech_supertonic(text, ...)`    : 합성 후 WAV 파일 경로 반환
- `get_available_voice_presets()`           : 사용 가능한 음성 프리셋 목록
- `get_available_languages()`               : 사용 가능한 언어 코드 목록
- `DEFAULT_VOICE_PRESET`                    : 기본 음성 프리셋 (`M1`)
"""
import logging
import tempfile
import threading
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# TTS 임시 파일 저장 경로 (cogs/tts.py 와 호환)
TTS_TEMP_DIR = Path('logs/tts_temp')
TTS_TEMP_DIR.mkdir(parents=True, exist_ok=True)

# 합성 모델 식별자 (라이브러리 기본값 supertonic-3 사용)
_MODEL_NAME = 'supertonic-3'

# 음성 프리셋: Supertonic 가 공식 제공하는 10개 voice
# (M=male, F=female / 숫자가 클수록 새로 추가된 보이스)
_VOICE_PRESETS: List[str] = ['M1', 'M2', 'M3', 'M4', 'M5', 'F1', 'F2', 'F3', 'F4', 'F5']
DEFAULT_VOICE_PRESET = 'M1'

# 라이브러리/모델 가용성 캐시
_library_available: Optional[bool] = None

# TTS 인스턴스 캐시 (모델 로드는 비용이 크므로 1회만 로드 후 재사용)
# - 스레드 안전성을 위해 lock 사용
_tts_instance = None
_tts_instance_lock = threading.Lock()


def _check_library_available() -> bool:
    """공식 supertonic 라이브러리가 import 가능한지 확인한다."""
    global _library_available
    if _library_available is not None:
        return _library_available
    try:
        import supertonic  # noqa: F401
        _library_available = True
        logger.info("Supertonic 라이브러리 사용 가능 (`pip install supertonic`)")
    except ImportError as e:
        _library_available = False
        logger.warning(f"Supertonic 라이브러리 미설치: {e}. `pip install supertonic` 로 설치하세요.")
    return _library_available


def check_supertonic_available() -> bool:
    """외부에서 호출하는 호환용 alias."""
    return _check_library_available()


def _get_tts_instance(auto_download: bool = True):
    """TTS 인스턴스를 lazy 하게 로드해 캐시한다."""
    global _tts_instance
    if _tts_instance is not None:
        return _tts_instance

    with _tts_instance_lock:
        if _tts_instance is not None:
            return _tts_instance
        if not _check_library_available():
            return None
        try:
            from supertonic import TTS
            logger.info(f"Supertonic 모델 로드 중... (model={_MODEL_NAME}, auto_download={auto_download})")
            _tts_instance = TTS(model=_MODEL_NAME, auto_download=auto_download)
            logger.info("Supertonic 모델 로드 완료")
        except Exception as e:
            logger.error(f"Supertonic 모델 로드 실패: {e}", exc_info=True)
            _tts_instance = None
    return _tts_instance


def check_supertonic_model_exists() -> bool:
    """모델 자산이 로컬에 캐싱되어 있는지 가볍게 확인한다.

    공식 라이브러리는 모델을 Hugging Face Hub 캐시에 받는다. 정확한 경로를 모르더라도
    `auto_download=False` 로 TTS 인스턴스 생성을 시도해서 성공하면 자산이 존재하는 것으로
    간주한다. 한 번 인스턴스화에 성공하면 캐시되어 다음 호출은 즉시 True 를 반환한다.
    """
    global _tts_instance
    if _tts_instance is not None:
        return True
    if not _check_library_available():
        return False
    try:
        from supertonic import TTS
        # auto_download=False 로 시도. 자산이 없으면 예외 발생.
        with _tts_instance_lock:
            if _tts_instance is None:
                _tts_instance = TTS(model=_MODEL_NAME, auto_download=False)
        return True
    except Exception as e:
        logger.debug(f"Supertonic 모델 자산 미확인 (auto_download=False 실패): {e}")
        return False


def download_supertonic_model() -> bool:
    """모델 자산을 미리 다운로드한다 (auto_download=True 로 인스턴스 생성)."""
    return _get_tts_instance(auto_download=True) is not None


def get_available_voice_presets() -> List[str]:
    """사용 가능한 음성 프리셋 목록 (`M1` ~ `F5`) 을 반환한다."""
    return list(_VOICE_PRESETS)


def get_available_languages() -> List[str]:
    """라이브러리가 지원하는 언어 코드 목록을 반환한다."""
    if not _check_library_available():
        return []
    try:
        from supertonic import AVAILABLE_LANGUAGES
        return list(AVAILABLE_LANGUAGES)
    except Exception:
        return []


def text_to_speech_supertonic(
    text: str,
    voice_preset: str = DEFAULT_VOICE_PRESET,
    lang: str = 'en',
    total_steps: int = 5,
    speed: float = 1.05,
) -> Path:
    """Supertonic 으로 텍스트를 합성하여 WAV 파일 경로를 반환한다.

    Args:
        text: 합성할 텍스트.
        voice_preset: 음성 프리셋 이름 (M1~M5, F1~F5). 잘못된 값이면 기본 프리셋으로 대체.
        lang: 언어 코드 (`en`, `ko`, `ja`, ...). 지원 외 언어는 `na` 폴백.
        total_steps: denoising step 수 (높을수록 품질↑, 속도↓). 라이브러리 기본 5.
        speed: 발화 속도 배율 (라이브러리 권장 0.9 ~ 1.5).

    Returns:
        생성된 WAV 파일 경로.

    Raises:
        RuntimeError: 라이브러리 미설치/모델 로드 실패/합성 실패.
    """
    if not _check_library_available():
        raise RuntimeError(
            "Supertonic 라이브러리가 설치되어 있지 않습니다. `pip install supertonic` 로 설치하세요."
        )

    tts = _get_tts_instance(auto_download=True)
    if tts is None:
        raise RuntimeError("Supertonic 모델을 로드할 수 없습니다.")

    # 음성 프리셋 정상화
    voice_name = voice_preset if voice_preset in _VOICE_PRESETS else DEFAULT_VOICE_PRESET
    if voice_preset != voice_name:
        logger.warning(
            f"알 수 없는 voice_preset='{voice_preset}', 기본값 '{voice_name}' 로 대체합니다."
        )

    # 언어 코드 정상화 (지원 외는 'na' 로)
    available = set(get_available_languages())
    if lang not in available:
        fallback_lang = 'na' if 'na' in available else 'en'
        logger.warning(
            f"지원하지 않는 언어 코드 '{lang}', '{fallback_lang}' 로 대체합니다."
        )
        lang = fallback_lang

    try:
        style = tts.get_voice_style(voice_name=voice_name)
        wav, _duration = tts.synthesize(
            text=text,
            voice_style=style,
            total_steps=total_steps,
            speed=speed,
            lang=lang,
        )

        # 임시 WAV 파일 생성
        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix='.wav',
            dir=TTS_TEMP_DIR,
        )
        temp_path = Path(temp_file.name)
        temp_file.close()

        tts.save_audio(wav, str(temp_path))

        logger.info(
            f"Supertonic TTS 생성 완료: voice={voice_name}, lang={lang}, "
            f"len(text)={len(text)}, file={temp_path.name}"
        )
        return temp_path
    except Exception as e:
        logger.error(f"Supertonic TTS 합성 실패: {e}", exc_info=True)
        raise
