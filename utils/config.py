"""설정 상수 및 환경 변수 관리"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv(dotenv_path=Path(__file__).parent.parent / 'TOKEN.env')

# 경로 설정
BASE_DIR = Path(__file__).parent.parent
FFMPEG_PATH = os.getenv('FFMPEG_PATH', r'C:\Users\User\ffmpeg-2024-10-21-git-baa23e40c1-full_build\bin\ffmpeg.exe')

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

