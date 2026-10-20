"""TTS (Text-to-Speech) 명령어 Cog"""
import asyncio
import logging
import re
from typing import Optional
import discord
from discord.ext import commands
from pathlib import Path
from utils.tts_utils import text_to_speech, cleanup_tts_file, sweep_stale_tts_files, VOICE_MODELS
from utils.file_utils import load_tts_settings, save_tts_settings, run_storage_io
from utils.config import COMMAND_MESSAGE_DELETE_DELAY, FFMPEG_PATH
from utils.discord_utils import safe_defer, safe_typing
from core.audio_mixer import play_overlay, release_held_main
from utils.rvc_utils import (
    _check_rvc_available, add_rvc_model, remove_rvc_model,
    get_all_rvc_models, get_rvc_model, text_to_speech_with_rvc_async,
    scan_and_register_rvc_models
)
from utils.supertonic_utils import (
    check_supertonic_available, text_to_speech_supertonic,
    check_supertonic_model_exists, download_supertonic_model,
    get_available_voice_presets, DEFAULT_VOICE_PRESET
)

logger = logging.getLogger(__name__)
TTS_QUEUE_MAX = 20
# 재생이 끝난 TTS 임시 파일을 지우기까지 기다리는 시간(초). FFmpeg 가 파일을 놓을 때까지의 여유.
TTS_FILE_DELETE_DELAY = 2.0

# TTS 읽기 전처리용 정규식
_URL_RE = re.compile(r'https?://\S+')
_CUSTOM_EMOJI_RE = re.compile(r'<a?:\w+:\d+>')
_REPEAT_CHAR_RE = re.compile(r'(.)\1{3,}')


class TTS(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tts_settings = load_tts_settings()
        # 활성화된 사용자 목록 (guild_id -> set of user_ids)
        self.active_users = {}
        # 재생 대기열 (guild_id -> list of (user_id, text, settings))
        self.tts_queue = {}
        # 재생 중인지 확인 (guild_id -> bool)
        self.playing = {}
        # TTS 재생 직렬화 락 (guild_id -> asyncio.Lock)
        self._playback_locks = {}
        # 음성 연결 경합 방지 락 (guild_id -> asyncio.Lock)
        self._voice_connect_locks = {}
        # 설정 변경부터 저장 완료까지 직렬화한다.
        self._settings_write_lock = asyncio.Lock()
        # 재생 중 다음 메시지 미리 합성: guild_id -> (대기열 항목, 합성 태스크)
        self._presynth = {}
        # 대기열 처리 태스크 참조 보관 (GC 방지)
        self._queue_tasks: set = set()

    def _get_playback_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._playback_locks:
            self._playback_locks[guild_id] = asyncio.Lock()
        return self._playback_locks[guild_id]

    async def enqueue_tts(self, guild, user_id: int, text: str, settings: dict) -> bool:
        """TTS를 길드 대기열에 추가하고, 재생 중이 아니면 처리를 시작합니다."""
        text = (text or "").strip()
        if not text or len(text) > 200:
            return False

        if guild.id not in self.tts_queue:
            self.tts_queue[guild.id] = []
        if len(self.tts_queue[guild.id]) >= TTS_QUEUE_MAX:
            logger.warning(f"TTS 대기열 상한 초과 (guild={guild.id})")
            return False

        self.tts_queue[guild.id].append((user_id, text, settings))
        async with self._get_playback_lock(guild.id):
            if not self.playing.get(guild.id, False):
                task = asyncio.create_task(self._process_queue(guild))
                # 참조를 들고 있지 않으면 태스크가 GC 로 사라질 수 있다.
                self._queue_tasks.add(task)
                task.add_done_callback(self._queue_tasks.discard)
            else:
                # 재생 중에 들어온 메시지는 지금 합성을 시작해 두면 차례가 왔을 때 바로 읽는다.
                self._start_presynthesis(guild.id)
        return True

    def _get_voice_connect_lock(self, guild_id: int) -> asyncio.Lock:
        """길드별 음성 연결 락을 반환합니다."""
        if guild_id not in self._voice_connect_locks:
            self._voice_connect_locks[guild_id] = asyncio.Lock()
        return self._voice_connect_locks[guild_id]

    def get_user_settings(self, user_id: int, guild_id: int):
        """사용자의 TTS 설정을 가져옵니다."""
        user_key = f"{guild_id}_{user_id}"
        if user_key not in self.tts_settings:
            # 기본 설정
            return {
                'enabled': False,
                'lang': 'ko',
                'voice_model': '기본',
                'slow': False,
                'use_rvc': False,  # RVC 사용 여부
                'rvc_model': None,  # RVC 모델 이름
                'use_supertonic': False,  # Supertonic 사용 여부
                'supertonic_voice': DEFAULT_VOICE_PRESET  # Supertonic 음성 프리셋 (M1)
            }
        return self.tts_settings[user_key]

    async def set_user_settings(self, user_id: int, guild_id: int, **kwargs):
        """저장 완료 후 설정을 적용합니다. 실패는 명령/버튼 호출자에게 전파합니다."""
        async with self._settings_write_lock:
            snapshot = {
                key: dict(value) if isinstance(value, dict) else value
                for key, value in self.tts_settings.items()
            }
            user_key = f"{guild_id}_{user_id}"
            snapshot.setdefault(user_key, {
                'enabled': False, 'lang': 'ko', 'voice_model': '기본', 'slow': False,
            }).update(kwargs)
            await run_storage_io(save_tts_settings, snapshot)
            self.tts_settings = snapshot

    def is_user_active(self, user_id: int, guild_id: int) -> bool:
        """사용자의 TTS 자동 읽기가 활성화되어 있는지 확인합니다."""
        settings = self.get_user_settings(user_id, guild_id)
        return settings.get('enabled', False)

    @staticmethod
    def preprocess_tts_text(text: str) -> str:
        """메시지를 TTS로 읽기 좋게 정리합니다.

        - URL은 '링크'로 치환
        - 커스텀 이모지 태그(<:이름:id>)는 제거
        - 같은 문자 4회 이상 반복은 3회로 축약 (예: ㅋㅋㅋㅋㅋㅋ → ㅋㅋㅋ)
        - 멘션 기호(@)는 제거하고 잔여 공백을 정리
        """
        if not text:
            return ''
        text = _URL_RE.sub('링크', text)
        text = _CUSTOM_EMOJI_RE.sub('', text)
        text = _REPEAT_CHAR_RE.sub(lambda m: m.group(1) * 3, text)
        text = text.replace('@', '')
        text = re.sub(r'\s+', ' ', text)
        return text.strip()


    async def _synthesize_tts(self, text: str, lang: str = 'ko', voice_model: str = '기본', slow: bool = False, use_rvc: bool = False, rvc_model: str = None, use_supertonic: bool = False, supertonic_voice: str = 'default') -> Path:
        """설정에 맞는 엔진으로 음성 파일을 만듭니다. (Supertonic → RVC → gTTS 순으로 폴백)

        재생과 분리해 두어, 앞 메시지가 재생되는 동안 다음 메시지를 미리 합성할 수 있다.
        모든 엔진이 실패하면 예외를 던진다.
        """
        loop = asyncio.get_running_loop()

        # Supertonic 사용 (가장 빠른 TTS, 31개 언어 지원)
        if use_supertonic:
            if check_supertonic_available():
                try:
                    # 모델 자산이 없으면 라이브러리가 자동 다운로드 (첫 실행 시 ~305MB).
                    # lang 도 함께 전달해 한국어 등 다국어 합성 지원.
                    tts_file = await loop.run_in_executor(
                        None,
                        lambda: text_to_speech_supertonic(
                            text,
                            voice_preset=supertonic_voice,
                            lang=lang,
                        ),
                    )
                    logger.info(
                        f"Supertonic TTS 파일 생성 완료: voice={supertonic_voice}, lang={lang}"
                    )
                    return tts_file
                except Exception as e:
                    logger.error(f"Supertonic TTS 생성 실패: {e}, gTTS로 폴백합니다.")
            else:
                logger.warning(
                    "Supertonic 라이브러리가 설치되지 않았습니다. `pip install supertonic` 로 설치하세요. gTTS로 폴백합니다."
                )

        # RVC 사용 (TTS + RVC 결합). Supertonic 이 성공했으면 위에서 반환해 여기까지 오지 않는다
        # (예전에는 둘 다 켜져 있으면 두 번 합성하고 앞 파일을 지우지 않았다).
        if use_rvc:
            if _check_rvc_available():
                if rvc_model is None:
                    rvc_models = get_all_rvc_models()
                    if rvc_models:
                        rvc_model = list(rvc_models.keys())[0]
                    else:
                        logger.warning("등록된 RVC 모델이 없습니다. gTTS로 폴백합니다.")

                if rvc_model:
                    try:
                        tts_file = await text_to_speech_with_rvc_async(text, rvc_model, pitch=0, speed=1.0, lang=lang)
                        logger.info(f"RVC TTS 파일 생성 완료: {rvc_model}")
                        return tts_file
                    except Exception as e:
                        logger.error(f"RVC TTS 생성 실패: {e}, gTTS로 폴백합니다.")
            else:
                logger.warning("RVC를 사용할 수 없습니다. pip install tts-with-rvc-onnx 또는 pip install tts-with-rvc로 설치하세요. gTTS로 폴백합니다.")

        # Supertonic 과 RVC 를 쓰지 않거나 실패한 경우 gTTS 사용
        tld = 'com'  # 기본값
        if lang in VOICE_MODELS and voice_model in VOICE_MODELS[lang]:
            tld = VOICE_MODELS[lang][voice_model]['tld']

        # TTS 파일 생성 (동기 함수이므로 스레드에서 실행)
        return await loop.run_in_executor(
            None,
            lambda: text_to_speech(text, lang=lang, slow=slow, tld=tld)
        )

    async def play_tts(self, ctx, text: str, lang: str = 'ko', voice_model: str = '기본', slow: bool = False, use_rvc: bool = False, rvc_model: str = None, use_supertonic: bool = False, supertonic_voice: str = 'default', pre_synthesized: Path = None):
        """TTS를 재생합니다.

        pre_synthesized 가 주어지면 합성을 건너뛰고 그 파일을 재생한다. 재생을 시작하지 못하면
        (조기 반환 포함) 아래 finally 에서 파일을 정리한다.
        """
        tts_file = pre_synthesized
        playback_started = False
        try:
            # 텍스트 유효성 검사
            if not text or not text.strip():
                logger.warning("빈 텍스트는 TTS로 변환할 수 없습니다.")
                return False
            
            # 텍스트 길이 검사
            text = text.strip()
            if len(text) > 200:
                logger.warning(f"텍스트가 너무 깁니다 ({len(text)}자). 최대 200자까지 지원됩니다.")
                return False
            
            # voice_client 확인
            if not ctx.voice_client:
                logger.error("voice_client가 없습니다. 음성 채널에 연결되어 있지 않습니다.")
                return False
            
            if tts_file is None:
                tts_file = await self._synthesize_tts(
                    text, lang, voice_model, slow, use_rvc, rvc_model, use_supertonic, supertonic_voice
                )
            
            # FFmpeg / 파일 검증 — 음악 pause 전에 수행해 실패 시 음악이 멈춘 채 남지 않게 한다.
            ffmpeg_executable = FFMPEG_PATH if FFMPEG_PATH and Path(FFMPEG_PATH).exists() else None
            if not ffmpeg_executable:
                import shutil
                ffmpeg_executable = shutil.which('ffmpeg')

            if not ffmpeg_executable:
                logger.error("FFmpeg를 찾을 수 없습니다. FFMPEG_PATH를 설정하거나 시스템 PATH에 ffmpeg를 추가해주세요.")
                cleanup_tts_file(tts_file)
                return False

            if not tts_file.exists():
                logger.error(f"TTS 파일이 존재하지 않습니다: {tts_file}")
                cleanup_tts_file(tts_file)
                return False

            file_size = tts_file.stat().st_size
            if file_size == 0:
                logger.error(f"TTS 파일이 비어있습니다: {tts_file} (크기: {file_size} bytes)")
                cleanup_tts_file(tts_file)
                return False

            logger.debug(f"TTS 파일 재생 준비: {tts_file} (크기: {file_size} bytes)")

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(str(tts_file), executable=ffmpeg_executable, options='-vn'),
                volume=1.0,
            )

            def after_play(error):
                """재생 완료 후 콜백 (믹서가 오디오 스레드에서 부를 수 있다)"""
                if error:
                    logger.error(f"TTS 재생 오류: {error}")
                else:
                    logger.debug(f"TTS 재생 완료: {tts_file}")
                asyncio.run_coroutine_threadsafe(self._cleanup_after_play(tts_file, ctx), self.bot.loop)

            # 음악이 재생 중이면 음악을 멈추지 않고 그 위에 겹쳐 읽는다 (음악은 잠시 작아진다).
            # 음악이 일시정지 상태면 음악은 그 자리에 둔 채 TTS 만 나오고, 다 읽으면 다시 멈춘다.
            logger.info(f"TTS 재생 시작: {tts_file.name}")
            await play_overlay(ctx.voice_client, source, after=after_play)
            playback_started = True
            return True
        except Exception as e:
            logger.error(f"TTS 재생 오류: {e}", exc_info=True)
            return False
        finally:
            if tts_file and not playback_started:
                cleanup_tts_file(tts_file)

    async def _after_tts_finished(self, ctx):
        """TTS 한 건이 끝난 뒤: 대기열이 남아 있으면 이어서 읽고, 다 읽었으면 음악 홀드를 푼다."""
        guild_id = ctx.guild.id
        self.playing[guild_id] = False

        if self.tts_queue.get(guild_id):
            await self._process_queue(ctx.guild)
            if self.playing.get(guild_id):
                return

        # 일시정지 상태였던 음악 위에 TTS 를 읽었다면 다시 일시정지 상태로 되돌린다.
        try:
            release_held_main(ctx.guild.voice_client)
        except Exception as e:
            logger.debug(f"음악 홀드 해제 중 오류 (무시됨): {e}")

    async def delete_command_message(self, ctx):
        """명령어 메시지를 삭제합니다."""
        await asyncio.sleep(COMMAND_MESSAGE_DELETE_DELAY)
        try:
            if self.bot.is_closed():
                return
            await ctx.message.delete()
        except (discord.NotFound, discord.HTTPException, discord.Forbidden, RuntimeError, AttributeError):
            pass

    async def _safe_ctx_send(self, ctx, content: str, delete_after: float = None):
        """종료 상태를 고려해 안전하게 메시지를 전송합니다."""
        try:
            if self.bot.is_closed():
                return
            await ctx.send(content, delete_after=delete_after)
        except (discord.NotFound, discord.HTTPException, discord.Forbidden, RuntimeError, AttributeError):
            pass

    async def _connect_with_retry(self, channel, guild, max_attempts: int = 3):
        """음성 채널 연결을 재시도하며 강제 재연결을 수행합니다."""
        delays = [1.0, 3.0, 5.0]
        last_error = None

        for attempt in range(max_attempts):
            if self.bot.is_closed():
                return False
            try:
                voice_client = guild.voice_client
                if voice_client:
                    if voice_client.is_connected():
                        if voice_client.channel and voice_client.channel.id == channel.id:
                            return True
                        await voice_client.move_to(channel)
                        return True
                    # 연결 객체가 남아있지만 비정상 상태면 강제 해제
                    try:
                        await voice_client.disconnect(force=True)
                    except Exception:
                        pass
                    # 끊긴 세션이 남아있는 경우를 대비해 음성 상태를 초기화
                    try:
                        await guild.change_voice_state(channel=None)
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)

                await channel.connect(reconnect=True, timeout=20.0)
                return True
            except Exception as e:
                last_error = e
                # 종료 진행 중이면 추가 재시도하지 않음
                if isinstance(e, RuntimeError) and "Session is closed" in str(e):
                    logger.warning("세션이 닫혀 음성 연결 재시도를 중단합니다.")
                    return False
                if attempt < max_attempts - 1:
                    await asyncio.sleep(delays[min(attempt, len(delays) - 1)])

        logger.error(f"음성 채널 연결 재시도 실패: {last_error}")
        return False

    async def ensure_voice_client(self, ctx):
        """음성 채널에 연결되어 있는지 확인하고, 없으면 연결합니다."""
        if self.bot.is_closed():
            return False

        if not getattr(ctx.author, "voice", None) or not ctx.author.voice.channel:
            await self._safe_ctx_send(ctx, "선생님, 음성 채널에 먼저 입장해주세요! 아리스가 어디로 가야 할지 모르겠어요~", delete_after=10)
            await self.delete_command_message(ctx)
            return False

        async with self._get_voice_connect_lock(ctx.guild.id):
            if ctx.voice_client and ctx.voice_client.is_connected():
                # 다른 채널에 있으면 호출자 채널로 이동
                if ctx.voice_client.channel and ctx.voice_client.channel.id != ctx.author.voice.channel.id:
                    try:
                        await ctx.voice_client.move_to(ctx.author.voice.channel)
                    except Exception as e:
                        logger.error(f"음성 채널 이동 실패: {e}")
                        return False
                return True

            connected = await self._connect_with_retry(ctx.author.voice.channel, ctx.guild)
            if connected:
                return True

            await self._safe_ctx_send(ctx, "선생님, 음성 채널 연결에 실패했어요. 잠시 후 다시 시도해주세요!", delete_after=10)
            await self.delete_command_message(ctx)
            return False

    async def _cleanup_after_play(self, file_path: Path, ctx):
        """재생 후 파일 정리 및 대기열 처리"""
        # 파일은 잠시 뒤에 지우고(FFmpeg 가 파일을 놓을 여유), 다음 메시지는 바로 이어서 읽는다.
        # (예전에는 여기서 1초를 기다려 메시지 사이마다 1초씩 공백이 생겼다)
        asyncio.get_running_loop().call_later(TTS_FILE_DELETE_DELAY, cleanup_tts_file, file_path)
        await self._after_tts_finished(ctx)

    @staticmethod
    def _synthesis_args(text: str, settings: dict) -> tuple:
        """대기열 항목 설정을 _synthesize_tts / play_tts 인자 순서로 변환합니다."""
        return (
            text,
            settings.get('lang', 'ko'),
            settings.get('voice_model', '기본'),
            settings.get('slow', False),
            settings.get('use_rvc', False),
            settings.get('rvc_model', None),
            settings.get('use_supertonic', False),
            settings.get('supertonic_voice', 'default'),
        )

    def _start_presynthesis(self, guild_id: int) -> None:
        """재생 중에 대기열 맨 앞 메시지를 미리 합성해 둡니다. (메시지 사이 공백 제거)"""
        queue = self.tts_queue.get(guild_id)
        if not queue:
            return
        item = queue[0]
        current = self._presynth.get(guild_id)
        if current is not None and current[0] is item:
            return
        self._discard_presynth(guild_id)

        _, text, settings = item
        text = (text or '').strip()
        if not text or len(text) > 200:
            return
        task = asyncio.create_task(self._synthesize_tts(*self._synthesis_args(text, settings)))
        self._presynth[guild_id] = (item, task)

    def _discard_presynth(self, guild_id: int) -> None:
        """쓰지 않게 된 미리 합성 결과를 버리고, 만들어진(또는 만들어질) 파일을 지웁니다."""
        entry = self._presynth.pop(guild_id, None)
        if entry is None:
            return
        task = entry[1]

        def _cleanup(t):
            # 합성은 스레드에서 끝까지 진행되므로 취소 대신 완료 후 파일을 정리한다.
            if not t.cancelled() and t.exception() is None and t.result():
                cleanup_tts_file(t.result())

        if task.done():
            _cleanup(task)
        else:
            task.add_done_callback(_cleanup)

    async def _take_presynth(self, guild_id: int, item) -> Optional[Path]:
        """이 대기열 항목을 미리 합성해 둔 파일이 있으면 반환합니다. 실패했으면 None."""
        entry = self._presynth.get(guild_id)
        if entry is None or entry[0] is not item:
            return None
        self._presynth.pop(guild_id, None)
        try:
            return await entry[1]
        except Exception as e:
            # 재생 시점에 폴백을 포함해 다시 합성한다.
            logger.debug(f"미리 합성 실패, 다시 합성합니다: {e}")
            return None

    async def _process_queue(self, guild):
        """TTS 대기열을 처리합니다."""
        lock = self._get_playback_lock(guild.id)
        async with lock:
            if self.playing.get(guild.id, False):
                return

            if not guild.voice_client:
                self.tts_queue[guild.id] = []
                self.playing[guild.id] = False
                self._discard_presynth(guild.id)
                return

            if guild.id not in self.tts_queue or not self.tts_queue[guild.id]:
                self.playing[guild.id] = False
                return

            while self.tts_queue[guild.id]:
                item = self.tts_queue[guild.id].pop(0)
                user_id, text, settings = item

                if not text or not text.strip() or len(text.strip()) > 200:
                    logger.debug(f"유효하지 않은 텍스트 건너뛰기: '{text[:50]}...'")
                    continue

                self.playing[guild.id] = True

                class TempContext:
                    def __init__(self, guild, voice_client, bot):
                        self.guild = guild
                        self.voice_client = voice_client
                        self.bot = bot

                temp_ctx = TempContext(guild, guild.voice_client, self.bot)
                pre_synthesized = await self._take_presynth(guild.id, item)
                success = await self.play_tts(
                    temp_ctx,
                    *self._synthesis_args(text.strip(), settings),
                    pre_synthesized=pre_synthesized,
                )

                if success:
                    # 이 메시지가 재생되는 동안 다음 메시지를 미리 합성한다.
                    self._start_presynthesis(guild.id)
                    return

                self.playing[guild.id] = False
                logger.warning("TTS 재생 실패, 대기열의 다음 항목 처리 시도")

            self.playing[guild.id] = False

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        """봇이 길드에서 나가면 그 길드용 상태를 정리합니다 (길드별 딕셔너리 누적 방지)."""
        self._discard_presynth(guild.id)
        for store in (
            self._playback_locks, self._voice_connect_locks,
            self.tts_queue, self.playing, self.active_users,
        ):
            store.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_message(self, message):
        """메시지를 받으면 TTS 자동 읽기 모드가 활성화된 사용자의 메시지를 읽습니다."""
        if self.bot.is_closed():
            return

        # 봇 메시지 무시
        if message.author.bot:
            return
        
        # 명령어 메시지 무시
        if message.content.startswith('!'):
            return
        
        # DM 무시
        if not message.guild:
            return
        
        # 사용자가 음성 채널에 없으면 무시
        if not message.author.voice:
            return
        
        # TTS 자동 읽기가 활성화되어 있는지 확인
        if not self.is_user_active(message.author.id, message.guild.id):
            return
        
        # 멘션은 닉네임으로 표시되는 clean_content를 사용하고, URL/이모지/반복 문자를 정리
        text = self.preprocess_tts_text(message.clean_content)
        if not text or len(text) > 200:
            return
        
        # 음성 채널에 연결되어 있는지 확인
        if not message.guild.voice_client:
            if message.author.voice:
                try:
                    async with self._get_voice_connect_lock(message.guild.id):
                        connected = await self._connect_with_retry(message.author.voice.channel, message.guild)
                    if not connected:
                        logger.error("음성 채널 연결 재시도 실패")
                        return
                    # 연결 후 재확인
                    if not message.guild.voice_client:
                        logger.error("음성 채널 연결 후에도 voice_client가 없습니다.")
                        return
                except Exception as e:
                    logger.error(f"음성 채널 연결 실패: {e}")
                    return
            else:
                return
        
        # 대기열에 추가
        settings = self.get_user_settings(message.author.id, message.guild.id)
        await self.enqueue_tts(message.guild, message.author.id, text, settings)

    @commands.command(name='tts', aliases=['말하기', '읽기', '읽어줘'])
    async def tts_command(self, ctx, *, text: str = None):
        """
        TTS 자동 읽기 모드를 토글하거나 텍스트를 읽어줍니다.
        인자 없이 사용하면 자동 읽기 모드를 켜고 끕니다.
        텍스트를 입력하면 해당 텍스트를 읽어줍니다.
        """
        if not await self.ensure_voice_client(ctx):
            return

        # 텍스트가 없으면 자동 읽기 모드 토글
        if text is None:
            current_settings = self.get_user_settings(ctx.author.id, ctx.guild.id)
            new_state = not current_settings.get('enabled', False)
            
            await self.set_user_settings(
                ctx.author.id,
                ctx.guild.id,
                enabled=new_state
            )
            
            if new_state:
                # 명령어 실행자에게만 보이도록 매우 짧게 표시
                await ctx.send(f"{ctx.author.mention}, 이제부터 선생님의 채팅을 자동으로 읽어드릴게요! `!tts목소리` 명령어로 목소리를 바꿀 수 있어요~", delete_after=2)
            else:
                # 명령어 실행자에게만 보이도록 매우 짧게 표시
                await ctx.send(f"{ctx.author.mention}, TTS 자동 읽기 모드를 껐어요. 다시 켜려면 `!tts`를 입력해주세요!", delete_after=2)
            
            await self.delete_command_message(ctx)
            return

        # 텍스트가 있으면 즉시 읽기 (URL/이모지/반복 문자 정리)
        text = self.preprocess_tts_text(text)
        if not text:
            await ctx.send("선생님, 읽을 수 있는 내용이 없어요!", delete_after=10)
            await self.delete_command_message(ctx)
            return
        if len(text) > 200:
            await ctx.send("선생님, 텍스트가 너무 길어요! 200자 이하로 입력해주세요~", delete_after=12)
            await self.delete_command_message(ctx)
            return

        async with safe_typing(ctx):
            settings = self.get_user_settings(ctx.author.id, ctx.guild.id)
            queued = await self.enqueue_tts(ctx.guild, ctx.author.id, text, settings)

            if queued:
                await ctx.send(f"선생님, '{text[:50]}{'...' if len(text) > 50 else ''}'을(를) 읽어드릴게요!", delete_after=10)
            else:
                await ctx.send("선생님, TTS 대기열이 가득 찼거나 생성 중 오류가 발생했어요!", delete_after=12)
            
            await self.delete_command_message(ctx)

    @commands.command(name='tts목소리', aliases=['ttsvoice', 'tts모델', '목소리변경'])
    async def tts_voice_command(self, ctx):
        """TTS 목소리 모델을 변경합니다. (gTTS 또는 RVC 선택 가능)"""
        settings = self.get_user_settings(ctx.author.id, ctx.guild.id)
        current_lang = settings.get('lang', 'ko')
        current_model = settings.get('voice_model', '기본')
        use_rvc = settings.get('use_rvc', False)
        use_supertonic = settings.get('use_supertonic', False)
        supertonic_voice = settings.get('supertonic_voice', 'default')
        rvc_model = settings.get('rvc_model', None)
        
        view = discord.ui.View()
        
        # RVC 사용 가능 여부 확인
        rvc_available = _check_rvc_available()
        supertonic_available = check_supertonic_available()
        
        # TTS 엔진 선택 (gTTS, Supertonic, 또는 RVC)
        engine_options = [
            discord.SelectOption(
                label="gTTS (Google TTS)",
                value="gtts",
                description="빠르고 간단한 TTS, 다양한 언어 지원",
                default=not use_rvc and not use_supertonic
            )
        ]
        
        # Supertonic 옵션 추가 (사용 가능한 경우만)
        if supertonic_available:
            engine_options.append(
                discord.SelectOption(
                    label="Supertonic (⚡ 초고속 TTS)",
                    value="supertonic",
                    description="온디바이스 TTS, 한국어 포함 31개 언어 지원",
                    default=use_supertonic
                )
            )
        
        # RVC 옵션 추가 (사용 가능한 경우만)
        if rvc_available:
            engine_options.append(
                discord.SelectOption(
                    label="RVC (TTS + RVC)",
                    value="rvc",
                    description="텍스트를 원하는 목소리로 변환",
                    default=use_rvc and not use_supertonic
                )
            )
        
        if use_supertonic:
            current_engine = "Supertonic"
        elif use_rvc:
            current_engine = "RVC"
        else:
            current_engine = "gTTS"
        
        engine_select = discord.ui.Select(
            placeholder=f"TTS 엔진 선택 (현재: {current_engine})",
            options=engine_options
        )
        
        async def engine_select_callback(interaction):
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                return
            
            selected_engine = engine_select.values[0]
            new_use_rvc = (selected_engine == 'rvc')
            new_use_supertonic = (selected_engine == 'supertonic')
            
            await self.set_user_settings(
                ctx.author.id,
                ctx.guild.id,
                use_rvc=new_use_rvc,
                use_supertonic=new_use_supertonic
            )
            
            # 설정 업데이트 후 즉시 모델 선택 UI 표시
            updated_settings = self.get_user_settings(ctx.author.id, ctx.guild.id)
            updated_view = discord.ui.View()
            
            if new_use_rvc:
                # RVC 모델 선택 UI 표시
                rvc_models = get_all_rvc_models()
                
                if not rvc_models:
                    await interaction.response.send_message(
                        "선생님, RVC로 변경했어요! 하지만 등록된 RVC 모델이 없어요! `!ttsrvc자동등록`으로 모델을 등록해주세요!",
                        ephemeral=True,
                        delete_after=10
                    )
                    return
                
                rvc_model_options = [
                    discord.SelectOption(
                        label=f"🎤 {model_info['name']}",
                        value=model_name,
                        description=f"RVC - {model_info.get('lang', 'ja').upper()}",
                        default=(model_name == updated_settings.get('rvc_model'))
                    )
                    for model_name, model_info in rvc_models.items()
                ]
                
                rvc_model_select = discord.ui.Select(
                    placeholder=f"RVC 모델 선택 (현재: {updated_settings.get('rvc_model', '없음')})",
                    options=rvc_model_options[:25]
                )
                
                async def rvc_model_callback_inner(inner_interaction):
                    if inner_interaction.user.id != ctx.author.id:
                        await inner_interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                        return
                    
                    selected_model = rvc_model_select.values[0]
                    await self.set_user_settings(
                        ctx.author.id,
                        ctx.guild.id,
                        rvc_model=selected_model
                    )
                    
                    model_name = rvc_models[selected_model]['name']
                    await inner_interaction.response.send_message(
                        f"선생님, RVC 모델을 '{model_name}'으로 변경했어요!",
                        ephemeral=True,
                        delete_after=5
                    )
                
                rvc_model_select.callback = rvc_model_callback_inner
                updated_view.add_item(rvc_model_select)
                
                await interaction.response.send_message(
                    "선생님, RVC로 변경했어요! 아래에서 RVC 모델을 선택해주세요!",
                    view=updated_view,
                    ephemeral=True,
                    delete_after=30
                )
            elif new_use_supertonic:
                # Supertonic 음성 프리셋 선택 UI (M1~M5=남성, F1~F5=여성, 한국어 포함 31개 언어 지원)
                supertonic_voices = get_available_voice_presets()
                current_supertonic_voice = updated_settings.get('supertonic_voice', DEFAULT_VOICE_PRESET)
                
                voice_options = [
                    discord.SelectOption(
                        label=f"🎤 {voice}",
                        value=voice,
                        description=("남성 음성" if voice.startswith('M') else "여성 음성") + f" - Supertonic {voice}",
                        default=(voice == current_supertonic_voice)
                    )
                    for voice in supertonic_voices
                ]
                
                voice_select = discord.ui.Select(
                    placeholder=f"Supertonic 음성 선택 (현재: {current_supertonic_voice})",
                    options=voice_options
                )
                
                async def supertonic_voice_callback_inner(inner_interaction):
                    if inner_interaction.user.id != ctx.author.id:
                        await inner_interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                        return
                    
                    selected_voice = voice_select.values[0]
                    await self.set_user_settings(
                        ctx.author.id,
                        ctx.guild.id,
                        supertonic_voice=selected_voice
                    )
                    
                    await inner_interaction.response.send_message(
                        f"선생님, Supertonic 음성을 '{selected_voice}'로 변경했어요!",
                        ephemeral=True,
                        delete_after=5
                    )
                
                voice_select.callback = supertonic_voice_callback_inner
                updated_view.add_item(voice_select)
                
                await interaction.response.send_message(
                    "선생님, Supertonic으로 변경했어요! 아래에서 음성 프리셋을 선택해주세요! "
                    "(M1~M5 남성, F1~F5 여성, 한국어 포함 31개 언어 지원)",
                    view=updated_view,
                    ephemeral=True,
                    delete_after=30
                )
            else:
                # gTTS 모델 선택 UI 표시
                current_lang = updated_settings.get('lang', 'ko')
                current_model = updated_settings.get('voice_model', '기본')
                
                if current_lang not in VOICE_MODELS:
                    await interaction.response.send_message(
                        f"선생님, gTTS로 변경했어요! 하지만 '{current_lang}' 언어는 지원되지 않아요!",
                        ephemeral=True,
                        delete_after=10
                    )
                    return
                
                available_models = VOICE_MODELS[current_lang]
                
                model_select = discord.ui.Select(
                    placeholder=f"목소리 모델 선택 (현재: {current_model})",
                    options=[
                        discord.SelectOption(
                            label=model_info['name'],
                            value=model_name,
                            description=f"gTTS - {model_info['name']}",
                            default=(model_name == current_model)
                        )
                        for model_name, model_info in available_models.items()
                    ]
                )
                
                async def model_callback_inner(inner_interaction):
                    if inner_interaction.user.id != ctx.author.id:
                        await inner_interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                        return
                    
                    selected_model = model_select.values[0]
                    await self.set_user_settings(
                        ctx.author.id,
                        ctx.guild.id,
                        voice_model=selected_model
                    )
                    
                    model_name = available_models[selected_model]['name']
                    await inner_interaction.response.send_message(
                        f"선생님, 목소리 모델을 '{model_name}'으로 변경했어요!",
                        ephemeral=True,
                        delete_after=5
                    )
                
                model_select.callback = model_callback_inner
                updated_view.add_item(model_select)
                
                await interaction.response.send_message(
                    "선생님, gTTS로 변경했어요! 아래에서 목소리 모델을 선택해주세요!",
                    view=updated_view,
                    ephemeral=True,
                    delete_after=30
                )
        
        engine_select.callback = engine_select_callback
        view.add_item(engine_select)
        
        # Supertonic을 사용하는 경우
        if use_supertonic and supertonic_available:
            # Supertonic 음성 프리셋 선택 (M1~M5 남성, F1~F5 여성)
            supertonic_voices = get_available_voice_presets()
            
            voice_options = [
                discord.SelectOption(
                    label=f"⚡ {voice}",
                    value=voice,
                    description=("남성 음성" if voice.startswith('M') else "여성 음성") + f" - Supertonic {voice}",
                    default=(voice == supertonic_voice)
                )
                for voice in supertonic_voices
            ]
            
            voice_select = discord.ui.Select(
                placeholder=f"Supertonic 음성 선택 (현재: {supertonic_voice})",
                options=voice_options
            )
            
            async def supertonic_voice_callback(interaction):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                    return
                
                selected_voice = voice_select.values[0]
                await self.set_user_settings(
                    ctx.author.id,
                    ctx.guild.id,
                    supertonic_voice=selected_voice
                )
                
                await interaction.response.send_message(
                    f"선생님, Supertonic 음성을 '{selected_voice}'로 변경했어요!",
                    ephemeral=True,
                    delete_after=5
                )
            
            voice_select.callback = supertonic_voice_callback
            view.add_item(voice_select)
        
        # RVC를 사용하는 경우
        if use_rvc and rvc_available:
            rvc_models = get_all_rvc_models()
            
            if not rvc_models:
                await ctx.send("선생님, 등록된 RVC 모델이 없어요! `!ttsrvc자동등록`으로 모델을 등록해주세요!", delete_after=15)
                await self.delete_command_message(ctx)
                return
            
            # RVC 모델 선택
            rvc_model_options = [
                discord.SelectOption(
                    label=f"🎤 {model_info['name']}",
                    value=model_name,
                    description=f"RVC - {model_info.get('lang', 'ja').upper()}",
                    default=(model_name == (rvc_model or list(rvc_models.keys())[0]))
                )
                for model_name, model_info in rvc_models.items()
            ]
            
            rvc_model_select = discord.ui.Select(
                placeholder=f"RVC 모델 선택 (현재: {rvc_model or '기본'})",
                options=rvc_model_options[:25]  # Discord는 최대 25개 옵션
            )
            
            async def rvc_model_callback(interaction):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                    return
                
                selected_model = rvc_model_select.values[0]
                await self.set_user_settings(
                    ctx.author.id,
                    ctx.guild.id,
                    rvc_model=selected_model
                )
                
                model_name = rvc_models[selected_model]['name']
                await interaction.response.send_message(
                    f"선생님, RVC 모델을 '{model_name}'으로 변경했어요!",
                    ephemeral=True,
                    delete_after=5
                )
            
            rvc_model_select.callback = rvc_model_callback
            view.add_item(rvc_model_select)
        
        # gTTS를 사용하는 경우
        if not use_rvc and not use_supertonic:
            if current_lang not in VOICE_MODELS:
                await ctx.send(f"선생님, '{current_lang}' 언어는 지원되지 않아요!", delete_after=12)
                await self.delete_command_message(ctx)
                return
            
            available_models = VOICE_MODELS[current_lang]
            
            # gTTS 모델 선택
            model_select = discord.ui.Select(
                placeholder=f"목소리 모델 선택 (현재: {current_model})",
                options=[
                    discord.SelectOption(
                        label=model_info['name'],
                        value=model_name,
                        description=f"gTTS - {model_info['name']}",
                        default=(model_name == current_model)
                    )
                    for model_name, model_info in available_models.items()
                ]
            )
            
            async def gtts_model_callback(interaction):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                    return
                
                selected_model = model_select.values[0]
                await self.set_user_settings(
                    ctx.author.id,
                    ctx.guild.id,
                    voice_model=selected_model
                )
                
                model_name = available_models[selected_model]['name']
                await interaction.response.send_message(
                    f"선생님, 목소리 모델을 '{model_name}'으로 변경했어요!",
                    ephemeral=True,
                    delete_after=5
                )
            
            model_select.callback = gtts_model_callback
            view.add_item(model_select)
            
            # 언어 선택 (gTTS 지원 언어)
            lang_select = discord.ui.Select(
                placeholder=f"언어 선택 (현재: {current_lang})",
                options=[
                    discord.SelectOption(
                        label=lang.upper(),
                        value=lang,
                        description=f"{lang} 언어로 변경",
                        default=(lang == current_lang)
                    )
                    for lang in VOICE_MODELS.keys()
                ]
            )
            
            async def gtts_lang_callback(interaction):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                    return
                
                selected_lang = gtts_lang_select.values[0]
                # 언어 변경 시 기본 모델로 설정
                if selected_lang in VOICE_MODELS:
                    default_model = list(VOICE_MODELS[selected_lang].keys())[0]
                    await self.set_user_settings(
                        ctx.author.id,
                        ctx.guild.id,
                        lang=selected_lang,
                        voice_model=default_model
                    )
                    
                    await interaction.response.send_message(
                        f"선생님, 언어를 '{selected_lang.upper()}'로 변경하고 기본 목소리 모델로 설정했어요!",
                        ephemeral=True,
                        delete_after=5
                    )
            
            gtts_lang_select = lang_select
            gtts_lang_select.callback = gtts_lang_callback
            view.add_item(gtts_lang_select)
        
        await ctx.send("선생님, TTS 엔진과 목소리 모델을 선택해주세요!", view=view, delete_after=60)
        await self.delete_command_message(ctx)

    @commands.command(name='tts느리게', aliases=['ttsslow'])
    async def tts_slow_toggle(self, ctx):
        """TTS 느린 속도 모드를 토글합니다."""
        settings = self.get_user_settings(ctx.author.id, ctx.guild.id)
        new_slow = not settings.get('slow', False)
        
        await self.set_user_settings(
            ctx.author.id,
            ctx.guild.id,
            slow=new_slow
        )
        
        if new_slow:
            await ctx.send("선생님, 이제부터 느리게 읽어드릴게요!", delete_after=10)
        else:
            await ctx.send("선생님, 이제부터 일반 속도로 읽어드릴게요!", delete_after=10)
        
        await self.delete_command_message(ctx)

    @commands.command(name='tts설정', aliases=['ttssettings'])
    async def tts_settings_command(self, ctx):
        """현재 TTS 설정을 확인합니다."""
        settings = self.get_user_settings(ctx.author.id, ctx.guild.id)
        
        lang = settings.get('lang', 'ko')
        voice_model = settings.get('voice_model', '기본')
        enabled = settings.get('enabled', False)
        slow = settings.get('slow', False)
        use_rvc = settings.get('use_rvc', False)
        use_supertonic = settings.get('use_supertonic', False)
        supertonic_voice = settings.get('supertonic_voice', 'default')
        rvc_model = settings.get('rvc_model', None)
        
        # 모델 이름 가져오기
        if use_supertonic:
            model_name = f"Supertonic ({supertonic_voice})"
            engine_name = "Supertonic ⚡"
        elif use_rvc:
            rvc_info = get_rvc_model(rvc_model) if rvc_model else None
            if rvc_info:
                model_name = rvc_info.get('name', rvc_model)
                engine_name = "TTS + RVC"
            else:
                model_name = rvc_model or "기본"
                engine_name = "TTS + RVC"
        else:
            model_name = VOICE_MODELS.get(lang, {}).get(voice_model, {}).get('name', voice_model)
            engine_name = "gTTS"
        
        embed = discord.Embed(
            title="아리스의 TTS 설정",
            description=f"선생님의 현재 TTS 설정이에요!",
            color=0x3498db
        )
        embed.add_field(name="자동 읽기", value="켜짐 ✅" if enabled else "꺼짐 ❌", inline=True)
        embed.add_field(name="TTS 엔진", value=engine_name, inline=True)
        embed.add_field(name="언어", value=lang.upper(), inline=True)
        embed.add_field(name="목소리 모델", value=model_name, inline=True)
        embed.add_field(name="느린 속도", value="켜짐 ✅" if slow else "꺼짐 ❌", inline=True)
        if use_supertonic:
            embed.add_field(name="Supertonic 음성", value=f"⚡ {supertonic_voice}", inline=True)
        if use_rvc:
            rvc_info = get_rvc_model(rvc_model) if rvc_model else None
            embed.add_field(name="RVC 모델", value=f"🎤 {rvc_info.get('name', rvc_model) if rvc_info else rvc_model}", inline=True)
        
        await ctx.send(embed=embed, delete_after=30)
        await self.delete_command_message(ctx)


    @commands.command(name='ttsrvc모델추가', aliases=['ttsrvcadd', 'rvc모델추가'])
    @commands.is_owner()
    async def tts_add_rvc_model(self, ctx, model_name: str, model_path: str, index_path: str = None, *, display_name: str = None):
        """
        RVC 모델을 추가합니다.
        
        사용법:
        !ttsrvc모델추가 <모델이름> <모델경로> [인덱스경로] [표시이름]
        
        예시:
        !ttsrvc모델추가 chisa_jp models/ChisaJP_e440_s12320_RVCv2_RMVPE/ChisaJP_e440_s12320.pth models/ChisaJP_e440_s12320_RVCv2_RMVPE/added_IVF736_Flat_nprobe_1_ChisaJP_v2.index "치사 JP"
        """
        if not _check_rvc_available():
            await ctx.send("선생님, RVC가 설치되지 않았어요! `pip install tts-with-rvc-onnx` 또는 `pip install tts-with-rvc`로 설치해주세요.", delete_after=15)
            await self.delete_command_message(ctx)
            return

        # 모델 이름 중복 확인
        existing = get_rvc_model(model_name)
        if existing:
            await ctx.send(f"선생님, '{model_name}' 모델이 이미 존재해요! 다른 이름을 사용해주세요.", delete_after=12)
            await self.delete_command_message(ctx)
            return
        
        # 언어 자동 감지 시도
        lang = 'ja'  # 기본값 (치사 JP는 일본어)
        if 'ko' in model_name.lower() or 'korean' in model_name.lower():
            lang = 'ko'
        elif 'en' in model_name.lower() or 'english' in model_name.lower():
            lang = 'en'
        
        try:
            add_rvc_model(
                model_name=model_name,
                model_path=model_path,
                index_path=index_path,
                display_name=display_name or model_name,
                lang=lang
            )
            
            embed = discord.Embed(
                title="RVC 모델 추가 완료",
                description=f"모델이 성공적으로 추가되었어요!",
                color=0x2ecc71
            )
            embed.add_field(name="모델 이름", value=model_name, inline=True)
            embed.add_field(name="모델 경로", value=model_path[:50] + ("..." if len(model_path) > 50 else ""), inline=False)
            if index_path:
                embed.add_field(name="인덱스 경로", value=index_path[:50] + ("..." if len(index_path) > 50 else ""), inline=False)
            embed.add_field(name="표시 이름", value=display_name or model_name, inline=True)
            embed.add_field(name="언어", value=lang.upper(), inline=True)
            embed.add_field(
                name="사용 방법",
                value=f"`!ttsrvc사용 {model_name}` 명령어로 RVC 모델을 활성화할 수 있어요!",
                inline=False
            )
            
            await ctx.send(embed=embed, delete_after=30)
        except Exception as e:
            logger.error(f"RVC 모델 추가 실패: {e}", exc_info=True)
            await ctx.send(f"선생님, 모델 추가 중 오류가 발생했어요: {str(e)[:100]}", delete_after=15)
        
        await self.delete_command_message(ctx)

    @commands.command(name='ttsrvc모델삭제', aliases=['ttsrvcremove', 'rvc모델삭제'])
    @commands.is_owner()
    async def tts_remove_rvc_model(self, ctx, model_name: str):
        """RVC 모델을 삭제합니다."""
        existing = get_rvc_model(model_name)
        if not existing:
            await ctx.send(f"선생님, '{model_name}' 모델을 찾을 수 없어요!", delete_after=12)
            await self.delete_command_message(ctx)
            return
        
        try:
            remove_rvc_model(model_name)
            await ctx.send(f"선생님, '{model_name}' 모델을 삭제했어요!", delete_after=12)
        except Exception as e:
            logger.error(f"RVC 모델 삭제 실패: {e}", exc_info=True)
            await ctx.send(f"선생님, 모델 삭제 중 오류가 발생했어요: {str(e)[:100]}", delete_after=15)
        
        await self.delete_command_message(ctx)

    @commands.command(name='ttsrvc모델목록', aliases=['ttsrvclist', 'rvc모델목록'])
    async def tts_list_rvc_models(self, ctx):
        """등록된 RVC 모델 목록을 보여줍니다."""
        rvc_models = get_all_rvc_models()
        
        if not rvc_models:
            await ctx.send("선생님, 등록된 RVC 모델이 없어요! `!ttsrvc모델추가`로 모델을 추가해주세요.", delete_after=15)
            await self.delete_command_message(ctx)
            return
        
        embed = discord.Embed(
            title="RVC 모델 목록",
            description=f"총 {len(rvc_models)}개의 RVC 모델이 등록되어 있어요!",
            color=0x9b59b6
        )
        
        for model_name, model_info in rvc_models.items():
            model_path = model_info.get('model_path', 'N/A')
            index_path = model_info.get('index_path')
            display_name = model_info.get('name', model_name)
            lang = model_info.get('lang', 'ja')
            
            value = f"**모델 경로:** `{model_path[:60]}{'...' if len(model_path) > 60 else ''}`\n"
            if index_path:
                value += f"**인덱스 경로:** `{index_path[:60]}{'...' if len(index_path) > 60 else ''}`\n"
            value += f"**언어:** {lang.upper()}\n"
            
            embed.add_field(
                name=f"🎤 {display_name} ({model_name})",
                value=value,
                inline=False
            )
        
        embed.set_footer(text="`!ttsrvc사용 <모델이름>`로 모델을 사용할 수 있어요.")
        
        await ctx.send(embed=embed, delete_after=60)
        await self.delete_command_message(ctx)

    @commands.command(name='ttsrvc사용', aliases=['ttsrvcuse', 'rvc사용'])
    async def tts_use_rvc(self, ctx, model_name: str = None):
        """RVC 모델을 사용하도록 설정합니다."""
        if not _check_rvc_available():
            await ctx.send("선생님, RVC가 설치되지 않았어요! `pip install tts-with-rvc-onnx` 또는 `pip install tts-with-rvc`로 설치해주세요.", delete_after=15)
            await self.delete_command_message(ctx)
            return
        
        if model_name is None:
            # 현재 설정 표시
            settings = self.get_user_settings(ctx.author.id, ctx.guild.id)
            use_rvc = settings.get('use_rvc', False)
            rvc_model = settings.get('rvc_model')
            
            if use_rvc and rvc_model:
                rvc_info = get_rvc_model(rvc_model)
                if rvc_info:
                    await ctx.send(f"선생님, 현재 '{rvc_info.get('name', rvc_model)}' RVC 모델을 사용 중이에요!", delete_after=15)
                else:
                    await ctx.send(f"선생님, 현재 '{rvc_model}' RVC 모델을 사용 중이지만 모델을 찾을 수 없어요!", delete_after=15)
            else:
                await ctx.send("선생님, 현재 RVC를 사용하지 않고 있어요! `!ttsrvc사용 <모델이름>`으로 모델을 선택해주세요.", delete_after=15)
            await self.delete_command_message(ctx)
            return
        
        # 모델 확인
        rvc_model = get_rvc_model(model_name)
        if not rvc_model:
            await ctx.send(f"선생님, '{model_name}' 모델을 찾을 수 없어요! `!ttsrvc모델목록`으로 등록된 모델을 확인해주세요.", delete_after=15)
            await self.delete_command_message(ctx)
            return
        
        # RVC 사용 설정
        await self.set_user_settings(
            ctx.author.id,
            ctx.guild.id,
            use_rvc=True,
            rvc_model=model_name
        )
        
        await ctx.send(f"선생님, '{rvc_model.get('name', model_name)}' RVC 모델을 사용하도록 설정했어요! 이제 텍스트가 이 목소리로 읽어질 거예요!", delete_after=15)
        await self.delete_command_message(ctx)

    @commands.command(name='ttsrvc끄기', aliases=['ttsrvcdisable', 'rvc끄기'])
    async def tts_disable_rvc(self, ctx):
        """RVC 사용을 중지합니다."""
        await self.set_user_settings(
            ctx.author.id,
            ctx.guild.id,
            use_rvc=False,
            rvc_model=None
        )
        
        await ctx.send("선생님, RVC 사용을 중지했어요! 이제 기본 TTS를 사용할 거예요.", delete_after=15)
        await self.delete_command_message(ctx)

    @commands.command(name='ttsrvc자동등록', aliases=['ttsrvcscan', 'rvc자동등록'])
    @commands.is_owner()
    async def tts_auto_register_rvc(self, ctx):
        """models 폴더를 스캔하여 RVC 모델을 자동으로 등록합니다."""
        await ctx.send("선생님, models 폴더를 스캔하고 있어요...", delete_after=5)
        
        try:
            discovered = scan_and_register_rvc_models(auto_register=True)
            
            if not discovered:
                await ctx.send("선생님, 등록할 새로운 RVC 모델을 찾지 못했어요! models 폴더에 .pth 파일이 있는지 확인해주세요.", delete_after=15)
                await self.delete_command_message(ctx)
                return
            
            embed = discord.Embed(
                title="RVC 모델 자동 등록 완료",
                description=f"총 {len(discovered)}개의 모델이 등록되었어요!",
                color=0x2ecc71
            )
            
            for model_name, model_info in discovered.items():
                embed.add_field(
                    name=f"🎤 {model_info['display_name']}",
                    value=f"**이름:** `{model_name}`\n**언어:** {model_info['lang'].upper()}",
                    inline=True
                )
            
            embed.set_footer(text="`!ttsrvc사용 <모델이름>`으로 모델을 사용할 수 있어요.")
            
            await ctx.send(embed=embed, delete_after=30)
        except Exception as e:
            logger.error(f"RVC 모델 자동 등록 실패: {e}", exc_info=True)
            await ctx.send(f"선생님, 모델 스캔 중 오류가 발생했어요: {str(e)[:100]}", delete_after=15)
        
        await self.delete_command_message(ctx)

    # ========== Slash Commands ==========
    
    @discord.app_commands.describe(text="읽을 텍스트 (입력하지 않으면 자동 읽기 모드 토글)")
    async def slash_tts(self, interaction: discord.Interaction, text: str = None):
        """Slash Command로 TTS 자동 읽기 모드 토글 또는 텍스트 읽기"""
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.tts_command(ctx, text=text)

    async def slash_tts_voice(self, interaction: discord.Interaction):
        """Slash Command로 TTS 목소리 모델 변경"""
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.tts_voice_command(ctx)

    async def slash_tts_slow(self, interaction: discord.Interaction):
        """Slash Command로 TTS 느린 속도 모드 토글"""
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.tts_slow_toggle(ctx)

    async def slash_tts_settings(self, interaction: discord.Interaction):
        """Slash Command로 현재 TTS 설정 확인"""
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.tts_settings_command(ctx)
    
    async def cog_load(self):
        """Cog가 로드될 때 Slash Commands 등록 및 자동 모델 스캔"""
        # 이전 실행에서 남은 TTS 임시 파일 정리 (실패해도 로드를 막지 않는다)
        try:
            await asyncio.to_thread(sweep_stale_tts_files)
        except Exception as e:
            logger.debug(f"TTS 임시 파일 정리 실패 (무시됨): {e}")

        # TTS 그룹 생성
        self.tts_group = discord.app_commands.Group(name="tts", description="TTS (Text-to-Speech) 관련 명령어")
        
        # 그룹에 명령어 추가
        self.tts_group.command(name="말하기", description="TTS 자동 읽기 모드를 토글하거나 텍스트를 읽어줍니다")(self.slash_tts)
        self.tts_group.command(name="목소리", description="TTS 목소리 모델을 변경합니다")(self.slash_tts_voice)
        self.tts_group.command(name="느리게", description="TTS 느린 속도 모드를 토글합니다")(self.slash_tts_slow)
        self.tts_group.command(name="설정", description="현재 TTS 설정을 확인합니다")(self.slash_tts_settings)
        
        # 봇에 그룹 등록
        self.bot.tree.add_command(self.tts_group, override=True)
        
        # 자동으로 models 폴더 스캔하여 RVC 모델 등록
        try:
            discovered = scan_and_register_rvc_models(auto_register=True)
            if discovered:
                logger.info(f"자동 모델 스캔 완료: {len(discovered)}개 모델 발견 및 등록")
        except Exception as e:
            logger.warning(f"자동 모델 스캔 중 오류 발생: {e}")
    
    async def cog_unload(self):
        """Cog가 언로드될 때 Slash Commands 제거"""
        try:
            if hasattr(self, 'tts_group'):
                self.bot.tree.remove_command(self.tts_group.name)
        except Exception as e:
            logger.error(f"Slash Commands 제거 중 오류: {e}")
        # 미리 합성해 둔 임시 파일이 남지 않도록 정리한다.
        for guild_id in list(self._presynth):
            self._discard_presynth(guild_id)
