"""TTS 대기열: 음악 위에 겹쳐 읽기·연속 메시지·미리 합성 동작 테스트 (실제 음성/FFmpeg 없이)."""
import asyncio
import sys

import discord
import pytest

import cogs.tts as tts_module
import core.audio_mixer as am
from cogs.tts import TTS
from tests.test_audio_mixer import FakeSource, FakeVoiceClient, Recorder


class FakeTTSAudio(FakeSource):
    """FFmpegPCMAudio 대신 쓰는 TTS 소스: 3 프레임 분량."""

    created = []

    def __init__(self, *args, **kwargs):
        super().__init__(300, 3)
        FakeTTSAudio.created.append(self)


class FakeGuild:
    def __init__(self, guild_id, voice_client):
        self.id = guild_id
        self.voice_client = voice_client


class FakeBot:
    def __init__(self, loop):
        self.loop = loop

    def get_cog(self, name):
        return None

    def is_closed(self):
        return False


@pytest.fixture
def tts_env(monkeypatch, tmp_path):
    FakeTTSAudio.created = []
    monkeypatch.setattr(discord, "FFmpegPCMAudio", FakeTTSAudio)
    monkeypatch.setattr(discord, "PCMVolumeTransformer", lambda source, volume=1.0: source)
    monkeypatch.setattr(tts_module, "FFMPEG_PATH", sys.executable)
    monkeypatch.setattr(tts_module, "TTS_FILE_DELETE_DELAY", 0)
    monkeypatch.setattr(tts_module, "load_tts_settings", lambda: {})

    synthesized = []

    async def fake_synthesize(self, text, *args):
        synthesized.append(text)
        path = tmp_path / f"{len(synthesized)}.mp3"
        path.write_bytes(b"ID3")
        return path

    monkeypatch.setattr(TTS, "_synthesize_tts", fake_synthesize)
    return synthesized


async def _drain():
    for _ in range(10):
        await asyncio.sleep(0)


async def _play_until_tts_done(vc, cog, guild_id, max_frames=100):
    """오디오 스레드 대신 프레임을 읽어 TTS 대기열이 다 끝날 때까지 진행한다."""
    for _ in range(max_frames):
        await _drain()
        if not cog.playing.get(guild_id) and not cog.tts_queue.get(guild_id):
            return
        vc.pump(1)
    raise AssertionError("TTS 가 끝나지 않았다")


def test_음악은_멈추지_않고_TTS_가_겹쳐_나온다(tts_env):
    synthesized = tts_env

    async def scenario():
        vc = FakeVoiceClient()
        music = FakeSource(1000, 10_000)
        music_done = Recorder()
        await am.play_main(vc, music, music_done)
        mixer = vc.source

        guild = FakeGuild(1, vc)
        cog = TTS(FakeBot(asyncio.get_running_loop()))
        for text in ("첫째", "둘째", "셋째"):
            assert await cog.enqueue_tts(guild, 10, text, {})
        await _drain()

        # 첫 메시지가 음악 위에 겹쳐졌고, 다음 메시지는 이미 합성을 시작했다
        assert mixer.overlay_count() == 1
        assert "둘째" in synthesized

        await _play_until_tts_done(vc, cog, 1)

        # 음악 FFmpeg 는 한 번도 멈추거나 다시 열리지 않았다
        assert vc.play_calls == 1 and vc.stop_calls == 0
        assert vc.source is mixer and mixer.main is music and not music.cleaned
        assert music_done.calls == []
        # 세 메시지 모두 재생되었고 각각 한 번만 합성되었다
        assert len(FakeTTSAudio.created) == 3 and all(s.cleaned for s in FakeTTSAudio.created)
        assert sorted(synthesized) == ["둘째", "셋째", "첫째"]

    asyncio.run(scenario())


def test_음악이_없으면_TTS_만_재생하고_끝나면_재생을_마친다(tts_env):
    async def scenario():
        vc = FakeVoiceClient()
        guild = FakeGuild(1, vc)
        cog = TTS(FakeBot(asyncio.get_running_loop()))
        assert await cog.enqueue_tts(guild, 10, "안녕", {})
        await _play_until_tts_done(vc, cog, 1)
        vc.pump(2)
        assert not vc.is_playing()

    asyncio.run(scenario())


def test_일시정지한_음악은_그대로_두고_TTS_만_읽은_뒤_다시_멈춘다(tts_env):
    async def scenario():
        vc = FakeVoiceClient()
        music = FakeSource(1000, 10_000)
        await am.play_main(vc, music, Recorder())
        vc.pause()
        frames_before = music.left

        guild = FakeGuild(1, vc)
        cog = TTS(FakeBot(asyncio.get_running_loop()))
        assert await cog.enqueue_tts(guild, 10, "첫째", {})
        assert await cog.enqueue_tts(guild, 10, "둘째", {})
        await _drain()
        assert vc.is_playing() and am.is_main_held(vc)

        await _play_until_tts_done(vc, cog, 1)

        assert vc.is_paused() and not am.is_main_held(vc)
        # 음악은 한 프레임도 진행되지 않아 멈춘 위치 그대로다
        assert music.left == frames_before
        assert vc.play_calls == 1 and vc.stop_calls == 0

    asyncio.run(scenario())


def test_TTS_재생에_실패해도_음악은_영향이_없다(tts_env, monkeypatch):
    async def scenario():
        vc = FakeVoiceClient()
        music = FakeSource(1000, 10_000)
        await am.play_main(vc, music, Recorder())

        def broken(*args, **kwargs):
            raise discord.ClientException("ffmpeg missing")

        monkeypatch.setattr(discord, "FFmpegPCMAudio", broken)
        guild = FakeGuild(1, vc)
        cog = TTS(FakeBot(asyncio.get_running_loop()))
        assert await cog.enqueue_tts(guild, 10, "첫째", {})
        await _drain()

        assert cog.playing[1] is False
        assert vc.is_playing() and vc.source.main is music and not music.cleaned

    asyncio.run(scenario())


def test_합성_엔진_폴백_순서(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_module, "load_tts_settings", lambda: {})
    calls = []

    def fake_supertonic(text, voice_preset, lang):
        calls.append("supertonic")
        if fail["supertonic"]:
            raise RuntimeError("supertonic down")
        return tmp_path / "s.wav"

    async def fake_rvc(text, model, pitch, speed, lang):
        calls.append("rvc")
        return tmp_path / "r.wav"

    def fake_gtts(text, lang, slow, tld):
        calls.append("gtts")
        return tmp_path / "g.mp3"

    monkeypatch.setattr(tts_module, "check_supertonic_available", lambda: True)
    monkeypatch.setattr(tts_module, "_check_rvc_available", lambda: True)
    monkeypatch.setattr(tts_module, "text_to_speech_supertonic", fake_supertonic)
    monkeypatch.setattr(tts_module, "text_to_speech_with_rvc_async", fake_rvc)
    monkeypatch.setattr(tts_module, "text_to_speech", fake_gtts)

    async def synth(**flags):
        cog = TTS(FakeBot(asyncio.get_running_loop()))
        return await cog._synthesize_tts("안녕", rvc_model="m", **flags)

    fail = {"supertonic": False}
    # 둘 다 켜져 있고 Supertonic 이 성공하면 RVC 로 한 번 더 합성하지 않는다
    assert asyncio.run(synth(use_supertonic=True, use_rvc=True)).name == "s.wav"
    assert calls == ["supertonic"]

    # Supertonic 이 실패하면 RVC, 그다음 gTTS 순으로 폴백한다
    calls.clear()
    fail["supertonic"] = True
    assert asyncio.run(synth(use_supertonic=True, use_rvc=True)).name == "r.wav"
    assert calls == ["supertonic", "rvc"]

    calls.clear()
    assert asyncio.run(synth()).name == "g.mp3"
    assert calls == ["gtts"]
