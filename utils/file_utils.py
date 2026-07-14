import json
import logging
import os
import tempfile
from pathlib import Path
from utils.config import USE_MYSQL

logger = logging.getLogger(__name__)

# 데이터 디렉토리
DATA_DIR = Path('data')
DATA_DIR.mkdir(exist_ok=True)

HISTORY_FILE = str(DATA_DIR / 'history.json')
PLAYLISTS_FILE = str(DATA_DIR / 'playlists.json')
TTS_SETTINGS_FILE = str(DATA_DIR / 'tts_settings.json')
TRPG_SAVES_FILE = str(DATA_DIR / 'trpg_saves.json')
TRPG_PARTY_SAVES_FILE = str(DATA_DIR / 'trpg_party_saves.json')
LOGS_DIR = Path('logs')


def atomic_write_json(path, data):
    """원자적 JSON 파일 쓰기 (임시 파일 사용 후 교체)"""
    dir_path = os.path.dirname(path) or "."
    fd = None
    tmp_path = None
    try:
        # 임시 파일 생성
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=".tmp_", suffix=".json")
        
        # 임시 파일에 쓰기
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        
        # 원자적 교체
        os.replace(tmp_path, path)
        fd = None  # 성공 시 fd는 이미 닫힘
        tmp_path = None
    except Exception as e:
        logger.error(f"원자적 쓰기 실패 ({path}): {e}")
        raise
    finally:
        # 정리: 실패 시 임시 파일 삭제
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_history_from_json_file():
    """JSON 파일에서 히스토리를 직접 로드합니다. (fallback용)"""
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content:
                return {}
            return json.loads(content)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.warning(f"히스토리 파일이 손상되었습니다. 새로운 히스토리를 시작합니다: {e}")
        # 백업 시도
        backup_path = f"{HISTORY_FILE}.backup"
        if os.path.exists(HISTORY_FILE):
            try:
                os.rename(HISTORY_FILE, backup_path)
                logger.info(f"손상된 파일을 {backup_path}로 백업했습니다.")
            except OSError:
                pass
        return {}


def load_history():
    """히스토리를 로드합니다. (MySQL 사용 시 비동기 함수 사용 필요)"""
    if USE_MYSQL:
        logger.warning("MySQL이 활성화되어 있습니다. load_history_from_db()를 사용하세요.")
        return {}
    return load_history_from_json_file()


def save_history(history):
    """히스토리를 저장합니다. (MySQL 사용 시 비동기 함수 사용 필요)"""
    if USE_MYSQL:
        logger.warning("MySQL이 활성화되어 있습니다. save_history_to_db()를 사용하세요.")
        return
    try:
        atomic_write_json(HISTORY_FILE, history)
    except Exception as e:
        logger.error(f"히스토리를 저장하는 중 오류가 발생했습니다: {e}")


def load_playlists_from_json_file():
    """JSON 파일에서 플레이리스트를 직접 로드합니다. (fallback용)"""
    try:
        with open(PLAYLISTS_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content:
                return {}
            return json.loads(content)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.warning(f"플레이리스트 파일이 손상되었습니다. 새로운 플레이리스트를 시작합니다: {e}")
        # 백업 시도
        backup_path = f"{PLAYLISTS_FILE}.backup"
        if os.path.exists(PLAYLISTS_FILE):
            try:
                os.rename(PLAYLISTS_FILE, backup_path)
                logger.info(f"손상된 파일을 {backup_path}로 백업했습니다.")
            except OSError:
                pass
        return {}


def load_playlists():
    """플레이리스트를 로드합니다. (MySQL 사용 시 비동기 함수 사용 필요)"""
    if USE_MYSQL:
        logger.warning("MySQL이 활성화되어 있습니다. load_playlists_from_db()를 사용하세요.")
        return {}
    return load_playlists_from_json_file()


def save_playlists(playlists):
    """플레이리스트를 저장합니다. (MySQL 사용 시 비동기 함수 사용 필요)"""
    if USE_MYSQL:
        logger.warning("MySQL이 활성화되어 있습니다. save_playlists_to_db()를 사용하세요.")
        return
    try:
        atomic_write_json(PLAYLISTS_FILE, playlists)
    except Exception as e:
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
    except json.JSONDecodeError as e:
        logger.warning(f"TTS 설정 파일이 손상되었습니다. 새로운 설정을 시작합니다: {e}")
        # 백업 시도
        backup_path = f"{TTS_SETTINGS_FILE}.backup"
        if os.path.exists(TTS_SETTINGS_FILE):
            try:
                os.rename(TTS_SETTINGS_FILE, backup_path)
                logger.info(f"손상된 파일을 {backup_path}로 백업했습니다.")
            except OSError:
                pass
        return {}


def save_tts_settings(tts_settings):
    """TTS 설정을 파일에 원자적으로 저장합니다."""
    try:
        atomic_write_json(TTS_SETTINGS_FILE, tts_settings)
    except Exception as e:
        logger.error(f"TTS 설정을 저장하는 중 오류가 발생했습니다: {e}")


def load_trpg_saves():
    """TRPG 세이브 데이터를 파일에서 로드합니다."""
    try:
        with open(TRPG_SAVES_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content:
                return {}
            return json.loads(content)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.warning(f"TRPG 세이브 파일이 손상되었습니다. 새로운 세이브를 시작합니다: {e}")
        # 백업 시도
        backup_path = f"{TRPG_SAVES_FILE}.backup"
        if os.path.exists(TRPG_SAVES_FILE):
            try:
                os.rename(TRPG_SAVES_FILE, backup_path)
                logger.info(f"손상된 파일을 {backup_path}로 백업했습니다.")
            except OSError:
                pass
        return {}


def save_trpg_saves(saves):
    """TRPG 세이브 데이터를 파일에 원자적으로 저장합니다."""
    try:
        atomic_write_json(TRPG_SAVES_FILE, saves)
    except Exception as e:
        logger.error(f"TRPG 세이브를 저장하는 중 오류가 발생했습니다: {e}")


def load_trpg_party_saves():
    """파티 TRPG 세이브 데이터를 파일에서 로드합니다."""
    try:
        with open(TRPG_PARTY_SAVES_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content:
                return {}
            return json.loads(content)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.warning(f"파티 TRPG 세이브 파일이 손상되었습니다. 새로운 세이브를 시작합니다: {e}")
        # 백업 시도
        backup_path = f"{TRPG_PARTY_SAVES_FILE}.backup"
        if os.path.exists(TRPG_PARTY_SAVES_FILE):
            try:
                os.rename(TRPG_PARTY_SAVES_FILE, backup_path)
                logger.info(f"손상된 파일을 {backup_path}로 백업했습니다.")
            except OSError:
                pass
        return {}


def save_trpg_party_saves(saves):
    """파티 TRPG 세이브 데이터를 파일에 원자적으로 저장합니다."""
    try:
        atomic_write_json(TRPG_PARTY_SAVES_FILE, saves)
    except Exception as e:
        logger.error(f"파티 TRPG 세이브를 저장하는 중 오류가 발생했습니다: {e}")


def ensure_logs_dir():
    """logs 디렉토리가 존재하는지 확인하고 없으면 생성합니다."""
    LOGS_DIR.mkdir(exist_ok=True)

