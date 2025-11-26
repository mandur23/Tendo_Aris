import os
import sys
import asyncio
import random
import logging
import discord
from discord.ui import Button, View
from async_timeout import timeout
from utils.ytdl_utils import create_ytdl_instance, ffmpeg_path, ffmpeg_options
from utils.config import IDLE_TIMEOUT, DEFAULT_VOLUME, MAX_RETRIES, BASE_DELAY, MAX_PLAY_RETRIES, QUEUE_TIMEOUT
from utils.discord_utils import safe_send, safe_delete_message, retry_on_403_error

logger = logging.getLogger(__name__)


class MusicPlayer:
    def __init__(self, ctx):
        self.bot = ctx.bot
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None
        self.volume = DEFAULT_VOLUME
        self.current = None
        self.loop = False
        self.queue_loop = False
        self.current_message = None
        self.button_message = None

        self.idle_timeout = IDLE_TIMEOUT
        self.last_activity = asyncio.Event()
        self.inactive_time = 0

        self.random_play = False
        self.voice_channel = (
            ctx.voice_client.channel if ctx.voice_client and ctx.voice_client.channel
            else ctx.author.voice.channel if ctx.author.voice
            else None
        )

        self.bot.loop.create_task(self.player_loop())
        self.bot.loop.create_task(self.register_voice_state_listener())
        self.bot.loop.create_task(self.check_idle_timeout())

    @property
    def is_playing(self):
        return self.guild.voice_client and self.guild.voice_client.is_playing()

    async def check_idle_timeout(self):
        while True:
            await asyncio.sleep(1)
            if not self.last_activity.is_set():
                self.inactive_time += 1
                if self.inactive_time >= self.idle_timeout:
                    logger.info(f"비활동 시간 {self.idle_timeout}초 초과. 봇을 재시작합니다.")
                    await self.stop()
                    self.restart_program()
            else:
                self.inactive_time = 0

    def restart_program(self):
        """현재 프로그램을 재시작합니다."""
        os.execv(sys.executable, ['python'] + sys.argv)

    def update_voice_channel(self, channel):
        if channel:
            self.voice_channel = channel

    def update_voice_channel_from_context(self, ctx):
        channel = None
        if ctx.voice_client and ctx.voice_client.channel:
            channel = ctx.voice_client.channel
        elif ctx.author.voice:
            channel = ctx.author.voice.channel
        self.update_voice_channel(channel)

    def refresh_voice_channel_from_client(self):
        if self.guild.voice_client and self.guild.voice_client.channel:
            self.voice_channel = self.guild.voice_client.channel

    async def attempt_reconnect(self, max_attempts: int = 3) -> bool:
        if not self.voice_channel:
            return False

        logger.warning(f"{self.guild.name} 음성 채널 연결 끊김 감지 - 재연결 시도 시작")

        for attempt in range(1, max_attempts + 1):
            try:
                voice_client = self.guild.voice_client
                if voice_client:
                    if voice_client.is_connected() and voice_client.channel == self.voice_channel:
                        logger.info("음성 채널 이미 연결되어 있음 - 재연결 불필요")
                        return True
                    try:
                        await voice_client.disconnect(force=True)
                    except Exception as disconnect_error:
                        logger.debug(f"재연결 전 기존 연결 해제 실패 (무시됨): {disconnect_error}")

                await self.voice_channel.connect(reconnect=True)
                self.refresh_voice_channel_from_client()
                await safe_send(
                    self.channel,
                    "선생님, 음성 채널 연결이 끊어져서 다시 붙었어요! 계속 재생해볼게요.",
                    delete_after=10
                )
                logger.info(f"{self.guild.name} 음성 채널 재연결 성공 (시도 {attempt}/{max_attempts})")
                return True
            except discord.ClientException as ce:
                if 'already connected' in str(ce).lower():
                    self.refresh_voice_channel_from_client()
                    return True
                logger.warning(f"음성 채널 재연결 시도 {attempt}/{max_attempts} 실패 (ClientException): {ce}")
            except Exception as e:
                logger.warning(f"음성 채널 재연결 시도 {attempt}/{max_attempts} 실패: {e}")

            await asyncio.sleep(3 * attempt)

        await safe_send(
            self.channel,
            "선생님, 음성 채널에 다시 연결하려 했지만 계속 실패했어요. 잠시 후 다시 시도해 주세요.",
            delete_after=10
        )
        return False

    async def ensure_voice_connection(self) -> bool:
        self.refresh_voice_channel_from_client()
        voice_client = self.guild.voice_client
        if voice_client and voice_client.is_connected():
            return True
        return await self.attempt_reconnect()

    async def register_voice_state_listener(self):
        @self.bot.listen('on_voice_state_update')
        async def on_voice_state_update(member, before, after):
            self.last_activity.set()
            
            # voice_client가 없으면 무시
            if not self.guild.voice_client:
                return

            if member == self.guild.me:
                if after.channel and after.channel != self.voice_channel:
                    self.voice_channel = after.channel
                if before.channel and after.channel is None:
                    if not self.current and self.queue.empty():
                        return
                    if await self.attempt_reconnect():
                        return
                    await safe_send(
                        self.channel,
                        "선생님, 음성 채널에서 쫓겨나서 재연결을 시도했지만 실패했어요. 다시 불러주실래요?",
                        delete_after=10
                    )
                    return
                
            if before.channel is not None and after.channel is None:
                if member == self.guild.me:
                    return

                if len(before.channel.members) > 1:
                    return

                try:
                    if self.guild.voice_client and self.guild.voice_client.is_connected():
                        await self.guild.voice_client.pause()
                except Exception as e:
                    logger.debug(f"일시정지 중 오류 (무시됨): {e}")
                    
                await asyncio.sleep(10)
                await self.stop()
                
                try:
                    message = await member.send("아리스가 음성 채널에서 나가요. 다음에 또 불러주세요!")
                    await asyncio.sleep(3)
                    await message.delete()
                except Exception as e:
                    logger.debug(f"메시지 전송/삭제 중 오류 (무시됨): {e}")

            if after.channel is not None and member != self.guild.me:
                if len(after.channel.members) == 1:
                    try:
                        if self.guild.voice_client and self.guild.voice_client.is_connected():
                            await self.guild.voice_client.disconnect()
                            await member.send("아리스가 음성 채널에서 나가요. 다음에 또 불러주세요!")
                    except Exception as e:
                        logger.debug(f"연결 해제 중 오류 (무시됨): {e}")

    async def player_loop(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()
            self.last_activity.set()

            await self.delete_messages()

            if self.loop and self.current:
                source = self.current
            elif self.queue_loop and self.queue.empty() and self.current:
                source = self.current
                await self.queue.put(source)
            else:
                try:
                    async with timeout(QUEUE_TIMEOUT):
                        source = await self.queue.get()
                except asyncio.TimeoutError:
                    await self.delete_messages()
                    return await self.stop()

            if not isinstance(source, dict):
                try:
                    source = await retry_on_403_error(
                        self.bot.loop.run_in_executor,
                        None,
                        lambda: create_ytdl_instance().extract_info(source, download=False),
                        max_retries=MAX_RETRIES,
                        base_delay=BASE_DELAY
                    )
                except Exception as e:
                    error_msg = str(e).lower()
                    if 'private' in error_msg or 'unavailable' in error_msg:
                        await safe_send(self.channel, f'앗, 이 영상은 비공개이거나 삭제되었어요: {str(e)[:100]}...', delete_after=10)
                    else:
                        await safe_send(self.channel, f'어머나, {MAX_RETRIES}번 시도했지만 노래를 불러올 수 없어요: {str(e)[:100]}...', delete_after=10)
                    continue

            self.current = source
            
            try:
                await self.cog.add_to_history(self.guild.id, source)
            except Exception as e:
                logger.error(f"히스토리 추가 중 오류: {e}")
            
            self.current_message = await safe_send(
                self.channel,
                f'선생님, 지금 재생 중인 노래예요: {source["title"]}\n주소: {source.get("webpage_url", "알 수 없음")}'
            )
            self.button_message, view = await self.create_player_message()
            await self.update_button_styles(view)

            try:
                # voice_client 확인
                if not await self.ensure_voice_connection():
                    self.next.set()
                    continue
                    
                if self.guild.voice_client.is_playing():
                    self.guild.voice_client.stop()

                for play_attempt in range(MAX_PLAY_RETRIES):
                    # 재생 시도 전에 voice_client 확인
                    if not self.guild.voice_client:
                        break
                        
                    try:
                        self.guild.voice_client.play(
                            discord.FFmpegPCMAudio(source['url'], executable=ffmpeg_path,
                                                   before_options=ffmpeg_options['before_options'],
                                                   options=ffmpeg_options['options']),
                            after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set)
                        )
                        self.guild.voice_client.source = discord.PCMVolumeTransformer(self.guild.voice_client.source)
                        self.guild.voice_client.source.volume = self.volume
                        break
                    except Exception as play_error:
                        if play_attempt == MAX_PLAY_RETRIES - 1:
                            await safe_send(self.channel, f"앗, 재생 중에 문제가 생겼어요. 다음 곡으로 넘어갈게요: {str(play_error)[:100]}...", delete_after=10)
                            self.next.set()
                        else:
                            logger.warning(f"재생 시도 {play_attempt + 1}/{MAX_PLAY_RETRIES} 실패: {str(play_error)}")
                            await asyncio.sleep(3)
                            
                            try:
                                source = await retry_on_403_error(
                                    self.bot.loop.run_in_executor,
                                    None,
                                    lambda: create_ytdl_instance().extract_info(source.get('webpage_url', source.get('url')), download=False),
                                    max_retries=MAX_RETRIES,
                                    base_delay=BASE_DELAY
                                )
                                self.current = source
                            except Exception as refresh_error:
                                logger.error(f"URL 새로고침 실패: {refresh_error}")
                                pass
            except Exception as e:
                await safe_send(self.channel, f"재생 시스템에 문제가 있어요. 다음 곡으로 넘어갈게요: {str(e)[:100]}...", delete_after=10)
                logger.error(f"상세 오류 정보: {e.__class__.__name__}: {str(e)}")
                self.next.set()

            await self.next.wait()

            if self.queue_loop and not self.loop:
                await self.queue.put(self.current)

            if self.queue.empty() and not (self.loop or self.queue_loop):
                await self.delete_messages()
                await self.stop()
                await safe_send(self.channel, "선생님, 재생할 곡이 더 이상 없어요. 아리스가 음성 채널에서 나갈게요~", delete_after=10)
                break

    async def delete_messages(self):
        """메시지 삭제 시 오류 처리 개선"""
        if self.current_message:
            await safe_delete_message(self.current_message)
            self.current_message = None
            
        if self.button_message:
            await safe_delete_message(self.button_message)
            self.button_message = None

    async def stop(self):
        self.queue._queue.clear()
        if self.guild.voice_client:
            await self.guild.voice_client.disconnect()
        self.voice_channel = None
        self.current = None
        self.loop = False
        self.queue_loop = False
        await self.delete_messages()

    async def create_player_message(self):
        view = View(timeout=None)

        play_pause = Button(label="재생/일시정지", style=discord.ButtonStyle.primary)
        skip = Button(label="다음 노래로!", style=discord.ButtonStyle.secondary)

        loop = Button(label="이 노래 계속 들을래요", style=discord.ButtonStyle.danger)
        queue_loop = Button(label="전체 반복", style=discord.ButtonStyle.danger)

        random_play = Button(label="랜덤 재생", style=discord.ButtonStyle.secondary)

        volume_up = Button(label="더 크게!", style=discord.ButtonStyle.secondary)
        volume_down = Button(label="조금만 작게", style=discord.ButtonStyle.secondary)
        stop_button = Button(label="종료", style=discord.ButtonStyle.danger)

        async def play_pause_callback(interaction):
            try:
                if not self.guild.voice_client:
                    await interaction.response.send_message("음성 채널에 연결되어 있지 않아요!", ephemeral=True, delete_after=3)
                    return
                    
                if self.guild.voice_client.is_paused():
                    self.guild.voice_client.resume()
                    await interaction.response.send_message("선생님, 노래를 다시 재생할게요!", ephemeral=True, delete_after=3)
                else:
                    self.guild.voice_client.pause()
                    await interaction.response.send_message("노래를 잠시 멈췄어요. 계속 들으시려면 다시 눌러주세요, 선생님!", ephemeral=True,
                                                            delete_after=3)
            except discord.errors.NotFound:
                logger.debug("인터랙션 응답 시간 초과 (무시됨)")
            except Exception as e:
                logger.debug(f"play_pause_callback 중 오류 (무시됨): {e}")

        async def skip_callback(interaction):
            try:
                if not self.guild.voice_client:
                    await interaction.response.send_message("음성 채널에 연결되어 있지 않아요!", ephemeral=True, delete_after=3)
                    return
                    
                self.guild.voice_client.stop()
                await interaction.response.send_message("알겠어요, 선생님! 다음 노래로 넘어갈게요!", ephemeral=True, delete_after=3)
            except discord.errors.NotFound:
                logger.debug("인터랙션 응답 시간 초과 (무시됨)")
            except Exception as e:
                logger.debug(f"skip_callback 중 오류 (무시됨): {e}")

        async def loop_callback(interaction):
            try:
                if self.random_play:
                    await interaction.response.send_message("랜덤 재생이 활성화되어 있어요. 한 곡 반복을 켜기 전에 랜덤 재생을 꺼야 해요.", ephemeral=True)
                    return

                self.loop = not self.loop
                loop.style = discord.ButtonStyle.success if self.loop else discord.ButtonStyle.danger
                await interaction.response.edit_message(view=view)
            except discord.errors.NotFound:
                logger.debug("인터랙션 응답 시간 초과 (무시됨)")
            except Exception as e:
                logger.debug(f"loop_callback 중 오류 (무시됨): {e}")

        async def queue_loop_callback(interaction):
            try:
                self.queue_loop = not self.queue_loop
                queue_loop.style = discord.ButtonStyle.success if self.queue_loop else discord.ButtonStyle.danger
                await interaction.response.edit_message(view=view)
            except discord.errors.NotFound:
                logger.debug("인터랙션 응답 시간 초과 (무시됨)")
            except Exception as e:
                logger.debug(f"queue_loop_callback 중 오류 (무시됨): {e}")

        async def random_play_callback(interaction):
            try:
                if self.loop:
                    await interaction.response.send_message("한 곡 반복이 활성화되어 있어요. 랜덤 재생을 켜기 전에 한 곡 반복을 꺼야 해요.",
                                                            ephemeral=True)
                    return

                self.random_play = not self.random_play
                status = "켜졌어요" if self.random_play else "꺼졌어요"
                await interaction.response.send_message(f"랜덤 재생 모드가 {status}!", ephemeral=True)

                random_play.style = discord.ButtonStyle.success if self.random_play else discord.ButtonStyle.secondary
                await interaction.message.edit(view=view)

                if self.random_play:
                    await self.play_next()
            except discord.errors.NotFound:
                logger.debug("인터랙션 응답 시간 초과 (무시됨)")
            except Exception as e:
                logger.debug(f"random_play_callback 중 오류 (무시됨): {e}")

        async def volume_up_callback(interaction):
            try:
                if not self.guild.voice_client or not self.guild.voice_client.source:
                    await interaction.response.send_message("음성 채널에 연결되어 있지 않아요!", ephemeral=True, delete_after=3)
                    return
                    
                if self.volume < 1.0:
                    self.volume = min(1.0, self.volume + 0.1)
                    self.guild.voice_client.source.volume = self.volume
                    await interaction.response.send_message(f"선생님, 볼륨을 {int(self.volume * 100)}%로 올렸어요! 이제 잘 들리나요?",
                                                            ephemeral=True, delete_after=3)
                else:
                    await interaction.response.send_message("앗, 볼륨이 이미 최대예요! 아리스의 귀가 아파요~", ephemeral=True, delete_after=3)
            except discord.errors.NotFound:
                logger.debug("인터랙션 응답 시간 초과 (무시됨)")
            except Exception as e:
                logger.debug(f"volume_up_callback 중 오류 (무시됨): {e}")

        async def volume_down_callback(interaction):
            try:
                if not self.guild.voice_client or not self.guild.voice_client.source:
                    await interaction.response.send_message("음성 채널에 연결되어 있지 않아요!", ephemeral=True, delete_after=3)
                    return
                    
                if self.volume > 0.0:
                    self.volume = max(0.0, self.volume - 0.1)
                    self.guild.voice_client.source.volume = self.volume
                    await interaction.response.send_message(f"선생님, 볼륨을 {int(self.volume * 100)}%로 낮췄어요! 이정도면 괜찮으신가요?",
                                                            ephemeral=True, delete_after=3)
                else:
                    await interaction.response.send_message("어머, 볼륨이 이미 최소예요! 더 이상 낮추면 아무 소리도 안 들릴 거예요~", ephemeral=True,
                                                            delete_after=3)
            except discord.errors.NotFound:
                logger.debug("인터랙션 응답 시간 초과 (무시됨)")
            except Exception as e:
                logger.debug(f"volume_down_callback 중 오류 (무시됨): {e}")

        async def stop_callback(interaction):
            try:
                # voice_client 확인
                if self.guild.voice_client and self.guild.voice_client.is_playing():
                    await self.stop()
                    try:
                        await interaction.response.send_message("알겠습니다, 선생님! 아리스가 음악 재생을 종료하고 음성 채널에서 나갔어요~ 다음에 또 불러주세요!",
                                                                ephemeral=True, delete_after=3)
                    except discord.errors.NotFound:
                        # 인터랙션이 이미 만료된 경우
                        logger.debug("인터랙션 응답 시간 초과 (무시됨)")
                    except Exception as e:
                        logger.debug(f"인터랙션 응답 중 오류 (무시됨): {e}")
                else:
                    try:
                        await interaction.response.send_message("현재 재생 중인 음악이 없어요!", ephemeral=True, delete_after=3)
                    except discord.errors.NotFound:
                        logger.debug("인터랙션 응답 시간 초과 (무시됨)")
                    except Exception as e:
                        logger.debug(f"인터랙션 응답 중 오류 (무시됨): {e}")
            except Exception as e:
                logger.error(f"stop_callback 중 오류: {e}")

        play_pause.callback = play_pause_callback
        skip.callback = skip_callback
        loop.callback = loop_callback
        queue_loop.callback = queue_loop_callback
        random_play.callback = random_play_callback
        volume_up.callback = volume_up_callback
        volume_down.callback = volume_down_callback
        stop_button.callback = stop_callback

        view.add_item(play_pause)
        view.add_item(skip)
        view.add_item(loop)
        view.add_item(queue_loop)
        view.add_item(random_play)
        view.add_item(volume_up)
        view.add_item(volume_down)
        view.add_item(stop_button)

        message = await safe_send(self.channel, "선생님, 아리스의 특별 음악 컨트롤이에요! 어떤 걸 눌러볼까요?", view=view)
        return message, view

    async def update_button_styles(self, view):
        """버튼 색상을 현재 상태에 맞게 업데이트합니다."""
        try:
            for item in view.children:
                if isinstance(item, Button):
                    if item.label == "이 노래 계속 들을래요":
                        item.style = discord.ButtonStyle.success if self.loop else discord.ButtonStyle.danger
                    elif item.label == "전체 반복":
                        item.style = discord.ButtonStyle.success if self.queue_loop else discord.ButtonStyle.danger
                    elif item.label == "랜덤 재생":
                        item.style = discord.ButtonStyle.success if self.random_play else discord.ButtonStyle.secondary
            if self.button_message:
                await self.button_message.edit(view=view)
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            # 메시지가 삭제되었거나 권한이 없는 경우 무시
            pass

    def destroy(self, guild):
        return self.bot.loop.create_task(self.cog.cleanup(guild))

    async def play_next(self):
        if self.queue.empty():
            return

        if self.guild.voice_client is None:
            if self.channel.guild.me.voice and self.channel.guild.me.voice.channel:
                voice_channel = self.channel.guild.me.voice.channel
                await voice_channel.connect()
            elif self.channel.author and self.channel.author.voice:
                await self.channel.author.voice.channel.connect()
            else:
                return

        if self.loop and self.current:
            source = self.current
        elif self.random_play and self.queue._queue:
            queue_list = list(self.queue._queue)
            index = random.randrange(len(queue_list))
            source = queue_list[index]
            for _ in range(self.queue.qsize()):
                item = await self.queue.get()
                if item == source:
                    break
                await self.queue.put(item)
        else:
            source = await self.queue.get()

        if self.loop and not self.random_play:
            await self.queue.put(source)

        if not isinstance(source, dict):
            try:
                ydl = create_ytdl_instance()
                source = await self.bot.loop.run_in_executor(None, lambda: ydl.extract_info(source, download=False))
            except Exception as e:
                await safe_send(self.channel, f'어머나, 오류가 발생했어요: {str(e)}')
                return

        if self.current_message:
            await safe_delete_message(self.current_message)
        if self.button_message:
            await safe_delete_message(self.button_message)

        self.current = source
        self.current_message = await safe_send(
            self.channel,
            f'선생님, 지금 재생 중인 노래예요: {source["title"]}\n주소: {source.get("webpage_url", "알 수 없음")}'
        )
        self.button_message, view = await self.create_player_message()

        await self.update_button_styles(view)

        try:
            self.guild.voice_client.play(
                discord.FFmpegPCMAudio(source['url'], executable=ffmpeg_path,
                                       before_options=ffmpeg_options['before_options'],
                                       options=ffmpeg_options['options']),
                after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set)
            )
            self.guild.voice_client.source = discord.PCMVolumeTransformer(self.guild.voice_client.source)
            self.guild.voice_client.source.volume = self.volume
        except Exception as e:
            await safe_send(self.channel, f"앗, 재생 중에 문제가 생겼어요: {str(e)}", delete_after=10)
            logger.error(f"상세 오류 정보: {e.__class__.__name__}: {str(e)}")
            
            if self.random_play:
                await asyncio.sleep(1)

