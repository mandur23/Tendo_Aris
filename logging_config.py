import logging
import logging.handlers
from pathlib import Path

def setup_logging():
    """프로덕션 환경을 위한 로깅 설정"""
    
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                log_dir / "yacht_bot.log",
                maxBytes=10*1024*1024,
                backupCount=5,
                encoding='utf-8'
            )
        ]
    )
    
    discord_logger = logging.getLogger('discord')
    discord_logger.setLevel(logging.WARNING)
    
    http_logger = logging.getLogger('discord.http')
    http_logger.setLevel(logging.ERROR)
    
    print("✅ 로깅 설정 완료: logs/yacht_bot.log")

if __name__ == "__main__":
    setup_logging() 