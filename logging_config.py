import logging
import logging.handlers
from pathlib import Path

def setup_logging():
    """프로덕션 환경을 위한 로깅 설정"""
    
    # 로그 디렉토리 생성
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # 기본 로거 설정
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            # 콘솔 출력
            logging.StreamHandler(),
            # 파일 출력 (회전 로그)
            logging.handlers.RotatingFileHandler(
                log_dir / "yacht_bot.log",
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5,
                encoding='utf-8'
            )
        ]
    )
    
    # Discord 라이브러리 로그 레벨 조정
    discord_logger = logging.getLogger('discord')
    discord_logger.setLevel(logging.WARNING)  # INFO는 너무 많음
    
    # HTTP 로그는 에러만
    http_logger = logging.getLogger('discord.http')
    http_logger.setLevel(logging.ERROR)
    
    print("✅ 로깅 설정 완료: logs/yacht_bot.log")

if __name__ == "__main__":
    setup_logging() 