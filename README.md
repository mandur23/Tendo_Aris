# 🎵 아리스 (Aris) - Discord 음악 봇 & TTS & 야추 다이스 게임

아리스는 Discord에서 음악 재생, TTS(음성 합성), 그리고 야추 다이스 게임을 즐길 수 있는 다기능 봇입니다. 친근한 한국어 인터페이스와 다양한 기능을 제공합니다.

## ✨ 주요 기능

### 🎶 음악 재생 기능
- **YouTube 음원 재생**: URL 또는 검색어로 음악 재생
- **대기열 관리**: 여러 곡을 순서대로 재생
- **재생 모드**: 
  - 한 곡 반복
  - 전체 반복
  - 랜덤 재생
- **볼륨 조절**: 실시간 볼륨 조절 (버튼 또는 명령어)
- **플레이리스트**: 개인별 플레이리스트 생성 및 관리
- **재생 기록**: 히스토리 기능으로 이전에 들은 곡 다시 재생
- **날짜별 재생**: 특정 날짜에 들었던 곡들을 다시 재생
- **주간 재생**: 이번 주에 들었던 곡들 재생
- **대기열 로그**: 현재 대기열을 파일로 저장
- **인터랙티브 컨트롤**: 버튼을 통한 직관적인 재생 제어

### 🗣️ TTS (Text-to-Speech) 기능
- **자동 읽기 모드**: 채팅 메시지를 자동으로 음성으로 읽어줌
- **즉시 읽기**: 특정 텍스트를 바로 읽어줌
- **다양한 TTS 엔진 지원**:
  - **gTTS (Google TTS)**: 빠르고 간단한 TTS, 다양한 언어 지원
  - **RVC (TTS + RVC)**: 텍스트를 원하는 목소리로 변환하는 고품질 TTS
    - Edge TTS와 RVC를 결합하여 자연스러운 음성 생성
    - 커스텀 RVC 모델 지원
    - 모델 인스턴스 캐싱으로 빠른 처리 속도
- **다양한 목소리 모델**: 
  - gTTS: 언어별 다양한 목소리 선택 가능
  - RVC: 등록된 RVC 모델 사용 가능
- **느린 속도 모드**: 더 천천히 읽기 (gTTS만 지원)
- **대기열 시스템**: 여러 메시지를 순서대로 읽기

### 🎲 야추 다이스 게임
- **멀티플레이어 지원**: 여러 명이 동시에 플레이 가능
- **실시간 게임**: 버튼을 통한 직관적인 조작
- **점수 계산**: 자동 점수 계산 및 보너스 시스템
- **게임 상태 관리**: 턴제 게임 진행
- **점수판**: 실시간 점수 확인

### 🔧 관리 기능
- **메시지 삭제**: 관리자 권한으로 메시지 정리
- **봇 재시작**: 관리자 권한으로 봇 재시작
- **봇 종료**: 관리자 권한으로 봇 종료
- **로그 시스템**: 상세한 로그 기록
- **명령어 자동 완성**: 오타가 있어도 비슷한 명령어 제안

## 🚀 설치 및 설정

### 필수 요구사항
- **Python 3.8 이상** (Coqui TTS 사용 시 Python 3.9~3.11 권장, Python 3.12는 Coqui TTS 미지원)
- **FFmpeg** (음성 처리용)
- **Discord Bot Token**
- **MySQL 5.7 이상** (선택사항, 히스토리/플레이리스트를 데이터베이스에 저장하려는 경우)

### 1. 저장소 클론
```bash
git clone <repository-url>
cd Tendo_Aris
```

### 2. 의존성 설치
```bash
pip install -r requirements.txt
```

**주요 패키지:**
- `discord.py>=2.3.0` - Discord API 래퍼
- `yt-dlp>=2023.10.0` - YouTube 다운로더
- `python-dotenv>=1.0.0` - 환경 변수 관리
- `rapidfuzz>=3.0.0` - 명령어 자동 완성 (문자열 매칭)
- `gtts>=2.5.0` - Google TTS
- `PyNaCl>=1.6.0` - Discord 음성 지원
- `async-timeout>=4.0.0` - 비동기 타임아웃 처리
- `aiomysql>=0.2.0` - MySQL 비동기 연결 (선택사항)
- `PyMySQL>=1.1.0` - MySQL 드라이버 (선택사항)

**선택적 패키지:**
- `tts-with-rvc-onnx>=0.1.0` - RVC TTS 지원 (ONNX 런타임 사용, 권장)
- 또는 `tts-with-rvc>=0.1.0` - RVC TTS 지원 (PyTorch 사용)

### 3. 환경 설정

#### 3.1 봇 토큰 설정
프로젝트 루트에 `TOKEN.env` 파일을 생성하고 다음 내용을 추가하세요:

```env
DISCORD_BOT_TOKEN=your_bot_token_here
FFMPEG_PATH=C:\path\to\your\ffmpeg.exe
```

#### 3.2 FFmpeg 설치 및 경로 설정
1. [FFmpeg 공식 사이트](https://ffmpeg.org/download.html)에서 FFmpeg 다운로드
2. `TOKEN.env` 파일에 `FFMPEG_PATH` 설정
   - Windows: `FFMPEG_PATH=C:\path\to\ffmpeg.exe`
   - Linux/Mac: `FFMPEG_PATH=/usr/bin/ffmpeg`

또는 `utils/config.py`에서 직접 경로를 수정할 수 있습니다.

#### 3.3 MySQL 데이터베이스 설정 (선택사항)

히스토리와 플레이리스트를 MySQL 데이터베이스에 저장하려면 다음 설정을 추가하세요:

**1. MySQL 데이터베이스 생성:**
```sql
CREATE DATABASE tendo_aris CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

**2. `TOKEN.env` 파일에 MySQL 설정 추가:**
```env
USE_MYSQL=true
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=tendo_aris
MYSQL_CHARSET=utf8mb4
```

**3. 데이터베이스 초기화:**
```bash
python init_database.py
```

이 스크립트는 필요한 테이블을 자동으로 생성합니다.

**4. (선택) 기존 JSON 데이터 마이그레이션:**
기존에 JSON 파일로 저장된 히스토리와 플레이리스트를 MySQL로 마이그레이션하려면:
```bash
python migrate_to_mysql.py
```

**참고:**
- `USE_MYSQL=false` 또는 설정하지 않으면 기존처럼 JSON 파일을 사용합니다.
- MySQL 사용 시 JSON 파일은 백업용으로 남겨두는 것을 권장합니다.

### 4. 봇 실행
```bash
python bot.py
```

## 📋 명령어 목록

### 🎵 음악 명령어

| 명령어 | 별칭 | 설명 |
|--------|------|------|
| `!play <URL/검색어>` | `!p`, `!재생`, `!플레이` | YouTube URL 또는 검색어로 음악 재생 |
| `!stop` | `!종료` | 음악 재생 종료 및 음성 채널에서 나가기 |
| `!leave` | `!나가!` | 봇을 음성 채널에서 내보냄 |
| `!volume <0-100>` | `!볼륨` | 볼륨 조절 (0-100%) |
| `!queue` | `!큐`, `!대기열` | 현재 재생 목록 표시 |

#### 플레이리스트 명령어

| 명령어 | 별칭 | 설명 |
|--------|------|------|
| `!플레이리스트` | `!플레이리스트목록`, `!플래이리스트` | 플레이리스트 목록 보기 |
| `!플레이리스트추가 <이름> <URL들>` | `!프래이리스트추가` | 플레이리스트에 곡 추가 |
| `!플레이리스트재생 <이름>` | `!플래이리스트재생` | 플레이리스트 재생 |
| `!플레이리스트삭제` | `!플래이리스트삭제` | 플레이리스트 삭제 |
| `!플레이리스트노래삭제` | `!프래이리스트노래삭제` | 플레이리스트에서 특정 노래 삭제 |

#### 히스토리 명령어

| 명령어 | 별칭 | 설명 |
|--------|------|------|
| `!히스토리 [페이지]` | `!history`, `!기록`, `!record` | 재생 기록 보기 (페이지별) |
| `!다시재생 <번호>` | `!replay`, `!재재생`, `!playagain` | 히스토리에서 곡 재생 |
| `!히스토리삭제` | `!clearhistory`, `!기록삭제` | 재생 기록 삭제 |
| `!날짜별재생 [날짜]` | `!dateplay`, `!날짜재생`, `!playbydate` | 특정 날짜의 곡들 재생 (YYYY-MM-DD) |
| `!이번주재생` | `!weekplay`, `!주간재생` | 이번 주에 들은 곡들 재생 |
| `!큐로그` | `!queuelog`, `!대기열로그` | 현재 대기열을 로그 파일로 저장 |

### 🗣️ TTS 명령어

| 명령어 | 별칭 | 설명 |
|--------|------|------|
| `!tts` | `!말하기`, `!읽기`, `!읽어줘` | TTS 자동 읽기 모드 토글 또는 텍스트 읽기 |
| `!tts <텍스트>` | - | 특정 텍스트를 즉시 읽어줌 |
| `!tts목소리` | `!ttsvoice`, `!tts모델`, `!목소리변경` | TTS 엔진 및 목소리 모델 변경 (gTTS/RVC) |
| `!tts느리게` | `!ttsslow` | TTS 느린 속도 모드 토글 (gTTS만 지원) |
| `!tts설정` | `!ttssettings` | 현재 TTS 설정 확인 |
| `!ttsrvc모델추가` | - | RVC 모델 수동 추가 |
| `!ttsrvc모델목록` | - | 등록된 RVC 모델 목록 확인 |
| `!ttsrvc모델삭제` | - | RVC 모델 삭제 |
| `!ttsrvc자동등록` | - | models 폴더에서 RVC 모델 자동 등록 |
| `!ttsrvc사용` | - | RVC TTS 사용 설정 |
| `!ttsrvc끄기` | - | RVC TTS 비활성화 (gTTS로 전환) |

**TTS 사용법:**
- `!tts` - 자동 읽기 모드 켜기/끄기
- `!tts 안녕하세요` - "안녕하세요"를 즉시 읽어줌
- `!tts목소리` - TTS 엔진 및 목소리 모델 선택 (gTTS 또는 RVC)

**RVC TTS 사용법:**
1. RVC 모델 준비: `models` 폴더에 RVC 모델 파일(.pth, .index) 배치
2. 자동 등록: `!ttsrvc자동등록` 명령어로 모델 자동 등록
3. RVC 사용: `!tts목소리` 명령어로 RVC 엔진 선택 후 모델 선택
4. 모델 관리: `!ttsrvc모델목록`으로 등록된 모델 확인

### 🎲 게임 명령어

| 명령어 | 설명 |
|--------|------|
| `!야추` | 야추 다이스 게임 시작/참가 |
| `!시작` | 대기 중인 게임 시작 (게임장만 가능) |
| `!야추취소` | 진행 중인 게임 취소 (게임장 또는 관리자) |

### 🔧 관리 명령어

| 명령어 | 별칭 | 설명 | 권한 |
|--------|------|------|------|
| `!삭제 <개수>` | - | 메시지 삭제 | 메시지 관리 |
| `!재시작` | `!restart`, `!try` | 봇 재시작 | 관리자 |
| `!종료봇` | `!exit` | 봇 종료 | 관리자 |
| `!도움말` | `!도움`, `!help` | 명령어 목록 | - |

## 🎮 게임 조작법

### 야추 다이스 게임

1. **게임 시작**: `!야추` 명령어로 게임 생성
2. **참가**: 다른 플레이어들이 `!야추`로 참가
3. **게임 시작**: 게임장이 `!시작` 명령어 실행
4. **게임 진행**:
   - `굴리기` 버튼으로 주사위 굴리기 (최대 3회)
   - 주사위 버튼으로 고정/해제 (🔒 = 고정, 🎲 = 미고정)
   - `점수 기록` 버튼으로 카테고리 선택
   - `점수판` 버튼으로 현재 점수 확인

### 점수 계산

- **상단 (1-6)**: 해당 숫자의 개수 × 숫자
- **풀하우스**: 3개 + 2개 조합 → 25점
- **포카드**: 4개 이상 → 모든 주사위 합계
- **스몰 스트레이트**: 1-4, 2-5, 3-6 중 하나 → 30점
- **라지 스트레이트**: 1-5 또는 2-6 → 40점
- **야추**: 5개 모두 같은 숫자 → 50점
- **찬스**: 모든 주사위 합계
- **보너스**: 상단 합계 63점 이상 시 35점 추가

## 📁 프로젝트 구조

```
Tendo_Aris/
├── bot.py                      # 메인 봇 진입점
├── logging_config.py           # 로깅 설정
├── requirements.txt            # Python 패키지 의존성
├── TOKEN.env                   # 환경 변수 (봇 토큰, FFmpeg 경로)
│
├── core/                       # 핵심 모듈
│   ├── __init__.py
│   ├── bot.py                  # FuzzyBot 클래스 (명령어 자동 완성)
│   └── music_player.py         # 음악 재생 플레이어
│
├── cogs/                       # Discord 명령어 확장
│   ├── __init__.py
│   ├── music.py                # 음악 재생 명령어
│   └── tts.py                  # TTS 명령어
│
├── GameSystem/                 # 게임 시스템
│   └── YachtDiceGame.py        # 야추 다이스 게임
│
├── utils/                      # 유틸리티 함수
│   ├── __init__.py
│   ├── config.py               # 설정 상수 및 환경 변수
│   ├── db_utils.py             # MySQL 데이터베이스 유틸리티
│   ├── discord_utils.py        # Discord 유틸리티
│   ├── file_utils.py           # 파일 I/O 유틸리티
│   ├── tts_utils.py            # TTS 유틸리티 (gTTS)
│   ├── rvc_utils.py            # RVC TTS 유틸리티
│   └── ytdl_utils.py           # YouTube 다운로더 유틸리티
│
├── logs/                       # 로그 파일 디렉토리
│   ├── YYYY/                   # 년도별 폴더 (예: 2025)
│   │   └── MM/                 # 월별 폴더 (예: 11)
│   │       └── yacht_bot_YYYY-MM-DD.log  # 날짜별 로그 파일
│
├── data/                       # 데이터 파일 디렉토리
│   ├── playlists.json          # 플레이리스트 데이터 (JSON 모드, 자동 생성)
│   ├── history.json            # 재생 기록 데이터 (JSON 모드, 자동 생성)
│   ├── tts_settings.json       # TTS 설정 데이터 (자동 생성)
│   └── rvc_models.json         # RVC 모델 설정 데이터 (자동 생성)
├── models/                     # RVC 모델 디렉토리
│   └── [RVC 모델 폴더들]       # .pth 및 .index 파일 포함
├── init_database.py            # MySQL 데이터베이스 초기화 스크립트
└── migrate_to_mysql.py         # JSON → MySQL 마이그레이션 스크립트
```

## 🔧 설정 및 커스터마이징

### 환경 변수 설정 (`TOKEN.env`)

**기본 설정 (JSON 파일 사용):**
```env
DISCORD_BOT_TOKEN=your_bot_token_here
FFMPEG_PATH=C:\path\to\ffmpeg.exe
```

**MySQL 데이터베이스 사용 시 추가 설정:**
```env
DISCORD_BOT_TOKEN=your_bot_token_here
FFMPEG_PATH=C:\path\to\ffmpeg.exe
USE_MYSQL=true
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=tendo_aris
MYSQL_CHARSET=utf8mb4
```

**연결 감지/워치독 설정 (선택):**
```env
# 오프라인 알림용 웹훅 (선택)
OFFLINE_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...

# 시작 후 연결이 안 될 때 재시작까지 대기 시간 (초)
OFFLINE_STARTUP_GRACE_SECONDS=300

# 오프라인 지속 시 자동 재시작 기준 시간 (초)
OFFLINE_RESTART_SECONDS=1800

# 오프라인 자동 재시작 활성화 여부
AUTO_RESTART_ON_OFFLINE=true
```

### 코드 내 설정 (`utils/config.py`)

주요 설정값들:

```python
# 봇 설정
COMMAND_PREFIX = '!'

# 음악 플레이어 설정
DEFAULT_VOLUME = 0.2              # 기본 볼륨 (0.0 ~ 1.0)
IDLE_TIMEOUT = 300                # 비활동 시간 초과 (초)
MAX_HISTORY_ITEMS = 100           # 최대 히스토리 항목 수
QUEUE_TIMEOUT = 300               # 대기열 타임아웃 (초)

# 재시도 설정
MAX_RETRIES = 5                   # 최대 재시도 횟수
MAX_EXTRACT_RETRIES = 3           # 정보 추출 최대 재시도
MAX_PLAY_RETRIES = 2              # 재생 최대 재시도
BASE_DELAY = 2                    # 재시도 기본 지연 시간 (초)

# 메시지 삭제 지연
COMMAND_MESSAGE_DELETE_DELAY = 3  # 명령어 메시지 삭제 지연 (초)

# 연결/오프라인 감지 및 재시작
OFFLINE_ALERT_WEBHOOK_URL = ''          # 오프라인 알림 웹훅 (선택)
OFFLINE_STARTUP_GRACE_SECONDS = 300     # 시작 후 유예 시간 (초)
OFFLINE_RESTART_SECONDS = 1800          # 오프라인 재시작 기준 (초)
AUTO_RESTART_ON_OFFLINE = True          # 오프라인 자동 재시작 여부

# MySQL 데이터베이스 설정
MYSQL_HOST = 'localhost'          # MySQL 호스트
MYSQL_PORT = 3306                 # MySQL 포트
MYSQL_USER = 'root'               # MySQL 사용자
MYSQL_PASSWORD = ''               # MySQL 비밀번호
MYSQL_DATABASE = 'tendo_aris'     # MySQL 데이터베이스 이름
MYSQL_CHARSET = 'utf8mb4'         # MySQL 문자셋

USE_MYSQL = False                 # MySQL 사용 여부 (True면 MySQL, False면 JSON)
```

### 로깅 설정 (`logging_config.py`)

- **로그 레벨**: INFO
- **로그 파일 구조**: 날짜별로 자동 분리
  - 경로: `logs/YYYY/MM/yacht_bot_YYYY-MM-DD.log`
  - 예시: `logs/2025/11/yacht_bot_2025-11-19.log`
- **자동 롤오버**: 날짜가 변경되면 자동으로 새 파일 생성
- **폴더 구조**: 년도/월 폴더로 자동 정리

## 🛠️ 문제 해결

### 일반적인 문제들

#### 1. 봇이 음성 채널에 연결되지 않음
- 봇에 음성 채널 접근 권한 확인
- FFmpeg 설치 및 경로 확인 (`TOKEN.env` 또는 `utils/config.py`)
- 봇이 음성 채널에 입장할 수 있는 권한이 있는지 확인

#### 2. YouTube 영상 재생 오류
- 403 오류는 자동 재시도로 해결 (최대 5회)
- 비공개/삭제된 영상은 자동으로 건너뜀
- 네트워크 연결 확인

#### 3. 게임이 시작되지 않음
- 최소 2명 이상의 플레이어 필요
- 게임장(첫 번째 참가자)만 `!시작` 명령어 사용 가능

#### 4. TTS가 작동하지 않음
- gTTS는 기본적으로 작동하며 추가 설정 불필요
- RVC TTS 사용 시 `tts-with-rvc-onnx` 또는 `tts-with-rvc` 패키지 설치 필요
- `!tts목소리` 명령어로 TTS 엔진 확인
- RVC 모델이 등록되어 있는지 `!ttsrvc모델목록`으로 확인

#### 5. RVC TTS가 느림
- 첫 사용 시 모델 로딩으로 인해 느릴 수 있음 (정상)
- 이후 사용 시 인스턴스 캐싱으로 빠르게 처리됨
- 동일한 모델을 반복 사용하면 속도가 향상됨

#### 5. 명령어를 찾을 수 없음
- 봇이 명령어 자동 완성 기능을 제공 (오타 시 비슷한 명령어 제안)
- `!도움말` 명령어로 전체 명령어 목록 확인

#### 6. MySQL 연결 오류
- MySQL 서버가 실행 중인지 확인
- `TOKEN.env`의 MySQL 설정 정보 확인
- 데이터베이스가 생성되어 있는지 확인 (`init_database.py` 실행)
- 사용자 권한 확인 (데이터베이스 접근 권한 필요)
- MySQL 연결 실패 시 자동으로 JSON 파일 모드로 전환됩니다

### 로그 확인

```bash
# 로그 파일 위치 (날짜별로 자동 생성)
logs/YYYY/MM/yacht_bot_YYYY-MM-DD.log
# 예시: logs/2025/11/yacht_bot_2025-11-19.log

# 실시간 로그 확인 (Linux/Mac)
tail -f logs/$(date +%Y)/$(date +%m)/yacht_bot_$(date +%Y-%m-%d).log

# 실시간 로그 확인 (Windows PowerShell)
# 먼저 현재 날짜의 로그 파일 경로 확인
Get-Content logs/2025/11/yacht_bot_2025-11-19.log -Wait
# 또는 날짜별로 직접 경로 지정
```

## 🎯 주요 특징

### 명령어 자동 완성
- 오타가 있어도 비슷한 명령어를 자동으로 제안
- RapidFuzz 알고리즘 사용

### 인터랙티브 컨트롤
- 음악 재생 시 버튼을 통한 직관적인 제어
- 재생/일시정지, 다음 곡, 반복 모드, 볼륨 조절 등

### 자동 재시도 시스템
- YouTube 403 오류 자동 재시도
- 네트워크 오류 복구

### 비활동 감지
- 음성 채널에 아무도 없을 때 자동으로 나가기
- 일정 시간 비활동 시 자동 재시작

### 데이터 저장 옵션
- **JSON 파일 모드** (기본): 히스토리와 플레이리스트를 JSON 파일로 저장
- **MySQL 데이터베이스 모드**: 히스토리와 플레이리스트를 MySQL 데이터베이스에 저장
  - 더 안정적이고 확장 가능한 데이터 저장
  - 여러 봇 인스턴스에서 동일한 데이터 공유 가능
  - 자동 테이블 생성 및 마이그레이션 지원

## 🤝 기여하기

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 라이선스

이 프로젝트는 MIT 라이선스 하에 배포됩니다. 자세한 내용은 `LICENSE` 파일을 참조하세요.

## 🙏 감사의 말

- [discord.py](https://github.com/Rapptz/discord.py) - Discord API 래퍼
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - YouTube 다운로더
- [FFmpeg](https://ffmpeg.org/) - 음성 처리
- [gTTS](https://github.com/pndurette/gTTS) - Google Text-to-Speech
- [tts-with-rvc](https://github.com/litagin02/tts-with-rvc) - TTS와 RVC를 결합한 라이브러리
- [Edge TTS](https://github.com/rany2/edge-tts) - Microsoft Edge TTS (RVC와 함께 사용)
- [RapidFuzz](https://github.com/rapidfuzz/rapidfuzz) - 빠르고 효율적인 문자열 매칭
- [aiomysql](https://github.com/aio-libs/aiomysql) - MySQL 비동기 연결
- [PyMySQL](https://github.com/PyMySQL/PyMySQL) - MySQL 드라이버

## 📞 지원

문제가 발생하거나 기능 요청이 있으시면 이슈를 생성해 주세요.

---

**아리스와 함께 즐거운 음악, TTS, 그리고 게임 시간을 보내세요! 🎵🗣️🎲**
