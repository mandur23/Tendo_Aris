"""시세 조회: HTTP 세션 재사용과 캐시 정리 테스트 (네트워크 없이)."""
import asyncio
import time

import utils.krx_api as krx
import utils.naver_stock as naver


def test_세션은_같은_루프에서_재사용되고_루프가_바뀌면_새로_만든다():
    async def get_twice():
        first = naver._get_session()
        second = naver._get_session()
        return first, second

    try:
        a1, a2 = asyncio.run(get_twice())
        assert a1 is a2
        b1, _ = asyncio.run(get_twice())
        assert b1 is not a1
    finally:
        asyncio.run(naver.close_session())
    assert naver._session is None


def test_네이버_캐시는_만료_항목을_정리한다(monkeypatch):
    monkeypatch.setattr(naver, "_CACHE_PRUNE_THRESHOLD", 3)
    cache = {}
    old = time.monotonic() - 10_000
    for i in range(5):
        cache[f"old{i}"] = (old, [])
    cache["fresh"] = (time.monotonic(), [])

    naver._prune_expired(cache, ttl=60)
    assert list(cache) == ["fresh"]


def test_KRX_캐시와_락을_정리한다(monkeypatch):
    monkeypatch.setattr(krx, "_CACHE_PRUNE_THRESHOLD", 2)
    monkeypatch.setattr(krx, "_cache", {})
    monkeypatch.setattr(krx, "_locks", {})
    old = time.monotonic() - 10_000
    for day in ("20240101", "20240102", "20240103"):
        krx._cache[("kospi", day)] = (old, [])
        krx._locks[("kospi", day)] = asyncio.Lock()
    krx._cache[("kospi", "20240104")] = (time.monotonic(), [])
    krx._locks[("kospi", "20240104")] = asyncio.Lock()

    krx._prune_cache()
    assert list(krx._cache) == [("kospi", "20240104")]
    assert list(krx._locks) == [("kospi", "20240104")]
