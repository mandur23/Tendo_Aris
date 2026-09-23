"""대화형 AI 답변 스트리밍 테스트: Ollama 스트림 파싱, 미리보기 메시지 편집, 오류·폴백 처리.

가짜 Ollama HTTP 서버가 토큰을 시간차를 두고 한 줄씩 보내, 실제 청크 응답을
urllib 로 받아 가며 처리하는지까지 확인한다.
"""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import discord
import pytest

import cogs.chat_ai as chat_ai_module
from cogs.chat_ai import ChatAI, _ReplyStreamer


# ---------------------------------------------------------------------- 가짜 Ollama 서버
class _OllamaHandler(BaseHTTPRequestHandler):
    tokens = ["안녕", "하세요", ", 선생님", "!"]
    delay = 0.05
    status = 200
    error_line = None
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        if self.status != 200:
            payload = json.dumps({"error": "model 'x' not found"}).encode()
            self.send_response(self.status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write_chunk(obj):
            data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        if not body.get("stream"):
            text = "".join(self.tokens)
            write_chunk({"message": {"role": "assistant", "content": text}, "done": True})
        else:
            for token in self.tokens:
                write_chunk({"message": {"role": "assistant", "content": token}, "done": False})
                time.sleep(self.delay)
            if self.error_line:
                write_chunk({"error": self.error_line})
            write_chunk({"message": {"role": "assistant", "content": ""}, "done": True})
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def ollama(monkeypatch):
    handler = type("Handler", (_OllamaHandler,), {"requests": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(chat_ai_module, "LOCAL_AI_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    yield handler
    server.shutdown()
    server.server_close()


def test_스트리밍은_토큰이_도착하는_대로_전달된다(ollama):
    cog = ChatAI(None)
    received = []
    started = time.perf_counter()
    reply = cog._request_chat_stream(
        "m", [{"role": "user", "content": "(유저) 안녕"}],
        on_partial=lambda text: received.append((time.perf_counter() - started, text)),
    )
    assert reply == "안녕하세요, 선생님!"
    assert [t for _, t in received] == ["안녕", "안녕하세요", "안녕하세요, 선생님", "안녕하세요, 선생님!"]
    # 첫 토큰은 전체 생성이 끝나기 전에 도착해야 한다 (한 번에 받은 게 아님)
    first_at, last_at = received[0][0], received[-1][0]
    assert last_at - first_at >= ollama.delay * 2
    assert ollama.requests[-1]["stream"] is True


def test_스트림_중간_오류는_예외로_올린다(ollama):
    ollama.error_line = "out of memory"
    cog = ChatAI(None)
    with pytest.raises(RuntimeError, match="out of memory"):
        cog._request_chat_stream("m", [{"role": "user", "content": "x"}], on_partial=lambda t: None)


def test_모델이_없으면_FileNotFoundError(ollama):
    ollama.status = 404
    cog = ChatAI(None)
    with pytest.raises(FileNotFoundError):
        cog._request_chat_stream("m", [{"role": "user", "content": "x"}], on_partial=lambda t: None)


def test_외래_문자가_섞이면_재시도_답변으로_교체한다(monkeypatch):
    cog = ChatAI(None)
    monkeypatch.setattr(cog, "_request_chat_stream", lambda *a, **k: "안녕 你好")
    monkeypatch.setattr(cog, "_request_chat_once", lambda *a, **k: "안녕하세요")
    assert cog._request_chat_korean("m", [], on_partial=lambda t: None) == "안녕하세요"


def test_스트리밍을_끄면_한_번에_받는다(ollama):
    cog = ChatAI(None)
    assert cog._request_chat_korean("m", [{"role": "user", "content": "x"}]) == "안녕하세요, 선생님!"
    assert ollama.requests[-1]["stream"] is False


# ---------------------------------------------------------------------- 미리보기 메시지 편집
class FakeMessage:
    def __init__(self, content):
        self.contents = [content]
        self.deleted = False

    @property
    def content(self):
        return self.contents[-1]

    async def edit(self, *, content):
        self.contents.append(content)

    async def delete(self):
        self.deleted = True


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeCtx:
    def __init__(self):
        self.messages = []

    def typing(self):
        return _Typing()

    async def send(self, content, **kwargs):
        message = FakeMessage(content)
        self.messages.append(message)
        return message


async def _feed_over_time(streamer, texts, gap):
    for text in texts:
        streamer.feed_threadsafe(text)
        await asyncio.sleep(gap)


def test_미리보기는_첫_토큰에_보내고_간격을_두고_고쳐_쓴다():
    async def scenario():
        ctx = FakeCtx()
        streamer = _ReplyStreamer(ctx, interval=0.05)
        texts = [f"토큰{i}" * (i + 1) for i in range(20)]
        await _feed_over_time(streamer, texts, 0.005)   # 100ms 동안 20번 갱신
        await streamer.finish(["최종 답변"])

        assert len(ctx.messages) == 1
        message = ctx.messages[0]
        assert message.contents[0].startswith("아리스: 토큰0")
        assert message.contents[0].endswith(chat_ai_module.STREAM_CURSOR)
        # 토큰마다 편집하지 않고 간격을 둔다 (Discord rate limit)
        assert len(message.contents) < len(texts) / 2
        # 최종 답변은 커서 없이 확정된다
        assert message.content == "아리스: 최종 답변"

    asyncio.run(scenario())


def test_긴_답변은_미리보기를_첫_조각으로_고치고_나머지를_이어_보낸다():
    async def scenario():
        ctx = FakeCtx()
        streamer = _ReplyStreamer(ctx, interval=0.01)
        streamer.feed_threadsafe("가" * 3000)
        await asyncio.sleep(0.03)
        preview = ctx.messages[0].content
        assert len(preview) < 2000       # 미리보기도 Discord 제한 안쪽
        await streamer.finish(["첫 조각", "둘째 조각"])
        assert [m.content for m in ctx.messages] == ["아리스: 첫 조각", "(계속) 둘째 조각"]

    asyncio.run(scenario())


def test_토큰이_오기_전에_끝나면_새로_보낸다():
    async def scenario():
        ctx = FakeCtx()
        streamer = _ReplyStreamer(ctx)
        await streamer.finish(["답변"])
        assert [m.content for m in ctx.messages] == ["아리스: 답변"]

    asyncio.run(scenario())


def test_오류가_나면_미리보기를_지우고_늦은_토큰은_무시한다():
    async def scenario():
        ctx = FakeCtx()
        streamer = _ReplyStreamer(ctx, interval=0.01)
        streamer.feed_threadsafe("중간까지")
        await asyncio.sleep(0.02)
        await streamer.discard()
        assert ctx.messages[0].deleted
        streamer.feed_threadsafe("늦게 온 토큰")
        await asyncio.sleep(0.02)
        assert len(ctx.messages) == 1 and ctx.messages[0].contents == ["아리스: 중간까지" + chat_ai_module.STREAM_CURSOR]

    asyncio.run(scenario())


def test_미리보기에는_외래_문자를_보여주지_않는다():
    async def scenario():
        ctx = FakeCtx()
        streamer = _ReplyStreamer(ctx, interval=0.01)
        streamer.feed_threadsafe("안녕 你好 선생님")
        await asyncio.sleep(0.02)
        assert "你" not in ctx.messages[0].content
        await streamer.discard()

    asyncio.run(scenario())


def test_미리보기_전송_실패는_최종_답변을_막지_않는다():
    async def scenario():
        class FlakyCtx(FakeCtx):
            calls = 0

            async def send(self, content, **kwargs):
                FlakyCtx.calls += 1
                if FlakyCtx.calls == 1:
                    raise discord.HTTPException(type("R", (), {"status": 500, "reason": "x"})(), "boom")
                return await super().send(content, **kwargs)

        ctx = FlakyCtx()
        streamer = _ReplyStreamer(ctx, interval=0.01)
        streamer.feed_threadsafe("미리보기")
        await asyncio.sleep(0.02)
        await streamer.finish(["최종"])
        assert [m.content for m in ctx.messages] == ["아리스: 최종"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------- 대화 흐름 통합
def test_대화_흐름은_스트리밍_미리보기를_최종_답변으로_확정한다(ollama, monkeypatch):
    monkeypatch.setattr(chat_ai_module, "STREAM_EDIT_INTERVAL", 0.01)
    monkeypatch.setattr(chat_ai_module, "LOCAL_AI_STREAM_REPLIES", True)

    async def scenario():
        cog = ChatAI(None)
        key = (1, 2, 3)
        session = chat_ai_module._ChatSession(use_tts=False)
        cog._sessions[key] = session

        async def no_context(ctx, prompt, session):
            return "", "", ""

        monkeypatch.setattr(cog, "_gather_contexts", no_context)
        ctx = FakeCtx()
        await cog._respond(ctx, key, "안녕", author_name="유저")

        assert len(ctx.messages) == 1
        message = ctx.messages[0]
        assert len(message.contents) >= 2                 # 미리보기 후 확정
        assert message.content == "아리스: 안녕하세요, 선생님!"
        assert session.history[-1] == {"role": "assistant", "content": "안녕하세요, 선생님!"}

    asyncio.run(scenario())
