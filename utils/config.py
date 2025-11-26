"""설정 상수 및 환경 변수 관리"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv(dotenv_path=Path(__file__).parent.parent / 'TOKEN.env')

# 경로 설정
BASE_DIR = Path(__file__).parent.parent
# FFmpeg 경로 설정 (환경 변수 또는 기본값)
# 하드코딩된 경로 대신 시스템 PATH에서 찾거나 환경 변수 사용
FFMPEG_PATH = os.getenv('FFMPEG_PATH', None)

# FFmpeg 경로 검증 및 설정
import logging
logger = logging.getLogger(__name__)

if FFMPEG_PATH and Path(FFMPEG_PATH).exists():
    logger.info(f"FFmpeg 경로 설정됨: {FFMPEG_PATH}")
else:
    # 시스템 PATH에서 ffmpeg 찾기 시도
    import shutil
    system_ffmpeg = shutil.which('ffmpeg')
    if system_ffmpeg:
        FFMPEG_PATH = system_ffmpeg
        logger.info(f"시스템 PATH에서 ffmpeg를 찾았습니다: {FFMPEG_PATH}")
    else:
        logger.warning("FFmpeg를 찾을 수 없습니다. 환경 변수 FFMPEG_PATH를 설정하거나, TOKEN.env 파일에 FFMPEG_PATH를 추가해주세요.")
        logger.warning("또는 시스템 PATH에 ffmpeg를 추가해주세요.")
        FFMPEG_PATH = None

# tts-with-rvc 라이브러리가 FFmpeg를 찾을 수 있도록 환경 변수 설정
if FFMPEG_PATH:
    # FFmpeg 디렉토리를 PATH에 추가 (tts-with-rvc가 찾을 수 있도록)
    ffmpeg_dir = str(Path(FFMPEG_PATH).parent)
    current_path = os.environ.get('PATH', '')
    if ffmpeg_dir not in current_path:
        os.environ['PATH'] = f"{ffmpeg_dir};{current_path}"
        logger.debug(f"FFmpeg 디렉토리를 PATH에 추가: {ffmpeg_dir}")

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

# MySQL 데이터베이스 설정
MYSQL_HOST = os.getenv('MYSQL_HOST', 'localhost')
MYSQL_PORT = int(os.getenv('MYSQL_PORT', '3306'))
MYSQL_USER = os.getenv('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', 'tendo_aris')
MYSQL_CHARSET = os.getenv('MYSQL_CHARSET', 'utf8mb4')

# DB 사용 여부 (True면 MySQL 사용, False면 JSON 파일 사용)
USE_MYSQL = os.getenv('USE_MYSQL', 'false').lower() == 'true'

# RVC GPU 설정
# 'auto': CUDA 사용 가능하면 GPU 사용, 아니면 CPU 사용
# 'cuda' 또는 'cuda:0': GPU 강제 사용 (CUDA 사용 불가능하면 오류 발생)
# 'cpu': CPU 강제 사용
RVC_DEVICE = os.getenv('RVC_DEVICE', 'auto').lower()

