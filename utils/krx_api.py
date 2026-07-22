"""KRX 데이터 마켓플레이스 OPEN API 클라이언트.

https://openapi.krx.co.kr/ 에서 발급받은 인증키(AUTH_KEY)로
일별매매정보·종목기본정보 등을 비동기(aiohttp)로 조회한다.

주의:
- 인증키를 발급받았어도 마이페이지에서 사용할 서비스(컨텐츠)를 별도로
  '이용신청'해야 해당 엔드포인트가 열린다.
- basDd(기준일자)는 영업일이어야 데이터가 있다. 주말/공휴일은 빈 목록을 반환한다.
  → get_latest_daily_trade() 는 최근 영업일을 자동으로 찾아준다.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from utils.config import KRX_API_KEY, KRX_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# KRX OPEN API 기준 도메인 (개발가이드/Spec 문서 기준, https)
BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"

# 서비스별 엔드포인트 경로
ENDPOINTS = {
    # 일별매매정보
    "kospi_daily": "/sto/stk_bydd_trd",   # 유가증권 일별매매정보
    "kosdaq_daily": "/sto/ksq_bydd_trd",  # 코스닥 일별매매정보
    "konex_daily": "/sto/knx_bydd_trd",   # 코넥스 일별매매정보
    # 종목기본정보
    "kospi_base": "/sto/stk_isu_base_info",
    "kosdaq_base": "/sto/ksq_isu_base_info",
    "konex_base": "/sto/knx_isu_base_info",
}

MARKETS = ("kospi", "kosdaq", "konex")
MARKET_LABELS = {"kospi": "코스피", "kosdaq": "코스닥", "konex": "코넥스"}

# 일별 데이터는 하루가 지나야 갱신되므로, 조회 결과를 메모리에 캐시해
# 반복 조회(웹 대시보드 자동 새로고침 등)에서 API 호출을 아낀다.
_CACHE_TTL_SECONDS = 300
_cache: dict[tuple, tuple[float, list[dict]]] = {}
_locks: dict[tuple, asyncio.Lock] = {}


class KrxApiError(RuntimeError):
    """KRX API 호출 실패."""


class KrxAuthError(KrxApiError):
    """인증/이용신청 문제 (HTTP 401·403). 해당 서비스를 신청하지 않았을 때 주로 발생."""


def is_configured() -> bool:
    """인증키가 설정되어 있는지."""
    return bool(KRX_API_KEY)


async def _request(path: str, params: dict) -> list[dict]:
    """KRX OPEN API에 GET 요청을 보내고 OutBlock_1 목록을 돌려준다."""
    if not KRX_API_KEY:
        raise KrxApiError(
            "KRX_API_KEY가 설정되지 않았어요. TOKEN.env에 KRX_API_KEY=<인증키>를 추가해주세요."
        )

    url = f"{BASE_URL}{path}"
    headers = {"AUTH_KEY": KRX_API_KEY}
    timeout = aiohttp.ClientTimeout(total=KRX_TIMEOUT_SECONDS)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params=params) as resp:
                text = await resp.text()
                if resp.status in (401, 403):
                    raise KrxAuthError(
                        "인증 실패(HTTP {}). 인증키가 맞는지, 해당 서비스 이용신청을 했는지 "
                        "확인해주세요.".format(resp.status)
                    )
                if resp.status != 200:
                    raise KrxApiError(f"KRX API 오류 (HTTP {resp.status}): {text[:200]}")
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:  # JSON이 아니면 인증 실패/서비스 미신청일 때가 많음
                    raise KrxApiError(
                        "KRX 응답을 JSON으로 파싱하지 못했어요. "
                        "인증키/서비스 이용신청을 확인해주세요. 응답: " + text[:200]
                    ) from exc
    except aiohttp.ClientError as exc:
        raise KrxApiError(f"KRX API 연결 실패: {exc}") from exc

    # 정상 응답은 {"OutBlock_1": [ {...}, ... ]} 형태
    rows = data.get("OutBlock_1")
    if rows is None:
        raise KrxApiError(f"예상치 못한 응답 형식이에요: {str(data)[:200]}")
    return rows


def _normalize_date(bas_dd: Optional[str]) -> str:
    """기준일자를 YYYYMMDD로 정규화한다. None이면 오늘 날짜를 사용."""
    if bas_dd is None:
        return datetime.now().strftime("%Y%m%d")
    digits = str(bas_dd).replace("-", "").replace("/", "").strip()
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"기준일자는 YYYYMMDD 형식이어야 해요: {bas_dd!r}")
    return digits


def _validate_market(market: str) -> str:
    m = (market or "").lower()
    if m not in MARKETS:
        raise ValueError(f"지원하지 않는 시장이에요: {market!r} (kospi/kosdaq/konex)")
    return m


async def get_daily_trade(market: str = "kospi", bas_dd: Optional[str] = None) -> list[dict]:
    """일별매매정보 조회 (캐시 사용).

    Args:
        market: "kospi" | "kosdaq" | "konex"
        bas_dd: 기준일자(YYYYMMDD). None이면 오늘.

    Returns:
        종목별 시세 dict 목록. 주요 필드:
          ISU_CD(종목코드), ISU_NM(종목명), TDD_CLSPRC(종가),
          TDD_OPNPRC(시가), TDD_HGPRC(고가), TDD_LWPRC(저가),
          CMPPREVDD_PRC(대비), FLUC_RT(등락률), ACC_TRDVOL(거래량),
          ACC_TRDVAL(거래대금), MKTCAP(시가총액)
    """
    m = _validate_market(market)
    day = _normalize_date(bas_dd)
    key = (m, day)

    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]

    # 같은 (시장,날짜)에 대한 동시 요청은 하나만 실제 호출하도록 직렬화
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
        rows = await _request(ENDPOINTS[f"{m}_daily"], {"basDd": day})
        _cache[key] = (time.monotonic(), rows)
        return rows


async def get_latest_daily_trade(
    market: str = "kospi", max_lookback_days: int = 8
) -> tuple[Optional[str], list[dict]]:
    """가장 최근 영업일의 일별매매정보를 찾아 (기준일자, 목록)으로 돌려준다.

    오늘부터 하루씩 거슬러 올라가며(주말/공휴일/장 마감 전이면 빈 목록) 데이터가
    있는 첫 날짜를 반환한다. 전부 비어 있으면 (None, []).
    """
    m = _validate_market(market)
    today = datetime.now()
    for i in range(max_lookback_days):
        day = (today - timedelta(days=i)).strftime("%Y%m%d")
        rows = await get_daily_trade(m, day)
        if rows:
            return day, rows
    return None, []


def _match(row: dict, query: str) -> bool:
    """종목명 부분일치(공백/대소문자 무시) 또는 종목코드 정확일치.

    일별매매정보는 ISU_CD가 6자리 단축코드지만, 종목기본정보는 ISU_CD가 표준코드(12자리)이고
    ISU_SRT_CD가 6자리 단축코드다. 둘 다 대응한다.
    """
    codes = {
        str(row.get("ISU_CD", "")).strip(),
        str(row.get("ISU_SRT_CD", "")).strip(),
    }
    if query in codes:
        return True
    for field in ("ISU_NM", "ISU_ABBRV"):
        name = str(row.get(field, "")).replace(" ", "").lower()
        if name and query in name:
            return True
    return False


async def find_stock(
    name_or_code: str, market: str = "kospi", bas_dd: Optional[str] = None
) -> Optional[dict]:
    """한 시장에서 종목명/코드로 일별매매정보 한 건을 찾는다.

    bas_dd 가 None 이면 최근 영업일을 자동으로 찾아 조회한다.
    """
    query = str(name_or_code).strip().replace(" ", "").lower()
    if bas_dd is None:
        _, rows = await get_latest_daily_trade(market)
    else:
        rows = await get_daily_trade(market, bas_dd)
    for row in rows:
        if _match(row, query):
            return row
    return None


async def search_all_markets(
    name_or_code: str, bas_dd: Optional[str] = None
) -> Optional[tuple[str, dict]]:
    """코스피→코스닥→코넥스 순으로 종목을 찾아 (시장, 시세)로 돌려준다.

    bas_dd 가 None 이면 각 시장의 최근 영업일 데이터를 사용한다.
    이용신청이 안 된 시장(HTTP 401/403)은 건너뛰고 다음 시장을 시도한다.
    모든 시장이 미승인이면 KrxAuthError 를 던진다.
    """
    query = str(name_or_code).strip().replace(" ", "").lower()
    if not query:
        return None
    auth_failed: list[str] = []
    for market in MARKETS:
        try:
            if bas_dd is None:
                _, rows = await get_latest_daily_trade(market)
            else:
                rows = await get_daily_trade(market, bas_dd)
        except KrxAuthError:
            auth_failed.append(market)
            continue
        for row in rows:
            if _match(row, query):
                return market, row
    if len(auth_failed) == len(MARKETS):
        raise KrxAuthError(
            "구독 중인 '일별매매정보' 서비스가 없어요. openapi.krx.co.kr 마이페이지에서 "
            "'유가증권 일별매매정보'(코스피) 또는 '코스닥 일별매매정보'를 이용신청하세요. "
            "('종목기본정보'에는 시세가 없습니다.)"
        )
    return None
