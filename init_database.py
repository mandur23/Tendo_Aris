"""MySQL 데이터베이스 초기화 스크립트"""
import asyncio
import logging
import sys
from pathlib import Path
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv(dotenv_path=Path(__file__).parent / 'TOKEN.env')

# 상대 경로 추가
sys.path.insert(0, str(Path(__file__).parent))

from utils.config import USE_MYSQL, MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
from utils.db_utils import init_db_pool, close_db_pool, create_tables

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    """데이터베이스 초기화"""
    logger.info("=" * 60)
    logger.info("MySQL 데이터베이스 초기화 시작")
    logger.info("=" * 60)
    
    if not USE_MYSQL:
        logger.warning("MySQL이 활성화되어 있지 않습니다.")
        logger.info("TOKEN.env 파일에 다음을 추가하세요:")
        logger.info("  USE_MYSQL=true")
        logger.info("  MYSQL_HOST=localhost")
        logger.info("  MYSQL_PORT=3306")
        logger.info("  MYSQL_USER=root")
        logger.info("  MYSQL_PASSWORD=your_password")
        logger.info("  MYSQL_DATABASE=tendo_aris")
        return
    
    logger.info(f"데이터베이스 연결 정보:")
    logger.info(f"  호스트: {MYSQL_HOST}:{MYSQL_PORT}")
    logger.info(f"  사용자: {MYSQL_USER}")
    logger.info(f"  데이터베이스: {MYSQL_DATABASE}")
    
    try:
        logger.info("데이터베이스 연결 중...")
        await init_db_pool()
        
        logger.info("테이블 생성 중...")
        await create_tables()
        
        logger.info("=" * 60)
        logger.info("데이터베이스 초기화가 완료되었습니다!")
        logger.info("=" * 60)
        logger.info("생성된 테이블:")
        logger.info("  - music_history: 음악 재생 히스토리")
        logger.info("  - playlists: 플레이리스트")
        
    except Exception as e:
        logger.error(f"데이터베이스 초기화 실패: {e}")
        logger.error("다음 사항을 확인하세요:")
        logger.error("  1. MySQL 서버가 실행 중인지 확인")
        logger.error("  2. 데이터베이스가 생성되어 있는지 확인")
        logger.error("  3. 사용자 권한이 올바른지 확인")
        logger.error("  4. TOKEN.env의 연결 정보가 올바른지 확인")
        sys.exit(1)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())

