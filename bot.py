"""봇 메인 진입점"""
import asyncio
import sys
import logging
from logging_config import setup_logging
from core.launcher import main
from core.shutdown_handler import setup_signal_handlers

# 로깅 설정
setup_logging()
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    # 시그널 핸들러 설정
    setup_signal_handlers()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("프로그램이 종료되었습니다.")
    except Exception as e:
        logger.error(f"프로그램 실행 중 오류: {e}", exc_info=True)
        sys.exit(1)
