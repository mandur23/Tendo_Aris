"""설정 상수 및 환경 변수 관리"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv(dotenv_path=Path(__file__).parent.parent / 'TOKEN.env')

# 경로 설정
BASE_DIR = Path(__file__).parent.parent
# FFmpeg 경로 설정 (환경 변수 또는 기본값)
_default_ffmpeg_path = r'C:\Users\User\ffmpeg-2024-10-21-git-baa23e40c1-full_build\bin\ffmpeg.exe'
FFMPEG_PATH = os.getenv('FFMPEG_PATH', _default_ffmpeg_path)

# FFmpeg 경로 검증
if not Path(FFMPEG_PATH).exists():
    import logging
    logger = logging.getLogger(__name__)
    logger.warning(f"FFmpeg 경로를 찾을 수 없습니다: {FFMPEG_PATH}")
    logger.warning("환경 변수 FFMPEG_PATH를 설정하거나, TOKEN.env 파일에 FFMPEG_PATH를 추가해주세요.")
    # 시스템 PATH에서 ffmpeg 찾기 시도
    import shutil
    system_ffmpeg = shutil.which('ffmpeg')
    if system_ffmpeg:
        FFMPEG_PATH = system_ffmpeg
        logger.info(f"시스템 PATH에서 ffmpeg를 찾았습니다: {FFMPEG_PATH}")
    else:
        logger.error("FFmpeg를 찾을 수 없습니다. 음악 재생 기능이 정상적으로 작동하지 않을 수 있습니다.")

# 봇 설정
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN', '').strip()
COMMAND_PREFIX = '!'

# 음악 플레이어 설정
DEFAULT_VOLUME = 0.2
IDLE_TIMEOUT = 300  # 초
MAX_HISTORY_ITEMS = 100
QUEUE_TIMEOUT = 300  # 초

# 재시도 설정
MAX_RETRIES = 5
MAX_EXTRACT_RETRIES = 3
MAX_PLAY_RETRIES = 2
BASE_DELAY = 2

# 메시지 삭제 지연
COMMAND_MESSAGE_DELETE_DELAY = 3  # 초

# 연결/오프라인 감지 및 재시작 설정
OFFLINE_ALERT_WEBHOOK_URL = os.getenv('OFFLINE_ALERT_WEBHOOK_URL', '').strip()
OFFLINE_STARTUP_GRACE_SECONDS = int(os.getenv('OFFLINE_STARTUP_GRACE_SECONDS', '300'))
OFFLINE_RESTART_SECONDS = int(os.getenv('OFFLINE_RESTART_SECONDS', '1800'))
AUTO_RESTART_ON_OFFLINE = os.getenv('AUTO_RESTART_ON_OFFLINE', 'true').lower() == 'true'

# MySQL 데이터베이스 설정
MYSQL_HOST = os.getenv('MYSQL_HOST', 'localhost')
MYSQL_PORT = int(os.getenv('MYSQL_PORT', '3306'))
MYSQL_USER = os.getenv('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', 'tendo_aris')
MYSQL_CHARSET = os.getenv('MYSQL_CHARSET', 'utf8mb4')

# DB 사용 여부 (True면 MySQL 사용, False면 JSON 파일 사용)
USE_MYSQL = os.getenv('USE_MYSQL', 'false').lower() == 'true'

