import os
import yt_dlp
import random
from utils.config import FFMPEG_PATH

ytdl_format_options = {
    # m4a → webm → 그 외 audio-only → 최후수단으로 비디오 포함 best 까지 fallback.
    # tv_simply 등 일부 player_client 는 audio-only itag 를 제공하지 않으므로 폭넓게 허용.
    'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'cookiefile': None,
    'usenetrc': False,
    'username': None,
    'password': None,
    'twofactor': None,
    'videopassword': None,
    'ap_mso': None,
    'ap_username': None,
    'ap_password': None,
    'extractor_retries': 5,
    'socket_timeout': 60,
    'retries': 10,
    'retry_sleep': 3,
    'fragment_retries': 10,
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-us,en;q=0.5',
        'Sec-Fetch-Mode': 'navigate',
    },
    # YouTube 안티봇 우회는 yt_dlp 의 기본 player_client 우선순위에 맡긴다.
    # 2026 년 기준 yt_dlp 기본값(android_vr 등)이 PoToken 없이도 audio-only itag(139/140 등)를
    # 안정적으로 반환한다. 사용자가 직접 player_client 를 지정하면 오히려 PoToken 이 강제되는
    # 클라이언트로 한정되어 storyboard 만 남는 회귀가 발생하므로 명시하지 않는다.
    # 필요 시 player_skip 정도만 미세조정한다.
    'extractor_args': {
        'youtube': {
            'player_skip': ['configs'],
        },
    },
    'age_limit': None,
    'extract_flat': False,
    'geo_bypass': True,
    'geo_bypass_country': 'US',
    'writesubtitles': False,
    'writeautomaticsub': False,
    'allsubtitles': False,
    # ignoreerrors=True 는 추출 실패 시 None 을 반환시켜 downstream 에서
    # 'NoneType' object has no attribute 'get' 같은 미스리딩한 에러를 만든다.
    # 봇 로직은 예외 기반 재시도이므로 False 로 두어 yt_dlp 가 명확히 예외를 던지게 한다.
    'ignoreerrors': False,
}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1 -timeout 30000000',
    'options': '-vn -timeout 30000000 -user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"'
}

ffmpeg_path = FFMPEG_PATH


def create_ytdl_instance(custom_options=None):
    """403 오류를 방지하기 위한 개선된 yt-dlp 인스턴스 생성"""
    options = ytdl_format_options.copy()
    
    if custom_options:
        options.update(custom_options)
    
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ]
    
    options['http_headers']['User-Agent'] = random.choice(user_agents)
    
    return yt_dlp.YoutubeDL(options)


def flatten_ytdl_info(info):
    """yt-dlp 검색/플레이리스트 결과에서 첫 유효 항목을 단일 영상 dict 로 평탄화합니다."""
    if not info:
        return None
    if "entries" in info:
        for entry in info.get("entries") or []:
            if entry:
                return entry
        return None
    return info
