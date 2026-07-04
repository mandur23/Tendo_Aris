"""MySQL 데이터베이스 연결 및 CRUD 유틸리티"""
import logging
import asyncio
import warnings
from typing import Dict, List, Optional, Any
from datetime import datetime
import aiomysql
from utils.config import (
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, 
    MYSQL_DATABASE, MYSQL_CHARSET, USE_MYSQL
)

logger = logging.getLogger(__name__)

# MySQL 경고 메시지 억제 (테이블이 이미 존재하는 경우의 경고)
warnings.filterwarnings('ignore', category=aiomysql.Warning)

# 전역 연결 풀
_pool: Optional[aiomysql.Pool] = None


async def init_db_pool():
    """데이터베이스 연결 풀을 초기화합니다."""
    global _pool
    if _pool is None and USE_MYSQL:
        try:
            _pool = await aiomysql.create_pool(
                host=MYSQL_HOST,
                port=MYSQL_PORT,
                user=MYSQL_USER,
                password=MYSQL_PASSWORD,
                db=MYSQL_DATABASE,
                charset=MYSQL_CHARSET,
                autocommit=True,
                minsize=1,
                maxsize=10,
                loop=asyncio.get_event_loop()
            )
            logger.info(f"MySQL 연결 풀이 초기화되었습니다: {MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}")
            await create_tables()
        except Exception as e:
            logger.error(f"MySQL 연결 풀 초기화 실패: {e}")
            raise


async def close_db_pool():
    """데이터베이스 연결 풀을 종료합니다."""
    global _pool
    if _pool:
        try:
            # 이벤트 루프가 열려있는지 확인
            try:
                loop = asyncio.get_running_loop()
                if loop.is_closed():
                    logger.warning("이벤트 루프가 이미 닫혀있습니다. DB 연결 풀을 동기적으로 정리합니다.")
                    _pool = None
                    return
            except RuntimeError:
                logger.warning("실행 중인 이벤트 루프가 없습니다. DB 연결 풀을 정리하지 않습니다.")
                _pool = None
                return
            
            _pool.close()
            await _pool.wait_closed()
            logger.info("MySQL 연결 풀이 종료되었습니다.")
        except Exception as e:
            logger.warning(f"DB 연결 풀 종료 중 오류 발생 (무시됨): {e}")
        finally:
            _pool = None


async def get_pool() -> aiomysql.Pool:
    """연결 풀을 반환합니다."""
    if _pool is None:
        await init_db_pool()
    return _pool


async def create_tables():
    """필요한 테이블을 생성합니다."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 경고 메시지 억제를 위해 커서 생성 시 suppress_warnings 사용
        async with conn.cursor() as cur:
            # MySQL 경고 억제
            try:
                await cur.execute("SET sql_notes = 0")  # MySQL 경고 메시지 억제
            except Exception:
                pass  # 실패해도 계속 진행
            
            # 히스토리 테이블
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS music_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    title VARCHAR(500) NOT NULL,
                    url VARCHAR(500) NOT NULL,
                    duration INT NOT NULL,
                    played_at DATETIME NOT NULL,
                    INDEX idx_guild_id (guild_id),
                    INDEX idx_played_at (played_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            
            # 플레이리스트 테이블
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS playlists (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    playlist_id VARCHAR(100) NOT NULL,
                    url VARCHAR(500) NOT NULL,
                    position INT NOT NULL,
                    UNIQUE KEY uk_guild_playlist_position (guild_id, playlist_id, position),
                    INDEX idx_guild_playlist (guild_id, playlist_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            
            # 경고 설정 복원
            try:
                await cur.execute("SET sql_notes = 1")
            except Exception:
                pass
            
            await conn.commit()
            logger.info("데이터베이스 테이블이 생성/확인되었습니다.")


# ========== 히스토리 관련 함수 ==========

async def load_history_from_db() -> Dict[str, List[Dict[str, Any]]]:
    """히스토리를 데이터베이스에서 로드합니다."""
    if not USE_MYSQL:
        return {}
    
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""
                    SELECT guild_id, title, url, duration, played_at
                    FROM music_history
                    ORDER BY guild_id, played_at DESC
                """)
                
                rows = await cur.fetchall()
                history = {}
                
                for row in rows:
                    guild_id_str = str(row['guild_id'])
                    if guild_id_str not in history:
                        history[guild_id_str] = []
                    
                    history[guild_id_str].append({
                        'title': row['title'],
                        'url': row['url'],
                        'duration': row['duration'],
                        'played_at': row['played_at'].strftime('%Y-%m-%d %H:%M:%S') if isinstance(row['played_at'], datetime) else str(row['played_at'])
                    })
                
                return history
    except Exception as e:
        logger.error(f"히스토리 로드 중 오류: {e}")
        return {}


async def save_history_to_db(history: Dict[str, List[Dict[str, Any]]]):
    """히스토리를 데이터베이스에 저장합니다."""
    if not USE_MYSQL:
        return
    
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 삭제 후 재삽입 방식이므로 중간 실패 시 데이터 유실을 막기 위해 트랜잭션으로 묶는다.
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    # 모든 히스토리를 삭제하고 재삽입 (간단한 구현)
                    # 더 나은 방법은 변경사항만 업데이트하는 것이지만, 현재는 전체 교체
                    for guild_id_str, items in history.items():
                        guild_id = int(guild_id_str)

                        # 기존 히스토리 삭제
                        await cur.execute(
                            "DELETE FROM music_history WHERE guild_id = %s",
                            (guild_id,)
                        )

                        # 새 히스토리 삽입
                        for item in items:
                            played_at = datetime.strptime(item['played_at'], '%Y-%m-%d %H:%M:%S')
                            await cur.execute("""
                                INSERT INTO music_history (guild_id, title, url, duration, played_at)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (
                                guild_id,
                                item['title'][:500],  # VARCHAR(500) 제한
                                item['url'][:500],
                                item['duration'],
                                played_at
                            ))

                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
    except Exception as e:
        logger.error(f"히스토리 저장 중 오류: {e}")


async def add_history_item_to_db(guild_id: int, title: str, url: str, duration: int, played_at: Optional[datetime] = None):
    """히스토리 항목을 데이터베이스에 추가합니다."""
    if not USE_MYSQL:
        return
    
    try:
        if played_at is None:
            played_at = datetime.now()
        
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO music_history (guild_id, title, url, duration, played_at)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    guild_id,
                    title[:500],
                    url[:500],
                    duration,
                    played_at
                ))
                
                # 최대 개수 제한 (구버전과 호환)
                await cur.execute("""
                    DELETE FROM music_history
                    WHERE guild_id = %s
                    AND id NOT IN (
                        SELECT id FROM (
                            SELECT id FROM music_history
                            WHERE guild_id = %s
                            ORDER BY played_at DESC
                            LIMIT 100
                        ) AS t
                    )
                """, (guild_id, guild_id))
                
                await conn.commit()
    except Exception as e:
        logger.error(f"히스토리 항목 추가 중 오류: {e}")


async def get_history_from_db(guild_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    """특정 길드의 히스토리를 데이터베이스에서 가져옵니다."""
    if not USE_MYSQL:
        return []
    
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""
                    SELECT title, url, duration, played_at
                    FROM music_history
                    WHERE guild_id = %s
                    ORDER BY played_at DESC
                    LIMIT %s
                """, (guild_id, limit))
                
                rows = await cur.fetchall()
                result = []
                
                for row in rows:
                    result.append({
                        'title': row['title'],
                        'url': row['url'],
                        'duration': row['duration'],
                        'played_at': row['played_at'].strftime('%Y-%m-%d %H:%M:%S') if isinstance(row['played_at'], datetime) else str(row['played_at'])
                    })
                
                return result
    except Exception as e:
        logger.error(f"히스토리 조회 중 오류: {e}")
        return []


# ========== 플레이리스트 관련 함수 ==========

async def load_playlists_from_db() -> Dict[str, Dict[str, List[str]]]:
    """플레이리스트를 데이터베이스에서 로드합니다."""
    if not USE_MYSQL:
        return {}
    
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT guild_id, playlist_id, url, position
                    FROM playlists
                    ORDER BY guild_id, playlist_id, position
                """)
                
                rows = await cur.fetchall()
                playlists = {}
                
                for row in rows:
                    guild_id_str = str(row[0])
                    playlist_id = str(row[1])
                    url = row[2]
                    
                    if guild_id_str not in playlists:
                        playlists[guild_id_str] = {}
                    
                    if playlist_id not in playlists[guild_id_str]:
                        playlists[guild_id_str][playlist_id] = []
                    
                    playlists[guild_id_str][playlist_id].append(url)
                
                return playlists
    except Exception as e:
        logger.error(f"플레이리스트 로드 중 오류: {e}")
        return {}


async def save_playlists_to_db(playlists: Dict[str, Dict[str, List[str]]]):
    """플레이리스트를 데이터베이스에 저장합니다.

    주의: 플레이리스트는 코드상 사용자(user_id) 기준으로 관리되며,
    과거 호환성 때문에 DB의 guild_id 컬럼에 user_id가 저장됩니다.
    """
    if not USE_MYSQL:
        return

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 삭제 후 재삽입 방식이므로 중간 실패 시 데이터 유실을 막기 위해 트랜잭션으로 묶는다.
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    # 모든 플레이리스트를 삭제하고 재삽입
                    for guild_id_str, playlist_dict in playlists.items():
                        guild_id = int(guild_id_str)

                        # 기존 플레이리스트 삭제
                        await cur.execute(
                            "DELETE FROM playlists WHERE guild_id = %s",
                            (guild_id,)
                        )

                        # 새 플레이리스트 삽입
                        for playlist_id, urls in playlist_dict.items():
                            for position, url in enumerate(urls):
                                await cur.execute("""
                                    INSERT INTO playlists (guild_id, playlist_id, url, position)
                                    VALUES (%s, %s, %s, %s)
                                """, (
                                    guild_id,
                                    playlist_id,
                                    url[:500],
                                    position
                                ))

                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
    except Exception as e:
        logger.error(f"플레이리스트 저장 중 오류: {e}")


async def get_playlist_from_db(guild_id: int, playlist_id: str) -> List[str]:
    """특정 길드의 특정 플레이리스트를 데이터베이스에서 가져옵니다."""
    if not USE_MYSQL:
        return []
    
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT url
                    FROM playlists
                    WHERE guild_id = %s AND playlist_id = %s
                    ORDER BY position
                """, (guild_id, playlist_id))
                
                rows = await cur.fetchall()
                return [row[0] for row in rows]
    except Exception as e:
        logger.error(f"플레이리스트 조회 중 오류: {e}")
        return []


async def save_playlist_to_db(guild_id: int, playlist_id: str, urls: List[str]):
    """특정 플레이리스트를 데이터베이스에 저장합니다."""
    if not USE_MYSQL:
        return
    
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 기존 플레이리스트 삭제
                await cur.execute(
                    "DELETE FROM playlists WHERE guild_id = %s AND playlist_id = %s",
                    (guild_id, playlist_id)
                )
                
                # 새 플레이리스트 삽입
                if urls:  # 빈 리스트가 아닌 경우만
                    for position, url in enumerate(urls):
                        await cur.execute("""
                            INSERT INTO playlists (guild_id, playlist_id, url, position)
                            VALUES (%s, %s, %s, %s)
                        """, (
                            guild_id,
                            playlist_id,
                            url[:500],
                            position
                        ))
                
                await conn.commit()
    except Exception as e:
        logger.error(f"플레이리스트 저장 중 오류: {e}")

