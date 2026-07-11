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

# 대화형 AI (로컬 LLM/Ollama) 설정
LOCAL_AI_BASE_URL = os.getenv('LOCAL_AI_BASE_URL', 'http://127.0.0.1:11434').strip() or 'http://127.0.0.1:11434'
LOCAL_AI_MODEL = os.getenv('LOCAL_AI_MODEL', 'llama3.1:8b').strip() or 'llama3.1:8b'

try:
    LOCAL_AI_MAX_PROMPT_CHARS = int(os.getenv('LOCAL_AI_MAX_PROMPT_CHARS', '2000'))
except (TypeError, ValueError):
    LOCAL_AI_MAX_PROMPT_CHARS = 2000
    if os.getenv('LOCAL_AI_MAX_PROMPT_CHARS') is not None:
        logger.warning("LOCAL_AI_MAX_PROMPT_CHARS가 숫자가 아니에요. 2000을 사용할게요.")

try:
    LOCAL_AI_TEMPERATURE = float(os.getenv('LOCAL_AI_TEMPERATURE', '0.7'))
except (TypeError, ValueError):
    LOCAL_AI_TEMPERATURE = 0.7
    if os.getenv('LOCAL_AI_TEMPERATURE') is not None:
        logger.warning("LOCAL_AI_TEMPERATURE가 숫자가 아니에요. 0.7을 사용할게요.")

try:
    LOCAL_AI_TIMEOUT_SECONDS = int(os.getenv('LOCAL_AI_TIMEOUT_SECONDS', '120'))
except (TypeError, ValueError):
    LOCAL_AI_TIMEOUT_SECONDS = 120
    if os.getenv('LOCAL_AI_TIMEOUT_SECONDS') is not None:
        logger.warning("LOCAL_AI_TIMEOUT_SECONDS가 숫자가 아니에요. 120초를 사용할게요.")

# 온도 범위 보정
LOCAL_AI_TEMPERATURE = max(0.0, min(2.0, LOCAL_AI_TEMPERATURE))

try:
    LOCAL_AI_NUM_CTX = int(os.getenv('LOCAL_AI_NUM_CTX', '8192'))
except (TypeError, ValueError):
    LOCAL_AI_NUM_CTX = 8192
    if os.getenv('LOCAL_AI_NUM_CTX') is not None:
        logger.warning("LOCAL_AI_NUM_CTX가 숫자가 아니에요. 8192를 사용할게요.")

try:
    LOCAL_AI_NUM_PREDICT = int(os.getenv('LOCAL_AI_NUM_PREDICT', '512'))
except (TypeError, ValueError):
    LOCAL_AI_NUM_PREDICT = 512
    if os.getenv('LOCAL_AI_NUM_PREDICT') is not None:
        logger.warning("LOCAL_AI_NUM_PREDICT가 숫자가 아니에요. 512를 사용할게요.")

# 모델을 메모리에 유지할 시간 (유휴 후 첫 응답 지연 방지). 예: '30m', '1h', '-1'(무제한)
LOCAL_AI_KEEP_ALIVE = os.getenv('LOCAL_AI_KEEP_ALIVE', '30m').strip() or '30m'

# 웹 관리자 대시보드 설정
# 보안상 기본값은 로컬(127.0.0.1) 전용이며, WEB_ADMIN_PASSWORD가 비어 있으면 시작하지 않는다.
WEB_ADMIN_ENABLED = os.getenv('WEB_ADMIN_ENABLED', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
WEB_ADMIN_HOST = os.getenv('WEB_ADMIN_HOST', '127.0.0.1').strip() or '127.0.0.1'
try:
    WEB_ADMIN_PORT = int(os.getenv('WEB_ADMIN_PORT', '8899'))
except (TypeError, ValueError):
    WEB_ADMIN_PORT = 8899
    if os.getenv('WEB_ADMIN_PORT') is not None:
        logger.warning("WEB_ADMIN_PORT가 숫자가 아니에요. 8899를 사용할게요.")
WEB_ADMIN_PASSWORD = os.getenv('WEB_ADMIN_PASSWORD', '').strip()

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
try:
    MYSQL_PORT = int(os.getenv('MYSQL_PORT', '3306'))
except (TypeError, ValueError):
    MYSQL_PORT = 3306
    if os.getenv('MYSQL_PORT') is not None:
        logger.warning("MYSQL_PORT가 숫자가 아니에요. 3306을 사용할게요. TOKEN.env에서 MYSQL_PORT를 숫자(예: 3306)로 설정해주세요.")
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

