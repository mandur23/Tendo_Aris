import os
import sys
import asyncio
import logging
import discord
from discord.ext import commands
from discord.app_commands import command, describe, choices, Choice
from discord.ui import Button, View, Select
from datetime import datetime, timedelta
from core.music_player import MusicPlayer
from utils.ytdl_utils import create_ytdl_instance, ytdl_format_options
from utils.file_utils import load_history, save_history, load_playlists, save_playlists, ensure_logs_dir, LOGS_DIR
from utils.config import MAX_HISTORY_ITEMS, MAX_EXTRACT_RETRIES, COMMAND_MESSAGE_DELETE_DELAY, USE_MYSQL
from utils.discord_utils import delete_command_message, ensure_voice_client, safe_send, handle_extract_error, ConfirmView, AuthorLockedView
from utils.db_utils import (
    init_db_pool, close_db_pool, load_history_from_db, save_history_to_db,
    add_history_item_to_db, get_history_from_db, load_playlists_from_db,
    save_playlists_to_db, get_playlist_from_db, save_playlist_to_db
)

logger = logging.getLogger(__name__)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}
        self.playlists = {} if USE_MYSQL else load_playlists()
        self.history = {} if USE_MYSQL else load_history()
        ensure_logs_dir()
        
        # Slash Commands 그룹은 cog_load에서 생성
        self.music_group = None
        self.playlist_group = None
        self.history_group = None

    async def cog_load(self):
        """Cog가 로드될 때 실행됩니다."""
        if USE_MYSQL:
            try:
                await init_db_pool()
                # 비동기로 데이터 로드
                self.history = await load_history_from_db()
                self.playlists = await load_playlists_from_db()
                logger.info("MySQL에서 히스토리와 플레이리스트를 로드했습니다.")
            except Exception as e:
                logger.error(f"MySQL 초기화 실패: {e}")
                logger.warning("JSON 파일 사용 모드로 전환합니다.")

    async def cog_unload(self):
        """Cog가 언로드될 때 실행됩니다."""
        if USE_MYSQL:
            await close_db_pool()

    async def save_history(self):
        """히스토리를 저장합니다."""
        if USE_MYSQL:
            await save_history_to_db(self.history)
        else:
            save_history(self.history)

    async def save_playlists(self):
        """플레이리스트를 저장합니다."""
        if USE_MYSQL:
            await save_playlists_to_db(self.playlists)
        else:
            save_playlists(self.playlists)

    async def add_to_history(self, guild_id, source):
        """재생된 노래를 히스토리에 추가합니다. (오류 처리 개선)"""
        try:
            if USE_MYSQL:
                # MySQL 사용 시 직접 DB에 추가
                title = source.get('title', '알 수 없는 제목')
                url = source.get('webpage_url', source.get('url', ''))
                duration = source.get('duration', 0)
                await add_history_item_to_db(guild_id, title, url, duration)
                
                # 메모리 캐시 업데이트
                guild_id_str = str(guild_id)
                if guild_id_str not in self.history:
                    self.history[guild_id_str] = []
                
                history_item = {
                    'title': title,
                    'url': url,
                    'duration': duration,
                    'played_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                
                self.history[guild_id_str].insert(0, history_item)
                if len(self.history[guild_id_str]) > MAX_HISTORY_ITEMS:
                    self.history[guild_id_str] = self.history[guild_id_str][:MAX_HISTORY_ITEMS]
            else:
                # JSON 파일 사용
                guild_id_str = str(guild_id)
                if guild_id_str not in self.history:
                    self.history[guild_id_str] = []
                
                history_item = {
                    'title': source.get('title', '알 수 없는 제목'),
                    'url': source.get('webpage_url', source.get('url', '')),
                    'duration': source.get('duration', 0),
                    'played_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                
                self.history[guild_id_str].insert(0, history_item)
                
                if len(self.history[guild_id_str]) > MAX_HISTORY_ITEMS:
                    self.history[guild_id_str] = self.history[guild_id_str][:MAX_HISTORY_ITEMS]
                
                save_history(self.history)
        except Exception as e:
            logger.error(f"히스토리 저장 중 오류: {e}")

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


    # ========== 음악 재생 관련 명령어 ==========
    
    @commands.command(name='play', aliases=['p', '재생', '플레이'])
    async def play_command(self, ctx, *, url):
        """YouTube URL을 재생합니다. (URL 검증 개선)"""
        async with ctx.typing():
            try:
                if not url.startswith(('http://', 'https://', 'ytsearch:')):
                    url = f"ytsearch:{url}"
                
                player = self.get_player(ctx)
                
                if not await ensure_voice_client(ctx):
                    return
                
                await player.queue.put(url)
                
                title = url
                try:
                    for retry in range(MAX_EXTRACT_RETRIES):
                        try:
                            extract_options = ytdl_format_options.copy()
                            if retry > 0:
                                extract_options['socket_timeout'] = 30 + (retry * 10)
                                await asyncio.sleep(retry * 2)
                            
                            ydl = create_ytdl_instance(extract_options)
                            info = await self.bot.loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                            title = info.get('title', url)
                            break
                        except Exception as retry_error:
                            error_msg = handle_extract_error(retry_error, MAX_EXTRACT_RETRIES, retry)
                            if error_msg:
                                title = error_msg
                                break
                            # 재시도 가능한 에러면 continue
                            continue
                except Exception as extract_error:
                    error_msg = handle_extract_error(extract_error, MAX_EXTRACT_RETRIES, MAX_EXTRACT_RETRIES)
                    title = error_msg or "❓ 알 수 없는 영상"
                
                if not player.is_playing:
                    await ctx.send(f"선생님, '{title}'을(를) 재생할게요!", delete_after=10)
                else:
                    await ctx.send(f"선생님, '{title}'을(를) 대기열에 추가했어요! 지금 재생 중인 곡이 끝나면 재생할게요~", delete_after=10)
                
                await delete_command_message(ctx)
                    
            except Exception as e:
                await ctx.send(f"선생님, 재생 중 오류가 발생했어요: {str(e)[:100]}...", delete_after=12)
                logger.error(f"재생 명령어 오류: {e}")

    @commands.command(aliases=['종료'])
    async def stop(self, ctx):
        """음악 재생을 종료하고 봇을 음성 채널에서 내보냅니다."""
        player = self.get_player(ctx)
        await player.stop()
        await ctx.send("선생님, 아리스가 음악 재생을 종료하고 음성 채널에서 나갔어요~ 다음에 또 불러주세요!", delete_after=10)
        await delete_command_message(ctx)

    @commands.command(aliases=['나가!'])
    async def leave(self, ctx):
        """봇을 음성 채널에서 내보냅니다."""
        await self.stop(ctx)

    @commands.command(aliases=['볼륨'])
    async def volume(self, ctx, volume: int):
        """볼륨을 설정합니다. (0-100)"""
        if ctx.voice_client is None:
            await safe_send(ctx, "어라? 아리스가 아직 음성 채널에 없어요. 먼저 들어가 볼게요!", delete_after=10)
            await delete_command_message(ctx)
            return

        player = self.get_player(ctx)
        if 0 <= volume <= 100:
            player.volume = volume / 100
            ctx.voice_client.source.volume = player.volume
            await ctx.send(f"볼륨을 {volume}%로 맞췄어요! 이제 잘 들리나요?", delete_after=10)
        else:
            await ctx.send("앗, 볼륨은 0에서 100 사이로 해주세요~ 아리스의 귀가 아파요!", delete_after=12)
        await delete_command_message(ctx)

    @commands.command(aliases=['큐', '대기열'])
    async def queue(self, ctx):
        """현재 재생 목록을 표시합니다."""
        player = self.get_player(ctx)
        try:
            if player.queue.empty():
                await ctx.send("앗, 선생님! 재생 목록이 비어있어요! 노래를 추가해주시면 아리스가 열심히 불러드릴게요~", delete_after=10)
                await delete_command_message(ctx)
                return

            upcoming = list(player.queue._queue)

            fmt_list = []
            for track in upcoming:
                if isinstance(track, dict) and 'title' in track:
                    fmt_list.append(f'**`{track["title"]}`**')
                elif isinstance(track, str):
                    fmt_list.append(f'**`{track}`**')
                else:
                    fmt_list.append("**`알 수 없는 형식의 곡 정보`**")

            fmt = '\n'.join(fmt_list)

            if len(fmt) > 2048:
                fmt = fmt[:2000] + "\n... (너무 길어서 일부 내용만 보여드려요)"

            embed = discord.Embed(title=f'아리스의 재생 목록 - 총 {len(upcoming)}곡이에요!', description=fmt)
            await ctx.send(embed=embed, delete_after=10)

        except Exception as e:
            await ctx.send(f"대기열을 표시하는 중 오류가 발생했어요: {e}", delete_after=12)

        await delete_command_message(ctx)

    # ========== 플레이리스트 관련 명령어 ==========

    @commands.command(aliases=['플레이리스트목록', '플래이리스트', 'vmffpdlfltmxm'])
    async def 플레이리스트(self, ctx):
        """선생님의 플레이리스트 목록을 보여드려요."""
        user_id = str(ctx.author.id)
        if user_id not in self.playlists or not self.playlists[user_id]:
            await ctx.send("선생님, 아직 플레이리스트가 없어요. 새로 만들어볼까요?", delete_after=10)
            await delete_command_message(ctx)
            return

        view = View(timeout=60)
        
        async def view_timeout():
            for item in view.children:
                item.disabled = True
            try:
                await view.message.edit(view=view)
            except (discord.NotFound, discord.HTTPException):
                pass
        
        view.on_timeout = view_timeout
        
        select = Select(placeholder="플레이리스트를 선택하세요", options=[discord.SelectOption(label=name, value=name) for name in
                                                              self.playlists[user_id].keys()])

        async def select_callback(interaction):
            # 권한 검증
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("선생님, 다른 사람의 플레이리스트를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                return
            
            playlist_name = select.values[0]
            playlist = self.playlists[user_id][playlist_name]
            playlist_str = "\n".join(f"{i + 1}. {url}" for i, url in enumerate(playlist))

            confirm_view = View(timeout=30)
            
            async def confirm_timeout():
                for item in confirm_view.children:
                    item.disabled = True
                try:
                    await confirm_view.message.edit(view=confirm_view)
                except (discord.NotFound, discord.HTTPException):
                    pass
            
            confirm_view.on_timeout = confirm_timeout

            async def add_to_queue_callback(add_interaction):
                # 권한 검증
                if add_interaction.user.id != ctx.author.id:
                    await add_interaction.response.send_message("선생님, 다른 사람의 플레이리스트를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                    return
                
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
                # 권한 검증
                if cancel_interaction.user.id != ctx.author.id:
                    await cancel_interaction.response.send_message("선생님, 다른 사람의 플레이리스트를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                    return
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
        await delete_command_message(ctx)

    @commands.command(name='플레이리스트추가', aliases=['프래이리스트추가', 'vmffpdlfltmxmcnrk'])
    async def 플레이리스트추가(self, ctx, name: str, *urls):
        """선생님의 플레이리스트에 새 플레이리스트를 추가하거나 기존 플레이리스트에 곡을 추가해요."""
        if not urls:
            await ctx.send("선생님, URL을 하나 이상 입력해주세요!", delete_after=10)
            await delete_command_message(ctx)
            return

        user_id = str(ctx.author.id)
        if user_id not in self.playlists:
            self.playlists[user_id] = {}

        if name not in self.playlists[user_id]:
            self.playlists[user_id][name] = []

        # 중복 URL 제거
        existing_urls = set(self.playlists[user_id][name])
        new_urls = [url for url in urls if url not in existing_urls]
        
        if not new_urls:
            await ctx.send(f"선생님, '{name}' 플레이리스트에 이미 모든 URL이 추가되어 있어요!", delete_after=10)
            await delete_command_message(ctx)
            return
        
        self.playlists[user_id][name].extend(new_urls)
        await self.save_playlists()
        await ctx.send(f"선생님의 '{name}' 플레이리스트에 {len(new_urls)}개의 곡을 추가했어요! (중복 제외: {len(urls) - len(new_urls)}개)", delete_after=10)
        await delete_command_message(ctx)

    @commands.command(name='플레이리스트재생', aliases=['플래이리스트재생', 'vmffpdlfltmxmwotod'])
    async def 플레이리스트재생(self, ctx, name: str):
        """선생님이 선택한 플레이리스트를 재생해요."""
        user_id = str(ctx.author.id)
        if user_id not in self.playlists or name not in self.playlists[user_id]:
            await ctx.send(f"선생님, '{name}' 플레이리스트를 찾을 수 없어요.", delete_after=10)
            await delete_command_message(ctx)
            return

        if not await ensure_voice_client(ctx):
            return

        player = self.get_player(ctx)

        for url in self.playlists[user_id][name]:
            await player.queue.put(url)

        await ctx.send(f"선생님의 '{name}' 플레이리스트의 모든 곡을 대기열에 추가했어요!", delete_after=10)
        await delete_command_message(ctx)
        if not player.is_playing:
            await player.play_next()

    @commands.command(name='플레이리스트삭제', aliases=['플래이리스트삭제', 'playrestdelete'])
    async def 플레이리스트삭제(self, ctx):
        """선생님의 플레이리스트 목록을 보여주고 삭제할 수 있어요."""
        user_id = str(ctx.author.id)
        if user_id not in self.playlists or not self.playlists[user_id]:
            await ctx.send("선생님, 아직 플레이리스트가 없어요. 새로 만들어볼까요?", delete_after=10)
            await delete_command_message(ctx)
            return

        view = View(timeout=60)
        
        async def view_timeout():
            for item in view.children:
                item.disabled = True
            try:
                await view.message.edit(view=view)
            except (discord.NotFound, discord.HTTPException):
                pass
        
        view.on_timeout = view_timeout
        
        select = Select(placeholder="삭제할 플레이리스트를 선택하세요",
                        options=[discord.SelectOption(label=name, value=name) for name in
                                 self.playlists[user_id].keys()])

        async def select_callback(interaction):
            # 권한 검증
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("선생님, 다른 사람의 플레이리스트를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                return
            
            playlist_name = select.values[0]
            
            async def on_confirm(confirm_interaction):
                del self.playlists[user_id][playlist_name]
                await self.save_playlists()
                await confirm_interaction.response.send_message(
                    f"선생님의 '{playlist_name}' 플레이리스트가 삭제되었어요!",
                    ephemeral=True,
                    delete_after=5
                )
            
            async def on_cancel(cancel_interaction):
                await cancel_interaction.response.send_message(
                    "플레이리스트 삭제가 취소되었어요.",
                    ephemeral=True,
                    delete_after=5
                )
            
            confirm_view = ConfirmView(
                author_id=ctx.author.id,
                confirm_label="삭제하기",
                cancel_label="취소하기",
                confirm_style=discord.ButtonStyle.danger,
                timeout=30.0,
                on_confirm=on_confirm,
                on_cancel=on_cancel
            )

            await interaction.response.send_message(
                f"선생님의 '{playlist_name}' 플레이리스트를 삭제할까요?",
                view=confirm_view,
                ephemeral=True,
                delete_after=30
            )

        select.callback = select_callback
        view.add_item(select)

        await ctx.send("선생님의 플레이리스트 목록이에요:", view=view, delete_after=60)
        await delete_command_message(ctx)

    @commands.command(name='플레이리스트노래삭제', aliases=['프래이리스트노래삭제'])
    async def 플레이리스트노래삭제(self, ctx):
        """선생님의 플레이리스트에서 특정 노래를 삭제해요."""
        user_id = str(ctx.author.id)
        if user_id not in self.playlists or not self.playlists[user_id]:
            await ctx.send("선생님, 아직 플레이리스트가 없어요. 새로 만들어볼까요?", delete_after=10)
            await delete_command_message(ctx)
            return

        playlist_options = [discord.SelectOption(label=name, value=name) for name in self.playlists[user_id].keys()]
        playlist_select = Select(placeholder="삭제할 플레이리스트를 선택하세요", options=playlist_options)

        playlist_view = View(timeout=60)
        
        async def playlist_view_timeout():
            for item in playlist_view.children:
                item.disabled = True
            try:
                await playlist_view.message.edit(view=playlist_view)
            except (discord.NotFound, discord.HTTPException):
                pass
        
        playlist_view.on_timeout = playlist_view_timeout

        async def playlist_select_callback(interaction):
            # 권한 검증
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("선생님, 다른 사람의 플레이리스트를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                return
            
            selected_playlist_name = playlist_select.values[0]
            selected_playlist = self.playlists[user_id][selected_playlist_name]

            if not selected_playlist:
                await interaction.response.send_message(f"선생님, '{selected_playlist_name}' 플레이리스트에 노래가 없어요.",
                                                        ephemeral=True, delete_after=5)
                return

            song_options = [discord.SelectOption(label=f"{i + 1}. {url}", value=str(i)) for i, url in
                            enumerate(selected_playlist)]
            song_select = Select(placeholder="삭제할 노래를 선택하세요", options=song_options)

            song_view = View(timeout=60)
            
            async def song_view_timeout():
                for item in song_view.children:
                    item.disabled = True
                try:
                    await song_view.message.edit(view=song_view)
                except (discord.NotFound, discord.HTTPException):
                    pass
            
            song_view.on_timeout = song_view_timeout

            async def song_select_callback(interaction):
                # 권한 검증
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("선생님, 다른 사람의 플레이리스트를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                    return
                
                index = int(song_select.values[0])
                removed_song = selected_playlist[index]

                async def on_confirm(confirm_interaction):
                    selected_playlist.pop(index)
                    await self.save_playlists()
                    await confirm_interaction.response.send_message(
                        f"선생님의 '{selected_playlist_name}' 플레이리스트에서 '{removed_song}' 노래가 삭제되었어요!",
                        ephemeral=True,
                        delete_after=5
                    )

                async def on_cancel(cancel_interaction):
                    await cancel_interaction.response.send_message(
                        "노래 삭제가 취소되었어요.",
                        ephemeral=True,
                        delete_after=5
                    )

                confirm_view = ConfirmView(
                    author_id=ctx.author.id,
                    confirm_label="삭제하기",
                    cancel_label="취소하기",
                    confirm_style=discord.ButtonStyle.danger,
                    timeout=30.0,
                    on_confirm=on_confirm,
                    on_cancel=on_cancel
                )

                await interaction.response.send_message(
                    f"정말로 '{removed_song}' 노래를 삭제할까요?",
                    view=confirm_view,
                    ephemeral=True,
                    delete_after=30
                )

            song_select.callback = song_select_callback
            song_view.add_item(song_select)

            await interaction.response.send_message(f"선생님의 '{selected_playlist_name}' 플레이리스트에서 삭제할 노래를 선택하세요:",
                                                    view=song_view, ephemeral=True, delete_after=60)

        playlist_select.callback = playlist_select_callback
        playlist_view.add_item(playlist_select)

        await ctx.send("선생님의 플레이리스트 목록이에요:", view=playlist_view, delete_after=60)
        await delete_command_message(ctx)

    # ========== 히스토리 관련 명령어 ==========

    @commands.command(name='히스토리', aliases=['history', '기록', 'record'])
    async def show_history(self, ctx, page: int = 1):
        """재생 기록을 보여줍니다."""
        guild_id_str = str(ctx.guild.id)
        
        if guild_id_str not in self.history or not self.history[guild_id_str]:
            await ctx.send("선생님, 아직 재생 기록이 없어요! 노래를 들어보시면 기록이 남을 거예요~", delete_after=10)
            await delete_command_message(ctx)
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
        
        view = View(timeout=60)
        
        async def view_timeout():
            for item in view.children:
                item.disabled = True
            try:
                await view.message.edit(view=view)
            except (discord.NotFound, discord.HTTPException):
                pass
        
        view.on_timeout = view_timeout
        
        if page > 1:
            prev_button = Button(label="이전 페이지", style=discord.ButtonStyle.secondary)
            async def prev_callback(interaction):
                # 권한 검증
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("선생님, 다른 사람의 히스토리를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                    return
                await interaction.response.defer()
                # 명령어를 재귀적으로 호출
                await self.show_history(ctx, page - 1)
            prev_button.callback = prev_callback
            view.add_item(prev_button)
        
        if page < total_pages:
            next_button = Button(label="다음 페이지", style=discord.ButtonStyle.secondary)
            async def next_callback(interaction):
                # 권한 검증
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("선생님, 다른 사람의 히스토리를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                    return
                await interaction.response.defer()
                # 명령어를 재귀적으로 호출
                await self.show_history(ctx, page + 1)
            next_button.callback = next_callback
            view.add_item(next_button)
        
        await ctx.send(embed=embed, view=view, delete_after=60)
        await delete_command_message(ctx)

    @commands.command(name='다시재생', aliases=['replay', '재재생', 'playagain'])
    async def replay_from_history(self, ctx, index: int = 1):
        """히스토리에서 선택한 노래를 다시 재생합니다."""
        guild_id_str = str(ctx.guild.id)
        
        if guild_id_str not in self.history or not self.history[guild_id_str]:
            await ctx.send("선생님, 아직 재생 기록이 없어요! 노래를 들어보시면 기록이 남을 거예요~", delete_after=10)
            await delete_command_message(ctx)
            return
        
        history_list = self.history[guild_id_str]
        
        if index < 1 or index > len(history_list):
            await ctx.send(f"선생님, 1부터 {len(history_list)} 사이의 번호를 입력해주세요!", delete_after=12)
            await delete_command_message(ctx)
            return
        
        selected_song = history_list[index - 1]
        url = selected_song['url']
        title = selected_song['title']
        
        if not await ensure_voice_client(ctx):
            return
        
        player = self.get_player(ctx)
        
        await player.queue.put(url)
        
        if not player.is_playing:
            await ctx.send(f"선생님, 히스토리에서 '{title}'을(를) 다시 재생할게요!", delete_after=10)
            await player.play_next()
        else:
            await ctx.send(f"선생님, 히스토리에서 '{title}'을(를) 대기열에 추가했어요!", delete_after=10)
        
        await delete_command_message(ctx)

    @commands.command(name='히스토리삭제', aliases=['clearhistory', '기록삭제'])
    async def clear_history(self, ctx):
        """재생 기록을 삭제합니다."""
        guild_id_str = str(ctx.guild.id)
        
        if guild_id_str not in self.history or not self.history[guild_id_str]:
            await ctx.send("선생님, 삭제할 재생 기록이 없어요!", delete_after=10)
            await delete_command_message(ctx)
            return
        
        async def on_confirm(interaction):
            self.history[guild_id_str] = []
            await self.save_history()
            await interaction.response.send_message(
                "선생님의 재생 기록을 모두 삭제했어요!",
                ephemeral=True,
                delete_after=5
            )
        
        async def on_cancel(interaction):
            await interaction.response.send_message(
                "기록 삭제가 취소되었어요.",
                ephemeral=True,
                delete_after=5
            )
        
        confirm_view = ConfirmView(
            author_id=ctx.author.id,
            confirm_label="삭제하기",
            cancel_label="취소하기",
            confirm_style=discord.ButtonStyle.danger,
            timeout=30.0,
            on_confirm=on_confirm,
            on_cancel=on_cancel
        )
        
        message = await ctx.send(
            f"선생님, 정말로 {len(self.history[guild_id_str])}개의 재생 기록을 모두 삭제할까요?",
            view=confirm_view,
            delete_after=30
        )
        confirm_view.message = message
        await delete_command_message(ctx)

    @commands.command(name='큐로그', aliases=['queuelog', '대기열로그'])
    async def queue_log(self, ctx):
        """현재 대기열의 노래들을 로그로 저장합니다."""
        player = self.get_player(ctx)
        
        if player.queue.empty():
            await ctx.send("선생님, 현재 대기열이 비어있어요! 노래를 추가해주세요~", delete_after=10)
            await delete_command_message(ctx)
            return
        
        queue_list = list(player.queue._queue)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_filename = LOGS_DIR / f'queue_log_{timestamp}.txt'
        
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
            
            await ctx.send(f"선생님, 대기열 {len(queue_list)}곡을 '{log_filename.name}' 파일로 저장했어요!", delete_after=10)
            
        except Exception as e:
            await ctx.send(f"로그 저장 중 오류가 발생했어요: {str(e)}", delete_after=12)
        
        await delete_command_message(ctx)

    @commands.command(name='날짜별재생', aliases=['dateplay', '날짜재생', 'playbydate'])
    async def play_by_date(self, ctx, date_str: str = None):
        """특정 날짜에 들었던 노래들을 모두 대기열에 추가합니다. 형식: YYYY-MM-DD"""
        guild_id_str = str(ctx.guild.id)
        
        if guild_id_str not in self.history or not self.history[guild_id_str]:
            await ctx.send("선생님, 아직 재생 기록이 없어요! 노래를 들어보시면 기록이 남을 거예요~", delete_after=10)
            await delete_command_message(ctx)
            return
        
        if date_str is None:
            await self.show_available_dates(ctx)
            return
        
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            await ctx.send("선생님, 날짜 형식이 올바르지 않아요! YYYY-MM-DD 형식으로 입력해주세요. (예: 2024-01-15)", delete_after=12)
            await delete_command_message(ctx)
            return
        
        target_date_str = target_date.strftime('%Y-%m-%d')
        songs_for_date = []
        
        for song in self.history[guild_id_str]:
            song_date = song['played_at'][:10]
            if song_date == target_date_str:
                songs_for_date.append(song)
        
        if not songs_for_date:
            await ctx.send(f"선생님, {target_date_str}에 들었던 노래가 없어요!", delete_after=10)
            await delete_command_message(ctx)
            return
        
        if not await ensure_voice_client(ctx):
            return
        
        player = self.get_player(ctx)
        
        unique_songs = []
        seen_urls = set()
        for song in songs_for_date:
            if song['url'] not in seen_urls:
                unique_songs.append(song)
                seen_urls.add(song['url'])
        
        added_count = 0
        for song in unique_songs:
            try:
                await player.queue.put(song['url'])
                added_count += 1
            except Exception as e:
                logger.error(f"노래 추가 중 오류: {e}")
        
        if not player.is_playing and added_count > 0:
            await ctx.send(f"선생님, {target_date_str}에 들었던 {added_count}곡을 대기열에 추가하고 재생할게요!", delete_after=10)
            await player.play_next()
        else:
            await ctx.send(f"선생님, {target_date_str}에 들었던 {added_count}곡을 대기열에 추가했어요!", delete_after=10)
        
        await delete_command_message(ctx)

    async def show_available_dates(self, ctx):
        """재생 기록이 있는 날짜들을 선택할 수 있도록 표시합니다."""
        guild_id_str = str(ctx.guild.id)
        history_list = self.history[guild_id_str]
        
        date_counts = {}
        for song in history_list:
            date_key = song['played_at'][:10]
            if date_key not in date_counts:
                date_counts[date_key] = 0
            date_counts[date_key] += 1
        
        sorted_dates = sorted(date_counts.items(), reverse=True)
        
        if not sorted_dates:
            await ctx.send("선생님, 아직 재생 기록이 없어요!", delete_after=10)
            return
        
        available_dates = sorted_dates[:25]
        
        embed = discord.Embed(
            title="📅 날짜별 재생 기록",
            description="재생하고 싶은 날짜를 선택해주세요!",
            color=0x3498db
        )
        
        view = View(timeout=60)
        
        async def view_timeout():
            for item in view.children:
                item.disabled = True
            try:
                await view.message.edit(view=view)
            except (discord.NotFound, discord.HTTPException):
                pass
        
        view.on_timeout = view_timeout
        
        options = []
        for date_str, count in available_dates:
            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                formatted_date = date_obj.strftime('%Y년 %m월 %d일')
                options.append(discord.SelectOption(
                    label=f"{formatted_date} ({count}곡)",
                    value=date_str,
                    description=f"이 날 {count}곡을 들었어요"
                ))
            except (ValueError, TypeError):
                options.append(discord.SelectOption(
                    label=f"{date_str} ({count}곡)",
                    value=date_str
                ))
        
        select = Select(placeholder="날짜를 선택하세요", options=options)
        
        async def date_select_callback(interaction):
            # 권한 검증
            if interaction.user.id != ctx.author.id:
                await interaction.response.send_message("선생님, 다른 사람의 히스토리를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                return
            
            selected_date = select.values[0]
            
            songs_for_date = []
            for song in history_list:
                if song['played_at'][:10] == selected_date:
                    songs_for_date.append(song)
            
            unique_songs = []
            seen_urls = set()
            for song in songs_for_date:
                if song['url'] not in seen_urls:
                    unique_songs.append(song)
                    seen_urls.add(song['url'])
            
            try:
                date_obj = datetime.strptime(selected_date, '%Y-%m-%d')
                formatted_date = date_obj.strftime('%Y년 %m월 %d일')
            except (ValueError, TypeError):
                formatted_date = selected_date
            
            song_list = "\n".join([f"{i+1}. {song['title']}" for i, song in enumerate(unique_songs[:10])])
            if len(unique_songs) > 10:
                song_list += f"\n... 외 {len(unique_songs) - 10}곡"
            
            confirm_view = View(timeout=30)
            
            async def confirm_timeout():
                for item in confirm_view.children:
                    item.disabled = True
                try:
                    await confirm_view.message.edit(view=confirm_view)
                except (discord.NotFound, discord.HTTPException):
                    pass
            
            confirm_view.on_timeout = confirm_timeout
            
            async def add_all_callback(add_interaction):
                # 권한 검증
                if add_interaction.user.id != ctx.author.id:
                    await add_interaction.response.send_message("선생님, 다른 사람의 히스토리를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                    return
                
                player = self.get_player(ctx)
                if not ctx.voice_client:
                    if ctx.author.voice:
                        await ctx.author.voice.channel.connect()
                    else:
                        await add_interaction.response.send_message(
                            "선생님, 음성 채널에 먼저 입장해주세요!", ephemeral=True, delete_after=5)
                        return
                
                added_count = 0
                for song in unique_songs:
                    try:
                        await player.queue.put(song['url'])
                        added_count += 1
                    except Exception as e:
                        logger.error(f"노래 추가 중 오류: {e}")
                
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
                # 권한 검증
                if cancel_interaction.user.id != ctx.author.id:
                    await cancel_interaction.response.send_message("선생님, 다른 사람의 히스토리를 조작할 수 없어요!", ephemeral=True, delete_after=5)
                    return
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
        await delete_command_message(ctx)

    @commands.command(name='이번주재생', aliases=['weekplay', '주간재생'])
    async def play_this_week(self, ctx):
        """이번 주에 들었던 노래들을 모두 대기열에 추가합니다."""
        guild_id_str = str(ctx.guild.id)
        
        if guild_id_str not in self.history or not self.history[guild_id_str]:
            await ctx.send("선생님, 아직 재생 기록이 없어요! 노래를 들어보시면 기록이 남을 거예요~", delete_after=10)
            await delete_command_message(ctx)
            return
        
        today = datetime.now().date()
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        
        songs_this_week = []
        for song in self.history[guild_id_str]:
            try:
                song_date = datetime.strptime(song['played_at'][:10], '%Y-%m-%d').date()
                if start_of_week <= song_date <= end_of_week:
                    songs_this_week.append(song)
            except (ValueError, TypeError, KeyError):
                continue
        
        if not songs_this_week:
            await ctx.send(f"선생님, 이번 주({start_of_week.strftime('%m월 %d일')} ~ {end_of_week.strftime('%m월 %d일')})에 들었던 노래가 없어요!", delete_after=10)
            await delete_command_message(ctx)
            return
        
        if not await ensure_voice_client(ctx):
            return
        
        player = self.get_player(ctx)
        
        unique_songs = []
        seen_urls = set()
        for song in songs_this_week:
            if song['url'] not in seen_urls:
                unique_songs.append(song)
                seen_urls.add(song['url'])
        
        added_count = 0
        for song in unique_songs:
            try:
                await player.queue.put(song['url'])
                added_count += 1
            except Exception as e:
                logger.error(f"노래 추가 중 오류: {e}")
        
        if not player.is_playing and added_count > 0:
            await ctx.send(f"선생님, 이번 주에 들었던 {added_count}곡을 대기열에 추가하고 재생할게요!", delete_after=10)
            await player.play_next()
        else:
            await ctx.send(f"선생님, 이번 주에 들었던 {added_count}곡을 대기열에 추가했어요!", delete_after=10)
        
        await delete_command_message(ctx)

    # ========== 관리자/기타 명령어 ==========

    @commands.command()
    @commands.has_permissions(manage_messages=True)
    async def 삭제(self, ctx, amount: int = 2):
        """지정된 수의 메시지를 삭제합니다."""
        if amount < 1:
            return await ctx.send("어라? 1개 이상의 메시지를 지정해 주셔야 해요. 아리스가 삭제할 수 있게요!", delete_after=12)

        deleted = await ctx.channel.purge(limit=amount + 1)
        await ctx.send(f"선생님, {len(deleted) - 1}개의 메시지를 깨끗하게 지웠어요! 아리스가 열심히 청소했답니다~", delete_after=5)
        await delete_command_message(ctx)

    @삭제.error
    async def 삭제_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("앗, 죄송해요 선생님. 이 명령어는 특별한 권한이 필요해요. 아리스가 도와드리고 싶어도 못 해드려요...", delete_after=12)
        elif isinstance(error, commands.BadArgument):
            await ctx.send("어머, 선생님? 숫자를 올바르게 입력해 주셔야 해요. 아리스가 이해할 수 있게요!", delete_after=12)
        await delete_command_message(ctx)

    @commands.command(name='도움말', aliases=['도움', 'help'])
    async def help_command(self, ctx):
        """모든 사용 가능한 명령어와 설명을 보여줍니다."""
        # 명령어를 카테고리별로 분류
        categories = {
            "음악": ["play", "stop", "leave", "volume", "queue"],
            "플레이리스트": ["플레이리스트", "플레이리스트추가", "플레이리스트재생", "플레이리스트삭제", "플레이리스트노래삭제"],
            "히스토리": ["히스토리", "다시재생", "히스토리삭제", "날짜별재생", "이번주재생", "큐로그"],
            "TTS": ["tts", "tts목소리", "tts느리게", "tts설정"],
            "관리": ["삭제", "재시작", "종료봇"]
        }
        
        # 게임 명령어 확인
        game_commands = []
        for cmd in self.bot.commands:
            if cmd.cog and cmd.cog.__class__.__name__ == "YachtDiceGame":
                game_commands.append(cmd.name)
        if game_commands:
            categories["게임"] = game_commands
        
        # 현재 페이지 (기본: 음악)
        current_page = "음악"
        
        def create_embed(page: str):
            """페이지별 Embed 생성"""
            embed = discord.Embed(
                title=f"아리스의 명령어 목록 - {page}",
                description="카테고리별로 명령어를 확인할 수 있어요!",
                color=0x3498db
            )
            
            if page in categories:
                commands_list = categories[page]
                for cmd_name in commands_list:
                    cmd = self.bot.get_command(cmd_name)
                    if cmd and not cmd.hidden:
                        aliases = f" ({', '.join(['!' + a for a in cmd.aliases[:2]])})" if cmd.aliases else ""
                        embed.add_field(
                            name=f"!{cmd.name}{aliases}",
                            value=cmd.help or "설명이 없어요.",
                            inline=False
                        )
            else:
                embed.description = "이 카테고리에는 명령어가 없어요."
            
            embed.set_footer(text=f"페이지 {list(categories.keys()).index(page) + 1}/{len(categories)}")
            return embed
        
        # View 생성
        class HelpView(discord.ui.View):
            def __init__(self, author_id: int, categories: dict, create_embed_func):
                super().__init__(timeout=120)
                self.author_id = author_id
                self.categories = categories
                self.create_embed = create_embed_func
                self.current_page = "음악"
                self.message = None
                
                # 카테고리 버튼 추가
                for i, cat in enumerate(categories.keys()):
                    if i < 5:  # Discord 버튼 최대 5개 제한
                        btn = discord.ui.Button(
                            label=cat,
                            style=discord.ButtonStyle.primary if cat == self.current_page else discord.ButtonStyle.secondary,
                            row=i // 5
                        )
                        btn.callback = self.create_callback(cat)
                        self.add_item(btn)
            
            def create_callback(self, category: str):
                async def callback(interaction: discord.Interaction):
                    if interaction.user.id != self.author_id:
                        await interaction.response.send_message(
                            "선생님, 다른 사람의 도움말을 조작할 수 없어요!",
                            ephemeral=True,
                            delete_after=5
                        )
                        return
                    
                    self.current_page = category
                    new_embed = self.create_embed(category)
                    
                    # 버튼 스타일 업데이트
                    for item in self.children:
                        if isinstance(item, discord.ui.Button):
                            if item.label == category:
                                item.style = discord.ButtonStyle.primary
                            else:
                                item.style = discord.ButtonStyle.secondary
                    
                    await interaction.response.edit_message(embed=new_embed, view=self)
                
                return callback
            
            async def on_timeout(self):
                for item in self.children:
                    item.disabled = True
                try:
                    if self.message:
                        await self.message.edit(view=self)
                except Exception:
                    pass
        
        view = HelpView(ctx.author.id, categories, create_embed)
        embed = create_embed(current_page)
        message = await ctx.send(embed=embed, view=view)
        view.message = message
        await delete_command_message(ctx)

    @commands.command(name='재시작', aliases=['restart', 'try'])
    @commands.has_permissions(administrator=True)
    async def restart(self, ctx):
        """프로그램을 재시작합니다."""
        await ctx.send("아리스가 재시작할게요! 잠시만 기다려주세요...", delete_after=5)
        os.execv(sys.executable, ['python'] + sys.argv)

    @commands.command(name='종료봇', aliases=['exit'])
    @commands.has_permissions(administrator=True)
    async def exit_bot(self, ctx):
        """봇을 종료합니다."""
        async def on_confirm(interaction):
            await interaction.response.send_message(
                "아리스가 종료될게요! 안녕히 가세요!",
                ephemeral=False,
                delete_after=10
            )
            await self.bot.close()
        
        async def on_cancel(interaction):
            await interaction.response.send_message(
                "봇 종료가 취소되었어요.",
                ephemeral=True,
                delete_after=5
            )
        
        confirm_view = ConfirmView(
            author_id=ctx.author.id,
            confirm_label="종료하기",
            cancel_label="취소하기",
            confirm_style=discord.ButtonStyle.danger,
            timeout=30.0,
            on_confirm=on_confirm,
            on_cancel=on_cancel
        )
        
        message = await ctx.send(
            "선생님, 정말로 봇을 종료할까요?",
            view=confirm_view,
            delete_after=30
        )
        confirm_view.message = message
        await delete_command_message(ctx)

    # ========== Slash Commands (GUI) ==========
    
    # 그룹은 cog_load에서 생성되므로 여기서는 메서드만 정의
    @discord.app_commands.describe(query="YouTube URL 또는 검색어")
    async def slash_play(self, interaction: discord.Interaction, query: str):
        """Slash Command로 음악 재생"""
        # Context 객체 생성
        ctx = await self.bot.get_context(interaction)
        await self.play_command(ctx, url=query)
    
    async def slash_stop(self, interaction: discord.Interaction):
        """Slash Command로 음악 정지"""
        ctx = await self.bot.get_context(interaction)
        await self.stop(ctx)
    
    @discord.app_commands.describe(volume="볼륨 값 (0-100)")
    async def slash_volume(self, interaction: discord.Interaction, volume: int):
        """Slash Command로 볼륨 설정"""
        ctx = await self.bot.get_context(interaction)
        await self.volume(ctx, volume)
    
    async def slash_queue(self, interaction: discord.Interaction):
        """Slash Command로 대기열 표시"""
        ctx = await self.bot.get_context(interaction)
        await self.queue(ctx)
    
    @discord.app_commands.describe(page="페이지 번호 (기본값: 1)")
    async def slash_history(self, interaction: discord.Interaction, page: int = 1):
        """Slash Command로 히스토리 표시"""
        ctx = await self.bot.get_context(interaction)
        await self.show_history(ctx, page)
    
    @discord.app_commands.describe(index="히스토리 번호 (기본값: 1)")
    async def slash_replay(self, interaction: discord.Interaction, index: int = 1):
        """Slash Command로 히스토리에서 재생"""
        ctx = await self.bot.get_context(interaction)
        await self.replay_from_history(ctx, index)
    
    async def cog_load(self):
        """Cog가 로드될 때 Slash Commands 등록"""
        # 이미 등록된 명령어가 있으면 제거
        try:
            self.bot.tree.remove_command("음악")
        except Exception:
            pass
        try:
            self.bot.tree.remove_command("플레이리스트")
        except Exception:
            pass
        try:
            self.bot.tree.remove_command("히스토리")
        except Exception:
            pass
        
        # 그룹을 새로 생성하여 중복 방지
        self.music_group = discord.app_commands.Group(name="음악", description="음악 재생 관련 명령어")
        self.playlist_group = discord.app_commands.Group(name="플레이리스트", description="플레이리스트 관련 명령어")
        self.history_group = discord.app_commands.Group(name="히스토리", description="재생 기록 관련 명령어")
        
        # 그룹에 명령어 추가
        self.music_group.command(name="재생", description="YouTube URL 또는 검색어로 음악을 재생합니다")(self.slash_play)
        self.music_group.command(name="정지", description="음악 재생을 종료합니다")(self.slash_stop)
        self.music_group.command(name="볼륨", description="볼륨을 설정합니다 (0-100)")(self.slash_volume)
        self.music_group.command(name="대기열", description="현재 재생 목록을 표시합니다")(self.slash_queue)
        
        self.history_group.command(name="히스토리", description="재생 기록을 보여줍니다")(self.slash_history)
        self.history_group.command(name="다시재생", description="히스토리에서 노래를 다시 재생합니다")(self.slash_replay)
        
        # 봇에 그룹 등록 (override=True로 강제 덮어쓰기)
        self.bot.tree.add_command(self.music_group, override=True)
        self.bot.tree.add_command(self.playlist_group, override=True)
        self.bot.tree.add_command(self.history_group, override=True)
    
    async def cog_unload(self):
        """Cog가 언로드될 때 Slash Commands 제거"""
        try:
            if self.music_group:
                self.bot.tree.remove_command(self.music_group.name)
            if self.playlist_group:
                self.bot.tree.remove_command(self.playlist_group.name)
            if self.history_group:
                self.bot.tree.remove_command(self.history_group.name)
        except Exception as e:
            logger.error(f"Slash Commands 제거 중 오류: {e}")

