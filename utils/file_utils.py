import datetime
import json
import logging
import os
import tempfile
import threading
import zipfile
from pathlib import Path
from utils.config import USE_MYSQL

logger = logging.getLogger(__name__)


class JsonStorageError(Exception):
    """JSON 저장소가 손상되어 안전하게 갱신할 수 없을 때 발생합니다."""


# JSON 세이브 파일의 load-modify-save 를 직렬화하는 프로세스 전역 락.
# 여러 세션이 asyncio.to_thread 로 동시에 저장하면 같은 스냅샷을 읽고 서로의
# 변경을 덮어쓸 수 있어, 갱신 경로는 반드시 이 락 아래에서 수행한다.
_JSON_UPDATE_LOCK = threading.Lock()

# 데이터 디렉토리
DATA_DIR = Path('data')
DATA_DIR.mkdir(exist_ok=True)

HISTORY_FILE = str(DATA_DIR / 'history.json')
PLAYLISTS_FILE = str(DATA_DIR / 'playlists.json')
TTS_SETTINGS_FILE = str(DATA_DIR / 'tts_settings.json')
TRPG_SAVES_FILE = str(DATA_DIR / 'trpg_saves.json')
TRPG_PARTY_SAVES_FILE = str(DATA_DIR / 'trpg_party_saves.json')
TRPG_WORLD_SAVES_FILE = str(DATA_DIR / 'trpg_world_saves.json')
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
            # fdopen 이 성공하면 파일 객체가 fd 소유권을 가지므로,
            # 이후 실패 시 finally 에서 이중으로 닫지 않도록 표시한다.
            fd = None
            json.dump(data, f, ensure_ascii=False, indent=4)
        
        # 원자적 교체
        os.replace(tmp_path, path)
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


def _load_json_file(path, label, *, allow_empty_on_corrupt: bool = False):
    """JSON 파일을 로드합니다. 손상 시 타임스탬프 백업으로 보존합니다.

    allow_empty_on_corrupt 가 False 이면 JsonStorageError 를 발생시켜
    손상 직후 단일 키만 담긴 dict 로 전체 파일을 덮어쓰는 것을 막습니다.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content:
                return {}
            return json.loads(content)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = f"{path}.corrupt_{stamp}"
        try:
            os.replace(path, backup_path)
            logger.error(f"{label} 파일이 손상되어 {backup_path} 로 보존했습니다: {e}")
        except OSError as move_err:
            logger.error(f"{label} 파일이 손상되었고 백업 이동도 실패했습니다 ({move_err}): {e}")
        if allow_empty_on_corrupt:
            return {}
        raise JsonStorageError(
            f"{label} 파일이 손상되어 저장을 중단했습니다. "
            f"백업({backup_path})을 확인한 뒤 복구해주세요."
        ) from e


def _update_json_file(path, label, mutator):
    """파일 단위 락 아래에서 load → mutator(data) → 원자적 저장을 수행합니다.

    mutator 는 dict 를 받아 제자리에서 수정하고, 저장이 필요하면 True 를 반환한다.
    저장 실패는 호출자가 알 수 있도록 예외를 그대로 전파한다.
    """
    with _JSON_UPDATE_LOCK:
        data = _load_json_file(path, label)
        if mutator(data):
            atomic_write_json(path, data)
            return True
        return False


def load_history_from_json_file():
    """JSON 파일에서 히스토리를 직접 로드합니다. (fallback용)"""
    return _load_json_file(HISTORY_FILE, "히스토리")


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
    return _load_json_file(PLAYLISTS_FILE, "플레이리스트")


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
    return _load_json_file(TTS_SETTINGS_FILE, "TTS 설정")


def save_tts_settings(tts_settings):
    """TTS 설정을 파일에 원자적으로 저장합니다."""
    try:
        atomic_write_json(TTS_SETTINGS_FILE, tts_settings)
    except Exception as e:
        logger.error(f"TTS 설정을 저장하는 중 오류가 발생했습니다: {e}")


def atomic_write_text(path, content: str, *, encoding: str = "utf-8") -> None:
    """텍스트 파일을 임시 파일에 쓴 뒤 원자적으로 교체합니다."""
    dir_path = os.path.dirname(path) or "."
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=".tmp_", suffix=".txt")
        with os.fdopen(fd, "w", encoding=encoding) as f:
            fd = None
            f.write(content)
        os.replace(tmp_path, path)
        tmp_path = None
    except Exception as e:
        logger.error(f"원자적 텍스트 쓰기 실패 ({path}): {e}")
        raise
    finally:
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


def load_trpg_saves():
    """TRPG 세이브 데이터를 파일에서 로드합니다."""
    return _load_json_file(TRPG_SAVES_FILE, "TRPG 세이브", allow_empty_on_corrupt=True)


def save_trpg_saves(saves):
    """TRPG 세이브 데이터를 파일에 원자적으로 저장합니다."""
    try:
        atomic_write_json(TRPG_SAVES_FILE, saves)
    except Exception as e:
        logger.error(f"TRPG 세이브를 저장하는 중 오류가 발생했습니다: {e}")


def load_trpg_party_saves():
    """파티 TRPG 세이브 데이터를 파일에서 로드합니다."""
    return _load_json_file(TRPG_PARTY_SAVES_FILE, "파티 TRPG 세이브", allow_empty_on_corrupt=True)


def save_trpg_party_saves(saves):
    """파티 TRPG 세이브 데이터를 파일에 원자적으로 저장합니다."""
    try:
        atomic_write_json(TRPG_PARTY_SAVES_FILE, saves)
    except Exception as e:
        logger.error(f"파티 TRPG 세이브를 저장하는 중 오류가 발생했습니다: {e}")


def load_trpg_world_saves():
    """자유 모험(공유 세계) 세이브 데이터를 파일에서 로드합니다."""
    return _load_json_file(TRPG_WORLD_SAVES_FILE, "자유 모험 세이브", allow_empty_on_corrupt=True)


def save_trpg_world_saves(saves):
    """자유 모험(공유 세계) 세이브 데이터를 파일에 원자적으로 저장합니다."""
    try:
        atomic_write_json(TRPG_WORLD_SAVES_FILE, saves)
    except Exception as e:
        logger.error(f"자유 모험 세이브를 저장하는 중 오류가 발생했습니다: {e}")


# --------------------------------------------------------- 세이브 갱신 (락 직렬화)
# 여러 세션의 동시 자동저장이 서로의 키를 덮어쓰지 않도록,
# 세이브 단건 갱신/삭제는 아래 함수들만 사용해야 한다. 저장 실패는 예외로 전파된다.

def set_trpg_save(key_str, adv_dict):
    """1인 모험 세이브 한 건을 락 아래에서 갱신합니다."""
    def _set(saves):
        saves[key_str] = adv_dict
        return True
    _update_json_file(TRPG_SAVES_FILE, "TRPG 세이브", _set)


def delete_trpg_save(key_str):
    """1인 모험 세이브 한 건을 락 아래에서 삭제합니다. 삭제 여부를 반환합니다."""
    def _delete(saves):
        return saves.pop(key_str, None) is not None
    return _update_json_file(TRPG_SAVES_FILE, "TRPG 세이브", _delete)


def set_trpg_party_save(key_str, adv_dict):
    """파티 모험 세이브 한 건을 락 아래에서 갱신합니다."""
    def _set(saves):
        saves[key_str] = adv_dict
        return True
    _update_json_file(TRPG_PARTY_SAVES_FILE, "파티 TRPG 세이브", _set)


def delete_trpg_party_save(key_str):
    """파티 모험 세이브 한 건을 락 아래에서 삭제합니다. 삭제 여부를 반환합니다."""
    def _delete(saves):
        return saves.pop(key_str, None) is not None
    return _update_json_file(TRPG_PARTY_SAVES_FILE, "파티 TRPG 세이브", _delete)


def set_trpg_world_save(key_str, adv_dict):
    """자유 모험 세이브 한 건을 락 아래에서 갱신합니다."""
    def _set(saves):
        saves[key_str] = adv_dict
        return True
    _update_json_file(TRPG_WORLD_SAVES_FILE, "자유 모험 세이브", _set)


def delete_trpg_world_save(key_str):
    """자유 모험 세이브 한 건을 락 아래에서 삭제합니다. 삭제 여부를 반환합니다."""
    def _delete(saves):
        return saves.pop(key_str, None) is not None
    return _update_json_file(TRPG_WORLD_SAVES_FILE, "자유 모험 세이브", _delete)


def ensure_logs_dir():
    """logs 디렉토리가 존재하는지 확인하고 없으면 생성합니다."""
    LOGS_DIR.mkdir(exist_ok=True)


# ------------------------------------------------------------------ 데이터 백업
BACKUPS_DIR = Path('backups')
BACKUP_MAX_KEEP = 14   # 보관할 일일 백업 개수 (약 2주치)


def backup_data_dir(max_keep: int = BACKUP_MAX_KEEP):
    """data/ 폴더 전체를 하루 1회 zip 으로 백업합니다.

    세이브·플레이리스트·설정이 디스크 문제나 실수로 날아가는 것을 대비한다.
    오늘자 백업이 이미 있으면 건너뛰고 None, 새로 만들면 경로 문자열을 반환한다.
    실패해도 예외를 던지지 않는다 (봇 시작을 막지 않기 위해).
    """
    try:
        BACKUPS_DIR.mkdir(exist_ok=True)
        stamp = datetime.date.today().isoformat()
        target = BACKUPS_DIR / f"data_backup_{stamp}.zip"
        if target.exists():
            return None

        files = [p for p in DATA_DIR.rglob('*') if p.is_file()]
        if not files:
            return None

        # 임시 파일에 만든 뒤 교체해 불완전한 zip 이 남지 않게 한다.
        tmp_path = target.with_suffix('.zip.tmp')
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, path.relative_to(DATA_DIR.parent))
        os.replace(tmp_path, target)

        # 오래된 백업 정리
        backups = sorted(BACKUPS_DIR.glob('data_backup_*.zip'))
        for old in backups[:-max_keep]:
            try:
                old.unlink()
            except OSError:
                pass

        logger.info(f"데이터 백업 생성: {target} ({len(files)}개 파일)")
        return str(target)
    except Exception as e:
        logger.error(f"데이터 백업 실패 (무시하고 계속): {e}")
        return None

