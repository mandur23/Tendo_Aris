"""RVC (Retrieval-based Voice Conversion) 유틸리티"""
import logging
from pathlib import Path
import tempfile
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# RVC 사용 가능 여부 확인 (lazy import)
RVC_AVAILABLE = None
_TTS_WITH_RVC = None

def _check_rvc_available():
    """RVC 사용 가능 여부를 확인합니다 (lazy check)."""
    global RVC_AVAILABLE, _TTS_WITH_RVC
    
    if RVC_AVAILABLE is not None:
        return RVC_AVAILABLE
    
    # tts-with-rvc-onnx 우선 시도 (inference_onnx에서 TTS_RVC import)
    try:
        from tts_with_rvc.inference_onnx import TTS_RVC
        _TTS_WITH_RVC = TTS_RVC
        RVC_AVAILABLE = True
        logger.info("RVC 로드 성공: tts-with-rvc-onnx (inference_onnx)")
        return True
    except (ImportError, OSError, Exception) as e1:
        # tts-with-rvc 시도 (inference에서 TTS_RVC import)
        try:
            from tts_with_rvc.inference import TTS_RVC
            _TTS_WITH_RVC = TTS_RVC
            RVC_AVAILABLE = True
            logger.info("RVC 로드 성공: tts-with-rvc (inference)")
            return True
        except (ImportError, OSError, Exception) as e2:
            # 최후의 수단: __init__에서 import 시도
            try:
                from tts_with_rvc import TTS_RVC
                _TTS_WITH_RVC = TTS_RVC
                RVC_AVAILABLE = True
                logger.info("RVC 로드 성공: tts-with-rvc (__init__)")
                return True
            except (ImportError, OSError, Exception) as e3:
                RVC_AVAILABLE = False
                logger.warning(f"RVC를 로드할 수 없습니다. inference_onnx: {e1}, inference: {e2}, __init__: {e3}")
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


def load_rvc_models():
    """RVC 모델 목록을 파일에서 로드합니다."""
    try:
        if not RVC_MODELS_FILE.exists():
            return {}
        with open(RVC_MODELS_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content:
                return {}
            import json
            return json.loads(content)
    except (FileNotFoundError, Exception) as e:
        logger.warning(f"RVC 모델 파일 로드 실패: {e}")
        return {}


def save_rvc_models(rvc_models: dict):
    """RVC 모델 목록을 파일에 저장합니다."""
    try:
        import json
        with open(RVC_MODELS_FILE, 'w', encoding='utf-8') as f:
            json.dump(rvc_models, f, ensure_ascii=False, indent=4)
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
        
        # RVC 인스턴스 캐시 키 생성 (모델 경로 + 인덱스 경로 + voice)
        cache_key = f"{rvc_model['model_path']}_{rvc_model.get('index_path', '')}_{edge_voice}"
        
        # 캐시된 인스턴스가 있으면 재사용, 없으면 새로 생성
        if cache_key in _rvc_instances:
            tts_rvc = _rvc_instances[cache_key]
            logger.debug(f"RVC 인스턴스 재사용: {rvc_model_name}")
            # voice가 변경되었을 수 있으므로 업데이트
            if hasattr(tts_rvc, 'set_voice'):
                tts_rvc.set_voice(edge_voice)
        else:
            # TTS + RVC 인스턴스 생성
            # output_directory는 디렉토리 경로여야 하며, 파일 경로가 아닙니다.
            output_dir = str(output_path.parent)
            logger.info(f"RVC 인스턴스 생성 중: {rvc_model_name} (처음 사용 시 느릴 수 있습니다)")
            tts_rvc = _TTS_WITH_RVC(
                model_path=str(rvc_model['model_path']),
                index_path=str(rvc_model.get('index_path', '')) if rvc_model.get('index_path') else '',
                voice=edge_voice,  # Edge TTS voice 추가
                tmp_directory=str(TTS_TEMP_DIR),
                output_directory=output_dir  # 디렉토리 경로
            )
            # 인스턴스 캐시에 저장
            _rvc_instances[cache_key] = tts_rvc
            logger.info(f"RVC 인스턴스 생성 완료 및 캐시 저장: {rvc_model_name}")
        
        # 텍스트를 음성으로 변환 (__call__ 메서드 사용)
        # speed는 tts_rate 파라미터로 변환 (0 = 기본 속도, 양수/음수로 속도 조정)
        tts_rate = int((speed - 1.0) * 100) if speed != 1.0 else 0
        
        logger.debug(f"RVC TTS 호출: text='{text[:50]}{'...' if len(text) > 50 else ''}', voice={edge_voice}, pitch={pitch}, tts_rate={tts_rate}, filename={output_path.name}")
        
        # 텍스트가 비어있거나 너무 짧으면 오류 발생 가능
        if not text or len(text.strip()) == 0:
            raise ValueError("텍스트가 비어있습니다.")
        
        # output_filename은 파일 이름만 (확장자 포함)
        try:
            output_file = tts_rvc(
                text=text,
                pitch=pitch,
                tts_rate=tts_rate,
                output_filename=output_path.name  # 파일 이름만 (예: "output.wav")
            )
        except Exception as e:
            logger.error(f"TTS_RVC 호출 중 오류: {e}, voice={edge_voice}, text_length={len(text)}")
            raise
        
        # 반환된 경로가 다를 수 있으므로 확인
        if isinstance(output_file, str):
            output_path = Path(output_file)
        
        logger.info(f"TTS+RVC 파일 생성 완료: {output_path.name} (모델: {rvc_model_name}, 언어: {lang}, voice: {edge_voice}, 텍스트 길이: {len(text)})")
        return output_path
        
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

