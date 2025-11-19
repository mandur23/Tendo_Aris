import logging
import logging.handlers
from pathlib import Path
from datetime import datetime


class DateRotatingFileHandler(logging.FileHandler):
    """날짜별로 로그 파일을 분리하고 년도/월 폴더 구조로 저장하는 핸들러"""
    
    def __init__(self, base_dir="logs", filename_base="yacht_bot", encoding='utf-8'):
        self.base_dir = Path(base_dir)
        self.filename_base = filename_base
        self.encoding = encoding
        self.current_date = None
        self.current_file = None
        
        # 초기 파일 경로 생성
        _, file_path, date_str = self._get_date_folder_and_file()
        self.current_date = date_str
        self.current_file = file_path
        
        # 부모 클래스 초기화 (첫 파일로)
        # line buffering으로 설정하여 실시간 저장
        logging.FileHandler.__init__(self, str(file_path), mode='a', encoding=encoding)
        # line buffering 활성화 (각 줄마다 즉시 저장)
        if hasattr(self.stream, 'reconfigure'):
            # Python 3.7+ 버전
            try:
                self.stream.reconfigure(line_buffering=True)
            except (ValueError, AttributeError):
                pass
    
    def _get_date_folder_and_file(self):
        """현재 날짜에 맞는 폴더 경로와 파일 경로를 반환"""
        now = datetime.now()
        year = now.strftime('%Y')
        month = now.strftime('%m')
        date_str = now.strftime('%Y-%m-%d')
        
        # logs/년도/월 폴더 생성
        date_folder = self.base_dir / year / month
        date_folder.mkdir(parents=True, exist_ok=True)
        
        # 파일명: yacht_bot_2025-11-19.log
        filename = f"{self.filename_base}_{date_str}.log"
        file_path = date_folder / filename
        
        return date_folder, file_path, date_str
    
    def _update_file_path(self):
        """현재 날짜에 맞게 파일 경로 업데이트"""
        date_folder, file_path, date_str = self._get_date_folder_and_file()
        
        # 날짜가 바뀐 경우에만 파일 변경
        if self.current_date != date_str:
            # 기존 스트림 닫기
            if self.stream and not self.stream.closed:
                self.stream.flush()  # 변경 전 기존 로그 저장
                self.stream.close()
            
            self.current_date = date_str
            self.current_file = file_path
            
            # 새 파일 열기
            self.baseFilename = str(file_path)
            if self.stream is None or self.stream.closed:
                self.stream = self._open()
                # line buffering 활성화 (각 줄마다 즉시 저장)
                if hasattr(self.stream, 'reconfigure'):
                    try:
                        self.stream.reconfigure(line_buffering=True)
                    except (ValueError, AttributeError):
                        pass
    
    def shouldRollover(self, record):
        """날짜가 바뀌었는지 확인"""
        _, _, date_str = self._get_date_folder_and_file()
        return self.current_date != date_str
    
    def doRollover(self):
        """날짜가 바뀌면 새 파일로 전환"""
        self._update_file_path()
    
    def emit(self, record):
        """로그 레코드를 파일에 기록 (실시간 저장)"""
        try:
            # 날짜 확인 및 롤오버 처리
            if self.shouldRollover(record):
                self.doRollover()
            
            logging.FileHandler.emit(self, record)
            # 실시간으로 파일에 저장하기 위해 즉시 flush
            if self.stream:
                self.stream.flush()
        except Exception:
            self.handleError(record)


def setup_logging():
    """프로덕션 환경을 위한 로깅 설정"""
    
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # 현재 날짜에 맞는 파일 경로 생성
    now = datetime.now()
    year = now.strftime('%Y')
    month = now.strftime('%m')
    date_str = now.strftime('%Y-%m-%d')
    date_folder = log_dir / year / month
    date_folder.mkdir(parents=True, exist_ok=True)
    log_file = date_folder / f"yacht_bot_{date_str}.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            DateRotatingFileHandler(
                base_dir="logs",
                filename_base="yacht_bot",
                encoding='utf-8'
            )
        ]
    )
    
    discord_logger = logging.getLogger('discord')
    discord_logger.setLevel(logging.WARNING)
    
    http_logger = logging.getLogger('discord.http')
    http_logger.setLevel(logging.ERROR)
    
    print(f"✅ 로깅 설정 완료: {log_file}")

if __name__ == "__main__":
    setup_logging() 