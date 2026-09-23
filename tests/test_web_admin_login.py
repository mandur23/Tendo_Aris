"""웹 관리자 로그인: 비ASCII 비밀번호, 실패 횟수 제한 테스트."""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

import core.web_admin as wa


def _run(monkeypatch, password, scenario):
    monkeypatch.setattr(wa, "WEB_ADMIN_PASSWORD", password)

    async def sleep_nothing(_):
        return None

    async def main():
        admin = wa.WebAdmin(bot=None)
        monkeypatch.setattr(wa.asyncio, "sleep", sleep_nothing)
        async with TestClient(TestServer(admin.app)) as client:
            await scenario(client)

    asyncio.run(main())


def test_비밀번호_비교는_한글도_지원한다():
    assert wa._safe_compare_password("아리스비번123", "아리스비번123")
    assert not wa._safe_compare_password("아리스비번", "아리스비번123")
    assert not wa._safe_compare_password("x", "")


def test_한글_비밀번호로_로그인할_수_있다(monkeypatch):
    async def scenario(client):
        resp = await client.post("/login", data={"password": "아리스비번"}, allow_redirects=False)
        assert resp.status == 302
        assert wa.COOKIE_NAME in resp.cookies

    _run(monkeypatch, "아리스비번", scenario)


def test_실패가_많으면_올바른_비밀번호도_잠시_막는다(monkeypatch):
    monkeypatch.setattr(wa, "LOGIN_MAX_FAILURES", 3)

    async def scenario(client):
        for _ in range(3):
            resp = await client.post("/login", data={"password": "틀림"}, allow_redirects=False)
            assert resp.status == 401
        resp = await client.post("/login", data={"password": "정답"}, allow_redirects=False)
        assert resp.status == 429

    _run(monkeypatch, "정답", scenario)


def test_성공하면_실패_기록이_초기화된다(monkeypatch):
    monkeypatch.setattr(wa, "LOGIN_MAX_FAILURES", 3)

    async def scenario(client):
        for _ in range(2):
            await client.post("/login", data={"password": "틀림"}, allow_redirects=False)
        resp = await client.post("/login", data={"password": "정답"}, allow_redirects=False)
        assert resp.status == 302
        for _ in range(2):
            resp = await client.post("/login", data={"password": "틀림"}, allow_redirects=False)
            assert resp.status == 401

    _run(monkeypatch, "정답", scenario)
