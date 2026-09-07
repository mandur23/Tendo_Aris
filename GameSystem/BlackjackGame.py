import logging
import random
import asyncio
from typing import Optional

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

SUITS = ['♠', '♥', '♦', '♣']
RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']

MAX_PLAYERS = 6
TURN_TIMEOUT = 180  # 초. 시간 초과 시 자동 스탠드


def card_str(card) -> str:
    rank, suit = card
    return f"{suit}{rank}"


def hand_str(hand) -> str:
    return " ".join(card_str(c) for c in hand)


def hand_value(hand) -> int:
    """에이스를 1 또는 11로 계산한 최적(21 이하 최대) 값을 반환합니다."""
    total = 0
    aces = 0
    for rank, _ in hand:
        if rank == 'A':
            total += 11
            aces += 1
        elif rank in ('J', 'Q', 'K'):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def is_blackjack(hand) -> bool:
    return len(hand) == 2 and hand_value(hand) == 21


class BlackjackView(discord.ui.View):
    def __init__(self, cog: "BlackjackGame", channel_id: int, player_id: int, timeout=TURN_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.channel_id = channel_id
        self.player_id = player_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        game = self.cog.games.get(self.channel_id)
        if not game or game['state'] != 'playing':
            if not interaction.response.is_done():
                await interaction.response.send_message("진행 중인 게임을 찾을 수 없습니다.", ephemeral=True)
            else:
                await interaction.followup.send("진행 중인 게임을 찾을 수 없습니다.", ephemeral=True)
            return False

        current_player_id = game['players'][game['current_player']]
        if interaction.user.id != current_player_id:
            if not interaction.response.is_done():
                await interaction.response.send_message("아직 당신의 턴이 아니에요!", ephemeral=True)
            else:
                await interaction.followup.send("아직 당신의 턴이 아니에요!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🃏 히트", style=discord.ButtonStyle.green)
    async def hit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.player_hit(interaction, self.channel_id, self)

    @discord.ui.button(label="✋ 스탠드", style=discord.ButtonStyle.red)
    async def stand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.player_stand(interaction, self.channel_id, self)

    @discord.ui.button(label="📋 카드 현황", style=discord.ButtonStyle.blurple)
    async def status_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.show_table(interaction, self.channel_id)

    async def on_timeout(self):
        """시간 초과 시 현재 플레이어를 자동 스탠드 처리해 게임이 멈추지 않게 합니다."""
        # 게임이 이미 끝나 정리된 경우 락을 다시 만들지 않도록 먼저 확인
        if self.channel_id not in self.cog.games:
            return
        lock = self.cog._get_lock(self.channel_id)
        async with lock:
            game = self.cog.games.get(self.channel_id)
            if not game or game['state'] != 'playing':
                return
            current_player_id = game['players'][game['current_player']]
            # 이미 턴이 넘어갔으면 아무것도 하지 않음
            if current_player_id != self.player_id or game['status'].get(self.player_id) != 'playing':
                return

            # hit/stand과 동일하게 이전 메시지의 버튼을 비활성화해 Interaction failed 방지
            for item in self.children:
                item.disabled = True
            self.stop()
            msg = self.message
            if msg is None:
                mid = game.get('last_message_id')
                channel_for_msg = self.cog.bot.get_channel(self.channel_id)
                if mid and channel_for_msg:
                    try:
                        msg = await channel_for_msg.fetch_message(mid)
                    except discord.HTTPException:
                        msg = None
            if msg is not None:
                try:
                    await msg.edit(view=self)
                except discord.HTTPException:
                    logger.exception("블랙잭 타임아웃 메시지 편집 실패")

            game['status'][self.player_id] = 'stand'
            channel = self.cog.bot.get_channel(self.channel_id)
            if channel:
                try:
                    await channel.send(f"⏰ 시간 초과로 <@{self.player_id}>님은 자동 스탠드 처리되었습니다.")
                except discord.HTTPException:
                    logger.exception("블랙잭 타임아웃 알림 전송 실패")
            try:
                await self.cog._advance_turn_unlocked(self.channel_id)
            except Exception:
                logger.exception("블랙잭 타임아웃 후 턴 진행 실패")


class BlackjackGame(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.games = {}
        self.locks = {}

    def _get_lock(self, channel_id: int) -> asyncio.Lock:
        lock = self.locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[channel_id] = lock
        return lock

    def _build_deck(self):
        deck = [(rank, suit) for suit in SUITS for rank in RANKS]
        random.shuffle(deck)
        return deck

    def _draw(self, game):
        # 카드가 부족하면 새 덱을 섞어서 보충
        if not game['deck']:
            game['deck'] = self._build_deck()
        return game['deck'].pop()

    def _member_name(self, channel, player_id: int) -> str:
        user = None
        if channel and getattr(channel, 'guild', None):
            user = channel.guild.get_member(player_id)
        if user is None:
            user = self.bot.get_user(player_id)
        return user.display_name if user else f"사용자 {player_id}"

    # ========== 게임 진행 ==========

    @commands.command(name='블랙잭', help='블랙잭 게임을 시작하거나 참가합니다.')
    async def blackjack_game(self, ctx):
        channel_id = ctx.channel.id
        player = ctx.author

        async with self._get_lock(channel_id):
            if channel_id not in self.games:
                self.games[channel_id] = {
                    'players': [player.id],
                    'state': 'waiting',
                    'deck': [],
                    'hands': {},
                    'status': {},
                    'dealer': [],
                    'current_player': 0,
                    'last_message_id': None,
                }

                embed = discord.Embed(
                    title="🃏 블랙잭 게임 대기실",
                    description=(
                        f"**{player.display_name}**님이 블랙잭 게임을 열었습니다!\n\n"
                        "다른 플레이어들은 `!블랙잭`을 입력해서 참가하세요.\n"
                        "게임장은 `!블랙잭시작`으로 게임을 시작할 수 있습니다. (혼자서도 가능)"
                    ),
                    color=0x1abc9c
                )
                embed.add_field(name="참가자", value=player.display_name, inline=False)
                embed.add_field(
                    name="게임 규칙",
                    value=(
                        "• 딜러보다 21에 가까우면 승리 (21 초과 시 버스트 = 패배)\n"
                        "• J/Q/K는 10, A는 1 또는 11로 계산\n"
                        "• 딜러는 17 이상이 될 때까지 카드를 뽑습니다\n"
                        f"• 최대 {MAX_PLAYERS}명까지 참가 가능"
                    ),
                    inline=False
                )

                try:
                    await ctx.send(embed=embed)
                except discord.HTTPException:
                    logger.exception("블랙잭 게임 생성 메시지 전송 실패")
                return

            game = self.games[channel_id]
            if game['state'] != 'waiting':
                await ctx.send("이 채널에서 이미 블랙잭 게임이 진행 중입니다.")
                return

            if player.id in game['players']:
                await ctx.send("이미 참가 중입니다.")
                return

            if len(game['players']) >= MAX_PLAYERS:
                await ctx.send(f"최대 {MAX_PLAYERS}명까지만 참가할 수 있습니다.")
                return

            game['players'].append(player.id)

            player_names = [self._member_name(ctx.channel, p_id) for p_id in game['players']]
            embed = discord.Embed(
                title="🃏 블랙잭 게임 대기실",
                description=f"**{player.display_name}**님이 게임에 참가했습니다!\n\n게임장은 `!블랙잭시작`을 입력하세요.",
                color=0x1abc9c
            )
            embed.add_field(name="참가자", value="\n".join(player_names), inline=False)

            try:
                await ctx.send(embed=embed)
            except discord.HTTPException:
                logger.exception("블랙잭 참가 메시지 전송 실패")

    @commands.command(name='블랙잭시작', help='대기 중인 블랙잭 게임을 시작합니다.')
    async def start_blackjack_game(self, ctx):
        channel_id = ctx.channel.id

        async with self._get_lock(channel_id):
            game = self.games.get(channel_id)
            if not game:
                await ctx.send("진행 중인 게임이 없습니다. `!블랙잭`으로 새 게임을 만들어주세요.")
                return

            if game['state'] != 'waiting':
                await ctx.send("이미 게임이 진행 중입니다.")
                return

            if ctx.author.id != game['players'][0]:
                await ctx.send("게임을 시작할 권한이 없습니다. 게임장만 시작할 수 있습니다.")
                return

            game['state'] = 'playing'
            game['deck'] = self._build_deck()

            # 각 플레이어와 딜러에게 2장씩 배분
            for p_id in game['players']:
                game['hands'][p_id] = [self._draw(game), self._draw(game)]
                game['status'][p_id] = 'blackjack' if is_blackjack(game['hands'][p_id]) else 'playing'
            game['dealer'] = [self._draw(game), self._draw(game)]

            player_names = [self._member_name(ctx.channel, p_id) for p_id in game['players']]
            embed = discord.Embed(
                title="🎉 블랙잭 게임 시작!",
                description=f"참가자: {', '.join(player_names)}",
                color=0x2ecc71
            )
            embed.add_field(
                name="딜러",
                value=f"{card_str(game['dealer'][0])} 🂠",
                inline=False
            )
            for p_id in game['players']:
                hand = game['hands'][p_id]
                name = self._member_name(ctx.channel, p_id)
                extra = " — **블랙잭!** 🎊" if game['status'][p_id] == 'blackjack' else ""
                embed.add_field(
                    name=name,
                    value=f"{hand_str(hand)} ({hand_value(hand)}점){extra}",
                    inline=True
                )

            try:
                await ctx.send(embed=embed)
            except discord.HTTPException:
                logger.exception("블랙잭 시작 메시지 전송 실패")

            # 첫 턴 시작 (블랙잭인 플레이어는 자동으로 건너뜀)
            game['current_player'] = -1
            try:
                await self._advance_turn_unlocked(channel_id)
            except Exception:
                logger.exception("블랙잭 첫 턴 시작 실패")

    async def player_hit(self, interaction: discord.Interaction, channel_id: int, view: BlackjackView):
        async with self._get_lock(channel_id):
            game = self.games.get(channel_id)
            if not game or game['state'] != 'playing':
                return

            p_id = interaction.user.id
            if game['status'].get(p_id) != 'playing':
                if not interaction.response.is_done():
                    await interaction.response.send_message("더 이상 카드를 받을 수 없습니다.", ephemeral=True)
                return

            hand = game['hands'][p_id]
            hand.append(self._draw(game))
            value = hand_value(hand)

            if value > 21:
                game['status'][p_id] = 'bust'
                for item in view.children:
                    item.disabled = True
                view.stop()
                embed = self._turn_embed(channel_id, p_id, f"💥 버스트! ({value}점)")
                try:
                    await interaction.response.edit_message(embed=embed, view=view)
                except discord.HTTPException:
                    logger.exception("버스트 메시지 편집 실패")
                await self._advance_turn_unlocked(channel_id)
                return

            if value == 21:
                game['status'][p_id] = 'stand'
                for item in view.children:
                    item.disabled = True
                view.stop()
                embed = self._turn_embed(channel_id, p_id, "✨ 21점! 자동 스탠드합니다.")
                try:
                    await interaction.response.edit_message(embed=embed, view=view)
                except discord.HTTPException:
                    logger.exception("21점 메시지 편집 실패")
                await self._advance_turn_unlocked(channel_id)
                return

            embed = self._turn_embed(channel_id, p_id, "히트 또는 스탠드를 선택하세요.")
            try:
                await interaction.response.edit_message(embed=embed, view=view)
            except discord.HTTPException:
                logger.exception("히트 메시지 편집 실패")

    async def player_stand(self, interaction: discord.Interaction, channel_id: int, view: BlackjackView):
        async with self._get_lock(channel_id):
            game = self.games.get(channel_id)
            if not game or game['state'] != 'playing':
                return

            p_id = interaction.user.id
            if game['status'].get(p_id) != 'playing':
                if not interaction.response.is_done():
                    await interaction.response.send_message("이미 턴이 끝났습니다.", ephemeral=True)
                return

            game['status'][p_id] = 'stand'
            for item in view.children:
                item.disabled = True
            view.stop()

            hand = game['hands'][p_id]
            embed = self._turn_embed(channel_id, p_id, f"✋ {hand_value(hand)}점으로 스탠드했습니다.")
            try:
                await interaction.response.edit_message(embed=embed, view=view)
            except discord.HTTPException:
                logger.exception("스탠드 메시지 편집 실패")

            await self._advance_turn_unlocked(channel_id)

    def _turn_embed(self, channel_id: int, player_id: int, description: str) -> discord.Embed:
        game = self.games.get(channel_id)
        channel = self.bot.get_channel(channel_id)
        name = self._member_name(channel, player_id)

        embed = discord.Embed(title="🃏 블랙잭", description=description, color=0x3498db)
        embed.set_author(name=f"{name}님의 턴")
        if game:
            hand = game['hands'].get(player_id, [])
            embed.add_field(name="내 카드", value=f"{hand_str(hand)} (**{hand_value(hand)}점**)", inline=False)
            embed.add_field(name="딜러", value=f"{card_str(game['dealer'][0])} 🂠", inline=False)
        return embed

    async def _advance_turn_unlocked(self, channel_id: int):
        """다음 'playing' 상태 플레이어에게 턴을 넘기고, 없으면 딜러 턴으로 넘어갑니다. (락 보유 상태에서 호출)"""
        game = self.games.get(channel_id)
        if not game or game['state'] != 'playing':
            return

        next_idx = None
        for i in range(game['current_player'] + 1, len(game['players'])):
            p_id = game['players'][i]
            if game['status'].get(p_id) == 'playing':
                next_idx = i
                break

        if next_idx is None:
            await self._dealer_turn_and_finish(channel_id)
            return

        game['current_player'] = next_idx
        p_id = game['players'][next_idx]

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        embed = self._turn_embed(channel_id, p_id, f"<@{p_id}>님의 턴입니다! 히트 또는 스탠드를 선택하세요.")
        view = BlackjackView(self, channel_id, p_id)
        try:
            msg = await channel.send(embed=embed, view=view)
            game['last_message_id'] = msg.id
        except discord.HTTPException:
            logger.exception("블랙잭 턴 메시지 전송 실패")

    async def _dealer_turn_and_finish(self, channel_id: int):
        game = self.games.get(channel_id)
        if not game:
            return

        game['state'] = 'dealer'
        channel = self.bot.get_channel(channel_id)

        # 딜러는 17 이상이 될 때까지 뽑음
        while hand_value(game['dealer']) < 17:
            game['dealer'].append(self._draw(game))

        dealer_value = hand_value(game['dealer'])
        dealer_bj = is_blackjack(game['dealer'])
        dealer_bust = dealer_value > 21

        embed = discord.Embed(title="🏁 블랙잭 결과", color=0xf1c40f)
        dealer_text = f"{hand_str(game['dealer'])} (**{dealer_value}점**)"
        if dealer_bj:
            dealer_text += " — 블랙잭!"
        elif dealer_bust:
            dealer_text += " — 💥 버스트!"
        embed.add_field(name="🎩 딜러", value=dealer_text, inline=False)

        lines = []
        for p_id in game['players']:
            hand = game['hands'][p_id]
            value = hand_value(hand)
            status = game['status'][p_id]
            name = self._member_name(channel, p_id)

            if status == 'bust':
                result = "❌ 패배 (버스트)"
            elif status == 'blackjack':
                result = "🤝 무승부 (둘 다 블랙잭)" if dealer_bj else "🎊 블랙잭 승리!"
            elif dealer_bj:
                result = "❌ 패배 (딜러 블랙잭)"
            elif dealer_bust or value > dealer_value:
                result = "🏆 승리!"
            elif value == dealer_value:
                result = "🤝 무승부"
            else:
                result = "❌ 패배"

            lines.append(f"**{name}**: {hand_str(hand)} ({value}점) → {result}")

        field_value = "\n".join(lines)
        if len(field_value) > 1024:
            field_value = field_value[:1021] + "..."
        embed.add_field(name="플레이어 결과", value=field_value, inline=False)
        embed.set_footer(text="새 게임은 !블랙잭으로 시작할 수 있어요.")

        # 게임 데이터 정리
        try:
            del self.games[channel_id]
        except KeyError:
            logger.debug("블랙잭 종료 시 게임 데이터가 이미 삭제됨")
        self.locks.pop(channel_id, None)

        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                logger.exception("블랙잭 결과 메시지 전송 실패")

    async def show_table(self, interaction: discord.Interaction, channel_id: int):
        """전체 테이블 현황을 에페메랄로 보여줍니다."""
        game = self.games.get(channel_id)
        if not game:
            if not interaction.response.is_done():
                await interaction.response.send_message("진행 중인 게임이 없습니다.", ephemeral=True)
            return

        status_names = {'playing': '진행 중', 'stand': '스탠드', 'bust': '버스트', 'blackjack': '블랙잭'}
        embed = discord.Embed(title="📋 테이블 현황", color=0x95a5a6)
        embed.add_field(name="🎩 딜러", value=f"{card_str(game['dealer'][0])} 🂠", inline=False)

        for p_id in game['players']:
            hand = game['hands'].get(p_id, [])
            name = self._member_name(interaction.channel, p_id)
            status = status_names.get(game['status'].get(p_id), '-')
            embed.add_field(
                name=name,
                value=f"{hand_str(hand)} ({hand_value(hand)}점)\n상태: {status}",
                inline=True
            )

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
        except discord.HTTPException:
            logger.exception("테이블 현황 표시 실패")

    @commands.command(name='블랙잭취소', help='진행 중인 블랙잭 게임을 취소합니다.')
    async def cancel_blackjack_game(self, ctx):
        channel_id = ctx.channel.id

        async with self._get_lock(channel_id):
            game = self.games.get(channel_id)
            if not game:
                await ctx.send("진행 중인 게임이 없습니다.")
                return

            is_host = ctx.author.id == game['players'][0]
            can_manage = getattr(ctx.author, 'guild_permissions', None) and ctx.author.guild_permissions.manage_messages
            if not is_host and not can_manage:
                await ctx.send("게임을 취소할 권한이 없습니다.", delete_after=10)
                return

            try:
                del self.games[channel_id]
            except KeyError:
                logger.debug("블랙잭 취소 시 데이터가 이미 삭제됨")

            try:
                await ctx.send("🛑 블랙잭 게임이 취소되었습니다.", delete_after=10)
            except discord.HTTPException:
                logger.exception("블랙잭 취소 메시지 전송 실패")

        self.locks.pop(channel_id, None)

    @blackjack_game.error
    @start_blackjack_game.error
    @cancel_blackjack_game.error
    async def blackjack_error(self, ctx, error):
        if isinstance(error, commands.CommandError):
            try:
                await ctx.send(f"오류가 발생했습니다: {str(error)}")
            except Exception:
                pass
            logger.exception(f"Blackjack game error: {error}")

    # ========== Slash Commands ==========

    async def slash_blackjack(self, interaction: discord.Interaction):
        """Slash Command로 블랙잭 게임 시작/참가"""
        ctx = await self.bot.get_context(interaction)
        await self.blackjack_game(ctx)

    async def slash_start(self, interaction: discord.Interaction):
        """Slash Command로 대기 중인 블랙잭 게임 시작"""
        ctx = await self.bot.get_context(interaction)
        await self.start_blackjack_game(ctx)

    async def slash_cancel(self, interaction: discord.Interaction):
        """Slash Command로 진행 중인 블랙잭 게임 취소"""
        ctx = await self.bot.get_context(interaction)
        await self.cancel_blackjack_game(ctx)

    async def cog_load(self):
        """Cog가 로드될 때 Slash Commands 등록"""
        self.blackjack_group = discord.app_commands.Group(name="블랙잭", description="블랙잭 게임 관련 명령어")

        self.blackjack_group.command(name="참가", description="블랙잭 게임을 시작하거나 참가합니다")(self.slash_blackjack)
        self.blackjack_group.command(name="시작", description="대기 중인 블랙잭 게임을 시작합니다")(self.slash_start)
        self.blackjack_group.command(name="취소", description="진행 중인 블랙잭 게임을 취소합니다")(self.slash_cancel)

        self.bot.tree.add_command(self.blackjack_group, override=True)

    async def cog_unload(self):
        """Cog가 언로드될 때 Slash Commands 제거"""
        try:
            if hasattr(self, 'blackjack_group'):
                self.bot.tree.remove_command(self.blackjack_group.name)
        except Exception as e:
            logger.error(f"Slash Commands 제거 중 오류: {e}")


async def setup(bot):
    await bot.add_cog(BlackjackGame(bot))
