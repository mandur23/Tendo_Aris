"""오디오 믹서: 음악 위에 TTS 를 겹쳐 재생하는 동작 테스트 (실제 음성 연결 없이)."""
import array
import asyncio

import discord
import pytest

import core.audio_mixer as am
from core.audio_mixer import FRAME_SIZE, MixerSource

SAMPLES = FRAME_SIZE // 2


def frame(value: int) -> bytes:
    return array.array("h", [value] * SAMPLES).tobytes()


def samples(data: bytes):
    return array.array("h", data)


class FakeSource(discord.AudioSource):
    """정해진 프레임 수만큼 같은 값을 내보내는 PCM 소스."""

    def __init__(self, value: int, frames: int):
        self.value = value
        self.left = frames
        self.cleaned = False

    def read(self) -> bytes:
        if self.left <= 0:
            return b""
        self.left -= 1
        return frame(self.value)

    def cleanup(self) -> None:
        self.cleaned = True


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, error):
        self.calls.append(error)


def test_음악만_있으면_그대로_내보낸다():
    mixer = MixerSource()
    mixer.set_main(FakeSource(1000, 2))
    assert samples(mixer.read())[0] == 1000


def test_TTS_가_겹치면_음악을_낮추고_더한다():
    mixer = MixerSource(duck_volume=0.5)
    mixer.set_main(FakeSource(1000, 100))
    mixer.add_overlay(FakeSource(300, 100))

    values = [samples(mixer.read())[0] for _ in range(10)]
    # 덕킹은 한 번에 떨어지지 않고 서서히 내려간다
    assert values[0] > values[-1]
    # 충분히 지나면 음악 1000*0.5 + TTS 300
    assert values[-1] == 800


def test_클리핑():
    mixed = am.mix_frames(frame(30000), 1.0, [frame(30000)])
    assert samples(mixed)[0] == 32767


def test_TTS_가_끝나면_콜백을_부르고_음악은_계속된다():
    mixer = MixerSource()
    music_done, tts_done = Recorder(), Recorder()
    music = FakeSource(1000, 50)
    tts = FakeSource(300, 2)
    mixer.set_main(music, music_done)
    mixer.add_overlay(tts, tts_done)

    for _ in range(3):
        assert mixer.read()
    assert tts_done.calls == [None] and tts.cleaned
    assert music_done.calls == [] and not music.cleaned
    assert mixer.overlay_count() == 0 and mixer.has_main()


def test_음악이_끝나도_TTS_는_끝까지_나오고_둘_다_끝나면_재생을_마친다():
    mixer = MixerSource()
    music_done, tts_done = Recorder(), Recorder()
    mixer.set_main(FakeSource(1000, 1), music_done)
    mixer.add_overlay(FakeSource(300, 3), tts_done)

    assert mixer.read()          # 음악 마지막 프레임 + TTS
    assert mixer.read()          # 음악 끝남 감지, TTS 계속
    assert music_done.calls == [None]
    assert samples(mixer.read())[0] == 300   # TTS 만 남음
    assert mixer.read() == b""   # 둘 다 끝남 → 재생 종료
    assert tts_done.calls == [None]
    assert mixer.closed
    # 닫힌 믹서는 새 소스를 받지 않는다
    assert not mixer.add_overlay(FakeSource(1, 1))


def test_스킵은_음악만_끝내고_TTS_는_유지한다():
    mixer = MixerSource()
    music_done, tts_done = Recorder(), Recorder()
    music = FakeSource(1000, 100)
    mixer.set_main(music, music_done)
    mixer.add_overlay(FakeSource(300, 100), tts_done)

    assert mixer.stop_main()
    assert music_done.calls == [None] and music.cleaned
    assert tts_done.calls == []
    assert samples(mixer.read())[0] > 0


def test_메인_교체는_이전_곡_콜백을_부르지_않는다():
    """교체 시 이전 곡 콜백이 불리면 다음 곡이 곧바로 스킵된다."""
    mixer = MixerSource()
    old_done = Recorder()
    old = FakeSource(1000, 100)
    mixer.set_main(old, old_done)
    mixer.set_main(FakeSource(500, 100), Recorder())
    assert old.cleaned and old_done.calls == []


def test_정지하면_남은_소스를_모두_정리하고_콜백을_부른다():
    mixer = MixerSource()
    music_done, tts_done = Recorder(), Recorder()
    mixer.set_main(FakeSource(1000, 100), music_done)
    mixer.add_overlay(FakeSource(300, 100), tts_done)
    mixer.cleanup()
    assert music_done.calls == [None] and tts_done.calls == [None]
    assert mixer.closed


# ---------------------------------------------------------------------- 음성 클라이언트 헬퍼
class FakeVoiceClient:
    """discord.VoiceClient 의 재생 상태만 흉내 낸다. pump() 로 오디오 스레드를 대신 돌린다."""

    def __init__(self):
        self.source = None
        self._playing = False
        self._paused = False
        self.play_calls = 0
        self.stop_calls = 0

    def is_connected(self):
        return True

    def is_playing(self):
        return self._playing and not self._paused

    def is_paused(self):
        return self._playing and self._paused

    def play(self, source, after=None):
        if self.is_playing():
            raise discord.ClientException("Already playing audio.")
        self.play_calls += 1
        self.source = source
        self._playing = True
        self._paused = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self.stop_calls += 1
        if self.source is not None:
            self.source.cleanup()
        self.source = None
        self._playing = False
        self._paused = False

    def pump(self, frames=1):
        for _ in range(frames):
            if not self.is_playing():
                return
            if not self.source.read():
                self.stop()


def test_TTS_만_재생중일_때_음악을_시작하면_끊지_않고_붙인다():
    async def scenario():
        vc = FakeVoiceClient()
        tts_done = Recorder()
        await am.play_overlay(vc, FakeSource(300, 100), tts_done)
        mixer = vc.source
        await am.play_main(vc, FakeSource(1000, 100), Recorder())
        assert vc.source is mixer and vc.play_calls == 1
        assert tts_done.calls == []
        assert mixer.has_main() and mixer.overlay_count() == 1

    asyncio.run(scenario())


def test_음악_재생중_TTS_는_같은_믹서에_겹친다():
    async def scenario():
        vc = FakeVoiceClient()
        await am.play_main(vc, FakeSource(1000, 100), Recorder())
        await am.play_overlay(vc, FakeSource(300, 5), Recorder())
        assert vc.play_calls == 1 and vc.stop_calls == 0
        assert vc.source.overlay_count() == 1

    asyncio.run(scenario())


def test_끝난_믹서에는_붙이지_않고_새로_시작한다():
    async def scenario():
        vc = FakeVoiceClient()
        await am.play_overlay(vc, FakeSource(300, 1), Recorder())
        vc.pump(3)  # TTS 가 끝나 재생 종료
        assert not vc.is_playing()
        await am.play_overlay(vc, FakeSource(300, 5), Recorder())
        assert vc.play_calls == 2 and vc.is_playing()

    asyncio.run(scenario())


def test_일시정지된_음악을_스킵하면_재생을_정리한다():
    async def scenario():
        vc = FakeVoiceClient()
        music_done = Recorder()
        await am.play_main(vc, FakeSource(1000, 100), music_done)
        vc.pause()
        assert am.stop_main(vc)
        assert music_done.calls == [None]
        assert not vc.is_playing() and not vc.is_paused()

    asyncio.run(scenario())


def test_일시정지_중_다음_곡을_붙이면_재생을_재개한다():
    async def scenario():
        vc = FakeVoiceClient()
        await am.play_main(vc, FakeSource(1000, 100), Recorder())
        vc.pause()
        await am.play_main(vc, FakeSource(500, 100), Recorder())
        assert vc.is_playing() and vc.play_calls == 1

    asyncio.run(scenario())


def test_볼륨_조절용_음악_소스():
    async def scenario():
        vc = FakeVoiceClient()
        music = FakeSource(1000, 100)
        await am.play_main(vc, music, Recorder())
        assert am.get_main_source(vc) is music

    asyncio.run(scenario())


def test_TTS_중_일시정지하면_TTS_는_끝까지_읽고_그다음_멈춘다():
    async def scenario():
        vc = FakeVoiceClient()
        music = FakeSource(1000, 1000)
        await am.play_main(vc, music, Recorder())
        tts_done = Recorder()
        await am.play_overlay(vc, FakeSource(300, 3), tts_done)

        am.pause_main(vc)
        assert vc.is_playing() and am.is_main_paused(vc)   # TTS 는 계속 나온다
        left = music.left
        vc.pump(5)
        assert tts_done.calls == [None]
        assert music.left == left                           # 음악은 진행되지 않았다

        assert am.release_held_main(vc)
        assert vc.is_paused() and am.is_main_paused(vc)

    asyncio.run(scenario())


def test_홀드_중_재개하면_음악이_바로_이어진다():
    async def scenario():
        vc = FakeVoiceClient()
        music = FakeSource(1000, 1000)
        await am.play_main(vc, music, Recorder())
        vc.pause()
        await am.play_overlay(vc, FakeSource(300, 50), Recorder())
        assert am.is_main_held(vc)

        am.resume_main(vc)
        left = music.left
        vc.pump(2)
        assert not am.is_main_paused(vc) and music.left < left
        # 재개한 뒤에는 TTS 가 끝나도 다시 멈추지 않는다
        assert not am.release_held_main(vc)

    asyncio.run(scenario())


def test_음악_소스를_읽는_중에도_이벤트_루프_쪽_조작이_막히지_않는다():
    """소스 read() 가 오래 걸려도 add_overlay/stop_main 이 락에 막히지 않아야 한다."""
    import threading

    entered, release = threading.Event(), threading.Event()

    class SlowSource(FakeSource):
        def read(self):
            entered.set()
            release.wait(2)
            return super().read()

    mixer = MixerSource()
    mixer.set_main(SlowSource(1000, 10), Recorder())
    reader = threading.Thread(target=mixer.read)
    reader.start()
    assert entered.wait(2)

    done = threading.Event()
    threading.Thread(target=lambda: (mixer.add_overlay(FakeSource(1, 1)), mixer.stop_main(), done.set())).start()
    assert done.wait(0.5), "소스를 읽는 동안 믹서 조작이 막혔다"
    release.set()
    reader.join(2)


def test_홀드_중_다음_곡으로_넘어가면_새_곡은_바로_재생된다():
    async def scenario():
        vc = FakeVoiceClient()
        await am.play_main(vc, FakeSource(1000, 1000), Recorder())
        vc.pause()
        await am.play_overlay(vc, FakeSource(300, 50), Recorder())
        assert am.is_main_held(vc)

        am.stop_main(vc)                       # 스킵
        new_song = FakeSource(500, 1000)
        await am.play_main(vc, new_song, Recorder())
        left = new_song.left
        vc.pump(2)
        assert not am.is_main_paused(vc) and new_song.left < left
        assert not am.release_held_main(vc)    # TTS 가 끝나도 새 곡을 멈추지 않는다

    asyncio.run(scenario())
