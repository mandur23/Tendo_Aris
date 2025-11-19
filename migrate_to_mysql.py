"""기존 JSON 파일 데이터를 MySQL 데이터베이스로 마이그레이션하는 스크립트"""
import asyncio
import json
import logging
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import sys

# 환경 변수 로드
load_dotenv(dotenv_path=Path(__file__).parent / 'TOKEN.env')

# 상대 경로 추가
sys.path.insert(0, str(Path(__file__).parent))

from utils.config import USE_MYSQL
from utils.db_utils import init_db_pool, close_db_pool, add_history_item_to_db, save_playlist_to_db
from utils.file_utils import load_history, load_playlists

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def migrate_history():
    """히스토리를 JSON에서 MySQL로 마이그레이션합니다."""
    if not USE_MYSQL:
        logger.error("MySQL이 활성화되어 있지 않습니다. TOKEN.env에서 USE_MYSQL=true로 설정하세요.")
        return False
    
    logger.info("히스토리 마이그레이션을 시작합니다...")
    
    # JSON 파일에서 히스토리 로드
    history = load_history()
    
    if not history:
        logger.info("마이그레이션할 히스토리가 없습니다.")
        return True
    
    try:
        await init_db_pool()
        
        total_items = 0
        for guild_id_str, items in history.items():
            guild_id = int(guild_id_str)
            logger.info(f"길드 {guild_id}의 히스토리 {len(items)}개 항목 마이그레이션 중...")
            
            for item in items:
                try:
                    title = item.get('title', '알 수 없는 제목')
                    url = item.get('url', '')
                    duration = item.get('duration', 0)
                    played_at_str = item.get('played_at', '')
                    
                    # played_at 문자열을 datetime으로 변환
                    try:
                        played_at = datetime.strptime(played_at_str, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        logger.warning(f"날짜 형식 오류: {played_at_str}, 현재 시간 사용")
                        played_at = datetime.now()
                    
                    await add_history_item_to_db(guild_id, title, url, duration, played_at)
                    total_items += 1
                except Exception as e:
                    logger.error(f"히스토리 항목 마이그레이션 실패: {item}, 오류: {e}")
        
        logger.info(f"히스토리 마이그레이션 완료: 총 {total_items}개 항목")
        return True
        
    except Exception as e:
        logger.error(f"히스토리 마이그레이션 실패: {e}")
        return False
    finally:
        await close_db_pool()


async def migrate_playlists():
    """플레이리스트를 JSON에서 MySQL로 마이그레이션합니다."""
    if not USE_MYSQL:
        logger.error("MySQL이 활성화되어 있지 않습니다. TOKEN.env에서 USE_MYSQL=true로 설정하세요.")
        return False
    
    logger.info("플레이리스트 마이그레이션을 시작합니다...")
    
    # JSON 파일에서 플레이리스트 로드
    playlists = load_playlists()
    
    if not playlists:
        logger.info("마이그레이션할 플레이리스트가 없습니다.")
        return True
    
    try:
        await init_db_pool()
        
        total_playlists = 0
        for guild_id_str, playlist_dict in playlists.items():
            guild_id = int(guild_id_str)
            logger.info(f"길드 {guild_id}의 플레이리스트 {len(playlist_dict)}개 마이그레이션 중...")
            
            for playlist_id, urls in playlist_dict.items():
                if urls:  # 빈 리스트가 아닌 경우만
                    try:
                        await save_playlist_to_db(guild_id, playlist_id, urls)
                        total_playlists += 1
                        logger.info(f"  - 플레이리스트 '{playlist_id}': {len(urls)}개 항목")
                    except Exception as e:
                        logger.error(f"플레이리스트 마이그레이션 실패: guild_id={guild_id}, playlist_id={playlist_id}, 오류: {e}")
        
        logger.info(f"플레이리스트 마이그레이션 완료: 총 {total_playlists}개 플레이리스트")
        return True
        
    except Exception as e:
        logger.error(f"플레이리스트 마이그레이션 실패: {e}")
        return False
    finally:
        await close_db_pool()


async def main():
    """메인 함수"""
    logger.info("=" * 60)
    logger.info("JSON에서 MySQL로 데이터 마이그레이션 시작")
    logger.info("=" * 60)
    
    if not USE_MYSQL:
        logger.error("MySQL이 활성화되어 있지 않습니다.")
        logger.info("TOKEN.env 파일에 다음을 추가하세요:")
        logger.info("  USE_MYSQL=true")
        logger.info("  MYSQL_HOST=localhost")
        logger.info("  MYSQL_PORT=3306")
        logger.info("  MYSQL_USER=root")
        logger.info("  MYSQL_PASSWORD=your_password")
        logger.info("  MYSQL_DATABASE=tendo_aris")
        return
    
    # 히스토리 마이그레이션
    history_success = await migrate_history()
    
    # 플레이리스트 마이그레이션
    playlist_success = await migrate_playlists()
    
    if history_success and playlist_success:
        logger.info("=" * 60)
        logger.info("마이그레이션이 성공적으로 완료되었습니다!")
        logger.info("=" * 60)
        logger.info("주의: 기존 JSON 파일은 백업용으로 남겨두세요.")
    else:
        logger.error("마이그레이션 중 일부 오류가 발생했습니다.")


if __name__ == "__main__":
    asyncio.run(main())

