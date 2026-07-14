"""지식 문서 검색 유틸리티 (data/knowledge/)

data/knowledge/ 폴더에 넣어 둔 .txt / .md 문서를 문단 단위 청크로 나누고,
rapidfuzz 퍼지 매칭으로 질의와 관련된 청크를 찾아 반환한다.
임베딩 없이 동작하는 경량 검색으로, 대화형 AI가 사용자 정의 지식
(세계관 설정, 서버 규칙, 자주 묻는 질문 등)을 참조해 답할 수 있게 한다.

문서 캐시는 파일 목록·수정 시각 서명이 바뀌면 자동으로 재구축된다.
모든 함수는 동기(blocking)이며, 호출부에서 asyncio.to_thread 로 감싸야 한다.
"""
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz, utils as rf_utils
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False
    logger.warning("rapidfuzz 가 설치되어 있지 않아 지식 문서 검색이 비활성화됩니다.")

KNOWLEDGE_DIR = Path("data") / "knowledge"
KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

KNOWLEDGE_EXTENSIONS = (".txt", ".md")
CHUNK_MAX_CHARS = 500
DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 55.0
MAX_FILE_BYTES = 1024 * 1024  # 파일당 1MB 상한 (실수로 넣은 대용량 파일 방지)

# (파일 경로, mtime_ns, size) 서명이 같으면 캐시를 재사용한다.
_cache_signature: Optional[Tuple] = None
_cache_chunks: List[Dict] = []


def _split_chunks(text: str, max_chars: int = CHUNK_MAX_CHARS) -> List[str]:
    """빈 줄 기준 문단으로 나눈 뒤 max_chars 를 넘지 않게 문단들을 묶는다.

    마크다운 헤더(#)로 시작하는 문단은 새 청크의 시작점으로 삼아,
    서로 다른 주제의 섹션이 한 청크에 섞여 검색 정밀도가 떨어지는 것을 막는다.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    current = ""
    for para in paragraphs:
        if para.startswith("#") and current:
            chunks.append(current)
            current = ""
        if len(para) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(para), max_chars):
                chunks.append(para[start:start + max_chars].strip())
            continue
        candidate = f"{current}\n{para}" if current else para
        if len(candidate) > max_chars:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def _scan_signature() -> Tuple:
    entries = []
    try:
        for path in sorted(KNOWLEDGE_DIR.rglob("*")):
            if path.is_file() and path.suffix.lower() in KNOWLEDGE_EXTENSIONS:
                stat = path.stat()
                entries.append((str(path), stat.st_mtime_ns, stat.st_size))
    except OSError as e:
        logger.warning(f"지식 문서 폴더 스캔 실패: {e}")
    return tuple(entries)


def _rebuild_cache(signature: Tuple) -> None:
    global _cache_signature, _cache_chunks
    chunks: List[Dict] = []
    for path_str, _mtime, size in signature:
        path = Path(path_str)
        if size > MAX_FILE_BYTES:
            logger.warning(f"지식 문서가 너무 커서 건너뜁니다 (1MB 초과): {path.name}")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning(f"지식 문서 읽기 실패({path.name}): {e}")
            continue
        for chunk in _split_chunks(text):
            chunks.append({"file": path.name, "text": chunk})
    _cache_signature = signature
    _cache_chunks = chunks
    logger.info(f"지식 문서 캐시 재구축: 파일 {len(signature)}개, 청크 {len(chunks)}개")


def _ensure_cache() -> List[Dict]:
    signature = _scan_signature()
    if signature != _cache_signature:
        _rebuild_cache(signature)
    return _cache_chunks


def knowledge_stats() -> Dict:
    """지식 문서 폴더 상태를 반환한다: {'dir', 'files': [이름...], 'chunks': 개수}."""
    chunks = _ensure_cache()
    files = sorted({chunk["file"] for chunk in chunks})
    return {"dir": str(KNOWLEDGE_DIR), "files": files, "chunks": len(chunks)}


def search_knowledge(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> List[Dict]:
    """질의와 관련된 지식 청크를 [{'file', 'text', 'score'}] 목록으로 반환한다.

    문서가 없거나 rapidfuzz 미설치, 관련 청크 없음 등은 모두 빈 리스트로 처리한다.
    """
    query = (query or "").strip()
    if not query or not _RAPIDFUZZ_AVAILABLE:
        return []

    chunks = _ensure_cache()
    if not chunks:
        return []

    scored: List[Dict] = []
    for chunk in chunks:
        text = chunk["text"]
        # partial_ratio: 질의가 본문 일부와 일치하는 경우 / token_set_ratio: 단어 집합 유사도
        score = max(
            fuzz.partial_ratio(query, text, processor=rf_utils.default_process),
            fuzz.token_set_ratio(query, text, processor=rf_utils.default_process),
        )
        if score >= min_score:
            scored.append({"file": chunk["file"], "text": text, "score": round(score, 1)})

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:max(1, top_k)]
