import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

HISTORY_FILE = 'history.json'
PLAYLISTS_FILE = 'playlists.json'
TTS_SETTINGS_FILE = 'tts_settings.json'
LOGS_DIR = Path('logs')


def load_history():
    """히스토리를 파일에서 로드합니다."""
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content:
                return {}
            return json.loads(content)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        logger.warning("히스토리 파일이 손상되었습니다. 새로운 히스토리를 시작합니다.")
        return {}


def save_history(history):
    """히스토리를 파일에 저장합니다."""
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=4)
    except IOError as e:
        logger.error(f"히스토리를 저장하는 중 오류가 발생했습니다: {e}")


def load_playlists():
    """플레이리스트를 파일에서 로드합니다."""
    try:
        with open(PLAYLISTS_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content:
                return {}
            return json.loads(content)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        logger.warning("플레이리스트 파일이 손상되었습니다. 새로운 플레이리스트를 시작합니다.")
        return {}


def save_playlists(playlists):
    """플레이리스트를 파일에 저장합니다."""
    try:
        with open(PLAYLISTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(playlists, f, ensure_ascii=False, indent=4)
    except IOError as e:
        logger.error(f"플레이리스트를 저장하는 중 오류가 발생했습니다: {e}")


def load_tts_settings():
    """TTS 설정을 파일에서 로드합니다."""
    try:
        with open(TTS_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content:
                return {}
            return json.loads(content)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        logger.warning("TTS 설정 파일이 손상되었습니다. 새로운 설정을 시작합니다.")
        return {}


def save_tts_settings(tts_settings):
    """TTS 설정을 파일에 저장합니다."""
    try:
        with open(TTS_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(tts_settings, f, ensure_ascii=False, indent=4)
    except IOError as e:
        logger.error(f"TTS 설정을 저장하는 중 오류가 발생했습니다: {e}")


def ensure_logs_dir():
    """logs 디렉토리가 존재하는지 확인하고 없으면 생성합니다."""
    LOGS_DIR.mkdir(exist_ok=True)

