"""TTS (Text-to-Speech) 유틸리티"""
import os
import logging
from pathlib import Path
from gtts import gTTS
import tempfile

# Coqui TTS 지원 (선택적, lazy import)
COQUI_AVAILABLE = False
COQUI_MODELS = {}
text_to_speech_coqui_async = None
list_available_models = None

def _lazy_import_coqui():
    """Coqui TTS를 지연 로딩합니다."""
    global COQUI_AVAILABLE, COQUI_MODELS, text_to_speech_coqui_async, list_available_models
    
    if COQUI_AVAILABLE is False and list_available_models is None:
        try:
            from utils.coqui_tts_utils import (
                _check_coqui_available,
                text_to_speech_coqui_async as _async_func,
                COQUI_MODELS as _models,
                list_available_models as _list_models
            )
            COQUI_AVAILABLE = _check_coqui_available()
            if COQUI_AVAILABLE:
                text_to_speech_coqui_async = _async_func
                COQUI_MODELS = _models
                list_available_models = _list_models
        except (ImportError, OSError, Exception) as e:
            logger.warning(f"Coqui TTS를 로드할 수 없습니다: {e}")
            COQUI_AVAILABLE = False
            COQUI_MODELS = {}

logger = logging.getLogger(__name__)

# TTS 임시 파일 저장 경로
TTS_TEMP_DIR = Path('logs/tts_temp')
TTS_TEMP_DIR.mkdir(parents=True, exist_ok=True)


# 지원하는 목소리 모델 (TLD 기반)
VOICE_MODELS = {
    'ko': {
        '기본': {'tld': 'com', 'name': '한국어 기본'},
    },
    'en': {
        '미국식': {'tld': 'com', 'name': '미국 영어'},
        '영국식': {'tld': 'co.uk', 'name': '영국 영어'},
        '호주식': {'tld': 'co.au', 'name': '호주 영어'},
        '캐나다식': {'tld': 'ca', 'name': '캐나다 영어'},
    },
    'ja': {
        '기본': {'tld': 'co.jp', 'name': '일본어 기본'},
    },
    'zh': {
        '기본': {'tld': 'com', 'name': '중국어 기본'},
    },
    'es': {
        '기본': {'tld': 'com', 'name': '스페인어 기본'},
        '스페인식': {'tld': 'es', 'name': '스페인 스페인어'},
    },
    'fr': {
        '기본': {'tld': 'com', 'name': '프랑스어 기본'},
        '프랑스식': {'tld': 'fr', 'name': '프랑스 프랑스어'},
    },
    'de': {
        '기본': {'tld': 'com', 'name': '독일어 기본'},
        '독일식': {'tld': 'de', 'name': '독일 독일어'},
    },
    'pt': {
        '기본': {'tld': 'com', 'name': '포르투갈어 기본'},
        '브라질식': {'tld': 'com.br', 'name': '브라질 포르투갈어'},
    },
}


def text_to_speech(
    text: str,
    lang: str = 'ko',
    slow: bool = False,
    tld: str = 'com',
    use_coqui: bool = False,
    coqui_model: str = None
) -> Path:
    """
    텍스트를 음성 파일로 변환합니다.
    
    Args:
        text: 변환할 텍스트
        lang: 언어 코드 (기본값: 'ko' - 한국어)
        slow: 느린 속도로 재생할지 여부 (gTTS만 지원)
        tld: Top Level Domain (목소리 모델 변경용, 기본값: 'com', gTTS만 지원)
        use_coqui: Coqui TTS 사용 여부 (True면 Coqui, False면 gTTS)
        coqui_model: Coqui TTS 모델 이름 (None이면 기본 모델 사용)
    
    Returns:
        생성된 음성 파일 경로
    """
    # Coqui TTS 사용
    if use_coqui:
        _lazy_import_coqui()
    
    if use_coqui and COQUI_AVAILABLE:
        if coqui_model is None:
            # 언어에 맞는 기본 모델 선택
            if lang in COQUI_MODELS:
                coqui_model = list(COQUI_MODELS[lang].keys())[0]
            else:
                coqui_model = 'tts_models/ko/korean/jets'  # 기본 모델
        
        # 동기 함수이므로 동기적으로 실행 (비동기는 호출하는 쪽에서 처리)
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 이미 실행 중인 루프가 있으면 동기적으로 실행
                from utils.coqui_tts_utils import text_to_speech_coqui
                return text_to_speech_coqui(text, coqui_model)
            else:
                return loop.run_until_complete(
                    text_to_speech_coqui_async(text, coqui_model)
                )
        except RuntimeError:
            # 루프가 없으면 새로 생성
            from utils.coqui_tts_utils import text_to_speech_coqui
            return text_to_speech_coqui(text, coqui_model)
    
    # gTTS 사용 (기본)
    try:
        # 임시 파일 생성
        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix='.mp3',
            dir=TTS_TEMP_DIR
        )
        temp_path = Path(temp_file.name)
        temp_file.close()
        
        # gTTS로 음성 생성 (tld 파라미터로 목소리 모델 변경)
        tts = gTTS(text=text, lang=lang, slow=slow, tld=tld)
        tts.save(str(temp_path))
        
        logger.info(f"gTTS 파일 생성 완료: {temp_path.name} (언어: {lang}, TLD: {tld}, 텍스트 길이: {len(text)})")
        return temp_path
        
    except Exception as e:
        logger.error(f"TTS 생성 중 오류: {e}")
        raise


def cleanup_tts_file(file_path: Path):
    """TTS 임시 파일을 삭제합니다."""
    try:
        if file_path.exists():
            file_path.unlink()
            logger.debug(f"TTS 임시 파일 삭제: {file_path.name}")
    except Exception as e:
        logger.warning(f"TTS 파일 삭제 실패: {file_path.name}: {e}")

