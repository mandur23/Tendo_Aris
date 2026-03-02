"""Supertonic TTS 유틸리티"""
import os
import logging
import numpy as np
from pathlib import Path
import tempfile
import wave

logger = logging.getLogger(__name__)

# Supertonic 모델 경로 (Hugging Face assets 디렉토리 구조 사용)
SUPERTONIC_MODEL_DIR = Path('models/supertonic')
SUPERTONIC_MODEL_DIR.mkdir(parents=True, exist_ok=True)

# TTS 임시 파일 저장 경로
TTS_TEMP_DIR = Path('logs/tts_temp')
TTS_TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Supertonic 모델 파일 경로 (Hugging Face 구조 기준)
SUPERTONIC_MODEL_PATH = SUPERTONIC_MODEL_DIR / 'model.onnx'
SUPERTONIC_CONFIG_PATH = SUPERTONIC_MODEL_DIR / 'config.json'
SUPERTONIC_VOICES_DIR = SUPERTONIC_MODEL_DIR / 'voices'

# Supertonic 사용 가능 여부
_supertonic_available = None
_onnx_session = None
_tokenizer = None


def _check_supertonic_available() -> bool:
    """Supertonic이 사용 가능한지 확인합니다."""
    global _supertonic_available
    
    if _supertonic_available is not None:
        return _supertonic_available
    
    try:
        import onnxruntime
        _supertonic_available = True
        logger.info("Supertonic TTS 사용 가능 (onnxruntime 설치됨)")
        return True
    except ImportError:
        _supertonic_available = False
        logger.warning("Supertonic TTS 사용 불가 (onnxruntime 미설치)")
        return False


def _load_supertonic_model():
    """Supertonic ONNX 모델을 로드합니다."""
    global _onnx_session
    
    if _onnx_session is not None:
        return _onnx_session
    
    if not _check_supertonic_available():
        return None
    
    # 모델 파일 찾기 (여러 가능한 위치 확인)
    model_paths = [
        SUPERTONIC_MODEL_PATH,
        SUPERTONIC_MODEL_DIR / 'assets' / 'model.onnx',
        Path('assets') / 'model.onnx',
    ]
    
    model_path = None
    for path in model_paths:
        if path.exists():
            model_path = path
            break
    
    if model_path is None:
        logger.error(f"Supertonic 모델 파일을 찾을 수 없습니다. 다음 위치를 확인했습니다: {model_paths}")
        return None
    
    try:
        import onnxruntime as ort
        
        # ONNX Runtime 세션 생성
        providers = ['CPUExecutionProvider']
        # GPU가 있으면 CUDA 사용 (선택사항)
        # if 'CUDAExecutionProvider' in ort.get_available_providers():
        #     providers.insert(0, 'CUDAExecutionProvider')
        
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        
        _onnx_session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=providers
        )
        
        logger.info(f"Supertonic 모델 로드 완료: {model_path}")
        return _onnx_session
    except Exception as e:
        logger.error(f"Supertonic 모델 로드 실패: {e}", exc_info=True)
        return None


def _download_supertonic_model():
    """Hugging Face에서 Supertonic 모델을 다운로드합니다."""
    try:
        from huggingface_hub import snapshot_download
        
        logger.info("Supertonic 모델 다운로드 시작...")
        
        # 전체 저장소 다운로드
        model_path = snapshot_download(
            repo_id="Supertone/supertonic",
            local_dir=str(SUPERTONIC_MODEL_DIR),
            local_dir_use_symlinks=False
        )
        
        logger.info(f"Supertonic 모델 다운로드 완료: {model_path}")
        return True
    except ImportError:
        logger.error("huggingface_hub가 설치되지 않았습니다. pip install huggingface_hub로 설치해주세요.")
        return False
    except Exception as e:
        logger.error(f"Supertonic 모델 다운로드 실패: {e}", exc_info=True)
        return False


def text_to_speech_supertonic(
    text: str,
    voice_preset: str = "default",
    inference_steps: int = 2
) -> Path:
    """
    Supertonic을 사용하여 텍스트를 음성 파일로 변환합니다.
    
    Args:
        text: 변환할 텍스트 (영어만 지원)
        voice_preset: 음성 프리셋 이름 (기본값: "default")
        inference_steps: 추론 단계 수 (기본값: 2, 더 높을수록 품질 향상)
    
    Returns:
        생성된 음성 파일 경로 (WAV 형식)
    """
    if not _check_supertonic_available():
        raise RuntimeError("Supertonic TTS를 사용할 수 없습니다. onnxruntime를 설치해주세요.")
    
    # 모델이 없으면 다운로드 시도
    if not _check_model_files_exist():
        logger.warning("Supertonic 모델이 없습니다. 다운로드를 시도합니다...")
        if not _download_supertonic_model():
            raise RuntimeError("Supertonic 모델을 다운로드할 수 없습니다. 수동으로 다운로드해주세요.")
    
    # 모델 로드
    session = _load_supertonic_model()
    if session is None:
        raise RuntimeError("Supertonic 모델을 로드할 수 없습니다.")
    
    try:
        # 임시 파일 생성
        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix='.wav',
            dir=TTS_TEMP_DIR
        )
        temp_path = Path(temp_file.name)
        temp_file.close()
        
        # Supertonic은 영어만 지원하므로, 다른 언어는 경고
        # 실제 구현은 Supertonic의 Python 예제 코드를 참고해야 합니다.
        # 여기서는 기본 구조만 제공합니다.
        
        # 입력 텍스트 전처리 및 토큰화
        # 실제로는 Supertonic의 토크나이저를 사용해야 합니다.
        input_ids = _encode_text(text)
        
        # ONNX 모델 추론
        # 실제 입력/출력 이름은 모델에 따라 다를 수 있습니다.
        input_name = session.get_inputs()[0].name
        
        # 배치 차원 추가
        if len(input_ids.shape) == 1:
            input_ids = np.expand_dims(input_ids, axis=0)
        
        # 추론 실행 (실제 구현은 Supertonic의 예제 코드 참고)
        # 여기서는 기본 구조만 제공합니다.
        outputs = session.run(None, {input_name: input_ids})
        
        # 출력 처리 (실제 형식은 모델에 따라 다름)
        audio_data = outputs[0] if len(outputs) > 0 else None
        
        if audio_data is None:
            raise RuntimeError("Supertonic 모델 출력을 가져올 수 없습니다.")
        
        # 오디오 데이터를 WAV 파일로 저장
        _save_wav(audio_path=temp_path, audio_data=audio_data, sample_rate=24000)
        
        logger.info(f"Supertonic TTS 파일 생성 완료: {temp_path.name} (텍스트 길이: {len(text)})")
        return temp_path
        
    except Exception as e:
        logger.error(f"Supertonic TTS 생성 중 오류: {e}", exc_info=True)
        raise


def _encode_text(text: str) -> np.ndarray:
    """
    텍스트를 모델 입력 형식으로 인코딩합니다.
    
    실제 구현은 Supertonic의 토크나이저를 사용해야 합니다.
    여기서는 기본 구조만 제공합니다.
    """
    # 실제로는 Supertonic의 토크나이저를 사용해야 합니다.
    # 예시: tokenizer.encode(text) 또는 유사한 방법
    
    # 임시로 간단한 인코딩 (실제로는 모델에 맞는 토크나이저 필요)
    # ASCII 기반 간단한 인코딩 (실제 구현에서는 적절한 토크나이저 사용)
    encoded = [ord(c) for c in text[:512]]  # 최대 길이 제한
    return np.array(encoded, dtype=np.int64)


def _save_wav(audio_path: Path, audio_data: np.ndarray, sample_rate: int = 24000):
    """
    오디오 데이터를 WAV 파일로 저장합니다.
    
    Args:
        audio_path: 저장할 파일 경로
        audio_data: 오디오 데이터 (numpy 배열)
        sample_rate: 샘플 레이트 (기본값: 24000)
    """
    # 오디오 데이터 정규화 및 변환
    if audio_data.dtype != np.int16:
        # float32를 int16으로 변환
        if audio_data.dtype == np.float32 or audio_data.dtype == np.float64:
            # -1.0 ~ 1.0 범위를 -32768 ~ 32767로 변환
            audio_data = np.clip(audio_data, -1.0, 1.0)
            audio_data = (audio_data * 32767).astype(np.int16)
        else:
            audio_data = audio_data.astype(np.int16)
    
    # 1차원 배열로 변환
    if len(audio_data.shape) > 1:
        audio_data = audio_data.flatten()
    
    # WAV 파일 저장
    with wave.open(str(audio_path), 'wb') as wav_file:
        wav_file.setnchannels(1)  # 모노
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_data.tobytes())


def _check_model_files_exist() -> bool:
    """Supertonic 모델 파일들이 존재하는지 확인합니다."""
    model_paths = [
        SUPERTONIC_MODEL_PATH,
        SUPERTONIC_MODEL_DIR / 'assets' / 'model.onnx',
        Path('assets') / 'model.onnx',
    ]
    return any(path.exists() for path in model_paths)


def check_supertonic_model_exists() -> bool:
    """Supertonic 모델 파일이 존재하는지 확인합니다."""
    return _check_model_files_exist()


def download_supertonic_model() -> bool:
    """Supertonic 모델을 다운로드합니다."""
    return _download_supertonic_model()


def check_supertonic_available() -> bool:
    """Supertonic이 사용 가능한지 확인합니다 (외부에서 호출 가능)."""
    return _check_supertonic_available()

