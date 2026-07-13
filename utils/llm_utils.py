"""로컬 LLM(Ollama) 공용 호출 유틸리티

cogs/chat_ai.py 의 Ollama 연동 패턴을 다른 기능(TRPG 등)에서도 재사용할 수 있도록
공용 함수로 제공한다. JSON 강제 출력(format: json)과 비-한국어 외래 문자 정리를 지원한다.
"""
import json
import logging
import re
from typing import Dict, List, Optional
from urllib import error, request

from utils.config import (
    LOCAL_AI_BASE_URL,
    LOCAL_AI_MODEL,
    LOCAL_AI_TEMPERATURE,
    LOCAL_AI_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

# 한글 / ASCII 가시 / 일반 문장부호만 허용. 그 외(한자·가나·라틴 확장·키릴 등) = 외래 문자.
_NON_KOREAN_RE = re.compile(
    r"[^"
    r"가-힣"
    r"ᄀ-ᇿ㄰-㆏"
    r"\x09\x0A\x0D\x20-\x7E"
    r"‐-‧‰-⁞"
    r"]"
)
# URL / 코드 블록은 외래 문자 검사·치환에서 제외하기 위해 따로 매칭한다.
_URL_OR_CODE_RE = re.compile(
    r"(`{1,3}[^`]*`{1,3}|https?://\S+)",
    flags=re.DOTALL,
)


def is_local_ai_configured() -> bool:
    """로컬 AI(Ollama) 설정이 채워져 있는지 확인한다."""
    return bool(LOCAL_AI_BASE_URL and LOCAL_AI_MODEL)


def check_model_available(model: Optional[str] = None) -> None:
    """Ollama에 모델이 설치되어 있는지 /api/tags 로 확인한다.

    모델을 찾을 수 없으면 FileNotFoundError, 연결 실패는 RuntimeError를 던진다.
    (asyncio.to_thread 로 감싸서 사용할 것)
    """
    model_name = model or LOCAL_AI_MODEL
    endpoint = f"{LOCAL_AI_BASE_URL.rstrip('/')}/api/tags"
    req = request.Request(endpoint, method="GET")
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
    except error.URLError as e:
        raise RuntimeError("로컬 AI 서버에 연결할 수 없습니다. Ollama 실행 상태를 확인해주세요.") from e
    except TimeoutError as e:
        raise RuntimeError("로컬 AI 응답 시간 초과가 발생했습니다.") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("로컬 AI 응답을 해석하지 못했습니다.") from e

    # 응답 형식: {"models": [{"name": "model:tag", ...}, ...]}
    # 태그 없이도 비교 (예: "gemma3" → "gemma3:latest" 매칭)
    available = [m.get("name", "") for m in data.get("models", [])]
    base = model_name.split(":")[0]
    for name in available:
        if name == model_name or name.split(":")[0] == base:
            return
    raise FileNotFoundError(
        f"모델 '{model_name}'이 설치되어 있지 않습니다. "
        f"설치된 모델: {available or '없음'}"
    )


def strip_non_korean(text: str) -> str:
    """URL/코드 블록을 보존한 채 본문의 비-한국어 외래 문자만 제거하고 잔여 공백을 정리한다."""

    def _clean_segment(segment: str) -> str:
        cleaned = _NON_KOREAN_RE.sub("", segment)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([,.!?\)\]\}])", r"\1", cleaned)
        return cleaned

    pieces: List[str] = []
    last_end = 0
    for match in _URL_OR_CODE_RE.finditer(text):
        pieces.append(_clean_segment(text[last_end:match.start()]))
        pieces.append(match.group(0))
        last_end = match.end()
    pieces.append(_clean_segment(text[last_end:]))
    return "".join(pieces).strip()


def ollama_chat_sync(
    messages: List[Dict[str, str]],
    *,
    system: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    format_json: bool = False,
    timeout: Optional[int] = None,
) -> str:
    """Ollama /api/chat 를 동기 호출하고 응답 텍스트를 반환한다.

    format_json=True 이면 Ollama 의 JSON 강제 출력 모드를 사용한다.
    모델을 찾을 수 없으면 FileNotFoundError, 그 외 실패는 RuntimeError 를 던진다.
    (asyncio.to_thread 로 감싸서 사용할 것)
    """
    endpoint = f"{LOCAL_AI_BASE_URL.rstrip('/')}/api/chat"
    payload_messages: List[Dict[str, str]] = []
    if system:
        payload_messages.append({"role": "system", "content": system})
    payload_messages.extend(messages)

    payload = {
        "model": model or LOCAL_AI_MODEL,
        "messages": payload_messages,
        "stream": False,
        "options": {
            "temperature": LOCAL_AI_TEMPERATURE if temperature is None else temperature,
        },
    }
    if format_json:
        payload["format"] = "json"

    req = request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=timeout or LOCAL_AI_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
    except error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else str(e)
        if e.code == 404 and "not found" in detail and "model" in detail:
            raise FileNotFoundError(detail) from e
        raise RuntimeError(f"로컬 AI HTTP 오류({e.code}): {detail[:200]}") from e
    except error.URLError as e:
        raise RuntimeError("로컬 AI 서버에 연결할 수 없습니다. Ollama 실행 상태를 확인해주세요.") from e
    except TimeoutError as e:
        raise RuntimeError("로컬 AI 응답 시간 초과가 발생했습니다.") from e

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("로컬 AI 응답을 해석하지 못했습니다.") from e

    content = (parsed.get("message", {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("로컬 AI가 빈 응답을 반환했습니다.")
    return content


def extract_json_object(text: str) -> dict:
    """LLM 응답 텍스트에서 첫 JSON 객체를 찾아 파싱한다.

    코드 펜스(```json ... ```)나 앞뒤 잡음 텍스트가 섞여 있어도 최대한 복구한다.
    파싱 실패 시 ValueError 를 던진다.
    """
    cleaned = text.strip()
    # 코드 펜스 제거
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, flags=re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 첫 '{' 부터 마지막 '}' 까지 잘라서 재시도
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            result = json.loads(cleaned[start:end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    raise ValueError(f"JSON 객체를 찾을 수 없습니다: {text[:120]}")
