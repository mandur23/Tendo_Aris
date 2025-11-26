"""TTS (Text-to-Speech) 명령어 Cog"""
import asyncio
import logging
import discord
from discord.ext import commands
from pathlib import Path
from utils.tts_utils import text_to_speech, cleanup_tts_file, VOICE_MODELS
from utils.file_utils import load_tts_settings, save_tts_settings
from utils.config import COMMAND_MESSAGE_DELETE_DELAY, FFMPEG_PATH
from utils.rvc_utils import (
    _check_rvc_available, add_rvc_model, remove_rvc_model,
    get_all_rvc_models, get_rvc_model, text_to_speech_with_rvc_async,
    scan_and_register_rvc_models
)

logger = logging.getLogger(__name__)


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

    def save_settings(self):
        """TTS 설정을 저장합니다."""
        save_tts_settings(self.tts_settings)

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
                'rvc_model': None  # RVC 모델 이름
            }
        return self.tts_settings[user_key]

    def set_user_settings(self, user_id: int, guild_id: int, **kwargs):
        """사용자의 TTS 설정을 업데이트합니다."""
        user_key = f"{guild_id}_{user_id}"
        if user_key not in self.tts_settings:
            self.tts_settings[user_key] = {
                'enabled': False,
                'lang': 'ko',
                'voice_model': '기본',
                'slow': False
            }
        self.tts_settings[user_key].update(kwargs)
        self.save_settings()

    def is_user_active(self, user_id: int, guild_id: int) -> bool:
        """사용자의 TTS 자동 읽기가 활성화되어 있는지 확인합니다."""
        settings = self.get_user_settings(user_id, guild_id)
        return settings.get('enabled', False)


    async def play_tts(self, ctx, text: str, lang: str = 'ko', voice_model: str = '기본', slow: bool = False, use_rvc: bool = False, rvc_model: str = None):
        """TTS를 재생합니다."""
        tts_file = None
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
            
            # RVC 사용 (TTS + RVC 결합)
            if use_rvc:
                if _check_rvc_available():
                    if rvc_model is None:
                        rvc_models = get_all_rvc_models()
                        if rvc_models:
                            rvc_model = list(rvc_models.keys())[0]
                        else:
                            logger.warning("등록된 RVC 모델이 없습니다. gTTS로 폴백합니다.")
                            use_rvc = False
                    
                    if use_rvc and rvc_model:
                        try:
                            tts_file = await text_to_speech_with_rvc_async(text, rvc_model, pitch=0, speed=1.0, lang=lang)
                            logger.info(f"RVC TTS 파일 생성 완료: {rvc_model}")
                        except Exception as e:
                            logger.error(f"RVC TTS 생성 실패: {e}, gTTS로 폴백합니다.")
                            use_rvc = False
                else:
                    logger.warning("RVC를 사용할 수 없습니다. pip install tts-with-rvc-onnx 또는 pip install tts-with-rvc로 설치하세요. gTTS로 폴백합니다.")
                    use_rvc = False
            
            # RVC를 사용하지 않는 경우 gTTS 사용
            if not use_rvc:
                tld = 'com'  # 기본값
                if lang in VOICE_MODELS and voice_model in VOICE_MODELS[lang]:
                    tld = VOICE_MODELS[lang][voice_model]['tld']
                
                # TTS 파일 생성 (동기 함수이므로 비동기 루프에서 실행)
                import asyncio
                loop = asyncio.get_event_loop()
                tts_file = await loop.run_in_executor(
                    None,
                    lambda: text_to_speech(text, lang=lang, slow=slow, tld=tld)
                )
            
            # 음악 재생 중이면 일시정지
            music_cog = self.bot.get_cog('Music')
            music_player = None
            volume_lowered = False
            music_was_playing = False
            music_was_paused = False
            
            if ctx.voice_client.is_playing() and music_cog:
                guild_id = ctx.guild.id if hasattr(ctx, 'guild') else None
                if guild_id and guild_id in music_cog.players:
                    music_player = music_cog.players[guild_id]
                    music_was_playing = True
                    # 음악 일시정지 및 재생 위치 저장
                    if ctx.voice_client.is_playing():
                        # 재생 위치 계산
                        import time
                        current_time = time.time()
                        
                        if music_player.playback_start_time:
                            # 재생 시작 시간이 있으면 정확한 위치 계산
                            elapsed = current_time - music_player.playback_start_time
                            music_player.paused_at_position = max(0, elapsed)
                            logger.debug(f"음악 일시정지 위치: {elapsed:.2f}초 (정확한 추적)")
                        elif music_player.last_position_update:
                            # 재생 시작 시간은 없지만 마지막 위치 업데이트가 있으면
                            # 마지막 업데이트 이후 경과 시간을 더해서 계산
                            time_since_update = current_time - music_player.last_position_update
                            estimated_position = music_player.paused_at_position + time_since_update
                            music_player.paused_at_position = max(0, estimated_position)
                            # 재생 시작 시간을 역산하여 설정
                            music_player.playback_start_time = current_time - music_player.paused_at_position
                            logger.debug(f"음악 일시정지 위치: {estimated_position:.2f}초 (추정, 마지막 업데이트 기준)")
                        else:
                            # 재생 시작 시간도 없고 마지막 업데이트도 없으면 (첫 TTS 호출)
                            # 현재 paused_at_position을 유지 (이미 추적 태스크에서 업데이트되었을 수 있음)
                            if music_player.paused_at_position > 0:
                                # 이미 위치가 있으면 그대로 사용
                                music_player.playback_start_time = current_time - music_player.paused_at_position
                                logger.debug(f"음악 일시정지 위치: {music_player.paused_at_position:.2f}초 (추적 태스크에서 업데이트됨)")
                            else:
                                # 위치가 없으면 현재 시간 기준으로 설정 (다음 TTS부터는 연속 재생)
                                music_player.playback_start_time = current_time
                                music_player.paused_at_position = 0
                                logger.debug("재생 시작 시간이 없어서 현재 시간으로 설정 (첫 TTS 호출, 위치는 0으로 유지)")
                        
                        # 일시정지 시점 저장 (TTS 재생 중 위치 추적 중지용)
                        music_player.paused_at_time = current_time
                        ctx.voice_client.pause()
                        music_was_paused = True
                        logger.debug(f"음악 일시정지 완료 (위치: {music_player.paused_at_position:.2f}초)")
            
            # FFmpeg 경로 설정 (None이거나 빈 문자열이면 기본값 사용)
            ffmpeg_executable = FFMPEG_PATH if FFMPEG_PATH and Path(FFMPEG_PATH).exists() else None
            if not ffmpeg_executable:
                # 시스템 PATH에서 ffmpeg 찾기
                import shutil
                ffmpeg_executable = shutil.which('ffmpeg')
            
            if not ffmpeg_executable:
                logger.error("FFmpeg를 찾을 수 없습니다. FFMPEG_PATH를 설정하거나 시스템 PATH에 ffmpeg를 추가해주세요.")
                cleanup_tts_file(tts_file)
                return False
            
            # 파일 존재 확인
            if not tts_file.exists():
                logger.error(f"TTS 파일이 존재하지 않습니다: {tts_file}")
                cleanup_tts_file(tts_file)
                return False
            
            # 파일 크기 확인
            file_size = tts_file.stat().st_size
            if file_size == 0:
                logger.error(f"TTS 파일이 비어있습니다: {tts_file} (크기: {file_size} bytes)")
                cleanup_tts_file(tts_file)
                return False
            
            logger.debug(f"TTS 파일 재생 준비: {tts_file} (크기: {file_size} bytes)")
            
            # FFmpeg 옵션 (로컬 파일이므로 간단한 옵션만 사용)
            # before_options는 로컬 파일에는 필요 없음
            options = '-vn'  # 비디오 없음, 오디오만
            
            source = discord.FFmpegPCMAudio(
                str(tts_file), 
                executable=ffmpeg_executable,
                options=options
            )
            
            # 볼륨 조정을 위해 PCMVolumeTransformer 사용
            source = discord.PCMVolumeTransformer(source, volume=1.0)
            
            def after_play(error):
                """재생 완료 후 콜백"""
                import asyncio  # 클로저 스코프 문제 해결을 위해 명시적으로 import
                if error:
                    logger.error(f"TTS 재생 오류: {error}")
                    # 재생 실패 시에도 파일 정리 및 대기열 처리
                    if tts_file:
                        cleanup_tts_file(tts_file)
                    # 비동기로 대기열 처리
                    asyncio.run_coroutine_threadsafe(
                        self._handle_playback_error(ctx, music_player, volume_lowered, music_was_playing, music_was_paused),
                        self.bot.loop
                    )
                else:
                    logger.debug(f"TTS 재생 완료: {tts_file}")
                    # 재생 성공 시 정상 처리
                    asyncio.run_coroutine_threadsafe(
                        self._cleanup_after_play(tts_file, ctx, music_player, volume_lowered, music_was_playing, music_was_paused),
                        self.bot.loop
                    )
            
            logger.info(f"TTS 재생 시작: {tts_file.name}")
            ctx.voice_client.play(source, after=after_play)
            
            return True
        except Exception as e:
            logger.error(f"TTS 재생 오류: {e}", exc_info=True)
            # 예외 발생 시 음악 재개 (비동기로 처리)
            if music_was_playing and music_player and ctx.voice_client:
                asyncio.create_task(self._restore_music_after_tts(ctx, music_player, volume_lowered, music_was_playing, music_was_paused))
            # 예외 발생 시 생성된 파일 정리
            if tts_file:
                cleanup_tts_file(tts_file)
            return False
    
    async def _restore_music_after_tts(self, ctx, music_player=None, volume_lowered=False, music_was_playing=False, music_was_paused=False):
        """TTS 재생 후 음악 복원 헬퍼 함수"""
        try:
            # 음악 재생 재개
            if music_was_playing and ctx.voice_client:
                if music_player:
                    # TTS 재생으로 source가 바뀌었으므로 음악을 다시 재생
                    success = await music_player.resume_after_tts()
                    if success:
                        logger.debug("음악 재생 재개 완료")
                    else:
                        logger.warning("음악 재생 재개 실패")
                else:
                    # music_player가 없으면 Music cog에서 가져오기
                    music_cog = self.bot.get_cog('Music')
                    if music_cog:
                        guild_id = ctx.guild.id if hasattr(ctx, 'guild') else None
                        if guild_id and guild_id in music_cog.players:
                            music_player = music_cog.players[guild_id]
                            success = await music_player.resume_after_tts()
                            if success:
                                logger.debug("음악 재생 재개 완료")
                            else:
                                logger.warning("음악 재생 재개 실패")
        except Exception as e:
            logger.error(f"음악 복원 중 오류: {e}", exc_info=True)

    async def _handle_playback_error(self, ctx, music_player=None, volume_lowered=False, music_was_playing=False, music_was_paused=False):
        """재생 오류 발생 시 처리"""
        # 음악 재개
        if music_was_playing and music_player and ctx.voice_client:
            await self._restore_music_after_tts(ctx, music_player, volume_lowered, music_was_playing, music_was_paused)
        
        # 재생 플래그 해제
        self.playing[ctx.guild.id] = False
        
        # 대기열 처리 (다음 항목 재생 시도)
        if ctx.guild.id in self.tts_queue and self.tts_queue[ctx.guild.id]:
            await self._process_queue(ctx.guild)

    async def delete_command_message(self, ctx):
        """명령어 메시지를 삭제합니다."""
        await asyncio.sleep(COMMAND_MESSAGE_DELETE_DELAY)
        try:
            await ctx.message.delete()
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            pass

    async def ensure_voice_client(self, ctx):
        """음성 채널에 연결되어 있는지 확인하고, 없으면 연결합니다."""
        if not ctx.voice_client:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
                return True
            else:
                await ctx.send("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 어디로 가야 할지 모르겠어요~", delete_after=10)
                await self.delete_command_message(ctx)
                return False
        return True

    async def _cleanup_after_play(self, file_path: Path, ctx, music_player=None, volume_lowered=False, music_was_playing=False, music_was_paused=False):
        """재생 후 파일 정리 및 대기열 처리"""
        await asyncio.sleep(1)  # 재생 완료 대기
        cleanup_tts_file(file_path)
        
        # 음악 재개
        if music_was_playing and music_player and ctx.voice_client:
            await self._restore_music_after_tts(ctx, music_player, volume_lowered, music_was_playing, music_was_paused)
        
        # 재생 플래그 해제
        self.playing[ctx.guild.id] = False
        
        # 대기열 처리 (재생 완료 후 다음 항목 재생)
        if ctx.guild.id in self.tts_queue and self.tts_queue[ctx.guild.id]:
            await self._process_queue(ctx.guild)

    async def _process_queue(self, guild):
        """TTS 대기열을 처리합니다."""
        # 재생 중이면 대기
        if self.playing.get(guild.id, False):
            return
        
        # voice_client 확인
        if not guild.voice_client:
            self.tts_queue[guild.id] = []
            self.playing[guild.id] = False
            return
        
        # 대기열이 비어있으면 종료
        if guild.id not in self.tts_queue or not self.tts_queue[guild.id]:
            self.playing[guild.id] = False
            return
        
        # 대기열에서 항목 가져오기 (빈 텍스트 필터링)
        while self.tts_queue[guild.id]:
            user_id, text, settings = self.tts_queue[guild.id].pop(0)
            
            # 텍스트 유효성 검사
            if not text or not text.strip() or len(text.strip()) > 200:
                logger.debug(f"유효하지 않은 텍스트 건너뛰기: '{text[:50]}...'")
                continue
            
            # 재생 플래그 설정
            self.playing[guild.id] = True
            
            # 임시 context 생성 (재생용)
            class TempContext:
                def __init__(self, guild, voice_client, bot):
                    self.guild = guild
                    self.voice_client = voice_client
                    self.bot = bot
            
            temp_ctx = TempContext(guild, guild.voice_client, self.bot)
            success = await self.play_tts(
                temp_ctx,
                text.strip(),
                settings.get('lang', 'ko'),
                settings.get('voice_model', '기본'),
                settings.get('slow', False),
                settings.get('use_rvc', False),
                settings.get('rvc_model', None)
            )
            
            # 재생 성공 시 종료 (재생 완료 후 _cleanup_after_play에서 다음 항목 처리)
            if success:
                return
            
            # 재생 실패 시 플래그 해제하고 다음 항목 시도
            self.playing[guild.id] = False
            logger.warning(f"TTS 재생 실패, 대기열의 다음 항목 처리 시도")
        
        # 모든 항목을 처리했거나 유효한 항목이 없으면 플래그 해제
        self.playing[guild.id] = False

    @commands.Cog.listener()
    async def on_message(self, message):
        """메시지를 받으면 TTS 자동 읽기 모드가 활성화된 사용자의 메시지를 읽습니다."""
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
        
        # 텍스트가 너무 짧거나 길면 무시
        text = message.content.strip()
        if not text or len(text) > 200:
            return
        
        # 음성 채널에 연결되어 있는지 확인
        if not message.guild.voice_client:
            if message.author.voice:
                try:
                    await message.author.voice.channel.connect()
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
        if message.guild.id not in self.tts_queue:
            self.tts_queue[message.guild.id] = []
        
        settings = self.get_user_settings(message.author.id, message.guild.id)
        self.tts_queue[message.guild.id].append((
            message.author.id,
            text,
            settings
        ))
        
        # 재생 중이 아니면 즉시 재생
        if not self.playing.get(message.guild.id, False):
            await self._process_queue(message.guild)

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
            
            self.set_user_settings(
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

        # 텍스트가 있으면 즉시 읽기
        if len(text) > 200:
            await ctx.send("선생님, 텍스트가 너무 길어요! 200자 이하로 입력해주세요~", delete_after=12)
            await self.delete_command_message(ctx)
            return

        async with ctx.typing():
            settings = self.get_user_settings(ctx.author.id, ctx.guild.id)
            success = await self.play_tts(
                ctx,
                text,
                settings.get('lang', 'ko'),
                settings.get('voice_model', '기본'),
                settings.get('slow', False),
                settings.get('use_rvc', False),
                settings.get('rvc_model', None)
            )
            
            if success:
                await ctx.send(f"선생님, '{text[:50]}{'...' if len(text) > 50 else ''}'을(를) 읽어드릴게요!", delete_after=10)
            else:
                await ctx.send("선생님, TTS 생성 중 오류가 발생했어요!", delete_after=12)
            
            await self.delete_command_message(ctx)

    @commands.command(name='tts목소리', aliases=['ttsvoice', 'tts모델', '목소리변경'])
    async def tts_voice_command(self, ctx):
        """TTS 목소리 모델을 변경합니다. (gTTS 또는 RVC 선택 가능)"""
        settings = self.get_user_settings(ctx.author.id, ctx.guild.id)
        current_lang = settings.get('lang', 'ko')
        current_model = settings.get('voice_model', '기본')
        use_rvc = settings.get('use_rvc', False)
        rvc_model = settings.get('rvc_model', None)
        
        view = discord.ui.View()
        
        # RVC 사용 가능 여부 확인
        rvc_available = _check_rvc_available()
        
        # TTS 엔진 선택 (gTTS 또는 RVC)
        engine_options = [
            discord.SelectOption(
                label="gTTS (Google TTS)",
                value="gtts",
                description="빠르고 간단한 TTS",
                default=not use_rvc
            )
        ]
        
        # RVC 옵션 추가 (사용 가능한 경우만)
        if rvc_available:
            engine_options.append(
                discord.SelectOption(
                    label="RVC (TTS + RVC)",
                    value="rvc",
                    description="텍스트를 원하는 목소리로 변환",
                    default=use_rvc
                )
            )
        
        current_engine = "RVC" if use_rvc else "gTTS"
        
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
            
            self.set_user_settings(
                ctx.author.id,
                ctx.guild.id,
                use_rvc=new_use_rvc
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
                    self.set_user_settings(
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
                    self.set_user_settings(
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
                self.set_user_settings(
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
        if not use_rvc:
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
                self.set_user_settings(
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
                    self.set_user_settings(
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
        
        self.set_user_settings(
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
        rvc_model = settings.get('rvc_model', None)
        
        # 모델 이름 가져오기
        if use_rvc:
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
        if use_rvc:
            embed.add_field(name="RVC 모델", value=f"🎤 {rvc_info.get('name', rvc_model) if rvc_info else rvc_model}", inline=True)
        
        await ctx.send(embed=embed, delete_after=30)
        await self.delete_command_message(ctx)


    @commands.command(name='ttsrvc모델추가', aliases=['ttsrvcadd', 'rvc모델추가'])
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
        self.set_user_settings(
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
        self.set_user_settings(
            ctx.author.id,
            ctx.guild.id,
            use_rvc=False,
            rvc_model=None
        )
        
        await ctx.send("선생님, RVC 사용을 중지했어요! 이제 기본 TTS를 사용할 거예요.", delete_after=15)
        await self.delete_command_message(ctx)

    @commands.command(name='ttsrvc자동등록', aliases=['ttsrvcscan', 'rvc자동등록'])
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
        ctx = await self.bot.get_context(interaction)
        await self.tts_command(ctx, text=text)
    
    async def slash_tts_voice(self, interaction: discord.Interaction):
        """Slash Command로 TTS 목소리 모델 변경"""
        ctx = await self.bot.get_context(interaction)
        await self.tts_voice_command(ctx)
    
    async def slash_tts_slow(self, interaction: discord.Interaction):
        """Slash Command로 TTS 느린 속도 모드 토글"""
        ctx = await self.bot.get_context(interaction)
        await self.tts_slow_toggle(ctx)
    
    async def slash_tts_settings(self, interaction: discord.Interaction):
        """Slash Command로 현재 TTS 설정 확인"""
        ctx = await self.bot.get_context(interaction)
        await self.tts_settings_command(ctx)
    
    async def cog_load(self):
        """Cog가 로드될 때 Slash Commands 등록 및 자동 모델 스캔"""
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