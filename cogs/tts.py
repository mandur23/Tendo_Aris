"""TTS (Text-to-Speech) 명령어 Cog"""
import asyncio
import logging
import discord
from discord.ext import commands
from pathlib import Path
from utils.tts_utils import text_to_speech, cleanup_tts_file, VOICE_MODELS, COQUI_AVAILABLE, COQUI_MODELS, _lazy_import_coqui
from utils.file_utils import load_tts_settings, save_tts_settings
from utils.config import COMMAND_MESSAGE_DELETE_DELAY

# Coqui TTS 지원 확인 (lazy import)
list_available_models = None

def _get_list_available_models():
    """list_available_models 함수를 lazy로 가져옵니다."""
    global list_available_models
    if list_available_models is None:
        _lazy_import_coqui()
        from utils.tts_utils import list_available_models as _func
        list_available_models = _func if _func else lambda: []
    return list_available_models

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
                'use_coqui': False,  # Coqui TTS 사용 여부
                'coqui_model': None  # Coqui TTS 모델 이름
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


    async def play_tts(self, ctx, text: str, lang: str = 'ko', voice_model: str = '기본', slow: bool = False, use_coqui: bool = False, coqui_model: str = None):
        """TTS를 재생합니다."""
        try:
            # Coqui TTS 사용
            if use_coqui:
                _lazy_import_coqui()
                # COQUI_AVAILABLE을 다시 확인 (lazy import 후 업데이트됨)
                from utils.tts_utils import COQUI_AVAILABLE as _coqui_avail, COQUI_MODELS as _models
                
                if _coqui_avail:
                    if coqui_model is None:
                        # 언어에 맞는 기본 모델 선택
                        if lang in _models:
                            coqui_model = list(_models[lang].keys())[0]
                        else:
                            coqui_model = 'tts_models/ko/korean/jets'
                    
                    # Coqui TTS는 비동기로 실행 (lazy import)
                    from utils.coqui_tts_utils import text_to_speech_coqui_async
                    tts_file = await text_to_speech_coqui_async(text, coqui_model)
                else:
                    # Coqui TTS를 사용할 수 없으면 gTTS로 폴백
                    use_coqui = False
            else:
                # gTTS 사용
                tld = 'com'  # 기본값
                if lang in VOICE_MODELS and voice_model in VOICE_MODELS[lang]:
                    tld = VOICE_MODELS[lang][voice_model]['tld']
                
                # TTS 파일 생성 (동기 함수이므로 비동기 루프에서 실행)
                import asyncio
                loop = asyncio.get_event_loop()
                tts_file = await loop.run_in_executor(
                    None,
                    lambda: text_to_speech(text, lang=lang, slow=slow, tld=tld, use_coqui=False)
                )
            
            # 음성 재생
            if ctx.voice_client.is_playing():
                ctx.voice_client.stop()
            
            source = discord.FFmpegPCMAudio(str(tts_file))
            ctx.voice_client.play(
                source,
                after=lambda e: asyncio.run_coroutine_threadsafe(
                    self._cleanup_after_play(tts_file, ctx),
                    self.bot.loop
                ) if e is None else logger.error(f"TTS 재생 오류: {e}")
            )
            
            return True
        except Exception as e:
            logger.error(f"TTS 재생 오류: {e}")
            return False

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

    async def _cleanup_after_play(self, file_path: Path, ctx):
        """재생 후 파일 정리 및 대기열 처리"""
        await asyncio.sleep(1)  # 재생 완료 대기
        cleanup_tts_file(file_path)
        
        # 대기열 처리
        if ctx.guild.id in self.tts_queue and self.tts_queue[ctx.guild.id]:
            self.playing[ctx.guild.id] = False
            await self._process_queue(ctx.guild)

    async def _process_queue(self, guild):
        """TTS 대기열을 처리합니다."""
        if guild.id not in self.tts_queue or not self.tts_queue[guild.id]:
            return
        
        if self.playing.get(guild.id, False):
            return
        
        if not guild.voice_client:
            self.tts_queue[guild.id] = []
            return
        
        user_id, text, settings = self.tts_queue[guild.id].pop(0)
        self.playing[guild.id] = True
        
        # 임시 context 생성 (재생용)
        class TempContext:
            def __init__(self, guild, voice_client):
                self.guild = guild
                self.voice_client = voice_client
        
        temp_ctx = TempContext(guild, guild.voice_client)
        await self.play_tts(
            temp_ctx,
            text,
            settings.get('lang', 'ko'),
            settings.get('voice_model', '기본'),
            settings.get('slow', False),
            settings.get('use_coqui', False),
            settings.get('coqui_model', None)
        )

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
                await ctx.send("선생님, 이제부터 선생님의 채팅을 자동으로 읽어드릴게요! `!tts목소리` 명령어로 목소리를 바꿀 수 있어요~", delete_after=10)
            else:
                await ctx.send("선생님, TTS 자동 읽기 모드를 껐어요. 다시 켜려면 `!tts`를 입력해주세요!", delete_after=10)
            
            await self.delete_command_message(ctx)
            return

        # 텍스트가 있으면 즉시 읽기
        if len(text) > 200:
            await ctx.send("선생님, 텍스트가 너무 길어요! 200자 이하로 입력해주세요~", delete_after=10)
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
                settings.get('use_coqui', False),
                settings.get('coqui_model', None)
            )
            
            if success:
                await ctx.send(f"선생님, '{text[:50]}{'...' if len(text) > 50 else ''}'을(를) 읽어드릴게요!", delete_after=10)
            else:
                await ctx.send("선생님, TTS 생성 중 오류가 발생했어요!", delete_after=10)
            
            await self.delete_command_message(ctx)

    @commands.command(name='tts목소리', aliases=['ttsvoice', 'tts모델', '목소리변경'])
    async def tts_voice_command(self, ctx):
        """TTS 목소리 모델을 변경합니다. (gTTS 또는 Coqui TTS 선택 가능)"""
        settings = self.get_user_settings(ctx.author.id, ctx.guild.id)
        current_lang = settings.get('lang', 'ko')
        current_model = settings.get('voice_model', '기본')
        use_coqui = settings.get('use_coqui', False)
        coqui_model = settings.get('coqui_model', None)
        
        view = discord.ui.View()
        
        # Coqui TTS 사용 가능 여부 확인 (lazy import)
        _lazy_import_coqui()
        from utils.tts_utils import COQUI_AVAILABLE as _coqui_avail
        
        # TTS 엔진 선택 (gTTS 또는 Coqui)
        engine_select = discord.ui.Select(
            placeholder=f"TTS 엔진 선택 (현재: {'Coqui TTS' if use_coqui else 'gTTS'})",
            options=[
                discord.SelectOption(
                    label="gTTS (Google TTS)",
                    value="gtts",
                    description="빠르고 간단한 TTS",
                    default=(not use_coqui)
                ),
                discord.SelectOption(
                    label="Coqui TTS",
                    value="coqui",
                    description="고품질 TTS, 커스텀 모델 지원",
                    default=use_coqui,
                    disabled=(not _coqui_avail)
                )
            ]
        )
        
        async def engine_select_callback(interaction):
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                return
            
            selected_engine = engine_select.values[0]
            new_use_coqui = (selected_engine == 'coqui')
            
            self.set_user_settings(
                ctx.author.id,
                ctx.guild.id,
                use_coqui=new_use_coqui
            )
            
            if new_use_coqui:
                await interaction.response.send_message(
                    "선생님, Coqui TTS로 변경했어요! 이제 `!tts목소리`를 다시 사용해서 모델을 선택해주세요!",
                    ephemeral=True,
                    delete_after=5
                )
            else:
                await interaction.response.send_message(
                    "선생님, gTTS로 변경했어요!",
                    ephemeral=True,
                    delete_after=5
                )
        
        engine_select.callback = engine_select_callback
        view.add_item(engine_select)
        
        # Coqui TTS를 사용하는 경우
        if use_coqui and _coqui_avail:
            # COQUI_MODELS 업데이트 확인
            from utils.tts_utils import COQUI_MODELS as _models
            if current_lang not in _models:
                await ctx.send(f"선생님, '{current_lang}' 언어는 Coqui TTS에서 지원되지 않아요!", delete_after=10)
                await self.delete_command_message(ctx)
                return
            
            available_models = _models[current_lang]
            
            # Coqui 모델 선택
            model_select = discord.ui.Select(
                placeholder=f"Coqui 모델 선택 (현재: {coqui_model or '기본'})",
                options=[
                    discord.SelectOption(
                        label=model_info['name'],
                        value=model_name,
                        description=f"Coqui TTS - {model_info['name']}",
                        default=(model_name == (coqui_model or list(available_models.keys())[0]))
                    )
                    for model_name, model_info in available_models.items()
                ]
            )
            
            async def coqui_model_callback(interaction):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                    return
                
                selected_model = model_select.values[0]
                self.set_user_settings(
                    ctx.author.id,
                    ctx.guild.id,
                    coqui_model=selected_model
                )
                
                model_name = available_models[selected_model]['name']
                await interaction.response.send_message(
                    f"선생님, Coqui TTS 모델을 '{model_name}'으로 변경했어요!",
                    ephemeral=True,
                    delete_after=5
                )
            
            model_select.callback = coqui_model_callback
            view.add_item(model_select)
            
            # 언어 선택 (Coqui 지원 언어만)
            lang_select = discord.ui.Select(
                placeholder=f"언어 선택 (현재: {current_lang})",
                options=[
                    discord.SelectOption(
                        label=lang.upper(),
                        value=lang,
                        description=f"{lang} 언어로 변경",
                        default=(lang == current_lang)
                    )
                    for lang in _models.keys()
                ]
            )
            
            async def coqui_lang_callback(interaction):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("선생님, 다른 사람의 설정을 변경할 수 없어요!", ephemeral=True)
                    return
                
                selected_lang = coqui_lang_select.values[0]
                # 언어 변경 시 기본 모델로 설정
                from utils.tts_utils import COQUI_MODELS as _models_update
                if selected_lang in _models_update:
                    default_model = list(_models_update[selected_lang].keys())[0]
                    self.set_user_settings(
                        ctx.author.id,
                        ctx.guild.id,
                        lang=selected_lang,
                        coqui_model=default_model
                    )
                    
                    await interaction.response.send_message(
                        f"선생님, 언어를 '{selected_lang.upper()}'로 변경하고 기본 Coqui 모델로 설정했어요!",
                        ephemeral=True,
                        delete_after=5
                    )
            
            coqui_lang_select = lang_select
            coqui_lang_select.callback = coqui_lang_callback
            view.add_item(coqui_lang_select)
        
        # gTTS를 사용하는 경우
        else:
            if current_lang not in VOICE_MODELS:
                await ctx.send(f"선생님, '{current_lang}' 언어는 지원되지 않아요!", delete_after=10)
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
        use_coqui = settings.get('use_coqui', False)
        coqui_model = settings.get('coqui_model', None)
        
        # 모델 이름 가져오기
        if use_coqui:
            _lazy_import_coqui()
            from utils.tts_utils import COQUI_AVAILABLE as _coqui_avail, COQUI_MODELS as _models
            
            if _coqui_avail and lang in _models and coqui_model in _models[lang]:
                model_name = _models[lang][coqui_model]['name']
            else:
                model_name = coqui_model or "기본"
            engine_name = "Coqui TTS"
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
        
        await ctx.send(embed=embed, delete_after=30)
        await self.delete_command_message(ctx)
