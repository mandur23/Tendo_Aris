import os
import yt_dlp
import random
from utils.config import FFMPEG_PATH

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
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
    'age_limit': None,
    'extract_flat': False,
    'geo_bypass': True,
    'geo_bypass_country': 'US',
    'writesubtitles': False,
    'writeautomaticsub': False,
    'allsubtitles': False,
    'ignoreerrors': True,
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

