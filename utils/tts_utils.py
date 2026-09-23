"""TTS (Text-to-Speech) 유틸리티"""
import os
import time
import logging
from pathlib import Path
from gtts import gTTS
import tempfile

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
    tld: str = 'com'
) -> Path:
    """
    텍스트를 음성 파일로 변환합니다. (gTTS 사용)
    
    Args:
        text: 변환할 텍스트
        lang: 언어 코드 (기본값: 'ko' - 한국어)
        slow: 느린 속도로 재생할지 여부
        tld: Top Level Domain (목소리 모델 변경용, 기본값: 'com')
    
    Returns:
        생성된 음성 파일 경로
    """
    # gTTS 사용
    temp_path = None
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
        # 합성 전에 만들어 둔 빈 임시 파일이 쌓이지 않도록 지운다.
        if temp_path is not None:
            cleanup_tts_file(temp_path)
        raise


def cleanup_tts_file(file_path: Path):
    """TTS 임시 파일을 삭제합니다."""
    try:
        if file_path.exists():
            file_path.unlink()
            logger.debug(f"TTS 임시 파일 삭제: {file_path.name}")
    except Exception as e:
        logger.warning(f"TTS 파일 삭제 실패: {file_path.name}: {e}")


def sweep_stale_tts_files(max_age_seconds: float = 3600) -> int:
    """TTS 임시 폴더에서 오래된 파일을 지웁니다. 지운 개수를 반환합니다.

    비정상 종료나 외부 라이브러리(RVC 등)가 남긴 파일이 계속 쌓이지 않도록 시작 시 호출한다.
    재생 중일 수 있는 최근 파일은 건드리지 않는다.
    """
    now = time.time()
    removed = 0
    try:
        entries = list(TTS_TEMP_DIR.iterdir())
    except OSError:
        return 0
    for path in entries:
        try:
            if path.is_file() and now - path.stat().st_mtime > max_age_seconds:
                path.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info(f"오래된 TTS 임시 파일 {removed}개를 정리했습니다.")
    return removed
