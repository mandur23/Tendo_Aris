"""Coqui TTS 유틸리티"""
import logging
from pathlib import Path
import tempfile
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Coqui TTS 사용 가능 여부 확인 (lazy import)
COQUI_AVAILABLE = None
_TTS_CLASS = None

def _check_coqui_available():
    """Coqui TTS 사용 가능 여부를 확인합니다 (lazy check)."""
    global COQUI_AVAILABLE, _TTS_CLASS
    
    if COQUI_AVAILABLE is not None:
        return COQUI_AVAILABLE
    
    try:
        from TTS.api import TTS
        _TTS_CLASS = TTS
        COQUI_AVAILABLE = True
        return True
    except (ImportError, OSError, Exception) as e:
        COQUI_AVAILABLE = False
        logger.warning(f"Coqui TTS를 로드할 수 없습니다: {e}")
        return False

# TTS 임시 파일 저장 경로
TTS_TEMP_DIR = Path('logs/tts_temp')
TTS_TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Coqui TTS 인스턴스 (전역)
_coqui_tts_instance = None
_executor = ThreadPoolExecutor(max_workers=2)

# 지원하는 Coqui TTS 모델
COQUI_MODELS = {
    'ko': {
        'tts_models/ko/korean/jets': {'name': '한국어 JETS', 'speaker': None},
        'tts_models/multilingual/multi-dataset/your_tts': {'name': '한국어 YourTTS', 'speaker': None},
    },
    'en': {
        'tts_models/en/ljspeech/tacotron2-DDC': {'name': '영어 Tacotron2', 'speaker': None},
        'tts_models/en/ljspeech/glow-tts': {'name': '영어 Glow-TTS', 'speaker': None},
        'tts_models/en/vctk/vits': {'name': '영어 VITS (다중 화자)', 'speaker': 'p225'},
        'tts_models/en/ek1/tacotron2': {'name': '영어 Tacotron2 (EK1)', 'speaker': None},
        'tts_models/en/ljspeech/speedy-speech': {'name': '영어 Speedy Speech', 'speaker': None},
        'tts_models/en/ljspeech/tacotron2-DCA': {'name': '영어 Tacotron2-DCA', 'speaker': None},
        'tts_models/en/ljspeech/neural_hmm': {'name': '영어 Neural HMM', 'speaker': None},
        'tts_models/en/ljspeech/fast_pitch': {'name': '영어 FastPitch', 'speaker': None},
        'tts_models/en/ljspeech/overflow': {'name': '영어 Overflow', 'speaker': None},
        'tts_models/en/ljspeech/neural_hmm': {'name': '영어 Neural HMM', 'speaker': None},
    },
    'ja': {
        'tts_models/ja/kokoro/tacotron2-DDC': {'name': '일본어 Kokoro', 'speaker': None},
    },
    'zh': {
        'tts_models/zh-CN/baker/tacotron2-DDC-GST': {'name': '중국어 Baker', 'speaker': None},
    },
    'es': {
        'tts_models/es/mai/tacotron2-DDC': {'name': '스페인어 Mai', 'speaker': None},
    },
    'fr': {
        'tts_models/fr/mai/tacotron2-DDC': {'name': '프랑스어 Mai', 'speaker': None},
    },
    'de': {
        'tts_models/de/thorsten/tacotron2-DDC': {'name': '독일어 Thorsten', 'speaker': None},
    },
    'pt': {
        'tts_models/pt/cv/vits': {'name': '포르투갈어 VITS', 'speaker': None},
    },
}


def get_coqui_tts(model_name: str = None):
    """Coqui TTS 인스턴스를 가져옵니다."""
    global _coqui_tts_instance, _TTS_CLASS
    
    if not _check_coqui_available():
        raise ImportError("Coqui TTS가 설치되지 않았거나 로드할 수 없습니다. pip install TTS로 설치하세요.")
    
    # TTS 클래스가 없으면 다시 import 시도
    if _TTS_CLASS is None:
        from TTS.api import TTS
        _TTS_CLASS = TTS
    
    # 기본 모델 선택
    if model_name is None:
        model_name = 'tts_models/ko/korean/jets'  # 한국어 기본 모델
    
    # 모델이 변경되었거나 인스턴스가 없으면 새로 생성
    if _coqui_tts_instance is None or getattr(_coqui_tts_instance, 'model_name', None) != model_name:
        try:
            _coqui_tts_instance = _TTS_CLASS(model_name=model_name, progress_bar=False)
            logger.info(f"Coqui TTS 모델 로드 완료: {model_name}")
        except Exception as e:
            logger.error(f"Coqui TTS 모델 로드 실패: {e}")
            raise
    
    return _coqui_tts_instance


def text_to_speech_coqui(
    text: str,
    model_name: str = 'tts_models/ko/korean/jets',
    speaker: str = None,
    output_path: Path = None
) -> Path:
    """
    Coqui TTS를 사용하여 텍스트를 음성 파일로 변환합니다.
    
    Args:
        text: 변환할 텍스트
        model_name: 사용할 모델 이름
        speaker: 화자 ID (모델이 다중 화자를 지원하는 경우)
        output_path: 출력 파일 경로 (None이면 임시 파일 생성)
    
    Returns:
        생성된 음성 파일 경로
    """
    if not _check_coqui_available():
        raise ImportError("Coqui TTS가 설치되지 않았거나 로드할 수 없습니다.")
    
    try:
        # 출력 파일 경로 설정
        if output_path is None:
            temp_file = tempfile.NamedTemporaryFile(
                delete=False,
                suffix='.wav',
                dir=TTS_TEMP_DIR
            )
            output_path = Path(temp_file.name)
            temp_file.close()
        
        # TTS 인스턴스 가져오기
        tts = get_coqui_tts(model_name)
        
        # 음성 생성 (동기 함수이므로 스레드 풀에서 실행)
        if speaker:
            tts.tts_to_file(text=text, file_path=str(output_path), speaker=speaker)
        else:
            tts.tts_to_file(text=text, file_path=str(output_path))
        
        logger.info(f"Coqui TTS 파일 생성 완료: {output_path.name} (모델: {model_name}, 텍스트 길이: {len(text)})")
        return output_path
        
    except Exception as e:
        logger.error(f"Coqui TTS 생성 중 오류: {e}")
        raise


async def text_to_speech_coqui_async(
    text: str,
    model_name: str = 'tts_models/ko/korean/jets',
    speaker: str = None
) -> Path:
    """
    Coqui TTS를 비동기로 실행합니다.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor,
        text_to_speech_coqui,
        text,
        model_name,
        speaker
    )


def list_available_models():
    """사용 가능한 Coqui TTS 모델 목록을 반환합니다."""
    if not _check_coqui_available():
        return {}
    
    try:
        if _TTS_CLASS is None:
            from TTS.api import TTS
            _TTS_CLASS = TTS
        tts = _TTS_CLASS()
        available_models = tts.list_models()
        return available_models
    except Exception as e:
        logger.error(f"모델 목록 가져오기 실패: {e}")
        return []


def get_model_info(model_name: str):
    """모델 정보를 가져옵니다."""
    if not _check_coqui_available():
        return None
    
    try:
        if _TTS_CLASS is None:
            from TTS.api import TTS
            _TTS_CLASS = TTS
        tts = _TTS_CLASS(model_name=model_name)
        return {
            'name': model_name,
            'language': getattr(tts, 'language', None),
            'speakers': getattr(tts, 'speakers', None),
        }
    except Exception as e:
        logger.error(f"모델 정보 가져오기 실패: {e}")
        return None


