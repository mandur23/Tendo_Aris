"""RVC (Retrieval-based Voice Conversion) 유틸리티"""
import logging
from pathlib import Path
import tempfile
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from utils.file_utils import atomic_write_json

logger = logging.getLogger(__name__)

# FFmpeg 경로 설정 (tts-with-rvc가 찾을 수 있도록)
def _setup_ffmpeg_path():
    """FFmpeg 경로를 환경 변수에 설정합니다."""
    try:
        from utils.config import FFMPEG_PATH
        if FFMPEG_PATH and Path(FFMPEG_PATH).exists():
            # FFmpeg 디렉토리를 PATH에 추가
            ffmpeg_dir = str(Path(FFMPEG_PATH).parent)
            current_path = os.environ.get('PATH', '')
            if ffmpeg_dir not in current_path:
                os.environ['PATH'] = f"{ffmpeg_dir};{current_path}"
                logger.debug(f"FFmpeg 디렉토리를 PATH에 추가: {ffmpeg_dir}")
    except Exception as e:
        logger.warning(f"FFmpeg 경로 설정 중 오류: {e}")

# 모듈 로드 시 FFmpeg 경로 설정
_setup_ffmpeg_path()

# RVC 사용 가능 여부 확인 (lazy import)
RVC_AVAILABLE = None
_TTS_WITH_RVC = None

def _check_rvc_available():
    """RVC 사용 가능 여부를 확인합니다 (lazy check)."""
    global RVC_AVAILABLE, _TTS_WITH_RVC
    
    if RVC_AVAILABLE is not None:
        return RVC_AVAILABLE
    
    # tts-with-rvc (PyTorch 버전) 우선 시도 - .pth 파일 직접 지원
    try:
        from tts_with_rvc.inference import TTS_RVC
        _TTS_WITH_RVC = TTS_RVC
        RVC_AVAILABLE = True
        logger.info("RVC 로드 성공: tts-with-rvc (inference - PyTorch)")
        return True
    except (ImportError, OSError, Exception) as e1:
        # 최후의 수단: __init__에서 import 시도
        try:
            from tts_with_rvc import TTS_RVC
            _TTS_WITH_RVC = TTS_RVC
            RVC_AVAILABLE = True
            logger.info("RVC 로드 성공: tts-with-rvc (__init__ - PyTorch)")
            return True
        except (ImportError, OSError, Exception) as e2:
            # tts-with-rvc-onnx 시도 (ONNX 버전 - .onnx 파일 필요)
            try:
                from tts_with_rvc.inference_onnx import TTS_RVC
                _TTS_WITH_RVC = TTS_RVC
                RVC_AVAILABLE = True
                logger.info("RVC 로드 성공: tts-with-rvc-onnx (inference_onnx - ONNX)")
                logger.warning("ONNX 버전을 사용 중입니다. .pth 파일 대신 .onnx 파일이 필요합니다.")
                return True
            except (ImportError, OSError, Exception) as e3:
                RVC_AVAILABLE = False
                logger.warning(f"RVC를 로드할 수 없습니다. inference: {e1}, __init__: {e2}, inference_onnx: {e3}")
                logger.warning("pip install tts-with-rvc (PyTorch 버전) 또는 pip install tts-with-rvc-onnx (ONNX 버전)를 설치하세요.")
                return False

# TTS 임시 파일 저장 경로
TTS_TEMP_DIR = Path('logs/tts_temp')
TTS_TEMP_DIR.mkdir(parents=True, exist_ok=True)

# RVC 인스턴스 캐시 (모델별로 캐싱하여 재사용)
_rvc_instances = {}  # {model_name: TTS_RVC instance}
_executor = ThreadPoolExecutor(max_workers=2)

# 데이터 디렉토리
DATA_DIR = Path('data')
DATA_DIR.mkdir(exist_ok=True)

# RVC 모델 설정 파일 경로
RVC_MODELS_FILE = DATA_DIR / 'rvc_models.json'

# 모델 디렉토리 경로
MODELS_DIR = Path('models')


def _remap_legacy_path(old_path: str, base_dir: Path) -> str:
    """다른 PC 의 절대경로를 사용자명에 무관하게 현재 `base_dir` 기준 경로로 재매핑한다.

    경로에 `Tendo_Aris` 가 포함되어 있을 때만 그 이후 부분을 상대경로로 잘라 사용한다.
    """
    if not old_path or 'Tendo_Aris' not in old_path:
        return old_path
    parts = old_path.split('Tendo_Aris', 1)
    if len(parts) <= 1:
        return old_path
    relative_path = parts[1].lstrip('\\/')
    return str(base_dir / relative_path)


def load_rvc_models():
    """RVC 모델 목록을 JSON 에서 로드한다.

    부수적으로 다른 PC 의 절대경로를 현재 프로젝트 기준으로 재매핑하고,
    재매핑 후에도 파일이 없는 항목은 자동으로 제거한 뒤 JSON 을 다시 저장한다.
    """
    try:
        if not RVC_MODELS_FILE.exists():
            return {}
        with open(RVC_MODELS_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content:
                return {}
            import json
            models = json.loads(content)

            from utils.config import BASE_DIR
            updated = False
            to_remove = []

            for model_name, model_info in models.items():
                old_model_path = model_info.get('model_path')
                if old_model_path and 'Tendo_Aris' in old_model_path and not Path(old_model_path).exists():
                    new_path = _remap_legacy_path(old_model_path, BASE_DIR)
                    if Path(new_path).exists():
                        model_info['model_path'] = new_path
                        updated = True
                        logger.info(f"RVC 경로 자동 보정: {model_name} → {new_path}")
                    else:
                        to_remove.append(model_name)
                        logger.warning(
                            f"RVC 모델 파일을 찾을 수 없어 등록 정보를 제거합니다: "
                            f"{model_name} (기존: {old_model_path})"
                        )
                        continue

                old_index_path = model_info.get('index_path')
                if old_index_path and 'Tendo_Aris' in old_index_path and not Path(old_index_path).exists():
                    new_path = _remap_legacy_path(old_index_path, BASE_DIR)
                    if Path(new_path).exists():
                        model_info['index_path'] = new_path
                        updated = True
                    else:
                        # 인덱스는 선택 사항이므로 항목 자체는 유지하고 None 으로만 표시.
                        model_info['index_path'] = None
                        updated = True
                        logger.warning(f"RVC index 파일 없음 (None 처리): {model_name}")

            for name in to_remove:
                del models[name]
                updated = True

            if updated:
                save_rvc_models(models)
                logger.info("RVC 모델 등록 정보가 자동으로 정리되었습니다.")

            return models
    except (FileNotFoundError, Exception) as e:
        logger.warning(f"RVC 모델 파일 로드 실패: {e}")
        return {}


def save_rvc_models(rvc_models: dict):
    """RVC 모델 목록을 파일에 저장합니다."""
    try:
        atomic_write_json(RVC_MODELS_FILE, rvc_models)
        logger.info(f"RVC 모델 저장 완료: {len(rvc_models)}개 모델")
    except Exception as e:
        logger.error(f"RVC 모델 저장 실패: {e}")
        raise


def add_rvc_model(model_name: str, model_path: str, index_path: str = None, display_name: str = None, lang: str = 'ja'):
    """
    RVC 모델을 추가합니다.
    
    Args:
        model_name: 모델의 고유 이름 (키로 사용)
        model_path: RVC 모델 파일 경로 (.pth 파일)
        index_path: 인덱스 파일 경로 (.index 파일, 선택사항)
        display_name: 표시할 이름 (None이면 model_name 사용)
        lang: 언어 코드
    
    Returns:
        성공 여부
    """
    rvc_models = load_rvc_models()
    
    # 경로 검증 및 정규화
    model_path_obj = Path(model_path)
    if not model_path_obj.is_absolute():
        from utils.config import BASE_DIR
        model_path_obj = BASE_DIR / model_path
        model_path = str(model_path_obj.resolve())
    
    if not model_path_obj.exists():
        raise FileNotFoundError(f"RVC 모델 파일을 찾을 수 없습니다: {model_path}")
    
    # 인덱스 파일 경로 처리
    if index_path:
        index_path_obj = Path(index_path)
        if not index_path_obj.is_absolute():
            from utils.config import BASE_DIR
            index_path_obj = BASE_DIR / index_path
            index_path = str(index_path_obj.resolve())
        
        if not index_path_obj.exists():
            logger.warning(f"인덱스 파일을 찾을 수 없습니다: {index_path}")
            index_path = None
    
    rvc_models[model_name] = {
        'model_path': model_path,
        'index_path': index_path,
        'name': display_name or model_name,
        'lang': lang,
        'is_rvc': True
    }
    
    save_rvc_models(rvc_models)
    logger.info(f"RVC 모델 추가: {model_name} -> {model_path}")
    return True


def get_rvc_model(model_name: str):
    """RVC 모델 정보를 가져옵니다."""
    rvc_models = load_rvc_models()
    return rvc_models.get(model_name)


def get_all_rvc_models():
    """모든 RVC 모델 목록을 가져옵니다."""
    return load_rvc_models()


def remove_rvc_model(model_name: str):
    """RVC 모델을 제거합니다."""
    rvc_models = load_rvc_models()
    if model_name in rvc_models:
        del rvc_models[model_name]
        save_rvc_models(rvc_models)
        logger.info(f"RVC 모델 제거: {model_name}")
        return True
    return False


# Edge TTS voice 매핑 (언어 코드 -> Edge TTS voice 이름)
EDGE_TTS_VOICES = {
    'ko': 'ko-KR-SunHiNeural',  # 한국어 여성
    'en': 'en-US-JennyNeural',  # 영어 여성
    'ja': 'ja-JP-NanamiNeural',  # 일본어 여성
    'zh': 'zh-CN-XiaoxiaoNeural',  # 중국어 여성
    'es': 'es-ES-ElviraNeural',  # 스페인어 여성
    'fr': 'fr-FR-DeniseNeural',  # 프랑스어 여성
    'de': 'de-DE-KatjaNeural',  # 독일어 여성
    'pt': 'pt-BR-FranciscaNeural',  # 포르투갈어 여성
}

def _get_edge_tts_voice(lang: str = 'ko') -> str:
    """언어 코드에 맞는 Edge TTS voice를 반환합니다."""
    return EDGE_TTS_VOICES.get(lang, EDGE_TTS_VOICES['ko'])


def _get_rvc_device(device_setting: str) -> str:
    """
    RVC 디바이스 설정을 반환합니다.
    
    Args:
        device_setting: 'auto', 'cuda', 'cuda:0', 'cpu' 등
    
    Returns:
        실제 사용할 디바이스 ('cuda:0', 'cpu' 등)
    """
    if device_setting == 'auto':
        # CUDA 사용 가능 여부 확인
        try:
            import torch
            if torch.cuda.is_available():
                device = 'cuda:0'  # tts-with-rvc는 'cuda:0' 형식 요구
                logger.info(f"CUDA 사용 가능: {torch.cuda.get_device_name(0)} (GPU 사용)")
                return device
            else:
                logger.info("CUDA 사용 불가능, CPU 사용")
                return 'cpu'
        except ImportError:
            logger.warning("PyTorch를 찾을 수 없습니다. CPU 사용")
            return 'cpu'
    elif device_setting.startswith('cuda'):
        # CUDA 강제 사용
        try:
            import torch
            if torch.cuda.is_available():
                # 'cuda'만 입력된 경우 'cuda:0'으로 변환
                if device_setting == 'cuda':
                    device_setting = 'cuda:0'
                logger.info(f"CUDA 강제 사용: {torch.cuda.get_device_name(0)}")
                return device_setting
            else:
                logger.warning("CUDA를 사용할 수 없지만 강제 설정됨. CPU로 폴백합니다.")
                return 'cpu'
        except ImportError:
            logger.warning("PyTorch를 찾을 수 없습니다. CPU 사용")
            return 'cpu'
    else:
        # CPU 또는 기타 설정
        logger.info(f"디바이스 설정: {device_setting}")
        return device_setting


def text_to_speech_with_rvc(
    text: str,
    rvc_model_name: str,
    output_path: Path = None,
    pitch: int = 0,
    speed: float = 1.0,
    lang: str = 'ko'
) -> Path:
    """
    TTS + RVC를 사용하여 텍스트를 원하는 목소리로 변환합니다.
    
    Args:
        text: 변환할 텍스트
        rvc_model_name: 사용할 RVC 모델 이름
        output_path: 출력 파일 경로 (None이면 임시 파일 생성)
        pitch: 피치 조정 (-12 ~ 12)
        speed: 속도 조정 (0.5 ~ 2.0)
        lang: 언어 코드 (기본값: 'ko' - 한국어)
    
    Returns:
        생성된 음성 파일 경로
    """
    if not _check_rvc_available():
        raise ImportError("RVC가 설치되지 않았거나 로드할 수 없습니다. pip install tts-with-rvc-onnx 또는 pip install tts-with-rvc로 설치하세요.")
    
    # RVC 모델 정보 가져오기
    rvc_model = get_rvc_model(rvc_model_name)
    if not rvc_model:
        raise ValueError(f"RVC 모델을 찾을 수 없습니다: {rvc_model_name}")
    
    try:
        # 출력 파일 경로 설정
        if output_path is None:
            temp_file = tempfile.NamedTemporaryFile(
                delete=False,
                suffix='.wav',
                dir=TTS_TEMP_DIR
            )
            output_path = Path(temp_file.name)
            temp_file.close()
        
        # Edge TTS voice 선택
        edge_voice = _get_edge_tts_voice(lang)
        logger.debug(f"Edge TTS voice 선택: {edge_voice} (언어: {lang})")
        
        # GPU/CPU 디바이스 설정
        from utils.config import RVC_DEVICE
        device = _get_rvc_device(RVC_DEVICE)
        
        # RVC 인스턴스 캐시 키 생성 (모델 경로 + 인덱스 경로 + voice + device)
        cache_key = f"{rvc_model['model_path']}_{rvc_model.get('index_path', '')}_{edge_voice}_{device}"
        
        # 캐시된 인스턴스가 있으면 재사용, 없으면 새로 생성
        tts_rvc = None
        if cache_key in _rvc_instances:
            tts_rvc = _rvc_instances[cache_key]
            logger.debug(f"RVC 인스턴스 재사용 시도: {rvc_model_name}")
            # voice가 변경되었을 수 있으므로 업데이트
            if hasattr(tts_rvc, 'set_voice'):
                try:
                    tts_rvc.set_voice(edge_voice)
                except Exception as e:
                    logger.warning(f"인스턴스 voice 설정 실패, 재생성: {e}")
                    # 인스턴스가 손상되었을 수 있으므로 제거하고 재생성
                    del _rvc_instances[cache_key]
                    tts_rvc = None
        
        if tts_rvc is None:
            # TTS + RVC 인스턴스 생성
            # output_directory는 디렉토리 경로여야 하며, 파일 경로가 아닙니다.
            output_dir = str(output_path.parent)
            
            # 출력 디렉토리가 존재하는지 확인하고 없으면 생성
            output_dir_path = Path(output_dir)
            output_dir_path.mkdir(parents=True, exist_ok=True)
            
            logger.info(f"RVC 인스턴스 생성 중: {rvc_model_name} (디바이스: {device}, 처음 사용 시 느릴 수 있습니다)")
            
            try:
                tts_rvc = _TTS_WITH_RVC(
                    model_path=str(rvc_model['model_path']),
                    index_path=str(rvc_model.get('index_path', '')) if rvc_model.get('index_path') else '',
                    voice=edge_voice,  # Edge TTS voice 추가
                    tmp_directory=str(TTS_TEMP_DIR),
                    output_directory=output_dir,  # 디렉토리 경로
                    device=device  # GPU/CPU 디바이스 설정
                )
                # 인스턴스 캐시에 저장
                _rvc_instances[cache_key] = tts_rvc
                logger.info(f"RVC 인스턴스 생성 완료 및 캐시 저장: {rvc_model_name}")
            except Exception as e:
                logger.error(f"RVC 인스턴스 생성 실패: {e}")
                raise
        
        # 텍스트를 음성으로 변환 (__call__ 메서드 사용)
        # speed는 tts_rate 파라미터로 변환 (0 = 기본 속도, 양수/음수로 속도 조정)
        tts_rate = int((speed - 1.0) * 100) if speed != 1.0 else 0
        
        logger.debug(f"RVC TTS 호출: text='{text[:50]}{'...' if len(text) > 50 else ''}', voice={edge_voice}, pitch={pitch}, tts_rate={tts_rate}, filename={output_path.name}")
        
        # 텍스트가 비어있거나 너무 짧으면 오류 발생 가능
        if not text or len(text.strip()) == 0:
            raise ValueError("텍스트가 비어있습니다.")
        
        # 텍스트가 너무 짧으면 공백 추가 (Edge TTS가 짧은 텍스트에서 실패할 수 있음)
        text_to_use = text.strip()
        if len(text_to_use) < 3:
            # 매우 짧은 텍스트는 공백을 추가하여 Edge TTS가 처리할 수 있도록 함
            text_to_use = f" {text_to_use} "
            logger.debug(f"텍스트가 너무 짧아 공백 추가: '{text_to_use}'")
        
        # output_filename은 파일 이름만 (확장자 포함)
        max_retries = 3
        retry_count = 0
        output_file = None
        
        while retry_count < max_retries:
            try:
                # TTS_RVC 호출
                output_file = tts_rvc(
                    text=text_to_use,
                    pitch=pitch,
                    tts_rate=tts_rate,
                    output_filename=output_path.name  # 파일 이름만 (예: "output.wav")
                )
                
                # 반환된 경로 확인
                possible_paths = []
                
                if isinstance(output_file, str):
                    possible_paths.append(Path(output_file))
                
                # output_directory에 파일이 생성되었을 수 있음
                possible_paths.append(Path(output_dir) / output_path.name)
                
                # 원래 output_path도 확인
                possible_paths.append(output_path)
                
                # TTS_TEMP_DIR에서도 찾기
                possible_paths.append(TTS_TEMP_DIR / output_path.name)
                
                # 파일 찾기
                found_file = None
                for file_path in possible_paths:
                    if file_path.exists():
                        file_size = file_path.stat().st_size
                        if file_size > 0:
                            found_file = file_path
                            logger.debug(f"출력 파일 발견: {file_path} (크기: {file_size} bytes)")
                            break
                        else:
                            logger.warning(f"파일이 비어있음: {file_path} (크기: {file_size} bytes)")
                
                if found_file:
                    output_path = found_file
                    logger.info(f"TTS+RVC 파일 생성 완료: {output_path.name} (크기: {found_file.stat().st_size} bytes, 모델: {rvc_model_name}, 언어: {lang}, voice: {edge_voice}, 텍스트 길이: {len(text)})")
                    return output_path
                else:
                    # 모든 가능한 경로를 시도했지만 파일을 찾지 못함
                    raise FileNotFoundError(
                        f"출력 파일이 생성되지 않았습니다: {output_path.name}\n"
                        f"시도한 경로: {[str(p) for p in possible_paths]}"
                    )
                    
            except Exception as e:
                retry_count += 1
                error_msg = str(e)
                logger.warning(f"TTS_RVC 호출 중 오류 (시도 {retry_count}/{max_retries}): {error_msg}, voice={edge_voice}, text_length={len(text_to_use)}, text='{text_to_use[:20]}'")
                
                # "No audio was received" 오류는 Edge TTS 문제일 수 있음
                if "No audio was received" in error_msg or "no audio" in error_msg.lower():
                    logger.warning("Edge TTS에서 오디오를 받지 못했습니다. 네트워크 문제이거나 Edge TTS API 문제일 수 있습니다.")
                    # 마지막 시도가 아니면 잠시 대기 후 재시도
                    if retry_count < max_retries:
                        import time
                        wait_time = retry_count * 2  # 2초, 4초 대기
                        logger.info(f"Edge TTS 재시도 전 {wait_time}초 대기...")
                        time.sleep(wait_time)
                
                if retry_count >= max_retries:
                    # 최대 재시도 횟수 초과 시 인스턴스를 캐시에서 제거하고 오류 발생
                    if cache_key in _rvc_instances:
                        logger.warning(f"손상된 RVC 인스턴스 제거: {cache_key}")
                        try:
                            del _rvc_instances[cache_key]
                        except:
                            pass
                    logger.error(f"TTS_RVC 호출 최종 실패: {error_msg}")
                    raise RuntimeError(f"RVC TTS 생성 실패: {error_msg}")
                else:
                    # 인스턴스 재생성 시도
                    logger.info(f"RVC 인스턴스 재생성 시도...")
                    if cache_key in _rvc_instances:
                        try:
                            del _rvc_instances[cache_key]
                        except:
                            pass
                    
                    # 인스턴스 재생성
                    output_dir = str(output_path.parent)
                    # 출력 디렉토리가 존재하는지 확인하고 없으면 생성
                    output_dir_path = Path(output_dir)
                    output_dir_path.mkdir(parents=True, exist_ok=True)
                    
                    try:
                        tts_rvc = _TTS_WITH_RVC(
                            model_path=str(rvc_model['model_path']),
                            index_path=str(rvc_model.get('index_path', '')) if rvc_model.get('index_path') else '',
                            voice=edge_voice,
                            tmp_directory=str(TTS_TEMP_DIR),
                            output_directory=output_dir,
                            device=device
                        )
                        _rvc_instances[cache_key] = tts_rvc
                        logger.info(f"RVC 인스턴스 재생성 완료")
                    except Exception as recreate_error:
                        logger.error(f"RVC 인스턴스 재생성 실패: {recreate_error}")
                        raise
        
    except Exception as e:
        logger.error(f"TTS+RVC 생성 중 오류: {e}")
        raise


async def text_to_speech_with_rvc_async(
    text: str,
    rvc_model_name: str,
    output_path: Path = None,
    pitch: int = 0,
    speed: float = 1.0,
    lang: str = 'ko'
) -> Path:
    """
    TTS + RVC를 비동기로 실행합니다.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor,
        text_to_speech_with_rvc,
        text,
        rvc_model_name,
        output_path,
        pitch,
        speed,
        lang
    )


def scan_and_register_rvc_models(models_dir: Path = None, auto_register: bool = True):
    """
    models 디렉토리를 스캔하여 RVC 모델을 자동으로 찾고 등록합니다.
    
    Args:
        models_dir: 스캔할 모델 디렉토리 (None이면 기본 models 디렉토리)
        auto_register: True면 자동으로 등록, False면 발견된 모델 목록만 반환
    
    Returns:
        발견된 모델 목록 (dict)
    """
    if models_dir is None:
        from utils.config import BASE_DIR
        models_dir = BASE_DIR / MODELS_DIR
    
    if not models_dir.exists():
        logger.warning(f"모델 디렉토리가 존재하지 않습니다: {models_dir}")
        return {}
    
    discovered_models = {}
    registered_count = 0
    
    # models 디렉토리 내의 모든 하위 디렉토리 스캔
    for model_folder in models_dir.iterdir():
        if not model_folder.is_dir():
            continue
        
        # README.md 같은 파일은 건너뛰기
        if model_folder.name.startswith('.'):
            continue
        
        # .pth 파일 찾기
        pth_files = list(model_folder.glob('*.pth'))
        if not pth_files:
            continue
        
        # 첫 번째 .pth 파일 사용
        model_file = pth_files[0]
        
        # .index 파일 찾기 (선택사항)
        index_files = list(model_folder.glob('*.index'))
        index_file = index_files[0] if index_files else None
        
        # 모델 이름 생성 (폴더 이름 기반)
        model_name = model_folder.name.lower().replace(' ', '_').replace('-', '_')
        # 특수문자 제거
        model_name = ''.join(c for c in model_name if c.isalnum() or c == '_')
        
        # 이미 등록된 모델인지 확인
        existing_models = load_rvc_models()
        if model_name in existing_models:
            logger.debug(f"모델 '{model_name}'은 이미 등록되어 있습니다. 건너뜁니다.")
            continue
        
        # 언어 자동 감지
        lang = 'ja'  # 기본값
        folder_name_lower = model_folder.name.lower()
        if 'ko' in folder_name_lower or 'korean' in folder_name_lower:
            lang = 'ko'
        elif 'en' in folder_name_lower or 'english' in folder_name_lower:
            lang = 'en'
        elif 'jp' in folder_name_lower or 'japanese' in folder_name_lower or 'chisa' in folder_name_lower:
            lang = 'ja'
        
        discovered_models[model_name] = {
            'model_path': str(model_file),
            'index_path': str(index_file) if index_file else None,
            'display_name': model_folder.name,
            'lang': lang
        }
        
        # 자동 등록
        if auto_register:
            try:
                add_rvc_model(
                    model_name=model_name,
                    model_path=str(model_file),
                    index_path=str(index_file) if index_file else None,
                    display_name=model_folder.name,
                    lang=lang
                )
                registered_count += 1
                logger.info(f"자동 등록 완료: {model_name} ({model_folder.name})")
            except Exception as e:
                logger.error(f"모델 자동 등록 실패 ({model_name}): {e}")
    
    if registered_count > 0:
        logger.info(f"총 {registered_count}개의 RVC 모델이 자동으로 등록되었습니다.")
    
    return discovered_models

