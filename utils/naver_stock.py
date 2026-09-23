"""네이버 금융 준실시간 시세 클라이언트 (비공식).

KRX OPEN API는 '일별 종가'만 제공하므로, 장중 현재가가 필요할 때 네이버 금융의
공개 폴링 엔드포인트를 사용한다. 별도 인증키가 필요 없다.

주의:
- 비공식(사설) 엔드포인트라 예고 없이 응답 형식이 바뀌거나 차단될 수 있다.
- 개인적/비상업적 조회 용도로만 사용한다.

엔드포인트:
- 검색(이름→코드): https://ac.stock.naver.com/ac?q=<질의>&target=stock
- 시세(코드→현재가): https://polling.finance.naver.com/api/realtime/domestic/stock/<코드>
"""
import asyncio
import logging
import re
import time
from typing import Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://ac.stock.naver.com/ac?q={q}&target=stock"
_QUOTE_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.naver.com/",
}
_TIMEOUT_SECONDS = 8

_MARKET_LABELS = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "KONEX": "코넥스"}
_CODE_RE = re.compile(r"^[0-9A-Za-z]{6}$")

# 준실시간이므로 아주 짧게만 캐시(자동 새로고침·다중 뷰어 요청 합치기용)
_QUOTE_TTL = 2.0
_quote_cache: dict[str, tuple[float, dict]] = {}
_search_cache: dict[str, tuple[float, list[dict]]] = {}
_SEARCH_TTL = 3600.0
# 캐시 항목이 이 개수를 넘으면 만료된 항목을 정리한다 (사용자 입력별로 계속 쌓이는 것 방지)
_CACHE_PRUNE_THRESHOLD = 256

# 대시보드가 장중 수 초마다 시세를 폴링하므로 요청마다 TCP/TLS 연결을 새로 맺지 않도록
# 세션을 재사용한다. 세션은 만든 이벤트 루프에 묶이므로 루프가 바뀌면 새로 만든다.
_session: Optional[aiohttp.ClientSession] = None
_session_loop: Optional[asyncio.AbstractEventLoop] = None


class NaverStockError(RuntimeError):
    """네이버 금융 조회 실패."""


def _get_session() -> aiohttp.ClientSession:
    global _session, _session_loop
    loop = asyncio.get_running_loop()
    if _session is None or _session.closed or _session_loop is not loop:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
            headers=_HEADERS,
        )
        _session_loop = loop
    return _session


async def close_session() -> None:
    """재사용 중인 HTTP 세션을 닫습니다. (봇 종료 시 호출)"""
    global _session, _session_loop
    session, _session, _session_loop = _session, None, None
    if session is not None and not session.closed:
        await session.close()


def _prune_expired(cache: dict, ttl: float) -> None:
    if len(cache) <= _CACHE_PRUNE_THRESHOLD:
        return
    now = time.monotonic()
    for key in [k for k, (stamp, _) in cache.items() if now - stamp >= ttl]:
        cache.pop(key, None)


async def _get_json(url: str):
    try:
        async with _get_session().get(url) as r:
            if r.status != 200:
                raise NaverStockError(f"네이버 응답 오류 (HTTP {r.status})")
            return await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise NaverStockError(f"네이버 연결 실패: {e}") from e


def _to_int(value) -> Optional[int]:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _direction(quote_data: dict) -> str:
    """상승/하락/보합을 'up'/'down'/'flat'으로 반환."""
    info = quote_data.get("compareToPreviousPrice") or {}
    name = str(info.get("name", "")).upper()
    code = str(info.get("code", ""))
    if "RIS" in name or "UPPER" in name or code in ("1", "2"):
        return "up"
    if "FALL" in name or "LOWER" in name or code in ("4", "5"):
        return "down"
    return "flat"


async def search(query: str) -> list[dict]:
    """종목명으로 검색해 [{code, name, market}] 목록을 반환한다 (상위 결과 순)."""
    q = (query or "").strip()
    if not q:
        return []
    hit = _search_cache.get(q)
    if hit and time.monotonic() - hit[0] < _SEARCH_TTL:
        return hit[1]
    data = await _get_json(_SEARCH_URL.format(q=quote(q)))
    items = []
    for it in (data.get("items") or []):
        if it.get("category") and it.get("category") != "stock":
            continue
        items.append({
            "code": it.get("code"),
            "name": it.get("name"),
            "market": it.get("typeCode"),
        })
    _search_cache[q] = (time.monotonic(), items)
    _prune_expired(_search_cache, _SEARCH_TTL)
    return items


async def get_quote(code: str) -> dict:
    """종목코드로 준실시간 시세를 조회해 정규화된 dict를 반환한다.

    반환 필드: code, name, market, market_label, price, prev_close, change, rate,
      direction('up'/'down'/'flat'), open, high, low, volume, trade_value,
      market_open(bool), time_text('HH:MM'), traded_at, polling_ms, source
    """
    code = (code or "").strip()
    if not _CODE_RE.match(code):
        raise NaverStockError(f"종목코드 형식이 올바르지 않아요: {code!r}")

    hit = _quote_cache.get(code)
    if hit and time.monotonic() - hit[0] < _QUOTE_TTL:
        return hit[1]

    data = await _get_json(_QUOTE_URL.format(code=code))
    datas = data.get("datas") or []
    if not datas:
        raise NaverStockError(f"'{code}' 시세를 찾지 못했어요.")
    d = datas[0]

    direction = _direction(d)
    sign = -1 if direction == "down" else 1
    change = _to_int(d.get("compareToPreviousClosePrice"))
    if change is not None:
        change = abs(change) * sign
    try:
        rate = abs(float(str(d.get("fluctuationsRatio", "0")).replace(",", ""))) * sign
    except (TypeError, ValueError):
        rate = None

    exch = d.get("stockExchangeType") or {}
    market = exch.get("nameEng") or exch.get("name") or ""
    traded_at = str(d.get("localTradedAt", ""))
    m = re.search(r"T(\d{2}:\d{2})", traded_at)

    result = {
        "code": code,
        "name": d.get("stockName", code),
        "market": market,
        "market_label": _MARKET_LABELS.get(market, market or "-"),
        "price": _to_int(d.get("closePrice")),
        "change": change,
        "rate": rate,
        "direction": direction,
        "open": _to_int(d.get("openPrice")),
        "high": _to_int(d.get("highPrice")),
        "low": _to_int(d.get("lowPrice")),
        "volume": _to_int(d.get("accumulatedTradingVolume")),
        "trade_value": str(d.get("accumulatedTradingValue", "") or ""),
        "market_open": str(d.get("marketStatus", "")).upper() == "OPEN",
        "time_text": m.group(1) if m else "",
        "traded_at": traded_at,
        "polling_ms": _to_int(data.get("pollingInterval")) or 7000,
        "source": "네이버 금융",
    }
    _quote_cache[code] = (time.monotonic(), result)
    _prune_expired(_quote_cache, _QUOTE_TTL)
    return result


async def quote_by_query(query: str) -> Optional[dict]:
    """종목명 또는 6자리 코드로 준실시간 시세를 조회한다. 못 찾으면 None."""
    q = (query or "").strip()
    if not q:
        return None
    # 6자리 코드처럼 보이면 바로 조회, 아니면 이름으로 검색해 첫 결과 사용
    if _CODE_RE.match(q):
        try:
            return await get_quote(q)
        except NaverStockError:
            pass  # 코드가 아니라 이름일 수 있으니 검색으로 폴백
    results = await search(q)
    if not results:
        return None
    return await get_quote(results[0]["code"])
