"""음성 채널 오디오 믹서: 음악(메인) 위에 TTS(오버레이)를 겹쳐 재생한다.

Discord 음성 클라이언트는 한 번에 소스 하나만 재생할 수 있다. 예전에는 TTS 를 읽을 때마다
음악 FFmpeg 를 끄고, TTS 가 끝나면 스트림을 -ss 로 다시 열어 이어 재생했다.
그래서 메시지마다 음악이 끊기고, 네트워크 스트림을 다시 여는 지연이 생겼다.

MixerSource 는 음성 클라이언트가 재생하는 단일 소스로, 안에 다음을 담는다.
- 메인 슬롯(음악) 하나: 끝나면 메인 콜백을 부른다 (다음 곡 진행).
- 오버레이(TTS) 여러 개: 각자 끝나면 자기 콜백을 부른다.
20ms 프레임마다 PCM 을 더해 내보내고, 오버레이가 있는 동안에는 음악 볼륨을 낮춘다(덕킹).
메인·오버레이가 모두 끝나면 빈 바이트를 돌려줘 재생을 마친다.

콜백은 discord.py 의 after 콜백처럼 오디오 스레드에서 호출될 수 있으므로,
이벤트 루프 작업은 call_soon_threadsafe / run_coroutine_threadsafe 로 넘겨야 한다.
"""
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional, Tuple

import discord

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy 는 requirements 에 포함되어 있다
    np = None
    import audioop

logger = logging.getLogger(__name__)

FRAME_SIZE = discord.opus.Encoder.FRAME_SIZE  # 20ms, 48kHz, 16bit 스테레오 = 3840 bytes
_SILENCE = b"\x00" * FRAME_SIZE

# TTS 가 겹쳐 나오는 동안 음악을 이 비율로 낮춘다.
DUCK_VOLUME = 0.35
# 덕킹 볼륨이 프레임(20ms)마다 움직이는 폭. 갑자기 바뀌면 딸깍 소리가 나므로 약 140ms 에 걸쳐 바꾼다.
DUCK_STEP = 0.1

# 믹서가 막 끝나는 순간과 새 재생 요청이 겹칠 때 재시도하는 횟수와 간격
_PLAY_RETRIES = 20
_PLAY_RETRY_DELAY = 0.02

AfterCallback = Optional[Callable[[Optional[Exception]], None]]

# 스킵·교체처럼 이벤트 루프에서 소스를 끝낼 때 FFmpeg 정리를 맡기는 스레드.
# discord.py 의 FFmpeg 정리는 프로세스를 죽인 뒤 파이프가 닫힐 때까지 기다리므로
# 스트리밍 중인 곡은 수~수십 ms 가 걸린다(측정: 로컬 스트림 약 7~9ms). 이벤트 루프에서 하면
# 그동안 봇 전체가 멈추므로 넘긴다. 워커를 하나로 두어 정리·콜백 순서를 유지한다.
_CLEANUP_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio-cleanup")


def drain_cleanup(timeout: Optional[float] = 5.0) -> None:
    """앞서 넘긴 정리 작업이 모두 끝날 때까지 기다린다. (테스트·종료용)"""
    _CLEANUP_EXECUTOR.submit(lambda: None).result(timeout)


def _pad(data: bytes) -> bytes:
    if len(data) >= FRAME_SIZE:
        return data[:FRAME_SIZE]
    return data + _SILENCE[: FRAME_SIZE - len(data)]


def mix_frames(main: bytes, main_gain: float, overlays: List[bytes]) -> bytes:
    """16bit PCM 프레임들을 더해 한 프레임으로 만든다. (클리핑 포함)"""
    if not overlays:
        if not main:
            return _SILENCE
        main = _pad(main)
        if main_gain >= 0.999:
            return main
        if np is not None:
            scaled = np.frombuffer(main, dtype=np.int16).astype(np.float32) * main_gain
            return scaled.astype(np.int16).tobytes()
        return audioop.mul(main, 2, main_gain)

    if np is not None:
        acc = np.zeros(FRAME_SIZE // 2, dtype=np.float32)
        if main:
            acc += np.frombuffer(_pad(main), dtype=np.int16).astype(np.float32) * main_gain
        for data in overlays:
            acc += np.frombuffer(_pad(data), dtype=np.int16)
        np.clip(acc, -32768, 32767, out=acc)
        return acc.astype(np.int16).tobytes()

    result = audioop.mul(_pad(main), 2, main_gain) if main else _SILENCE
    for data in overlays:
        result = audioop.add(result, _pad(data), 2)  # audioop.add 는 자체적으로 클리핑한다
    return result


def _cleanup_source(source) -> None:
    try:
        source.cleanup()
    except Exception as e:
        logger.debug(f"오디오 소스 정리 중 오류 (무시됨): {e}")


def _finish(finished: List[Tuple[discord.AudioSource, AfterCallback]], error: Optional[Exception] = None) -> None:
    """끝난 소스를 정리하고 각자의 콜백을 부른다. (락 밖에서 호출)"""
    for source, after in finished:
        # discord.py 처럼 FFmpeg 프로세스 실패(소스의 _current_error)를 콜백에 전달한다.
        inner = getattr(source, 'original', source)
        source_error = error or getattr(inner, '_current_error', None)
        _cleanup_source(source)
        if after is not None:
            try:
                after(source_error)
            except Exception:
                logger.exception("오디오 소스 종료 콜백 오류")


class MixerSource(discord.AudioSource):
    """메인(음악) 하나와 오버레이(TTS) 여러 개를 섞어 재생하는 오디오 소스."""

    def __init__(self, duck_volume: float = DUCK_VOLUME):
        self._lock = threading.Lock()
        self._main: Optional[discord.AudioSource] = None
        self._main_after: AfterCallback = None
        self._overlays: List[Tuple[discord.AudioSource, AfterCallback]] = []
        self._closed = False
        self.duck_volume = duck_volume
        self._main_gain = 1.0
        # True 면 음악(메인)을 읽지 않고 제자리에 둔다. 일시정지된 음악 위에 TTS 만 내보낼 때 쓴다.
        self.hold_main = False

    # ------------------------------------------------------------------ 상태
    @property
    def closed(self) -> bool:
        """더 이상 소스를 받지 않는 상태 (재생이 끝났거나 끝나는 중)."""
        return self._closed

    @property
    def main(self) -> Optional[discord.AudioSource]:
        return self._main

    def has_main(self) -> bool:
        return self._main is not None

    def overlay_count(self) -> int:
        return len(self._overlays)

    # ------------------------------------------------------------------ 슬롯 조작
    def set_main(self, source: discord.AudioSource, after: AfterCallback = None) -> bool:
        """메인 소스를 지정한다. 기존 메인은 콜백 없이 정리한다(교체). 닫혔으면 False."""
        with self._lock:
            if self._closed:
                return False
            old = self._main
            self._main, self._main_after = source, after
        if old is not None and old is not source:
            _CLEANUP_EXECUTOR.submit(_cleanup_source, old)
        return True

    def stop_main(self) -> bool:
        """메인 소스만 끝낸다(스킵). 오버레이는 계속 재생된다. 메인이 있었으면 True."""
        with self._lock:
            main, after = self._main, self._main_after
            self._main = self._main_after = None
        if main is None:
            return False
        # 이벤트 루프에서 불리므로 FFmpeg 정리와 콜백(다음 곡 진행)은 정리 스레드에서 한다.
        _CLEANUP_EXECUTOR.submit(_finish, [(main, after)])
        return True

    def add_overlay(self, source: discord.AudioSource, after: AfterCallback = None) -> bool:
        """오버레이 소스를 추가한다. 닫혔으면 False."""
        with self._lock:
            if self._closed:
                return False
            self._overlays.append((source, after))
        return True

    # ------------------------------------------------------------------ AudioSource
    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        # 소스 읽기(FFmpeg 파이프)는 네트워크가 멈추면 오래 막힐 수 있으므로 락 밖에서 한다.
        # 락을 쥔 채 읽으면 이벤트 루프의 add_overlay/stop_main 까지 같이 멈춰 봇 전체가 멎는다.
        with self._lock:
            if self._closed:
                return b""
            main, main_after = self._main, self._main_after
            if self.hold_main:
                main = main_after = None  # 홀드 중: 위치를 유지하도록 읽지 않는다
            overlays = list(self._overlays)

        main_data = main.read() if main is not None else b""
        overlay_reads = [(entry, entry[0].read()) for entry in overlays]

        finished: List[Tuple[discord.AudioSource, AfterCallback]] = []
        with self._lock:
            if self._closed:
                return b""

            if main is not None:
                if self._main is not main:
                    # 읽는 사이에 스킵·교체되었다: 이미 정리된 소스의 데이터는 쓰지 않는다.
                    main_data = b""
                elif len(main_data) < FRAME_SIZE:
                    # FFmpeg 소스는 끝나면 빈(또는 짧은) 프레임을 돌려준다.
                    finished.append((main, main_after))
                    self._main = self._main_after = None

            overlay_data: List[bytes] = []
            for entry, data in overlay_reads:
                if not any(e is entry for e in self._overlays):
                    continue  # 읽는 사이에 정리되었다
                if data:
                    overlay_data.append(data)
                if len(data) < FRAME_SIZE:
                    finished.append(entry)
                    self._overlays = [e for e in self._overlays if e is not entry]

            if not main_data and not overlay_data and self._main is None and not self._overlays:
                # 재생할 것이 없다: 새 소스를 더 받지 않고 재생을 마친다.
                self._closed = True
                frame = b""
            else:
                target = self.duck_volume if (overlay_data or self._overlays) else 1.0
                if self._main_gain < target:
                    self._main_gain = min(target, self._main_gain + DUCK_STEP)
                elif self._main_gain > target:
                    self._main_gain = max(target, self._main_gain - DUCK_STEP)
                frame = mix_frames(main_data, self._main_gain, overlay_data)

        _finish(finished)
        return frame

    def cleanup(self) -> None:
        """재생이 끝나거나 중지되면 남은 소스를 모두 정리하고 콜백을 부른다."""
        with self._lock:
            self._closed = True
            finished: List[Tuple[discord.AudioSource, AfterCallback]] = []
            if self._main is not None:
                finished.append((self._main, self._main_after))
            finished.extend(self._overlays)
            self._main = self._main_after = None
            self._overlays = []
        _finish(finished)


# ---------------------------------------------------------------------- 음성 클라이언트 헬퍼
def get_active_mixer(voice_client) -> Optional[MixerSource]:
    """음성 클라이언트가 지금 재생(또는 일시정지) 중인 믹서를 반환한다."""
    if voice_client is None:
        return None
    source = voice_client.source
    if not isinstance(source, MixerSource) or source.closed:
        return None
    if not (voice_client.is_playing() or voice_client.is_paused()):
        return None
    return source


def _log_player_error(error: Optional[Exception]) -> None:
    if error:
        logger.error(f"믹서 재생 오류: {error}")


async def _start_new_mixer(
    voice_client,
    attach: Callable[[MixerSource], bool],
    *,
    existing_must_be_playing: bool = False,
) -> MixerSource:
    """새 믹서를 만들어 재생을 시작한다. 이전 믹서가 막 끝나는 중이면 잠시 기다렸다 재시도한다.

    기다리는 사이에 다른 요청이 믹서를 만들었으면 그 믹서에 붙인다.
    """
    last_error: Optional[Exception] = None
    for _ in range(_PLAY_RETRIES):
        mixer = get_active_mixer(voice_client)
        if (
            mixer is not None
            and (not existing_must_be_playing or voice_client.is_playing())
            and attach(mixer)
        ):
            return mixer
        if not voice_client.is_playing() and not voice_client.is_paused():
            mixer = MixerSource()
            attach(mixer)
            try:
                voice_client.play(mixer, after=_log_player_error)
                return mixer
            except discord.ClientException as e:
                if not voice_client.is_connected():
                    raise
                last_error = e
        await asyncio.sleep(_PLAY_RETRY_DELAY)
    raise discord.ClientException(f"오디오 재생을 시작하지 못했습니다: {last_error or '다른 소스가 재생 중'}")


async def play_main(voice_client, source: discord.AudioSource, after: AfterCallback = None) -> MixerSource:
    """음악을 메인 슬롯으로 재생한다.

    - TTS 만 재생 중인 믹서가 있으면 그 위에 음악을 붙인다 (TTS 를 끊지 않는다).
    - 이전 음악이 남아 있으면 콜백 없이 교체한다 (다음 곡으로 잘못 넘어가지 않도록).
    - 일시정지 상태였다면 교체 후 재생을 재개한다.
    """
    mixer = get_active_mixer(voice_client)
    if mixer is not None and mixer.set_main(source, after):
        # 새 곡은 항상 재생 상태로 시작한다. (일시정지 음악을 TTS 때문에 홀드하던 중이었어도)
        mixer.hold_main = False
        if voice_client.is_paused():
            voice_client.resume()
        return mixer

    if voice_client.is_playing() or voice_client.is_paused():
        # 믹서가 아닌 소스(예전 방식)가 남아 있으면 정리한다.
        if not isinstance(voice_client.source, MixerSource):
            voice_client.stop()
    return await _start_new_mixer(voice_client, lambda m: m.set_main(source, after))


async def play_overlay(voice_client, source: discord.AudioSource, after: AfterCallback = None) -> MixerSource:
    """TTS 등을 오버레이로 재생한다. 음악이 재생 중이면 음악 위에 겹쳐 나온다.

    음악이 일시정지 상태면 음악은 그 자리에 멈춰 둔 채(hold_main) TTS 만 내보낸다.
    오버레이가 다 끝나면 release_held_main() 으로 다시 일시정지해야 한다.
    """
    mixer = get_active_mixer(voice_client)
    if mixer is not None and voice_client.is_paused() and mixer.has_main():
        mixer.hold_main = True  # 재개하기 전에 설정해야 음악이 한 프레임도 나가지 않는다
        if mixer.add_overlay(source, after):
            voice_client.resume()
            return mixer
        mixer.hold_main = False
    if mixer is not None and voice_client.is_playing() and mixer.add_overlay(source, after):
        return mixer
    return await _start_new_mixer(
        voice_client,
        lambda m: m.add_overlay(source, after),
        existing_must_be_playing=True,
    )


def stop_main(voice_client) -> bool:
    """음악(메인)만 멈춘다. TTS 오버레이는 계속 재생된다. 멈출 것이 있었으면 True."""
    mixer = get_active_mixer(voice_client)
    if mixer is not None and mixer.has_main():
        was_paused = voice_client.is_paused()
        mixer.stop_main()
        if was_paused and mixer.overlay_count() == 0:
            # 일시정지된 믹서는 read 가 호출되지 않아 스스로 끝나지 않으므로 직접 정리한다.
            voice_client.stop()
        return True
    if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
        if not isinstance(voice_client.source, MixerSource):
            voice_client.stop()
            return True
    return False


def release_held_main(voice_client) -> bool:
    """TTS 때문에 잠시 재개했던 일시정지 음악을 오버레이가 다 끝났으면 다시 일시정지한다."""
    mixer = get_active_mixer(voice_client)
    if mixer is None or not mixer.hold_main or mixer.overlay_count():
        return False
    mixer.hold_main = False
    if voice_client.is_playing():
        voice_client.pause()
    return True


def pause_main(voice_client) -> None:
    """음악만 일시정지한다. TTS 가 겹쳐 나오는 중이면 TTS 는 끝까지 읽고 그 뒤에 멈춘다."""
    mixer = get_active_mixer(voice_client)
    if mixer is not None and voice_client.is_playing() and mixer.overlay_count():
        # 음성 클라이언트를 멈추면 TTS 까지 끊기므로 음악 슬롯만 붙잡아 둔다.
        # 오버레이가 다 끝나면 release_held_main() 이 실제로 일시정지한다.
        mixer.hold_main = True
        return
    if voice_client.is_playing():
        voice_client.pause()


def resume_main(voice_client) -> None:
    """일시정지(또는 홀드)된 음악을 다시 재생한다."""
    mixer = get_active_mixer(voice_client)
    if mixer is not None and mixer.hold_main:
        mixer.hold_main = False
    if voice_client.is_paused():
        voice_client.resume()


def is_main_paused(voice_client) -> bool:
    """음악이 멈춰 있는지 (일시정지, 또는 TTS 가 나오는 동안 홀드) 여부."""
    return voice_client is not None and (voice_client.is_paused() or is_main_held(voice_client))


def is_main_held(voice_client) -> bool:
    """일시정지된 음악을 붙잡아 둔 채 TTS 를 내보내는 중인지 여부."""
    mixer = get_active_mixer(voice_client)
    return mixer is not None and mixer.hold_main


def get_main_source(voice_client) -> Optional[discord.AudioSource]:
    """지금 재생 중인 음악 소스(볼륨 조절용)를 반환한다."""
    mixer = get_active_mixer(voice_client)
    if mixer is not None:
        return mixer.main
    if voice_client is not None:
        return voice_client.source
    return None
