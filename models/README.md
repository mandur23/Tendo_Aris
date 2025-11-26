# TTS 모델 디렉토리

이 폴더에 커스텀 TTS 모델 파일을 넣어주세요.

## 모델 추가 방법

### 1. 모델 파일 준비
- Coqui TTS 형식의 모델 파일 또는 디렉토리를 이 폴더에 넣어주세요.
- 예: `models/my_voice_model/` 또는 `models/my_voice_model.pth`

### 2. 모델 등록
Discord 봇에서 다음 명령어를 사용하세요:

```
!tts커스텀모델추가 <모델이름> <경로> [표시이름]
```

**예시:**
```
!tts커스텀모델추가 my_voice models/my_voice_model "내 목소리"
!tts커스텀모델추가 korean_tts models/korean_tts "한국어 TTS"
```

### 3. 모델 사용
```
!tts목소리
```
명령어로 모델을 선택할 수 있습니다.

## 모델 경로 형식

- **상대 경로**: `models/my_model` (프로젝트 루트 기준)
- **절대 경로**: `C:/Users/User/models/my_model` (전체 경로)

## 지원하는 모델 형식

- Coqui TTS 모델 디렉토리
- 단일 모델 파일 (.pth 등)
- Hugging Face 모델 이름 (예: `tts_models/ko/korean/jets`)

