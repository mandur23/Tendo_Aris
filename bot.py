import os
import sys
from encodings.aliases import aliases

from aiohttp.web_routedef import delete
from dotenv import load_dotenv
import discord
from discord.ext import commands
import yt_dlp
from async_timeout import timeout
from functools import partial
import asyncio
from discord.ui import Button, View
import json
from discord.ui import Button, View, Select
from fuzzywuzzy import process
import random
from GameSystem.YachtDiceGame import YachtDiceGame
from datetime import datetime  # 날짜/시간 기록을 위해 추가

# 봇 토큰을 넣은 파일 작성후 주소에 대입.
load_dotenv(dotenv_path=r'C:\Users\User\PycharmProjects\Tendo_Aris\TOKEN.env')
token = os.getenv('DISCORD_BOT_TOKEN')

intents = discord.Intents.default()
intents.message_content = True


class FuzzyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.remove_command('help')  # 기본 help 명령어 제거

    def get_commands(self):
        return list(self.all_commands.values())

    async def get_context(self, message, *, cls=commands.Context):
        ctx = await super().get_context(message, cls=cls)

        if ctx.command is None:
            command_name = ctx.invoked_with
            commands = self.get_commands()
            matches = process.extractBests(command_name, [cmd.name for cmd in commands], score_cutoff=80, limit=1)
            if matches:
                ctx.command = self.all_commands.get(matches[0][0])
            else:
                # 비슷한 명령어가 없을 경우
                similar_commands = process.extractBests(command_name, [cmd.name for cmd in commands], score_cutoff=60)
                if similar_commands:
                    suggestions = ', '.join([match[0] for match in similar_commands])
                    await message.channel.send(f"어머나, '{command_name}' 명령어를 찾을 수 없어요: 비슷한 명령어: {suggestions}")

        return ctx

    async def on_ready(self):
        print(f'아리스가 준비 완료했어요! {self.user}로 로그인했답니다~')


# 봇 객체 생성
bot = FuzzyBot(command_prefix='!', intents=intents)

# 유튜브 음원 다운로드 설정 개선 (403 오류 방지)
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    # 403 오류 방지를 위한 추가 옵션들
    'cookiefile': None,
    'usenetrc': False,
    'username': None,
    'password': None,
    'twofactor': None,
    'videopassword': None,
    'ap_mso': None,
    'ap_username': None,
    'ap_password': None,
    'extractor_retries': 5,
    'socket_timeout': 60,
    'retries': 10,
    'retry_sleep': 3,
    'fragment_retries': 10,
    # 더 현실적인 User-Agent와 헤더
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-us,en;q=0.5',
        'Sec-Fetch-Mode': 'navigate',
    },
    # YouTube 특정 설정
    'age_limit': None,
    'extract_flat': False,
    'geo_bypass': True,
    'geo_bypass_country': 'US',
    # 추가적인 안정성 옵션
    'writesubtitles': False,
    'writeautomaticsub': False,
    'allsubtitles': False,
    'ignoreerrors': True,
}

# 음성 추출 기능 개선
ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1 -timeout 30000000',
    'options': '-vn -timeout 30000000 -user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"'
}

# 설치된 FFmpeg 실행 파일의 경로 (적절히 변경 필요)
ffmpeg_path = r'C:\Users\User\ffmpeg-2024-10-21-git-baa23e40c1-full_build\bin\ffmpeg.exe'


def create_ytdl_instance(custom_options=None):
    """403 오류를 방지하기 위한 개선된 yt-dlp 인스턴스 생성"""
    options = ytdl_format_options.copy()
    
    if custom_options:
        options.update(custom_options)
    
    # 랜덤 User-Agent 선택 (탐지 방지)
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ]
    
    options['http_headers']['User-Agent'] = random.choice(user_agents)
    
    return yt_dlp.YoutubeDL(options)


class MusicPlayer:
    def __init__(self, ctx):
        self.bot = ctx.bot
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None
        self.volume = .2
        self.current = None
        self.loop = False
        self.queue_loop = False
        self.current_message = None
        self.button_message = None  # 버튼 메시지를 저장할 변수 추가

        self.idle_timeout = 300  # 5분 (300초)
        self.last_activity = asyncio.Event()  # 마지막 활동을 기록할 이벤트
        self.inactive_time = 0  # 비활동 시간을 기록할 변수 추가

        self.random_play = False  # 랜덤 재생 모드 변수 초기화

        self.bot.loop.create_task(self.player_loop())
        self.bot.loop.create_task(self.register_voice_state_listener())  # 음성 상태 업데이트 리스너 등록
        self.bot.loop.create_task(self.check_idle_timeout())  # 아이들 타임아웃 체크 추가

    # is_playing 프로퍼티 추가
    @property
    def is_playing(self):
        return self.guild.voice_client and self.guild.voice_client.is_playing()

    async def check_idle_timeout(self):
        while True:
            await asyncio.sleep(1)  # 1초마다 체크
            if not self.last_activity.is_set():
                self.inactive_time += 1  # 비활동 시간 증가
                print(f"비활동 시간: {self.inactive_time}초")  # 비활동 시간 출력
                if self.inactive_time >= self.idle_timeout:
                    await self.stop()  # 활동이 없으면 음악 정지
                    self.restart_program()  # 프로그램 재시작
            else:
                self.inactive_time = 0  # 활동이 있으면 비활동 시간 초기화

    def restart_program(self):
        """현재 프로그램을 재시작합니다."""
        os.execv(sys.executable, ['python'] + sys.argv)

    async def register_voice_state_listener(self):
        @self.bot.listen('on_voice_state_update')
        async def on_voice_state_update(member, before, after):
            self.last_activity.set()  # 사용자가 음성 채널에 있을 때 활동 기록
            if before.channel is not None and after.channel is None:  # 사용자가 음성 채널에서 나갔을 때
                if member == self.guild.me:  # 봇이 나갈 경우
                    return

                # 음성 채널에 남아 있는 사용자 수 확인
                if len(before.channel.members) > 1:  # 다른 사용자가 남아 있는 경우
                    return

                await self.guild.voice_client.pause()  # 음원 일시 정지
                await asyncio.sleep(10)  # 10초 대기
                await self.stop()  # 음성 채널에서 나가기
                # 개인 메시지 전송 및 삭제
                message = await member.send("아리스가 음성 채널에서 나가요. 다음에 또 불러주세요!")  # 개인 메시지 전송
                await asyncio.sleep(3)  # 3초 대기
                await message.delete()  # 메시지 삭제

            # 사용자가 음성 채널에 들어왔을 때
            if after.channel is not None and member != self.guild.me:
                if len(after.channel.members) == 1:  # 사용자가 혼자 있을 경우
                    await self.guild.voice_client.disconnect()  # 봇이 음성 채널에서 나가기
                    await member.send("아리스가 음성 채널에서 나가요. 다음에 또 불러주세요!")  # 개인 메시지 전송

    async def player_loop(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()
            self.last_activity.set()  # 활동이 있을 때마다 이벤트 설정

            # 이전 메시지 삭제 (개선된 오류 처리)
            await self.delete_messages()

            # 단일 곡 반복 모드
            if self.loop and self.current:
                source = self.current
            # 전체 반복 모드
            elif self.queue_loop and self.queue.empty() and self.current:
                source = self.current
                await self.queue.put(source)
            else:
                try:
                    async with timeout(300):  # 5분 동안 대기
                        source = await self.queue.get()
                except asyncio.TimeoutError:
                    await self.delete_messages()
                    return await self.stop()  # 타임아웃 시 종료

            # URL 정보 추출 시 개선된 재시도 로직 (403 오류 대응)
            if not isinstance(source, dict):
                max_retries = 5
                base_delay = 2
                for attempt in range(max_retries):
                    try:
                        # 새로운 YoutubeDL 인스턴스 생성 (각 시도마다, 랜덤 User-Agent 포함)
                        ydl = create_ytdl_instance()
                        source = await self.bot.loop.run_in_executor(None, lambda: ydl.extract_info(source, download=False))
                        break  # 성공하면 루프 종료
                    except Exception as e:
                        error_msg = str(e).lower()
                        
                        # 403 오류 특별 처리
                        if '403' in error_msg or 'forbidden' in error_msg:
                            # 지수적 백오프 적용
                            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                            print(f"403 오류 감지 - 시도 {attempt + 1}/{max_retries}, {delay:.1f}초 대기 후 재시도")
                            await asyncio.sleep(delay)
                        elif 'private' in error_msg or 'unavailable' in error_msg:
                            # 비공개/삭제된 동영상은 즉시 포기
                            await self.channel.send(f'앗, 이 영상은 비공개이거나 삭제되었어요: {str(e)[:100]}...', delete_after=10)
                            break
                        else:
                            # 기타 오류는 짧은 대기
                            await asyncio.sleep(1)
                        
                        if attempt == max_retries - 1:  # 마지막 시도
                            await self.channel.send(f'어머나, {max_retries}번 시도했지만 노래를 불러올 수 없어요: {str(e)[:100]}...', delete_after=10)
                            continue  # 다음 곡으로
                        else:
                            print(f"시도 {attempt + 1}/{max_retries} 실패: {str(e)}")

            self.current = source
            
            # 히스토리에 노래 추가
            try:
                await self.cog.add_to_history(self.guild.id, source)
            except Exception as e:
                print(f"히스토리 추가 중 오류: {e}")
            
            try:
                self.current_message = await self.channel.send(
                    f'선생님, 지금 재생 중인 노래예요: {source["title"]}\n주소: {source.get("webpage_url", "알 수 없음")}')
                self.button_message, view = await self.create_player_message()  # 버튼 메시지와 뷰 저장

                # 버튼 색상 유지
                await self.update_button_styles(view)
            except Exception as e:
                print(f"메시지 전송 중 오류: {e}")

            try:
                # 이미 재생 중인 경우 중지
                if self.guild.voice_client.is_playing():
                    self.guild.voice_client.stop()

                # 새로운 곡 재생 (재시도 로직 추가)
                max_play_retries = 2
                for play_attempt in range(max_play_retries):
                    try:
                        self.guild.voice_client.play(
                            discord.FFmpegPCMAudio(source['url'], executable=ffmpeg_path,
                                                   before_options=ffmpeg_options['before_options'],
                                                   options=ffmpeg_options['options']),
                            after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set)
                        )
                        self.guild.voice_client.source = discord.PCMVolumeTransformer(self.guild.voice_client.source)
                        self.guild.voice_client.source.volume = self.volume
                        break  # 성공하면 루프 종료
                    except Exception as play_error:
                        if play_attempt == max_play_retries - 1:  # 마지막 시도
                            await self.channel.send(f"앗, 재생 중에 문제가 생겼어요. 다음 곡으로 넘어갈게요: {str(play_error)[:100]}...", delete_after=10)
                            self.next.set()  # 다음 곡으로 강제 이동
                        else:
                            print(f"재생 시도 {play_attempt + 1}/{max_play_retries} 실패: {str(play_error)}")
                            await asyncio.sleep(3)  # 3초 대기 후 재시도
                            
                            # URL 다시 가져오기 (403 오류 방지 개선)
                            try:
                                ydl = create_ytdl_instance()
                                source = await self.bot.loop.run_in_executor(None, lambda: ydl.extract_info(source.get('webpage_url', source.get('url')), download=False))
                                self.current = source
                            except Exception as refresh_error:
                                print(f"URL 새로고침 실패: {refresh_error}")
                                pass
            except Exception as e:
                await self.channel.send(f"재생 시스템에 문제가 있어요. 다음 곡으로 넘어갈게요: {str(e)[:100]}...", delete_after=10)
                print(f"상세 오류 정보: {e.__class__.__name__}: {str(e)}")
                self.next.set()  # 다음 곡으로 강제 이동

            await self.next.wait()

            # 전체 반복 모드일 때 현재 곡을 대기열 끝에 추가
            if self.queue_loop and not self.loop:
                await self.queue.put(self.current)

            # 다음 곡이 없고 반복 모드가 아닐 때 종료
            if self.queue.empty() and not (self.loop or self.queue_loop):
                await self.delete_messages()
                await self.stop()
                await self.channel.send("선생님, 재생할 곡이 더 이상 없어요. 아리스가 음성 채널에서 나갈게요~", delete_after=10)
                break

    async def delete_messages(self):
        """메시지 삭제 시 오류 처리 개선"""
        try:
            if self.current_message:
                await self.current_message.delete()
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            # 메시지가 이미 삭제되었거나 권한이 없는 경우 무시
            pass
        finally:
            self.current_message = None
            
        try:
            if self.button_message:
                await self.button_message.delete()
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            # 메시지가 이미 삭제되었거나 권한이 없는 경우 무시
            pass
        finally:
            self.button_message = None

    async def stop(self):
        self.queue._queue.clear()
        if self.guild.voice_client:
            await self.guild.voice_client.disconnect()
        self.current = None
        self.loop = False
        self.queue_loop = False
        await self.delete_messages()

    async def create_player_message(self):
        view = View(timeout=None)  # 타임아웃을 None으로 설정하여 버튼이 항상 유효하도록 함

        play_pause = Button(label="재생/일시정지", style=discord.ButtonStyle.primary)
        skip = Button(label="다음 노래로!", style=discord.ButtonStyle.secondary)

        loop = Button(label="이 노래 계속 들을래요", style=discord.ButtonStyle.danger)
        queue_loop = Button(label="전체 반복", style=discord.ButtonStyle.danger)

        random_play = Button(label="랜덤 재생", style=discord.ButtonStyle.secondary)  # 랜덤 재생 버튼 추가

        volume_up = Button(label="더 크게!", style=discord.ButtonStyle.secondary)
        volume_down = Button(label="조금만 작게", style=discord.ButtonStyle.secondary)
        stop_button = Button(label="종료", style=discord.ButtonStyle.danger)

        async def play_pause_callback(interaction):
            if self.guild.voice_client.is_paused():
                self.guild.voice_client.resume()
                await interaction.response.send_message("선생님, 노래를 다시 재생할게요!", ephemeral=True, delete_after=3)
            else:
                self.guild.voice_client.pause()
                await interaction.response.send_message("노래를 잠시 멈췄어요. 계속 들으시려면 다시 눌러주세요, 선생님!", ephemeral=True,
                                                        delete_after=3)

        async def skip_callback(interaction):
            self.guild.voice_client.stop()
            await interaction.response.send_message("알겠어요, 선생님! 다음 노래로 넘어갈게요!", ephemeral=True, delete_after=3)

        async def loop_callback(interaction):
            if self.random_play:  # 랜덤 재생이 켜져 있을 경우
                await interaction.response.send_message("랜덤 재생이 활성화되어 있어요. 한 곡 반복을 켜기 전에 랜덤 재생을 꺼야 해요.", ephemeral=True)
                return

            self.loop = not self.loop
            loop.style = discord.ButtonStyle.success if self.loop else discord.ButtonStyle.danger  # 버튼 색상 변경
            await interaction.response.edit_message(view=view)  # 메시지 업데이트

        async def queue_loop_callback(interaction):
            self.queue_loop = not self.queue_loop
            queue_loop.style = discord.ButtonStyle.success if self.queue_loop else discord.ButtonStyle.danger  # 버튼 색상 변경
            await interaction.response.edit_message(view=view)  # 메시지 업데이트

        async def random_play_callback(interaction):
            if self.loop:  # 한 곡 반복이 켜져 있을 경우
                await interaction.response.send_message("한 곡 반복이 활성화되어 있어요. 랜덤 재생을 켜기 전에 한 곡 반복을 꺼야 해요.",
                                                        ephemeral=True)
                return

            self.random_play = not self.random_play  # 랜덤 재생 모드 토글
            status = "켜졌어요" if self.random_play else "꺼졌어요"
            await interaction.response.send_message(f"랜덤 재생 모드가 {status}!", ephemeral=True)

            # 버튼 색상 업데이트
            random_play.style = discord.ButtonStyle.success if self.random_play else discord.ButtonStyle.secondary
            await interaction.message.edit(view=view)  # 버튼 상태 업데이트

            if self.random_play:  # 랜덤 재생이 활성화되면 다음 곡부터 랜덤하게 재생
                await self.play_next()  # 다음 곡 재생 호출

        async def volume_up_callback(interaction):
            if self.volume < 1.0:
                self.volume = min(1.0, self.volume + 0.1)
                self.guild.voice_client.source.volume = self.volume
                await interaction.response.send_message(f"선생님, 볼륨을 {int(self.volume * 100)}%로 올렸어요! 이제 잘 들리나요?",
                                                        ephemeral=True, delete_after=3)
            else:
                await interaction.response.send_message("앗, 볼륨이 이미 최대예요! 아리스의 귀가 아파요~", ephemeral=True, delete_after=3)

        async def volume_down_callback(interaction):
            if self.volume > 0.0:
                self.volume = max(0.0, self.volume - 0.1)
                self.guild.voice_client.source.volume = self.volume
                await interaction.response.send_message(f"선생님, 볼륨을 {int(self.volume * 100)}%로 낮췄어요! 이정도면 괜찮으신가요?",
                                                        ephemeral=True, delete_after=3)
            else:
                await interaction.response.send_message("어머, 볼륨이 이미 최소예요! 더 이상 낮추면 아무 소리도 안 들릴 거예요~", ephemeral=True,
                                                        delete_after=3)

        async def stop_callback(interaction):
            if self.guild.voice_client.is_playing():
                await self.stop()
                await interaction.response.send_message("알겠습니다, 선생님! 아리스가 음악 재생을 종료하고 음성 채널에서 나갔어요~ 다음에 또 불러주세요!",
                                                        ephemeral=True, delete_after=3)
            else:
                await interaction.response.send_message("현재 재생 중인 음악이 없어요!", ephemeral=True, delete_after=3)

        play_pause.callback = play_pause_callback
        skip.callback = skip_callback
        loop.callback = loop_callback
        queue_loop.callback = queue_loop_callback
        random_play.callback = random_play_callback  # 버튼 클릭 시 호출될 함수 설정
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

        message = await self.channel.send("선생님, 아리스의 특별 음악 컨트롤이에요! 어떤 걸 눌러볼까요?", view=view)
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
                    elif item.label == "랜덤 재생":  # 랜덤 재생 버튼 색상 업데이트 추가
                        item.style = discord.ButtonStyle.success if self.random_play else discord.ButtonStyle.secondary
            if self.button_message:
                await self.button_message.edit(view=view)  # 버튼 상태 업데이트
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            # 메시지가 삭제되었거나 권한이 없는 경우 무시
            pass

    def destroy(self, guild):
        return self.bot.loop.create_task(self.cog.cleanup(guild))

    async def play_next(self):
        if self.queue.empty():
            return

        if self.guild.voice_client is None:  # 음성 클라이언트가 None인지 확인
            if self.channel.guild.me.voice and self.channel.guild.me.voice.channel:  # 봇이 음성 채널에 있는지 확인
                voice_channel = self.channel.guild.me.voice.channel
                await voice_channel.connect()
            elif self.channel.author and self.channel.author.voice:  # 메시지 작성자가 음성 채널에 있는지 확인
                await self.channel.author.voice.channel.connect()
            else:
                return  # 음성 채널에 연결할 수 없으면 종료

        # 반복 모드나 랜덤 재생 모드에 따라 곡 선택
        if self.loop and self.current:  # 한 곡 반복 모드
            source = self.current
        elif self.random_play and self.queue._queue:  # 랜덤 재생 모드
            queue_list = list(self.queue._queue)
            index = random.randrange(len(queue_list))
            source = queue_list[index]
            # 큐에서 해당 항목 제거
            for _ in range(self.queue.qsize()):
                item = await self.queue.get()
                if item == source:
                    break
                await self.queue.put(item)
        else:  # 일반 재생 모드
            source = await self.queue.get()

        # 현재 곡 반복 모드가 활성화된 경우, 현재 곡을 대기열에 추가
        if self.loop and not self.random_play:
            await self.queue.put(source)

        # 곡 재생 시작
        if not isinstance(source, dict):
            try:
                ydl = create_ytdl_instance()
                source = await self.bot.loop.run_in_executor(None, lambda: ydl.extract_info(source, download=False))
            except Exception as e:
                await self.channel.send(f'어머나, 오류가 발생했어요: {str(e)}')
                return

        # 현재 메시지와 버튼 메시지 업데이트
        if self.current_message:
            await self.current_message.delete()
        if self.button_message:
            await self.button_message.delete()

        self.current = source
        self.current_message = await self.channel.send(
            f'선생님, 지금 재생 중인 노래예요: {source["title"]}\n주소: {source.get("webpage_url", "알 수 없음")}')
        self.button_message, view = await self.create_player_message()

        # 버튼 색상 유지
        await self.update_button_styles(view)

        try:
            # 노래 재생
            self.guild.voice_client.play(
                discord.FFmpegPCMAudio(source['url'], executable=ffmpeg_path,
                                       before_options=ffmpeg_options['before_options'],
                                       options=ffmpeg_options['options']),
                after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set)
            )
            self.guild.voice_client.source = discord.PCMVolumeTransformer(self.guild.voice_client.source)
            self.guild.voice_client.source.volume = self.volume
        except Exception as e:
            await self.channel.send(f"앗, 재생 중에 문제가 생겼어요: {str(e)}", delete_after=10)
            print(f"상세 오류 정보: {e.__class__.__name__}: {str(e)}")
            
            # 랜덤 재생이 활성화된 경우, 다음 곡을 계속해서 재생
            if self.random_play:
                await asyncio.sleep(1)  # 잠시 대기 후 다음 곡 재생


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}
        self.playlists = self.load_playlists()
        self.history = self.load_history()  # 히스토리 로드 추가

    def load_history(self):
        """히스토리를 파일에서 로드합니다."""
        try:
            with open('history.json', 'r', encoding='utf-8') as f:
                content = f.read()
                if not content:
                    return {}
                return json.loads(content)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            print("히스토리 파일이 손상되었습니다. 새로운 히스토리를 시작합니다.")
            return {}

    def save_history(self):
        """히스토리를 파일에 저장합니다."""
        try:
            with open('history.json', 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=4)
        except IOError as e:
            print(f"히스토리를 저장하는 중 오류가 발생했습니다: {e}")

    async def add_to_history(self, guild_id, source):
        """재생된 노래를 히스토리에 추가합니다. (오류 처리 개선)"""
        try:
            guild_id_str = str(guild_id)
            if guild_id_str not in self.history:
                self.history[guild_id_str] = []
            
            # 히스토리 항목 생성
            history_item = {
                'title': source.get('title', '알 수 없는 제목'),
                'url': source.get('webpage_url', source.get('url', '')),
                'duration': source.get('duration', 0),
                'played_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            
            # 히스토리에 추가 (최신 항목이 맨 앞에 오도록)
            self.history[guild_id_str].insert(0, history_item)
            
            # 히스토리 크기 제한 (최대 100곡)
            if len(self.history[guild_id_str]) > 100:
                self.history[guild_id_str] = self.history[guild_id_str][:100]
            
            # 파일에 저장
            self.save_history()
        except Exception as e:
            print(f"히스토리 저장 중 오류: {e}")

    @commands.command(name='play', aliases=['p', '재생', '플레이'])
    async def play_command(self, ctx, *, url):
        """YouTube URL을 재생합니다. (URL 검증 개선)"""
        async with ctx.typing():
            try:
                # URL 전처리
                if not url.startswith(('http://', 'https://', 'ytsearch:')):
                    # 검색어로 처리
                    url = f"ytsearch:{url}"
                
                # URL을 대기열에 추가
                player = self.get_player(ctx)
                
                if not player:
                    await ctx.send("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 따라갈게요~", delete_after=10)
                    await self.delete_command_message(ctx)
                    return
                    
                if not ctx.voice_client:
                    if ctx.author.voice:
                        await ctx.author.voice.channel.connect()
                    else:
                        await ctx.send("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 어디로 가야 할지 모르겠어요~", delete_after=10)
                        await self.delete_command_message(ctx)
                        return
                
                # URL을 대기열에 추가
                await player.queue.put(url)
                
                # URL 정보 추출하여 제목 표시 (403 오류 방지 개선)
                title = url  # 기본값 설정
                try:
                    # 여러 방법으로 정보 추출 시도
                    max_extract_retries = 3
                    for retry in range(max_extract_retries):
                        try:
                            # 새로운 yt-dlp 인스턴스 생성
                            extract_options = ytdl_format_options.copy()
                            if retry > 0:
                                # 재시도 시 더 보수적인 설정
                                extract_options['socket_timeout'] = 30 + (retry * 10)
                                await asyncio.sleep(retry * 2)  # 점진적 지연
                            
                            ydl = create_ytdl_instance(extract_options)
                            info = await self.bot.loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                            title = info.get('title', url)
                            break  # 성공하면 루프 종료
                        except Exception as retry_error:
                            error_msg = str(retry_error).lower()
                            if '403' in error_msg or 'forbidden' in error_msg:
                                if retry < max_extract_retries - 1:
                                    print(f"제목 추출 403 오류 - 재시도 {retry + 1}/{max_extract_retries}")
                                    continue
                            elif 'private' in error_msg or 'unavailable' in error_msg:
                                title = "❌ 비공개 또는 삭제된 영상"
                                break
                            if retry == max_extract_retries - 1:
                                print(f"정보 추출 최종 실패: {retry_error}")
                                title = "🔍 제목을 가져올 수 없는 영상"
                except Exception as extract_error:
                    print(f"정보 추출 완전 실패: {extract_error}")
                    title = "❓ 알 수 없는 영상"
                
                # 현재 재생 중인 곡이 없을 때만 재생 시작
                if not player.is_playing:
                    await ctx.send(f"선생님, '{title}'을(를) 재생할게요!", delete_after=10)
                else:
                    await ctx.send(f"선생님, '{title}'을(를) 대기열에 추가했어요! 지금 재생 중인 곡이 끝나면 재생할게요~", delete_after=10)
                
                try:
                    await ctx.message.delete()
                except:
                    pass  # 메시지 삭제 실패 시 무시
                    
            except Exception as e:
                await ctx.send(f"선생님, 재생 중 오류가 발생했어요: {str(e)[:100]}...", delete_after=10)
                print(f"재생 명령어 오류: {e}")

    def load_playlists(self):
        try:
            with open('playlists.json', 'r', encoding='utf-8') as f:
                content = f.read()
                if not content:
                    return {}
                return json.loads(content)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            print("플레이리스트 파일이 손상되었습니다. 새로운 플레이리스트를 시작합니다.", delete_after=10)
            return {}

    def save_playlists(self):
        try:
            with open('playlists.json', 'w', encoding='utf-8') as f:
                json.dump(self.playlists, f, ensure_ascii=False, indent=4)
        except IOError as e:
            print(f"플레이리스트를 저장하는 중 오류가 발생했습니다: {e}", delete_after=10)

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    def get_player(self, ctx):
        if ctx.guild.id in self.players:
            return self.players[ctx.guild.id]
        else:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player
            return player

    async def delete_command_message(self, ctx):
        await asyncio.sleep(3)
        await ctx.message.delete()

    @commands.command(aliases=['종료'])
    async def stop(self, ctx):
        """음악 재생을 종료하고 봇을 음성 채널에서 내보냅니다."""
        player = self.get_player(ctx)
        await player.stop()
        await ctx.send("선생님, 아리스가 음악 재생을 종료하고 음성 채널에서 나갔어요~ 다음에 또 불러주세요!", delete_after=10)
        await self.delete_command_message(ctx)

    @commands.command(aliases=['나가!'])
    async def leave(self, ctx):
        """봇을 음성 채널에서 내보냅니다."""
        await self.stop(ctx)  # stop 명령어를 재사용
        await self.delete_command_message(ctx)

    @commands.command(aliases=['볼륨'])
    async def volume(self, ctx, volume: int):
        """볼륨을 설정합니다. (0-100)"""
        if ctx.voice_client is None:
            await ctx.send("어라? 아리스가 아직 음성 채널에 없어요. 먼저 들어가 볼게요!", delete_after=10)
            await self.delete_command_message(ctx)
            return

        player = self.get_player(ctx)
        if 0 <= volume <= 100:
            player.volume = volume / 100
            ctx.voice_client.source.volume = player.volume
            await ctx.send(f"볼륨을 {volume}%로 맞췄어요! 이제 잘 들리나요?", delete_after=10)
        else:
            await ctx.send("앗, 볼륨은 0에서 100 사이로 해주세요~ 아리스의 귀가 아파요!", delete_after=10)
        await self.delete_command_message(ctx)

    @commands.command(aliases=['플레이리스트목록', '플래이리스트', 'vmffpdlfltmxm'])
    async def 플레이리스트(self, ctx):
        """선생님의 플레이리스트 목록을 보여드려요."""
        user_id = str(ctx.author.id)
        if user_id not in self.playlists or not self.playlists[user_id]:
            await ctx.send("선생님, 아직 플레이리스트가 없어요. 새로 만들어볼까요?", delete_after=10)
            await self.delete_command_message(ctx)
            return

        view = View()
        select = Select(placeholder="플레이리스트를 선택하세요", options=[discord.SelectOption(label=name, value=name) for name in
                                                              self.playlists[user_id].keys()])

        async def select_callback(interaction):
            playlist_name = select.values[0]
            playlist = self.playlists[user_id][playlist_name]
            playlist_str = "\n".join(f"{i + 1}. {url}" for i, url in enumerate(playlist))

            # 플레이리스트의 곡 목록을 보여주고 추가할지 물어봄
            confirm_view = View()

            async def add_to_queue_callback(add_interaction):
                player = self.get_player(ctx)

                if not ctx.voice_client:
                    if ctx.author.voice:
                        await ctx.author.voice.channel.connect()
                    else:
                        await add_interaction.response.send_message("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 어디로 가야 할지 모르겠어요~",
                                                                    ephemeral=True, delete_after=10)
                        return

                for url in playlist:
                    await player.queue.put(url)

                response_message = await add_interaction.response.send_message(
                    f"선생님의 '{playlist_name}' 플레이리스트의 모든 곡을 대기열에 추가했어요!", ephemeral=True, delete_after=5)
                await asyncio.sleep(3)
                await response_message.delete()

            async def cancel_callback(cancel_interaction):
                await cancel_interaction.response.send_message("곡 추가가 취소되었어요.", ephemeral=True, delete_after=10)

            add_button = Button(label="곡 추가하기", style=discord.ButtonStyle.success)
            cancel_button = Button(label="취소하기", style=discord.ButtonStyle.danger)

            add_button.callback = add_to_queue_callback
            cancel_button.callback = cancel_callback

            confirm_view.add_item(add_button)
            confirm_view.add_item(cancel_button)

            await interaction.response.send_message(f"선생님의 '{playlist_name}' 플레이리스트예요:\n{playlist_str}\n곡을 대기열에 추가할까요?",
                                                    view=confirm_view, ephemeral=True, delete_after=30)

        select.callback = select_callback
        view.add_item(select)

        await ctx.send("선생님의 플레이리스트 목록이에요:", view=view, delete_after=60)
        await self.delete_command_message(ctx)

    @commands.command(name='플레이리스트추가', aliases=['프래이리스트추가', 'vmffpdlfltmxmcnrk'])
    async def 플레이리스트추가(self, ctx, name: str, *urls):
        """선생님의 플레이리스트에 새 플레이리스트를 추가하거나 기존 플레이리스트에 곡을 추가해요."""
        if not urls:
            await ctx.send("선생님, URL을 하나 이상 입력해주세요!", delete_after=10)
            await self.delete_command_message(ctx)
            return

        user_id = str(ctx.author.id)
        if user_id not in self.playlists:
            self.playlists[user_id] = {}

        if name not in self.playlists[user_id]:
            self.playlists[user_id][name] = []

        self.playlists[user_id][name].extend(urls)
        self.save_playlists()
        await ctx.send(f"선생님의 '{name}' 플레이리스트에 {len(urls)}개의 곡을 추가했어요!", delete_after=10)
        await self.delete_command_message(ctx)

    @commands.command(name='플레이리스트재생', aliases=['플래이리스트재생', 'vmffpdlfltmxmwotod'])
    async def 플레이리스트재생(self, ctx, name: str):
        """선생님이 선택한 플레이리스트를 재생해요."""
        user_id = str(ctx.author.id)
        if user_id not in self.playlists or name not in self.playlists[user_id]:
            await ctx.send(f"선생님, '{name}' 플레이리스트를 찾을 수 없어요.", delete_after=10)
            await self.delete_command_message(ctx)
            return

        player = self.get_player(ctx)
        if not player:
            return await ctx.send("선생님, 음성 채널에 먼저 입장해주세요!", delete_after=10)

        if not ctx.voice_client:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                return await ctx.send("선생님, 음성 채널에 먼저 입장해주세요!", delete_after=10)

        for url in self.playlists[user_id][name]:
            await player.queue.put(url)

        await ctx.send(f"선생님의 '{name}' 플레이리스트의 모든 곡을 대기열에 추가했어요!", delete_after=10)
        await self.delete_command_message(ctx)
        if not player.is_playing:
            await player.play_next()

    @commands.command()
    @commands.has_permissions(manage_messages=True)
    async def 삭제(self, ctx, amount: int = 2):
        """지정된 수의 메시지를 삭제합니다."""
        if amount < 1:
            return await ctx.send("어라? 1개 이상의 메시지를 지정해 주셔야 해요. 아리스가 삭제할 수 있게요!", delete_after=10)

        deleted = await ctx.channel.purge(limit=amount + 1)  # 명령어 메시지도 포함해서 삭제
        await ctx.send(f"선생님, {len(deleted) - 1}개의 메시지를 깨끗하게 지웠어요! 아리스가 열심히 청소했답니다~", delete_after=5)
        await self.delete_command_message(ctx)

    @삭제.error
    async def 삭제_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("앗, 죄송해요 선생님. 이 명령어는 특별한 권한이 필요해요. 아리스가 도와드리고 싶어도 못 해드려요...", delete_after=10)
        elif isinstance(error, commands.BadArgument):
            await ctx.send("어머, 선생님? 숫자를 올바르게 입력해 주셔야 해요. 아리스가 이해할 수 있게요!", delete_after=10)
        await self.delete_command_message(ctx)

    @commands.command(name='도움말', aliases=['도움', 'help'])
    async def help_command(self, ctx):
        """모든 사용 가능한 명령어와 설명을 보여줍니다."""
        embed = discord.Embed(title="아리스의 명령어 목록", description="사용 가능한 모든 명령어와 설명이에요!", color=0x3498db)

        for command in self.bot.commands:
            if not command.hidden:
                embed.add_field(name=f"!{command.name}", value=command.help or "설명이 없어요.", inline=False)

        # 30초 후 자동으로 삭제되도록 설정
        message = await ctx.send(embed=embed)
        await asyncio.sleep(30)
        await message.delete(delay=2)

    @commands.command(aliases=['큐', '대기열'])
    async def queue(self, ctx):
        """현재 재생 목록을 표시합니다."""
        player = self.get_player(ctx)
        try:
            # 대기열이 비어있는지 확인
            if player.queue.empty():
                await ctx.send("앗, 선생님! 재생 목록이 비어있어요! 노래를 추가해주시면 아리스가 열심히 불러드릴게요~", delete_after=10)
                await self.delete_command_message(ctx)
                return

            # 대기열에서 요소 가져오기
            upcoming = list(player.queue._queue)  # 큐의 모든 요소 리스트로 가져오기

            # 곡 목록 형식화
            fmt_list = []
            for track in upcoming:
                if isinstance(track, dict) and 'title' in track:
                    # track이 딕셔너리일 경우 타이틀 정보 사용
                    fmt_list.append(f'**`{track["title"]}`**')
                elif isinstance(track, str):
                    # track이 문자열일 경우 (아마 URL일 가능성 높음)
                    fmt_list.append(f'**`{track}`**')  # URL을 출력하거나 추가적인 정보 필요시 수정 가능
                else:
                    fmt_list.append("**`알 수 없는 형식의 곡 정보`**")

            fmt = '\n'.join(fmt_list)

            # Embed 길이 제한 검사 및 수정
            if len(fmt) > 2048:
                fmt = fmt[:2000] + "\n... (너무 길어서 일부 내용만 보여드려요)"

            embed = discord.Embed(title=f'아리스의 재생 목록 - 총 {len(upcoming)}곡이에요!', description=fmt)
            await ctx.send(embed=embed, delete_after=10)

        except Exception as e:
            await ctx.send(f"대기열을 표시하는 중 오류가 발생했어요: {e}", delete_after=10)

        try:
            await self.delete_command_message(ctx)
        except Exception as e:
            print(f"delete_command_message 오류: {e}")

    @commands.command(name='재시작', aliases=['restart', 'try'])
    @commands.has_permissions(administrator=True)  # 관리자 권한 필요
    async def restart(self, ctx):
        """프로그램을 재시작합니다."""
        await ctx.send("아리스가 재시작할게요! 잠시만 기다려주세요...", delete_after=5)  # 수정된 부분
        self.restart_program()  # 프로그램 재시작 호출

    def restart_program(self):
        """현재 프로그램을 재시작합니다."""
        os.execv(sys.executable, ['python'] + sys.argv)

    @commands.command(name='종료봇', aliases=['exit'])
    @commands.has_permissions(administrator=True)  # 관리자 권한 필요
    async def exit_bot(self, ctx):
        """봇을 종료합니다."""
        await ctx.send("아리스가 종료될게요! 안녕히 가세요!", delete_after=10)
        await self.bot.close()  # 봇 종료

    @commands.command(name='플레이리스트삭제', aliases=['플래이리스트삭제', 'playrestdelete'])
    async def 플레이리스트삭제(self, ctx):
        """선생님의 플레이리스트 목록을 보여주고 삭제할 수 있어요."""
        user_id = str(ctx.author.id)
        if user_id not in self.playlists or not self.playlists[user_id]:
            await ctx.send("선생님, 아직 플레이리스트가 없어요. 새로 만들어볼까요?", delete_after=10)
            return

        view = View()
        select = Select(placeholder="삭제할 플레이리스트를 선택하세요",
                        options=[discord.SelectOption(label=name, value=name) for name in
                                 self.playlists[user_id].keys()])

        async def select_callback(interaction):
            playlist_name = select.values[0]
            confirm_view = View()

            async def delete_callback(delete_interaction):
                del self.playlists[user_id][playlist_name]
                self.save_playlists()
                await delete_interaction.response.send_message(f"선생님의 '{playlist_name}' 플레이리스트가 삭제되었어요!",
                                                               ephemeral=True, delete_after=5)

            async def cancel_callback(cancel_interaction):
                await cancel_interaction.response.send_message("플레이리스트 삭제가 취소되었어요.", ephemeral=True, delete_after=10)

            delete_button = Button(label="삭제하기", style=discord.ButtonStyle.danger)
            cancel_button = Button(label="취소하기", style=discord.ButtonStyle.secondary)

            delete_button.callback = delete_callback
            cancel_button.callback = cancel_callback

            confirm_view.add_item(delete_button)
            confirm_view.add_item(cancel_button)

            await interaction.response.send_message(f"선생님의 '{playlist_name}' 플레이리스트를 삭제할까요?", view=confirm_view,
                                                    ephemeral=True, delete_after=30)

        select.callback = select_callback
        view.add_item(select)

        await ctx.send("선생님의 플레이리스트 목록이에요:", view=view, delete_after=60)

    @commands.command(name='플레이리스트노래삭제', aliases=['프래이리스트노래삭제'])
    async def 플레이리스트노래삭제(self, ctx):
        """선생님의 플레이리스트에서 특정 노래를 삭제해요."""
        user_id = str(ctx.author.id)
        if user_id not in self.playlists or not self.playlists[user_id]:
            await ctx.send("선생님, 아직 플레이리스트가 없어요. 새로 만들어볼까요?", delete_after=10)
            return

        # 플레이리스트 선택을 위한 선택지 생성
        playlist_options = [discord.SelectOption(label=name, value=name) for name in self.playlists[user_id].keys()]
        playlist_select = Select(placeholder="삭제할 플레이리스트를 선택하세요", options=playlist_options)

        async def playlist_select_callback(interaction):
            selected_playlist_name = playlist_select.values[0]
            selected_playlist = self.playlists[user_id][selected_playlist_name]

            if not selected_playlist:
                await interaction.response.send_message(f"선생님, '{selected_playlist_name}' 플레이리스트에 노래가 없어요.",
                                                        ephemeral=True, delete_after=5)
                return

            # 노래 선택을 위한 선택지 생성
            song_options = [discord.SelectOption(label=f"{i + 1}. {url}", value=str(i)) for i, url in
                            enumerate(selected_playlist)]
            song_select = Select(placeholder="삭제할 노래를 선택하세요", options=song_options)

            async def song_select_callback(interaction):
                index = int(song_select.values[0])  # 선택된 값은 인덱스
                removed_song = selected_playlist[index]  # 삭제할 노래 저장

                # 삭제 확인을 위한 버튼 추가
                confirm_view = View()
                confirm_button = Button(label="삭제하기", style=discord.ButtonStyle.danger)
                cancel_button = Button(label="취소하기", style=discord.ButtonStyle.secondary)

                async def confirm_callback(confirm_interaction):
                    selected_playlist.pop(index)  # 선택된 노래 삭제
                    self.save_playlists()
                    await confirm_interaction.response.send_message(
                        f"선생님의 '{selected_playlist_name}' 플레이리스트에서 '{removed_song}' 노래가 삭제되었어요!", ephemeral=True,
                        delete_after=5)

                async def cancel_callback(cancel_interaction):
                    await cancel_interaction.response.send_message("노래 삭제가 취소되었어요.", ephemeral=True, delete_after=5)

                confirm_button.callback = confirm_callback
                cancel_button.callback = cancel_callback

                confirm_view.add_item(confirm_button)
                confirm_view.add_item(cancel_button)

                await interaction.response.send_message(f"정말로 '{removed_song}' 노래를 삭제할까요?", view=confirm_view,
                                                        ephemeral=True, delete_after=60)

            song_select.callback = song_select_callback
            song_view = View()
            song_view.add_item(song_select)

            await interaction.response.send_message(f"선생님의 '{selected_playlist_name}' 플레이리스트에서 삭제할 노래를 선택하세요:",
                                                    view=song_view, ephemeral=True, delete_after=60)

        playlist_select.callback = playlist_select_callback
        playlist_view = View()
        playlist_view.add_item(playlist_select)

        await ctx.send("선생님의 플레이리스트 목록이에요:", view=playlist_view, delete_after=60)

    @commands.command(name='히스토리', aliases=['history', '기록', 'record'])
    async def show_history(self, ctx, page: int = 1):
        """재생 기록을 보여줍니다."""
        guild_id_str = str(ctx.guild.id)
        
        if guild_id_str not in self.history or not self.history[guild_id_str]:
            await ctx.send("선생님, 아직 재생 기록이 없어요! 노래를 들어보시면 기록이 남을 거예요~", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        history_list = self.history[guild_id_str]
        per_page = 10
        total_pages = (len(history_list) - 1) // per_page + 1
        
        if page < 1 or page > total_pages:
            page = 1
        
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        page_history = history_list[start_idx:end_idx]
        
        embed = discord.Embed(
            title=f"🎵 아리스의 재생 기록 (페이지 {page}/{total_pages})",
            description=f"총 {len(history_list)}곡의 기록이 있어요!",
            color=0x3498db
        )
        
        for i, item in enumerate(page_history, start=start_idx + 1):
            embed.add_field(
                name=f"{i}. {item['title']}",
                value=f"재생 시간: {item['played_at']}\n[노래 듣기]({item['url']})",
                inline=False
            )
        
        # 버튼 추가
        view = View(timeout=60)
        
        if page > 1:
            prev_button = Button(label="이전 페이지", style=discord.ButtonStyle.secondary)
            async def prev_callback(interaction):
                await interaction.response.defer()
                await self.show_history.callback(self, ctx, page - 1)
            prev_button.callback = prev_callback
            view.add_item(prev_button)
        
        if page < total_pages:
            next_button = Button(label="다음 페이지", style=discord.ButtonStyle.secondary)
            async def next_callback(interaction):
                await interaction.response.defer()
                await self.show_history.callback(self, ctx, page + 1)
            next_button.callback = next_callback
            view.add_item(next_button)
        
        await ctx.send(embed=embed, view=view, delete_after=60)
        await self.delete_command_message(ctx)

    @commands.command(name='다시재생', aliases=['replay', '재재생', 'playagain'])
    async def replay_from_history(self, ctx, index: int = 1):
        """히스토리에서 선택한 노래를 다시 재생합니다."""
        guild_id_str = str(ctx.guild.id)
        
        if guild_id_str not in self.history or not self.history[guild_id_str]:
            await ctx.send("선생님, 아직 재생 기록이 없어요! 노래를 들어보시면 기록이 남을 거예요~", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        history_list = self.history[guild_id_str]
        
        if index < 1 or index > len(history_list):
            await ctx.send(f"선생님, 1부터 {len(history_list)} 사이의 번호를 입력해주세요!", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        # 히스토리에서 선택된 곡 가져오기
        selected_song = history_list[index - 1]
        url = selected_song['url']
        title = selected_song['title']
        
        # 음성 채널 연결 확인
        player = self.get_player(ctx)
        if not player:
            await ctx.send("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 따라갈게요~", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        if not ctx.voice_client:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                await ctx.send("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 어디로 가야 할지 모르겠어요~", delete_after=10)
                await self.delete_command_message(ctx)
                return
        
        # 대기열에 추가
        await player.queue.put(url)
        
        if not player.is_playing:
            await ctx.send(f"선생님, 히스토리에서 '{title}'을(를) 다시 재생할게요!", delete_after=10)
            await player.play_next()
        else:
            await ctx.send(f"선생님, 히스토리에서 '{title}'을(를) 대기열에 추가했어요!", delete_after=10)
        
        await self.delete_command_message(ctx)

    @commands.command(name='히스토리삭제', aliases=['clearhistory', '기록삭제'])
    async def clear_history(self, ctx):
        """재생 기록을 삭제합니다."""
        guild_id_str = str(ctx.guild.id)
        
        if guild_id_str not in self.history or not self.history[guild_id_str]:
            await ctx.send("선생님, 삭제할 재생 기록이 없어요!", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        # 확인 버튼 추가
        view = View(timeout=30)
        
        async def confirm_callback(interaction):
            self.history[guild_id_str] = []
            self.save_history()
            await interaction.response.send_message("선생님의 재생 기록을 모두 삭제했어요!", ephemeral=True, delete_after=5)
        
        async def cancel_callback(interaction):
            await interaction.response.send_message("기록 삭제가 취소되었어요.", ephemeral=True, delete_after=5)
        
        confirm_button = Button(label="삭제하기", style=discord.ButtonStyle.danger)
        cancel_button = Button(label="취소하기", style=discord.ButtonStyle.secondary)
        
        confirm_button.callback = confirm_callback
        cancel_button.callback = cancel_callback
        
        view.add_item(confirm_button)
        view.add_item(cancel_button)
        
        await ctx.send(f"선생님, 정말로 {len(self.history[guild_id_str])}개의 재생 기록을 모두 삭제할까요?", view=view, delete_after=30)
        await self.delete_command_message(ctx)

    @commands.command(name='큐로그', aliases=['queuelog', '대기열로그'])
    async def queue_log(self, ctx):
        """현재 대기열의 노래들을 로그로 저장합니다."""
        player = self.get_player(ctx)
        
        if player.queue.empty():
            await ctx.send("선생님, 현재 대기열이 비어있어요! 노래를 추가해주세요~", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        # 대기열 내용을 리스트로 변환
        queue_list = list(player.queue._queue)
        
        # 현재 시간으로 로그 파일명 생성
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_filename = f'queue_log_{timestamp}.txt'
        
        try:
            with open(log_filename, 'w', encoding='utf-8') as f:
                f.write(f"아리스의 대기열 로그 - {datetime.now().strftime('%Y년 %m월 %d일 %H시 %M분 %S초')}\n")
                f.write("=" * 50 + "\n\n")
                
                for i, track in enumerate(queue_list, 1):
                    if isinstance(track, dict):
                        f.write(f"{i}. {track.get('title', '알 수 없는 제목')}\n")
                        f.write(f"   URL: {track.get('webpage_url', track.get('url', ''))}\n\n")
                    else:
                        f.write(f"{i}. {track}\n\n")
            
            await ctx.send(f"선생님, 대기열 {len(queue_list)}곡을 '{log_filename}' 파일로 저장했어요!", delete_after=10)
            
        except Exception as e:
            await ctx.send(f"로그 저장 중 오류가 발생했어요: {str(e)}", delete_after=10)
        
        await self.delete_command_message(ctx)

    @commands.command(name='날짜별재생', aliases=['dateplay', '날짜재생', 'playbydate'])
    async def play_by_date(self, ctx, date_str: str = None):
        """특정 날짜에 들었던 노래들을 모두 대기열에 추가합니다. 형식: YYYY-MM-DD"""
        guild_id_str = str(ctx.guild.id)
        
        if guild_id_str not in self.history or not self.history[guild_id_str]:
            await ctx.send("선생님, 아직 재생 기록이 없어요! 노래를 들어보시면 기록이 남을 거예요~", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        # 날짜가 제공되지 않은 경우 사용 가능한 날짜 목록 표시
        if date_str is None:
            await self.show_available_dates(ctx)
            return
        
        # 날짜 형식 검증
        try:
            from datetime import datetime
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            await ctx.send("선생님, 날짜 형식이 올바르지 않아요! YYYY-MM-DD 형식으로 입력해주세요. (예: 2024-01-15)", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        # 해당 날짜의 노래들 찾기
        target_date_str = target_date.strftime('%Y-%m-%d')
        songs_for_date = []
        
        for song in self.history[guild_id_str]:
            song_date = song['played_at'][:10]  # YYYY-MM-DD 부분만 추출
            if song_date == target_date_str:
                songs_for_date.append(song)
        
        if not songs_for_date:
            await ctx.send(f"선생님, {target_date_str}에 들었던 노래가 없어요!", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        # 음성 채널 연결 확인
        player = self.get_player(ctx)
        if not player:
            await ctx.send("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 따라갈게요~", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        if not ctx.voice_client:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                await ctx.send("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 어디로 가야 할지 모르겠어요~", delete_after=10)
                await self.delete_command_message(ctx)
                return
        
        # 중복 제거 (같은 노래가 여러 번 재생된 경우)
        unique_songs = []
        seen_urls = set()
        for song in songs_for_date:
            if song['url'] not in seen_urls:
                unique_songs.append(song)
                seen_urls.add(song['url'])
        
        # 대기열에 추가
        added_count = 0
        for song in unique_songs:
            try:
                await player.queue.put(song['url'])
                added_count += 1
            except Exception as e:
                print(f"노래 추가 중 오류: {e}")
        
        # 재생 시작
        if not player.is_playing and added_count > 0:
            await ctx.send(f"선생님, {target_date_str}에 들었던 {added_count}곡을 대기열에 추가하고 재생할게요!", delete_after=10)
            await player.play_next()
        else:
            await ctx.send(f"선생님, {target_date_str}에 들었던 {added_count}곡을 대기열에 추가했어요!", delete_after=10)
        
        await self.delete_command_message(ctx)

    async def show_available_dates(self, ctx):
        """재생 기록이 있는 날짜들을 선택할 수 있도록 표시합니다."""
        guild_id_str = str(ctx.guild.id)
        history_list = self.history[guild_id_str]
        
        # 날짜별로 노래 수 집계
        date_counts = {}
        for song in history_list:
            date_key = song['played_at'][:10]  # YYYY-MM-DD 부분만 추출
            if date_key not in date_counts:
                date_counts[date_key] = 0
            date_counts[date_key] += 1
        
        # 날짜순으로 정렬 (최신순)
        sorted_dates = sorted(date_counts.items(), reverse=True)
        
        if not sorted_dates:
            await ctx.send("선생님, 아직 재생 기록이 없어요!", delete_after=10)
            return
        
        # 최대 25개 날짜만 표시 (Discord Select 제한)
        available_dates = sorted_dates[:25]
        
        embed = discord.Embed(
            title="📅 날짜별 재생 기록",
            description="재생하고 싶은 날짜를 선택해주세요!",
            color=0x3498db
        )
        
        view = View(timeout=60)
        
        # Select 옵션 생성
        options = []
        for date_str, count in available_dates:
            # 날짜를 더 읽기 쉽게 포맷
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                formatted_date = date_obj.strftime('%Y년 %m월 %d일')
                options.append(discord.SelectOption(
                    label=f"{formatted_date} ({count}곡)",
                    value=date_str,
                    description=f"이 날 {count}곡을 들었어요"
                ))
            except:
                options.append(discord.SelectOption(
                    label=f"{date_str} ({count}곡)",
                    value=date_str
                ))
        
        select = Select(placeholder="날짜를 선택하세요", options=options)
        
        async def date_select_callback(interaction):
            selected_date = select.values[0]
            
            # 선택된 날짜의 노래 목록 보여주기
            songs_for_date = []
            for song in history_list:
                if song['played_at'][:10] == selected_date:
                    songs_for_date.append(song)
            
            # 중복 제거
            unique_songs = []
            seen_urls = set()
            for song in songs_for_date:
                if song['url'] not in seen_urls:
                    unique_songs.append(song)
                    seen_urls.add(song['url'])
            
            # 날짜 포맷팅
            try:
                date_obj = datetime.strptime(selected_date, '%Y-%m-%d')
                formatted_date = date_obj.strftime('%Y년 %m월 %d일')
            except:
                formatted_date = selected_date
            
            song_list = "\n".join([f"{i+1}. {song['title']}" for i, song in enumerate(unique_songs[:10])])
            if len(unique_songs) > 10:
                song_list += f"\n... 외 {len(unique_songs) - 10}곡"
            
            confirm_view = View(timeout=30)
            
            async def add_all_callback(add_interaction):
                # 음성 채널 연결 확인
                player = self.get_player(ctx)
                if not ctx.voice_client:
                    if ctx.author.voice:
                        await ctx.author.voice.channel.connect()
                    else:
                        await add_interaction.response.send_message(
                            "선생님, 음성 채널에 먼저 입장해주세요!", ephemeral=True, delete_after=5)
                        return
                
                # 대기열에 추가
                added_count = 0
                for song in unique_songs:
                    try:
                        await player.queue.put(song['url'])
                        added_count += 1
                    except Exception as e:
                        print(f"노래 추가 중 오류: {e}")
                
                # 재생 시작
                if not player.is_playing and added_count > 0:
                    await add_interaction.response.send_message(
                        f"선생님, {formatted_date}에 들었던 {added_count}곡을 대기열에 추가하고 재생할게요!", 
                        ephemeral=True, delete_after=5)
                    await player.play_next()
                else:
                    await add_interaction.response.send_message(
                        f"선생님, {formatted_date}에 들었던 {added_count}곡을 대기열에 추가했어요!", 
                        ephemeral=True, delete_after=5)
            
            async def cancel_callback(cancel_interaction):
                await cancel_interaction.response.send_message("취소되었어요.", ephemeral=True, delete_after=3)
            
            add_button = Button(label=f"{len(unique_songs)}곡 모두 추가", style=discord.ButtonStyle.success)
            cancel_button = Button(label="취소", style=discord.ButtonStyle.secondary)
            
            add_button.callback = add_all_callback
            cancel_button.callback = cancel_callback
            
            confirm_view.add_item(add_button)
            confirm_view.add_item(cancel_button)
            
            await interaction.response.send_message(
                f"**{formatted_date}에 들었던 노래들:**\n```{song_list}```\n이 노래들을 대기열에 추가할까요?",
                view=confirm_view, ephemeral=True, delete_after=30)
        
        select.callback = date_select_callback
        view.add_item(select)
        
        await ctx.send(embed=embed, view=view, delete_after=60)
        await self.delete_command_message(ctx)

    @commands.command(name='이번주재생', aliases=['weekplay', '주간재생'])
    async def play_this_week(self, ctx):
        """이번 주에 들었던 노래들을 모두 대기열에 추가합니다."""
        guild_id_str = str(ctx.guild.id)
        
        if guild_id_str not in self.history or not self.history[guild_id_str]:
            await ctx.send("선생님, 아직 재생 기록이 없어요! 노래를 들어보시면 기록이 남을 거예요~", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        # 이번 주 시작과 끝 날짜 계산
        from datetime import datetime, timedelta
        today = datetime.now().date()
        start_of_week = today - timedelta(days=today.weekday())  # 월요일
        end_of_week = start_of_week + timedelta(days=6)  # 일요일
        
        # 이번 주의 노래들 찾기
        songs_this_week = []
        for song in self.history[guild_id_str]:
            try:
                song_date = datetime.strptime(song['played_at'][:10], '%Y-%m-%d').date()
                if start_of_week <= song_date <= end_of_week:
                    songs_this_week.append(song)
            except:
                continue
        
        if not songs_this_week:
            await ctx.send(f"선생님, 이번 주({start_of_week.strftime('%m월 %d일')} ~ {end_of_week.strftime('%m월 %d일')})에 들었던 노래가 없어요!", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        # 음성 채널 연결 확인
        player = self.get_player(ctx)
        if not player:
            await ctx.send("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 따라갈게요~", delete_after=10)
            await self.delete_command_message(ctx)
            return
        
        if not ctx.voice_client:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                await ctx.send("선생님, 음성 채널에 먼저 입장해주세요! 아리스가 어디로 가야 할지 모르겠어요~", delete_after=10)
                await self.delete_command_message(ctx)
                return
        
        # 중복 제거
        unique_songs = []
        seen_urls = set()
        for song in songs_this_week:
            if song['url'] not in seen_urls:
                unique_songs.append(song)
                seen_urls.add(song['url'])
        
        # 대기열에 추가
        added_count = 0
        for song in unique_songs:
            try:
                await player.queue.put(song['url'])
                added_count += 1
            except Exception as e:
                print(f"노래 추가 중 오류: {e}")
        
        # 재생 시작
        if not player.is_playing and added_count > 0:
            await ctx.send(f"선생님, 이번 주에 들었던 {added_count}곡을 대기열에 추가하고 재생할게요!", delete_after=10)
            await player.play_next()
        else:
            await ctx.send(f"선생님, 이번 주에 들었던 {added_count}곡을 대기열에 추가했어요!", delete_after=10)
        
        await self.delete_command_message(ctx)


async def main():
    async with bot:
        await bot.add_cog(Music(bot))
        await bot.add_cog(YachtDiceGame(bot))
        await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())