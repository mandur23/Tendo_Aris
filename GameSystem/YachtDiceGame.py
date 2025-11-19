import functools
import logging
import random
import asyncio
from typing import Optional

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)




class RecordView(discord.ui.View):
    category_names = {
        'ones': '에이스 (1)', 'twos': '듀스 (2)', 'threes': '트레이 (3)',
        'fours': '포 (4)', 'fives': '파이브 (5)', 'sixes': '식스 (6)',
        'full_house': '풀하우스', 'four_of_a_kind': '포카드',
        'little_straight': '스몰 스트레이트', 'big_straight': '라지 스트레이트',
        'yacht': '야추', 'choice': '찬스'
    }

    def __init__(self, cog: "YachtDiceGame", channel_id: int, user: discord.User, available_categories, timeout=120):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.channel_id = channel_id
        self.user = user
        self.available_categories = available_categories

        game = self.cog.games.get(channel_id)
        if not game:
            logger.warning(f"RecordView 초기화 시 게임을 찾을 수 없음: channel_id={channel_id}")
            return
        
        if not available_categories:
            logger.warning(f"RecordView 초기화 시 사용 가능한 카테고리가 없음: channel_id={channel_id}, user={user.id}")
            return
            
        dice = game['dice']

        for cat in available_categories:
            score = self.cog.calculate_score(dice, cat)
            kr_name = self.category_names.get(cat, cat)
            label = f"{kr_name} — {score}점"
            if len(label) > 80:
                label = f"{kr_name} — {score}"

            b = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)

            async def _cat_cb(interaction: discord.Interaction, category, score):
                if interaction.user.id != self.user.id:
                    if not interaction.response.is_done():
                        await interaction.response.send_message("이 카테고리는 당신이 선택할 수 없습니다.", ephemeral=True)
                    else:
                        await interaction.followup.send("이 카테고리는 당신이 선택할 수 없습니다.", ephemeral=True)
                    return

                lock = self.cog.locks.get(self.channel_id)
                if lock is None:
                    lock = asyncio.Lock()
                    self.cog.locks[self.channel_id] = lock

                async with lock:
                    game = self.cog.games.get(self.channel_id)
                    if not game:
                        if not interaction.response.is_done():
                            await interaction.response.send_message("게임 정보를 찾을 수 없습니다.", ephemeral=True)
                        else:
                            await interaction.followup.send("게임 정보를 찾을 수 없습니다.", ephemeral=True)
                        return

                    current_player_id = game['players'][game['current_player']]
                    if interaction.user.id != current_player_id:
                        if not interaction.response.is_done():
                            await interaction.response.send_message("지금은 당신의 턴이 아닙니다.", ephemeral=True)
                        else:
                            await interaction.followup.send("지금은 당신의 턴이 아닙니다.", ephemeral=True)
                        return

                    if game['scores'].get(self.user.id, {}).get(category) is not None:
                        if not interaction.response.is_done():
                            await interaction.response.send_message("이미 이 카테고리에 점수가 기록되어 있습니다.", ephemeral=True)
                        else:
                            await interaction.followup.send("이미 이 카테고리에 점수가 기록되어 있습니다.", ephemeral=True)
                        return

                    game['scores'][self.user.id][category] = score

                    try:
                        await self.cog._advance_turn_unlocked(self.channel_id, interaction.guild)
                    except Exception:
                        logger.exception("_advance_turn_unlocked 실패")

                for item in self.children:
                    item.disabled = True

                kr = self.category_names.get(category, category)
                try:
                    if not interaction.response.is_done():
                        await interaction.response.edit_message(content=f"`{kr}`에 **{score}점** 기록했습니다! 점수 기록 창이 닫힙니다.", embed=None, view=self)
                    else:
                        await interaction.followup.send(f"`{kr}`에 **{score}점** 기록했습니다!", ephemeral=True)
                except discord.HTTPException:
                    try:
                        if not interaction.response.is_done():
                            await interaction.response.send_message(f"`{kr}`에 **{score}점** 기록했습니다!", ephemeral=True)
                        else:
                            await interaction.followup.send(f"`{kr}`에 **{score}점** 기록했습니다!", ephemeral=True)
                    except Exception:
                        logger.exception("후속 응답 실패")

                channel = self.cog.bot.get_channel(self.channel_id)
                if channel:
                    try:
                        await channel.send(f"✅ **{self.user.display_name}**님이 `{kr}`에 **{score}점** 기록했습니다.")
                    except discord.Forbidden:
                        logger.debug("공지 전송 권한 없음")
                    except discord.HTTPException:
                        logger.exception("공지 전송 실패")

            b.callback = functools.partial(_cat_cb, category=cat, score=score)
            self.add_item(b)

        cancel_btn = discord.ui.Button(label="취소", style=discord.ButtonStyle.secondary)

        async def _cancel_cb(interaction: discord.Interaction):
            if interaction.user.id != self.user.id:
                if not interaction.response.is_done():
                    await interaction.response.send_message("취소할 권한이 없습니다.", ephemeral=True)
                else:
                    await interaction.followup.send("취소할 권한이 없습니다.", ephemeral=True)
                return

            game = self.cog.games.get(self.channel_id)
            if game:
                current_player_id = game['players'][game['current_player']]
                if interaction.user.id != current_player_id:
                    if not interaction.response.is_done():
                        await interaction.response.send_message("지금은 당신의 턴이 아닙니다.", ephemeral=True)
                    else:
                        await interaction.followup.send("지금은 당신의 턴이 아닙니다.", ephemeral=True)
                    return

            for item in self.children:
                item.disabled = True

            try:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(content="점수 기록을 취소했습니다.", embed=None, view=self)
                else:
                    await interaction.followup.send("점수 기록을 취소했습니다.", ephemeral=True)
            except discord.HTTPException:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message("점수 기록을 취소했습니다.", ephemeral=True)
                    else:
                        await interaction.followup.send("점수 기록을 취소했습니다.", ephemeral=True)
                except Exception:
                    logger.exception("취소 후속 응답 실패")

        cancel_btn.callback = _cancel_cb
        self.add_item(cancel_btn)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        channel = self.cog.bot.get_channel(self.channel_id)
        if channel:
            try:
                await channel.send(f"⚠️ **{self.user.display_name}**님의 점수 기록 창이 시간 초과로 닫혔습니다.", delete_after=10)
            except Exception:
                logger.exception("타임아웃 알림 전송 실패")
        self.stop()


class YachtGameView(discord.ui.View):
    def __init__(self, cog: "YachtDiceGame", ctx_or_channel, channel_id: int, timeout=3600):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.ctx_or_channel = ctx_or_channel
        self.channel_id = channel_id
        self.game = cog.games.get(channel_id)

        if not self.game:
            return

        self.roll_btn = discord.ui.Button(label="", style=discord.ButtonStyle.green)
        self.add_item(self.roll_btn)

        self.dice_buttons = [discord.ui.Button(label="", style=discord.ButtonStyle.secondary) for _ in range(5)]
        for b in self.dice_buttons:
            self.add_item(b)

        self.scoreboard_btn = discord.ui.Button(label="📋 점수판", style=discord.ButtonStyle.blurple)
        self.add_item(self.scoreboard_btn)

        self.record_btn = discord.ui.Button(label="✅ 점수 기록", style=discord.ButtonStyle.red)
        self.add_item(self.record_btn)

        self.roll_btn.callback = self._roll_cb
        for idx, b in enumerate(self.dice_buttons):
            b.callback = functools.partial(self._dice_cb, index=idx)
        self.scoreboard_btn.callback = self._scoreboard_cb
        self.record_btn.callback = self._record_cb

        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.game:
            if not interaction.response.is_done():
                await interaction.response.send_message("게임 정보를 찾을 수 없습니다.", ephemeral=True)
            else:
                await interaction.followup.send("게임 정보를 찾을 수 없습니다.", ephemeral=True)
            return False
        current_player_id = self.game['players'][self.game['current_player']]
        if interaction.user.id != current_player_id:
            if not interaction.response.is_done():
                await interaction.response.send_message("아직 당신의 턴이 아니에요!", ephemeral=True)
            else:
                await interaction.followup.send("아직 당신의 턴이 아니에요!", ephemeral=True)
            return False
        return True

    def update_buttons(self):
        self.roll_btn.label = f"굴리기 ({self.game['rolls_left']}/3)"
        self.roll_btn.style = discord.ButtonStyle.green if self.game['rolls_left'] > 0 else discord.ButtonStyle.secondary
        self.roll_btn.disabled = self.game['rolls_left'] <= 0

        for i, b in enumerate(self.dice_buttons):
            die = self.game['dice'][i]
            is_kept = self.game['kept_dice'][i]
            b.label = f"🔒 {die}" if is_kept else f"🎲 {die}"
            b.style = discord.ButtonStyle.primary if is_kept else discord.ButtonStyle.secondary
            b.disabled = False

        self.scoreboard_btn.disabled = False
        self.record_btn.disabled = False

    async def _roll_cb(self, interaction: discord.Interaction):
        if self.game['rolls_left'] <= 0:
            await interaction.response.send_message("더 이상 굴릴 수 없어요.", ephemeral=True)
            return

        for i in range(5):
            if not self.game['kept_dice'][i]:
                self.game['dice'][i] = random.randint(1, 6)
        self.game['rolls_left'] -= 1
        self.game['has_rolled'] = True

        self.update_buttons()
        embed = self.create_embed_for_current_player(f"🎲 주사위: {self.dice_display()}", "주사위를 고정하거나 점수를 기록하세요.")

        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.NotFound:
            try:
                await interaction.response.send_message(embed=embed, view=self)
            except Exception:
                logger.exception("roll 후 새 메시지 전송 실패")
        except discord.Forbidden:
            logger.error("roll_cb: 메시지 편집/전송 권한 없음")
        except discord.HTTPException:
            logger.exception("roll_cb: HTTP 에러")

    async def _dice_cb(self, interaction: discord.Interaction, index: int):
        self.game['kept_dice'][index] = not self.game['kept_dice'][index]
        self.update_buttons()
        embed = self.create_embed_for_current_player(f"🎲 주사위: {self.dice_display()}", "주사위를 고정하거나 점수를 기록하세요.")
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.NotFound:
            try:
                await interaction.response.send_message(embed=embed, view=self)
            except Exception:
                logger.exception("dice 버튼 후 새 메시지 전송 실패")
        except discord.Forbidden:
            logger.error("dice_cb: 메시지 편집/전송 권한 없음")
        except discord.HTTPException:
            logger.exception("dice_cb: HTTP 에러")

    async def _scoreboard_cb(self, interaction: discord.Interaction):
        await self.cog.show_game_status(interaction, self.channel_id, ephemeral=True)

    async def _record_cb(self, interaction: discord.Interaction):
        if not self.game['has_rolled']:
            if not interaction.response.is_done():
                await interaction.response.send_message("먼저 주사위를 굴려야 점수를 기록할 수 있어요!", ephemeral=True)
            else:
                await interaction.followup.send("먼저 주사위를 굴려야 점수를 기록할 수 있어요!", ephemeral=True)
            return

        available = [cat for cat, s in self.game['scores'].get(interaction.user.id, {}).items() if s is None]
        if not available:
            if not interaction.response.is_done():
                await interaction.response.send_message("기록할 수 있는 카테고리가 없습니다.", ephemeral=True)
            else:
                await interaction.followup.send("기록할 수 있는 카테고리가 없습니다.", ephemeral=True)
            return

        record_view = RecordView(self.cog, self.channel_id, interaction.user, available)
        embed = discord.Embed(title="📊 점수 기록", description="아래 버튼을 눌러 카테고리를 선택하세요.", color=0x2ecc71)

        preview_lines = []
        for cat in available:
            score = self.cog.calculate_score(self.game['dice'], cat)
            kr = RecordView.category_names.get(cat, cat)
            preview_lines.append(f"{kr}: **{score}점**")

        if len(preview_lines) <= 12:
            embed.add_field(name="예상 점수", value="\n".join(preview_lines), inline=False)
        else:
            half = len(preview_lines) // 2
            embed.add_field(name="예상 점수 (1/2)", value="\n".join(preview_lines[:half]), inline=True)
            embed.add_field(name="예상 점수 (2/2)", value="\n".join(preview_lines[half:]), inline=True)

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, view=record_view, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, view=record_view, ephemeral=True)
        except discord.Forbidden:
            logger.error("record 뷰 전송 권한 없음")
        except discord.HTTPException:
            logger.exception("record 뷰 전송 중 HTTP 에러")

    def dice_display(self):
        parts = []
        for i, d in enumerate(self.game['dice']):
            if self.game['kept_dice'][i]:
                parts.append(f"**{d}**")
            else:
                parts.append(str(d))
        return " ".join(parts)

    def create_embed_for_current_player(self, title, description):
        current_id = self.game['players'][self.game['current_player']]
        current_user = None
        try:
            current_user = self.cog.bot.get_user(current_id)
            if current_user is None and hasattr(self.ctx_or_channel, 'guild') and self.ctx_or_channel.guild:
                current_user = self.ctx_or_channel.guild.get_member(current_id)
        except Exception:
            current_user = None

        embed = discord.Embed(title=title, description=description, color=0x3498db)
        if current_user:
            try:
                avatar_url = getattr(current_user.display_avatar, 'url', None)
                if avatar_url:
                    embed.set_author(name=f"{current_user.display_name}님의 턴", icon_url=avatar_url)
                else:
                    embed.set_author(name=f"{current_user.display_name}님의 턴")
            except Exception:
                embed.set_author(name=f"사용자 {current_id}님의 턴")
        else:
            embed.set_author(name=f"사용자 {current_id}님의 턴")
        return embed

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

        if self.game and self.game.get('last_message_id'):
            channel = self.cog.bot.get_channel(self.channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(self.game['last_message_id'])
                    await msg.edit(view=self)
                except discord.NotFound:
                    logger.debug("타임아웃: 편집할 메시지가 없음")
                except discord.Forbidden:
                    logger.debug("타임아웃: 편집 권한 없음")
                except discord.HTTPException:
                    logger.exception("타임아웃 메시지 편집 실패")

        try:
            channel = self.ctx_or_channel if isinstance(self.ctx_or_channel, discord.abc.Messageable) else self.cog.bot.get_channel(self.channel_id)
            if channel:
                await channel.send("게임 뷰가 시간 초과로 비활성화되었습니다.", delete_after=10)
        except Exception:
            logger.exception("타임아웃 알림 전송 실패")


class YachtDiceGame(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.games = {}
        self.locks = {}

    def calculate_score(self, dice, category):
        # 각 주사위 값(1~6)의 개수를 계산
        dice_count = {i: dice.count(i) for i in range(1, 7)}
        counts_list = [dice_count[i] for i in range(1, 7)]
        
        if category == 'ones': return dice.count(1) * 1
        elif category == 'twos': return dice.count(2) * 2
        elif category == 'threes': return dice.count(3) * 3
        elif category == 'fours': return dice.count(4) * 4
        elif category == 'fives': return dice.count(5) * 5
        elif category == 'sixes': return dice.count(6) * 6
        elif category == 'choice': return sum(dice)
        elif category == 'four_of_a_kind':
            if any(c >= 4 for c in counts_list):
                return sum(dice)
            return 0
        elif category == 'full_house':
            # 풀하우스: 정확히 3개와 2개의 같은 숫자 조합
            counts = sorted([c for c in counts_list if c > 0])
            if len(counts) == 2 and counts == [2, 3]:
                return 25
            return 0
        elif category == 'little_straight':
            dice_set = set(dice)
            straights = [{1, 2, 3, 4}, {2, 3, 4, 5}, {3, 4, 5, 6}]
            for straight in straights:
                if straight.issubset(dice_set):
                    return 30
            return 0
        elif category == 'big_straight':
            dice_set = set(dice)
            if dice_set == {1, 2, 3, 4, 5} or dice_set == {2, 3, 4, 5, 6}:
                return 40
            return 0
        elif category == 'yacht':
            if any(c == 5 for c in counts_list):
                return 50
            return 0
        return 0

    def calculate_upper_bonus(self, player_scores):
        upper_categories = ['ones', 'twos', 'threes', 'fours', 'fives', 'sixes']
        upper_total = 0
        for cat in upper_categories:
            v = player_scores.get(cat)
            if v is not None:
                upper_total += v
        return 35 if upper_total >= 63 else 0

    def calculate_total_score(self, player_scores):
        total = sum(score for score in player_scores.values() if score is not None)
        bonus = self.calculate_upper_bonus(player_scores)
        return total + bonus

    @commands.command(name='야추', help='야추 다이스 게임을 시작합니다.')
    async def yacht_game(self, ctx):
        channel_id = ctx.channel.id
        player = ctx.author

        lock = self.locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[channel_id] = lock

        async with lock:
            if channel_id not in self.games:
                self.games[channel_id] = {
                    'players': [player.id],
                    'state': 'waiting',
                    'scores': {},
                    'current_player': 0,
                    'turn_count': 0,
                    'dice': [1, 1, 1, 1, 1],
                    'kept_dice': [False] * 5,
                    'rolls_left': 3,
                    'has_rolled': False,
                    'last_message_id': None
                }

                embed = discord.Embed(
                    title="🎲 야추 다이스 게임 대기실",
                    description=f"**{player.display_name}**님이 야추 게임을 시작했습니다!\n\n다른 플레이어들은 `!야추`를 입력해서 참가하세요.\n게임장은 `!시작`을 입력해서 게임을 시작할 수 있습니다.",
                    color=0xe74c3c
                )
                embed.add_field(name="참가자", value=player.display_name, inline=False)
                embed.add_field(name="게임 규칙", value="• 각 플레이어는 12개 카테고리에 점수를 기록\n• 상단(1~6) 합계 63점 이상 시 35점 보너스\n• 최고 점수를 얻은 플레이어 승리", inline=False)

                try:
                    await ctx.send(embed=embed)
                except discord.HTTPException:
                    logger.exception("게임 생성 메시지 전송 실패")
                return

            game = self.games[channel_id]
            if game['state'] == 'waiting':
                if player.id in game['players']:
                    await ctx.send("이미 참가 중입니다.")
                    return

                game['players'].append(player.id)

                player_names = []
                for p_id in game['players']:
                    user = ctx.guild.get_member(p_id) if ctx.guild else self.bot.get_user(p_id)
                    player_names.append(user.display_name if user else f"사용자 {p_id}")

                embed = discord.Embed(
                    title="🎲 야추 다이스 게임 대기실",
                    description=f"**{player.display_name}**님이 게임에 참가했습니다!\n\n게임장(시작 권한)은 `!시작`을 입력하세요.",
                    color=0xe74c3c
                )
                embed.add_field(name="참가자", value="\n".join(player_names), inline=False)

                try:
                    await ctx.send(embed=embed)
                except discord.HTTPException:
                    logger.exception("참가 메시지 전송 실패")
            else:
                await ctx.send("이 채널에서 이미 게임이 진행 중입니다.")

    @commands.command(name='시작', help='대기 중인 야추 게임을 시작합니다.')
    async def start_yacht_game(self, ctx):
        channel_id = ctx.channel.id
        lock = self.locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[channel_id] = lock

        async with lock:
            game = self.games.get(channel_id)
            if not game:
                await ctx.send("진행 중인 게임이 없습니다. `!야추`로 새 게임을 만들어주세요.")
                return

            if game['state'] != 'waiting':
                await ctx.send("이미 게임이 진행 중입니다.")
                return

            if ctx.author.id != game['players'][0]:
                await ctx.send("게임을 시작할 권한이 없습니다. 게임장만 시작할 수 있습니다.")
                return

            if len(game['players']) < 2:
                await ctx.send("최소 2명 이상의 플레이어가 필요합니다.")
                return

            for p_id in game['players']:
                game['scores'][p_id] = {
                    'ones': None, 'twos': None, 'threes': None, 'fours': None, 'fives': None, 'sixes': None,
                    'full_house': None, 'four_of_a_kind': None, 'little_straight': None,
                    'big_straight': None, 'yacht': None, 'choice': None
                }

            game['state'] = 'playing'

            player_names = []
            for p_id in game['players']:
                user = ctx.guild.get_member(p_id) if ctx.guild else self.bot.get_user(p_id)
                player_names.append(user.display_name if user else f"사용자 {p_id}")

            embed = discord.Embed(
                title="🎉 게임 시작!",
                description=f"참가자: {', '.join(player_names)}\n\n총 {len(game['players']) * 12}턴으로 진행됩니다.",
                color=0x2ecc71
            )

            try:
                await ctx.send(embed=embed)
                await self.start_turn(ctx, channel_id)
            except discord.HTTPException:
                logger.exception("게임 시작 메시지 전송 실패")

    async def start_turn(self, ctx: commands.Context, channel_id: int):
        game = self.games.get(channel_id)
        if not game:
            return

        current_player_id = game['players'][game['current_player']]
        current_user = ctx.guild.get_member(current_player_id) if ctx.guild else self.bot.get_user(current_player_id)
        current_name = current_user.display_name if current_user else f"사용자 {current_player_id}"

        embed = discord.Embed(
            title="🎲 야추 다이스 게임",
            description=f"**{current_name}**님의 턴입니다!\n`굴리기` 버튼을 눌러 시작하세요.",
            color=0x3498db
        )
        embed.add_field(name="턴 정보", value=f"턴 {game['turn_count'] + 1}/{len(game['players']) * 12}", inline=False)

        view = YachtGameView(self, ctx, channel_id)

        try:
            msg = await ctx.send(embed=embed, view=view)
            game['last_message_id'] = msg.id
        except discord.HTTPException:
            logger.exception("턴 시작 메시지 전송 실패")

    async def show_game_status(self, interaction: discord.Interaction, channel_id: int, ephemeral=False):
        game = self.games.get(channel_id)
        if not game:
            if not interaction.response.is_done():
                await interaction.response.send_message("해당 채널에 진행 중인 게임이 없습니다.", ephemeral=True)
            else:
                await interaction.followup.send("해당 채널에 진행 중인 게임이 없습니다.", ephemeral=True)
            return

        embed = discord.Embed(title="📋 점수판", color=0x95a5a6)

        for p_id in game['players']:
            scores = game['scores'].get(p_id, {})
            lines = []

            user = interaction.guild.get_member(p_id) if interaction.guild else self.bot.get_user(p_id)
            user_name = user.display_name if user else f"사용자 {p_id}"

            upper_total = 0
            upper_categories = ['ones', 'twos', 'threes', 'fours', 'fives', 'sixes']
            for k in upper_categories:
                v = scores.get(k)
                kr = RecordView.category_names.get(k, k)
                lines.append(f"{kr}: {v if v is not None else '-'}")
                if v is not None:
                    upper_total += v

            lines.append(f"상단 소계: {upper_total}")
            bonus = self.calculate_upper_bonus(scores)
            lines.append(f"보너스: {bonus}")
            lines.append("---")

            lower_categories = ['full_house', 'four_of_a_kind', 'little_straight', 'big_straight', 'yacht', 'choice']
            for k in lower_categories:
                v = scores.get(k)
                kr = RecordView.category_names.get(k, k)
                lines.append(f"{kr}: {v if v is not None else '-'}")

            total = self.calculate_total_score(scores)
            lines.append(f"**총점: {total}**")

            field_value = "\n".join(lines)
            if len(field_value) > 1024:
                field_value = field_value[:1021] + "..."

            embed.add_field(name=user_name, value=field_value, inline=True)

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            else:
                await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        except discord.HTTPException as e:
            logger.exception("점수판 표시 실패")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("점수판을 표시하는 중 오류가 발생했습니다.", ephemeral=True)
                else:
                    await interaction.followup.send("점수판을 표시하는 중 오류가 발생했습니다.", ephemeral=True)
            except Exception:
                logger.exception("점수판 오류 메시지 전송 실패")

    async def _advance_turn_unlocked(self, channel_id: int, guild: Optional[discord.Guild] = None):
        game = self.games.get(channel_id)
        if not game:
            return

        game['turn_count'] += 1

        if self.is_game_finished(channel_id):
            await self.end_game(channel_id, guild)
            return

        game['current_player'] = (game['current_player'] + 1) % len(game['players'])
        game['dice'] = [1, 1, 1, 1, 1]
        game['kept_dice'] = [False] * 5
        game['rolls_left'] = 3
        game['has_rolled'] = False

        channel = self.bot.get_channel(channel_id)
        if channel is None and guild:
            channel = guild.get_channel(channel_id)

        current_player_id = game['players'][game['current_player']]
        current_user = channel.guild.get_member(current_player_id) if channel and channel.guild else self.bot.get_user(current_player_id)
        current_name = current_user.display_name if current_user else f"사용자 {current_player_id}"

        embed = discord.Embed(
            title="🎲 다음 턴",
            description=f"**{current_name}**님의 턴입니다!\n`굴리기` 버튼을 눌러주세요.",
            color=0x3498db
        )
        embed.add_field(name="턴 정보", value=f"턴 {game['turn_count'] + 1}/{len(game['players']) * 12}", inline=False)

        view = YachtGameView(self, channel, channel_id)

        if channel:
            try:
                msg = await channel.send(embed=embed, view=view)
                game['last_message_id'] = msg.id
            except discord.HTTPException:
                logger.exception("다음 턴 메시지 전송 실패")

    async def next_turn(self, channel_id: int, guild: Optional[discord.Guild] = None):
        lock = self.locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[channel_id] = lock

        async with lock:
            await self._advance_turn_unlocked(channel_id, guild)

    def is_game_finished(self, channel_id: int) -> bool:
        game = self.games.get(channel_id)
        if not game:
            return True
        for p_id in game['players']:
            scores = game['scores'].get(p_id, {})
            if any(v is None for v in scores.values()):
                return False
        return True

    async def end_game(self, channel_id: int, guild: Optional[discord.Guild] = None):
        game = self.games.get(channel_id)
        if not game:
            return

        results = []
        for p_id in game['players']:
            total = self.calculate_total_score(game['scores'][p_id])
            bonus = self.calculate_upper_bonus(game['scores'][p_id])
            user = guild.get_member(p_id) if guild else self.bot.get_user(p_id)
            user_name = user.display_name if user else f"사용자 {p_id}"
            results.append((user_name, total, bonus))

        results.sort(key=lambda x: x[1], reverse=True)
        winner_name, winning_score, winner_bonus = results[0]

        embed = discord.Embed(
            title="🏁 게임 종료!",
            description=f"🎉 우승자: **{winner_name}** ({winning_score}점)",
            color=0xf1c40f
        )

        rank_text = []
        for i, (name, total, bonus) in enumerate(results):
            rank_emoji = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i+1}위"
            bonus_text = f" (+{bonus} 보너스)" if bonus > 0 else ""
            rank_text.append(f"{rank_emoji} {name}: **{total}점**{bonus_text}")

        embed.add_field(name="최종 순위", value="\n".join(rank_text), inline=False)

        try:
            del self.games[channel_id]
            if channel_id in self.locks:
                del self.locks[channel_id]
        except KeyError:
            logger.debug("종료 시 게임 데이터가 이미 삭제됨")

        channel = self.bot.get_channel(channel_id)
        if channel is None and guild:
            channel = guild.get_channel(channel_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                logger.exception("게임 종료 메시지 전송 실패")

    @commands.command(name='야추취소', help='진행 중인 야추 게임을 취소합니다.')
    async def cancel_yacht_game(self, ctx):
        channel_id = ctx.channel.id
        lock = self.locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[channel_id] = lock

        async with lock:
            game = self.games.get(channel_id)
            if not game:
                await ctx.send("진행 중인 게임이 없습니다.")
                return

            if ctx.author.id != game['players'][0] and not ctx.author.guild_permissions.manage_messages:
                await ctx.send("게임을 취소할 권한이 없습니다.", delete_after=10)
                return

            try:
                del self.games[channel_id]
                if channel_id in self.locks:
                    del self.locks[channel_id]
            except KeyError:
                logger.debug("취소 시 데이터가 이미 삭제됨")

            try:
                await ctx.send("🛑 야추 게임이 취소되었습니다.", delete_after=10)
            except discord.HTTPException:
                logger.exception("게임 취소 메시지 전송 실패")

    @yacht_game.error
    @start_yacht_game.error
    @cancel_yacht_game.error
    async def yacht_error(self, ctx, error):
        if isinstance(error, commands.CommandError):
            try:
                await ctx.send(f"오류가 발생했습니다: {str(error)}")
            except Exception:
                pass
            logger.exception(f"Yacht game error: {error}")


async def setup(bot):
    await bot.add_cog(YachtDiceGame(bot))