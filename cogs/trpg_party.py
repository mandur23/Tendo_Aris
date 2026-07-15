"""파티(멀티플레이어) TRPG 명령어 Cog

`!파티모험` 으로 호스트가 장르를 고르고 로비를 열면, 채널의 다른 사용자들이
직업 버튼으로 참가한다 (채널당 파티 1개, 최대 4명). 시작하면 로컬 LLM(Ollama)이
파티 공동 세계관·퀘스트·장면을 생성하고, 참가 순서대로 돌아가는 턴제로 진행된다.

- 자기 턴인 플레이어만 선택지/자유 행동을 쓸 수 있다 (턴 넘기기는 본인·호스트 가능)
- HP가 0이 된 멤버는 회복될 때까지 턴을 건너뛰고, 전원 쓰러지면 파티 전멸
- 매 턴 자동 저장되며 `!파티모험계속` 으로 이어서 할 수 있다 (1인용 `!모험` 과는 별개)

게임 규칙(주사위·HP·인벤토리)은 GameSystem/TRPGEngine.py 의 파티 엔진이 코드로 관리한다.
"""
import asyncio
import functools
import logging
import random
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import discord
from discord.ext import commands

from GameSystem.TRPGEngine import (
    CLASSES,
    DEFAULT_DC,
    FREE_ACTION_DC,
    GENRES,
    PARTY_MAX_MEMBERS,
    PartyAdventure,
    PartyTurnResult,
    TRPGCharacter,
    generate_party_scenario,
    run_party_turn,
    roll_check,
)
from cogs.trpg_ui import CharacterSetupModal, combat_status_lines, hp_bar as _hp_bar
from utils.config import LOCAL_AI_MODEL
from utils.discord_utils import AuthorLockedView, safe_defer, safe_edit_message
from utils.file_utils import delete_trpg_party_save, load_trpg_party_saves, set_trpg_party_save
from utils.llm_utils import check_model_available, is_local_ai_configured

logger = logging.getLogger(__name__)

# (guild_id_or_0, channel_id) — 파티 모험은 채널당 1개
PartyKey = Tuple[int, int]

SELECT_VIEW_TIMEOUT = 180       # 장르 선택 대기 시간 (초)
LOBBY_TIMEOUT = 600             # 로비 모집 시간 (초). 만료되면 로비 자동 취소.
ADVENTURE_VIEW_TIMEOUT = 1800   # 장면 버튼 유지 시간 (초). 만료돼도 !파티모험계속 으로 재개 가능.


@dataclass
class PartyLobby:
    """모험 시작 전 참가자를 모으는 로비 상태."""

    host_id: int
    genre_key: str
    members: Dict[int, str] = field(default_factory=dict)   # user_id -> class_key (참가 순서 유지)
    names: Dict[int, str] = field(default_factory=dict)     # user_id -> 표시 이름
    profiles: Dict[int, dict] = field(default_factory=dict) # user_id -> {name, race, background}
    message: Optional[discord.Message] = None
    view: Optional["PartyLobbyView"] = None

    def display_name(self, user_id: int) -> str:
        return self.profiles.get(user_id, {}).get("name") or self.names.get(user_id, "?")


class PartyGenreSelectView(AuthorLockedView):
    """호스트가 파티 모험의 장르를 고르는 버튼 뷰."""

    def __init__(self, cog: "TRPGParty", key: PartyKey, host_id: int):
        super().__init__(author_id=host_id, timeout=SELECT_VIEW_TIMEOUT)
        self.cog = cog
        self.key = key

        for genre_key, genre in GENRES.items():
            btn = discord.ui.Button(label=f"{genre['emoji']} {genre['label']}", style=discord.ButtonStyle.primary)
            btn.callback = functools.partial(self._genre_cb, genre_key=genre_key)
            self.add_item(btn)

        random_btn = discord.ui.Button(label="🎲 랜덤", style=discord.ButtonStyle.secondary)
        random_btn.callback = functools.partial(self._genre_cb, genre_key="random")
        self.add_item(random_btn)

    async def _genre_cb(self, interaction: discord.Interaction, genre_key: str):
        if genre_key == "random":
            genre_key = random.choice(list(GENRES))
        self.stop()
        await self.cog.open_lobby(interaction, self.key, genre_key)


class PartyLobbyView(discord.ui.View):
    """참가/직업 선택 로비 뷰. 직업 버튼은 누구나, 시작/취소는 호스트만 누를 수 있다."""

    def __init__(self, cog: "TRPGParty", key: PartyKey, lobby: PartyLobby):
        super().__init__(timeout=LOBBY_TIMEOUT)
        self.cog = cog
        self.key = key
        self.lobby = lobby

        for class_key, spec in CLASSES.items():
            btn = discord.ui.Button(
                label=f"{spec['emoji']} {spec['label']}",
                style=discord.ButtonStyle.primary,
                row=0 if list(CLASSES).index(class_key) < 2 else 1,
            )
            btn.callback = functools.partial(self._class_cb, class_key=class_key)
            self.add_item(btn)

        leave_btn = discord.ui.Button(label="🚪 나가기", style=discord.ButtonStyle.secondary, row=2)
        leave_btn.callback = self._leave_cb
        self.add_item(leave_btn)

        start_btn = discord.ui.Button(label="🚀 시작 (호스트)", style=discord.ButtonStyle.success, row=2)
        start_btn.callback = self._start_cb
        self.add_item(start_btn)

        cancel_btn = discord.ui.Button(label="❌ 취소 (호스트)", style=discord.ButtonStyle.danger, row=2)
        cancel_btn.callback = self._cancel_cb
        self.add_item(cancel_btn)

    async def _class_cb(self, interaction: discord.Interaction, class_key: str):
        user = interaction.user
        if user.id not in self.lobby.members and len(self.lobby.members) >= PARTY_MAX_MEMBERS:
            await self.cog.respond_ephemeral(interaction, f"파티가 가득 찼어요! (최대 {PARTY_MAX_MEMBERS}명)")
            return

        lobby = self.lobby
        prev_name = lobby.profiles.get(user.id, {}).get("name") or user.display_name

        async def _submit(modal_itx: discord.Interaction, name: str, race: str, background: str):
            # 모달 입력 중에 다른 사람이 자리를 채웠을 수 있으므로 다시 확인.
            if user.id not in lobby.members and len(lobby.members) >= PARTY_MAX_MEMBERS:
                await self.cog.respond_ephemeral(modal_itx, f"파티가 가득 찼어요! (최대 {PARTY_MAX_MEMBERS}명)")
                return
            lobby.members[user.id] = class_key
            lobby.names[user.id] = user.display_name
            lobby.profiles[user.id] = {"name": name, "race": race, "background": background}
            await self.cog.update_lobby_message(modal_itx, self.key)

        await interaction.response.send_modal(
            CharacterSetupModal(default_name=prev_name, on_submit_cb=_submit)
        )

    async def _leave_cb(self, interaction: discord.Interaction):
        if interaction.user.id not in self.lobby.members:
            await self.cog.respond_ephemeral(interaction, "아직 파티에 참가하지 않으셨어요.")
            return
        self.lobby.members.pop(interaction.user.id, None)
        self.lobby.names.pop(interaction.user.id, None)
        self.lobby.profiles.pop(interaction.user.id, None)
        await self.cog.update_lobby_message(interaction, self.key)

    async def _start_cb(self, interaction: discord.Interaction):
        if interaction.user.id != self.lobby.host_id:
            await self.cog.respond_ephemeral(interaction, "모험 시작은 호스트만 할 수 있어요!")
            return
        if not self.lobby.members:
            await self.cog.respond_ephemeral(interaction, "아직 참가자가 없어요. 직업 버튼으로 참가해주세요!")
            return
        self.stop()
        await self.cog.start_party_adventure(interaction, self.key)

    async def _cancel_cb(self, interaction: discord.Interaction):
        if interaction.user.id != self.lobby.host_id:
            await self.cog.respond_ephemeral(interaction, "로비 취소는 호스트만 할 수 있어요!")
            return
        self.stop()
        await self.cog.cancel_lobby(self.key, interaction=interaction)

    async def on_timeout(self):
        await self.cog.cancel_lobby(self.key, timed_out=True)


class PartyFreeActionModal(discord.ui.Modal, title="✍️ 자유 행동"):
    """선택지에 없는 행동을 직접 입력받는 모달. 운명 판정(d20, 보정 없음)이 따라붙는다."""

    action = discord.ui.TextInput(
        label="무엇을 하시겠습니까?",
        placeholder="예: 상인에게 소문에 대해 캐묻는다",
        max_length=150,
    )

    def __init__(self, cog: "TRPGParty", key: PartyKey, view: "PartyAdventureView"):
        super().__init__()
        self.cog = cog
        self.key = key
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        action_text = str(self.action).strip()
        if not action_text:
            await self.cog.respond_ephemeral(interaction, "행동 내용이 비어있어요.")
            return
        await self.cog.process_party_action(
            interaction, self.key, action_text,
            stat=None, dc=FREE_ACTION_DC, fate_roll=True, view=self.view,
        )


class PartyAdventureView(discord.ui.View):
    """현재 장면의 선택지 버튼 + 자유 행동/턴 넘기기/파티/중단 컨트롤.

    선택지·자유 행동은 현재 턴 플레이어만, 턴 넘기기는 본인·호스트,
    파티 시트는 누구나, 중단(저장)은 호스트만 누를 수 있다.
    """

    def __init__(self, cog: "TRPGParty", key: PartyKey, adv: PartyAdventure):
        super().__init__(timeout=ADVENTURE_VIEW_TIMEOUT)
        self.cog = cog
        self.key = key
        self.message: Optional[discord.Message] = None

        for idx, choice in enumerate(adv.choices):
            label = f"{idx + 1}. {choice['text']}"
            if choice.get("stat"):
                label += f" [{choice['stat']}]"
            btn = discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.primary if choice.get("stat") else discord.ButtonStyle.secondary,
                row=idx // 2,
            )
            btn.callback = functools.partial(self._choice_cb, index=idx)
            self.add_item(btn)

        free_btn = discord.ui.Button(label="✍️ 자유 행동", style=discord.ButtonStyle.success, row=2)
        free_btn.callback = self._free_cb
        self.add_item(free_btn)

        skip_btn = discord.ui.Button(label="⏭️ 턴 넘기기", style=discord.ButtonStyle.secondary, row=2)
        skip_btn.callback = self._skip_cb
        self.add_item(skip_btn)

        sheet_btn = discord.ui.Button(label="🧾 파티", style=discord.ButtonStyle.secondary, row=2)
        sheet_btn.callback = self._sheet_cb
        self.add_item(sheet_btn)

        suspend_btn = discord.ui.Button(label="💾 중단(호스트)", style=discord.ButtonStyle.danger, row=2)
        suspend_btn.callback = self._suspend_cb
        self.add_item(suspend_btn)

    async def _check_actor(self, interaction: discord.Interaction) -> Optional[PartyAdventure]:
        """현재 턴 플레이어인지 확인한다. 아니면 안내 후 None."""
        adv = self.cog.parties.get(self.key)
        if adv is None:
            await self.cog.respond_ephemeral(interaction, "모험 정보를 찾을 수 없어요. `!파티모험계속` 으로 다시 불러와주세요.")
            return None
        if str(interaction.user.id) not in adv.members:
            await self.cog.respond_ephemeral(interaction, "이 파티의 멤버가 아니에요!")
            return None
        if str(interaction.user.id) != adv.current_actor_id:
            current = adv.current_character
            name = current.name if current else "?"
            await self.cog.respond_ephemeral(interaction, f"지금은 **{name}** 의 턴이에요. 잠시만 기다려주세요!")
            return None
        return adv

    async def _choice_cb(self, interaction: discord.Interaction, index: int):
        adv = await self._check_actor(interaction)
        if adv is None:
            return
        if index >= len(adv.choices):
            await self.cog.respond_ephemeral(interaction, "선택지 정보를 찾을 수 없어요.")
            return
        choice = adv.choices[index]
        await self.cog.process_party_action(
            interaction, self.key, choice["text"],
            stat=choice.get("stat"), dc=choice.get("dc", DEFAULT_DC), fate_roll=False, view=self,
            choice=choice,
        )

    async def _free_cb(self, interaction: discord.Interaction):
        adv = await self._check_actor(interaction)
        if adv is None:
            return
        await interaction.response.send_modal(PartyFreeActionModal(self.cog, self.key, self))

    async def _skip_cb(self, interaction: discord.Interaction):
        adv = self.cog.parties.get(self.key)
        if adv is None:
            await self.cog.respond_ephemeral(interaction, "모험 정보를 찾을 수 없어요. `!파티모험계속` 으로 다시 불러와주세요.")
            return
        user_id = str(interaction.user.id)
        if user_id != adv.current_actor_id and user_id != adv.host_id:
            await self.cog.respond_ephemeral(interaction, "턴 넘기기는 현재 턴 플레이어나 호스트만 할 수 있어요.")
            return
        await self.cog.skip_party_turn(interaction, self.key, self)

    async def _sheet_cb(self, interaction: discord.Interaction):
        adv = self.cog.parties.get(self.key)
        if adv is None:
            await self.cog.respond_ephemeral(interaction, "모험 정보를 찾을 수 없어요.")
            return
        try:
            embed = self.cog.party_sheet_embed(adv)
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
        except discord.HTTPException:
            logger.exception("파티 시트 전송 실패")

    async def _suspend_cb(self, interaction: discord.Interaction):
        adv = self.cog.parties.get(self.key)
        if adv is not None and str(interaction.user.id) != adv.host_id:
            await self.cog.respond_ephemeral(interaction, "중단(저장)은 호스트만 할 수 있어요.")
            return
        await self.cog.suspend_party(interaction, self.key, self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            await safe_edit_message(self.message, view=self)


class TRPGParty(commands.Cog):
    """파티(멀티플레이어) TRPG — GM 아리스가 진행하는 협동 텍스트 어드벤처."""

    def __init__(self, bot):
        self.bot = bot
        self.party_group = None
        self.parties: Dict[PartyKey, PartyAdventure] = {}
        self.lobbies: Dict[PartyKey, PartyLobby] = {}
        self.locks: Dict[PartyKey, asyncio.Lock] = {}
        self.active_views: Dict[PartyKey, PartyAdventureView] = {}

    # ------------------------------------------------------------------ 공통 유틸
    @staticmethod
    def _make_key(guild: Optional[discord.Guild], channel) -> PartyKey:
        guild_id = guild.id if guild else 0
        return (guild_id, getattr(channel, "id", 0))

    @staticmethod
    def _key_str(key: PartyKey) -> str:
        return f"{key[0]}:{key[1]}"

    def _model(self) -> str:
        """ChatAI Cog가 모델 자동 전환을 했다면 그 모델을 함께 사용한다."""
        chat_cog = self.bot.get_cog("ChatAI")
        return getattr(chat_cog, "active_model", None) or LOCAL_AI_MODEL

    def _lock(self, key: PartyKey) -> asyncio.Lock:
        lock = self.locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[key] = lock
        return lock

    async def respond_ephemeral(self, interaction: discord.Interaction, content: str):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(content, ephemeral=True)
            else:
                await interaction.followup.send(content, ephemeral=True)
        except discord.HTTPException:
            logger.debug("ephemeral 응답 전송 실패 (무시됨)")

    # ------------------------------------------------------------------ 세이브 관리
    def _save_sync(self, key: PartyKey, adv_dict: dict):
        set_trpg_party_save(self._key_str(key), adv_dict)

    def _delete_save_sync(self, key: PartyKey):
        delete_trpg_party_save(self._key_str(key))

    def _load_save_sync(self, key: PartyKey) -> Optional[dict]:
        return load_trpg_party_saves().get(self._key_str(key))

    async def _autosave(self, key: PartyKey, adv: PartyAdventure):
        try:
            await asyncio.to_thread(self._save_sync, key, adv.to_dict())
        except Exception as e:
            logger.error(f"파티 TRPG 자동 저장 실패: {e}")

    async def _delete_save(self, key: PartyKey):
        try:
            await asyncio.to_thread(self._delete_save_sync, key)
        except Exception as e:
            logger.error(f"파티 TRPG 세이브 삭제 실패: {e}")

    # ------------------------------------------------------------------ 임베드
    def lobby_embed(self, lobby: PartyLobby) -> discord.Embed:
        genre = GENRES[lobby.genre_key]
        embed = discord.Embed(
            title=f"{genre['emoji']} {genre['label']} 파티 모험 — 참가자 모집",
            description=(
                f"{genre['hint']}.\n\n"
                "아래 직업 버튼을 누르면 이름·종족·배경을 입력하고 참가해요! (다시 누르면 변경)\n"
                f"최대 {PARTY_MAX_MEMBERS}명 · 참가 순서대로 턴이 돌아갑니다.\n"
                "모두 모이면 **호스트가 🚀 시작** 버튼으로 모험을 시작합니다."
            ),
            color=0x9B59B6,
        )
        if lobby.members:
            lines = []
            for idx, (user_id, class_key) in enumerate(lobby.members.items(), 1):
                spec = CLASSES[class_key]
                host_mark = " 👑" if user_id == lobby.host_id else ""
                profile = lobby.profiles.get(user_id, {})
                extra = f" · {profile['race']}" if profile.get("race") else ""
                lines.append(f"{idx}. {spec['emoji']} **{lobby.display_name(user_id)}** ({spec['label']}{extra}){host_mark}")
            embed.add_field(name=f"참가자 ({len(lobby.members)}/{PARTY_MAX_MEMBERS})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="참가자 (0명)", value="아직 없음 — 첫 번째 용사가 되어주세요!", inline=False)
        host_name = lobby.names.get(lobby.host_id) or f"<@{lobby.host_id}>"
        embed.set_footer(text=f"호스트: {host_name} · {LOBBY_TIMEOUT // 60}분 동안 시작하지 않으면 로비가 닫혀요.")
        return embed

    def _party_status_lines(self, adv: PartyAdventure) -> str:
        lines = []
        for uid in adv.turn_order:
            char = adv.members[uid]
            marker = "💀 " if char.hp <= 0 else ("⭐ " if uid == adv.current_actor_id and adv.is_playing else "")
            lines.append(f"{marker}{char.job_emoji} **{char.name}** ({char.job}) {_hp_bar(char.hp, char.max_hp)}")
        return "\n".join(lines)

    def party_scene_embed(
        self,
        adv: PartyAdventure,
        *,
        result: Optional[PartyTurnResult] = None,
        opening: bool = False,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{adv.genre_emoji} {adv.title} — {adv.turn}턴",
            description=adv.scene[:4000],
            color=0x9B59B6,
        )
        if opening:
            if adv.world:
                embed.add_field(name="🌍 세계관", value=adv.world[:1024], inline=False)
            if adv.quest:
                embed.add_field(name="🎯 퀘스트", value=adv.quest[:1024], inline=False)

        if result:
            changes = []
            for name, delta in result.hp_changes.items():
                changes.append(f"{name}: HP {'+' if delta > 0 else ''}{delta}")
            if result.items_added:
                changes.append(f"획득: {', '.join(result.items_added)}")
            if result.items_removed:
                changes.append(f"상실: {', '.join(result.items_removed)}")
            if changes:
                embed.add_field(name="📦 변화", value="\n".join(changes)[:1024], inline=False)

        if adv.combat is not None:
            embed.add_field(name="⚔️ 전투 중!", value=combat_status_lines(adv.combat)[:1024], inline=False)

        embed.add_field(name="👥 파티", value=self._party_status_lines(adv)[:1024], inline=False)

        current = adv.current_character
        turn_line = f"⭐ 지금은 {current.name} 의 턴!" if current and adv.is_playing else ""
        footer = f"{turn_line}\n버튼으로 행동을 고르거나 ✍️ 자유 행동으로 직접 입력하세요."
        if adv.combat is not None:
            footer = f"{turn_line}\n⚔️ 전투 중! 공격/방어 버튼이나 ✍️ 자유 행동(물약·도주·꾀)으로 싸우세요."
        elif not opening and adv.quest:
            footer = f"🎯 {adv.quest[:120]}\n{footer}"
        embed.set_footer(text=footer.strip()[:2048])
        return embed

    def party_sheet_embed(self, adv: PartyAdventure) -> discord.Embed:
        embed = discord.Embed(
            title=f"👥 파티 시트 — {adv.genre_emoji} {adv.title}",
            description=f"{adv.genre_label} · {adv.turn}턴 진행",
            color=0x3498DB,
        )
        for uid in adv.turn_order:
            char = adv.members[uid]
            inventory = ", ".join(char.inventory) if char.inventory else "비어 있음"
            status = " (쓰러짐 💀)" if char.hp <= 0 else ""
            race_part = f"종족: {char.race}\n" if char.race else ""
            embed.add_field(
                name=f"{char.job_emoji} {char.name} — {char.job}{status}",
                value=(
                    f"HP {_hp_bar(char.hp, char.max_hp)}\n"
                    f"{race_part}"
                    f"능력치: {char.stats_line()}\n"
                    f"소지품: {inventory}"
                )[:1024],
                inline=False,
            )
        if adv.quest:
            embed.set_footer(text=f"🎯 {adv.quest[:2048]}")
        return embed

    def party_ending_embed(self, adv: PartyAdventure) -> discord.Embed:
        if adv.status == "victory":
            title, color = "🏆 파티가 퀘스트를 완수했다!", 0xF1C40F
        elif adv.status == "dead":
            title, color = "💀 파티가 전멸했다...", 0x992D22
        else:
            title, color = "🌒 모험이 막을 내렸다", 0x95A5A6

        embed = discord.Embed(title=title, description=adv.scene[:4000], color=color)
        member_lines = "\n".join(
            f"{char.job_emoji} {char.name} ({char.job}) — HP {char.hp}/{char.max_hp}"
            for char in adv.members.values()
        )
        embed.add_field(
            name="모험 기록",
            value=f"{adv.genre_emoji} {adv.title} · 총 {adv.turn}턴\n{member_lines}"[:1024],
            inline=False,
        )
        embed.set_footer(text="`!파티모험` 으로 새로운 파티 모험을 시작할 수 있어요.")
        return embed

    # ------------------------------------------------------------------ 뷰 관리
    async def _retire_active_view(self, key: PartyKey):
        """이전 장면 뷰를 중지·비활성화해 오래된 메시지에서 중복 조작을 막는다."""
        old = self.active_views.pop(key, None)
        if old is None or old.is_finished():
            return
        old.stop()
        for item in old.children:
            item.disabled = True
        if old.message:
            await safe_edit_message(old.message, view=old)

    async def send_party_scene(
        self,
        channel: discord.abc.Messageable,
        key: PartyKey,
        adv: PartyAdventure,
        *,
        result: Optional[PartyTurnResult] = None,
        opening: bool = False,
    ):
        await self._retire_active_view(key)
        embed = self.party_scene_embed(adv, result=result, opening=opening)
        view = PartyAdventureView(self, key, adv)
        try:
            msg = await channel.send(embed=embed, view=view)
            view.message = msg
            self.active_views[key] = view
        except discord.HTTPException:
            logger.exception("파티 모험 장면 전송 실패")

    # ------------------------------------------------------------------ 로비 플로우
    async def open_lobby(self, interaction: discord.Interaction, key: PartyKey, genre_key: str):
        lobby = PartyLobby(host_id=interaction.user.id, genre_key=genre_key)
        # 호스트는 바로 첫 참가자로 등록하지 않는다 — 직업 버튼으로 직접 골라야 참가.
        lobby.names[lobby.host_id] = interaction.user.display_name
        self.lobbies[key] = lobby

        view = PartyLobbyView(self, key, lobby)
        lobby.view = view
        try:
            await interaction.response.edit_message(embed=self.lobby_embed(lobby), view=view)
            lobby.message = interaction.message
        except discord.HTTPException:
            logger.exception("파티 로비 표시 실패")
            self.lobbies.pop(key, None)

    async def update_lobby_message(self, interaction: discord.Interaction, key: PartyKey):
        lobby = self.lobbies.get(key)
        if lobby is None:
            await self.respond_ephemeral(interaction, "로비가 이미 닫혔어요.")
            return
        try:
            await interaction.response.edit_message(embed=self.lobby_embed(lobby), view=lobby.view)
        except discord.HTTPException:
            logger.debug("로비 갱신 실패 (무시됨)")

    async def cancel_lobby(
        self,
        key: PartyKey,
        *,
        interaction: Optional[discord.Interaction] = None,
        timed_out: bool = False,
    ):
        lobby = self.lobbies.pop(key, None)
        if lobby is None:
            return
        if lobby.view and not lobby.view.is_finished():
            lobby.view.stop()
        if lobby.message:
            embed = discord.Embed(
                title="🎲 파티 모험 로비 닫힘",
                description="시간이 지나 로비가 닫혔어요. `!파티모험` 으로 다시 열 수 있어요."
                if timed_out else "호스트가 로비를 취소했어요.",
                color=0x95A5A6,
            )
            if interaction is not None and not interaction.response.is_done():
                try:
                    await interaction.response.edit_message(embed=embed, view=None)
                    return
                except discord.HTTPException:
                    pass
            await safe_edit_message(lobby.message, embed=embed, view=None)

    async def start_party_adventure(self, interaction: discord.Interaction, key: PartyKey):
        lobby = self.lobbies.get(key)
        if lobby is None:
            await self.respond_ephemeral(interaction, "로비 정보를 찾을 수 없어요.")
            return

        lock = self._lock(key)
        if lock.locked():
            await self.respond_ephemeral(interaction, "이미 모험 생성이 진행 중이에요. 잠시만요!")
            return

        genre = GENRES[lobby.genre_key]
        progress = discord.Embed(
            title=f"{genre['emoji']} {genre['label']} 파티 모험 준비 중...",
            description=(
                f"🖋️ GM 아리스가 {len(lobby.members)}인 파티의 세계를 창조하는 중입니다...\n"
                "로컬 AI 성능에 따라 수십 초 정도 걸릴 수 있어요."
            ),
            color=0x9B59B6,
        )
        try:
            await interaction.response.edit_message(embed=progress, view=None)
        except discord.HTTPException:
            logger.exception("파티 모험 생성 대기 화면 표시 실패")

        async with lock:
            existing = self.parties.get(key)
            if existing is not None and existing.is_playing:
                return

            members: Dict[str, TRPGCharacter] = {}
            for user_id, class_key in lobby.members.items():
                profile = lobby.profiles.get(user_id, {})
                members[str(user_id)] = TRPGCharacter.create(
                    profile.get("name") or lobby.names.get(user_id, "모험가"),
                    class_key,
                    race=profile.get("race", ""),
                    background=profile.get("background", ""),
                )

            try:
                async with interaction.channel.typing():
                    adv = await asyncio.to_thread(
                        generate_party_scenario,
                        lobby.genre_key,
                        str(lobby.host_id),
                        members,
                        model=self._model(),
                    )
            except FileNotFoundError:
                await self._fail_progress(
                    interaction,
                    f"⚠️ 모델 `{self._model()}` 을 찾을 수 없어요.\n"
                    "`ollama pull` 로 설치하거나 `TOKEN.env`의 `LOCAL_AI_MODEL` 설정을 확인해주세요.",
                )
                return
            except Exception as e:
                logger.error(f"파티 TRPG 시나리오 생성 실패: {e}", exc_info=True)
                await self._fail_progress(
                    interaction,
                    f"⚠️ 세계 생성에 실패했어요: {str(e)[:100]}\n로비는 그대로니까 🚀 시작을 다시 눌러주세요.",
                )
                # 실패 시 로비를 되살려 재시도할 수 있게 한다.
                if lobby.message and self.lobbies.get(key) is lobby:
                    retry_view = PartyLobbyView(self, key, lobby)
                    lobby.view = retry_view
                    await safe_edit_message(lobby.message, embed=self.lobby_embed(lobby), view=retry_view)
                return

            self.lobbies.pop(key, None)
            self.parties[key] = adv
            await self._autosave(key, adv)
            finish = discord.Embed(
                title=f"{genre['emoji']} {genre['label']} 파티 모험 생성 완료!",
                description="용사들이여, 출진! 아래에서 모험이 시작됩니다.",
                color=0x2ECC71,
            )
            if interaction.message:
                await safe_edit_message(interaction.message, embed=finish, view=None)
            await self.send_party_scene(interaction.channel, key, adv, opening=True)

    async def _fail_progress(self, interaction: discord.Interaction, description: str):
        embed = discord.Embed(title="🎲 파티 TRPG", description=description, color=0xE74C3C)
        edited = False
        if interaction.message:
            edited = await safe_edit_message(interaction.message, embed=embed, view=None)
        if not edited:
            try:
                await interaction.channel.send(description, delete_after=20)
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ 턴 진행
    async def process_party_action(
        self,
        interaction: discord.Interaction,
        key: PartyKey,
        action_text: str,
        *,
        stat: Optional[str],
        dc: int,
        fate_roll: bool,
        view: PartyAdventureView,
        choice: Optional[dict] = None,
    ):
        adv = self.parties.get(key)
        if adv is None or not adv.is_playing:
            await self.respond_ephemeral(interaction, "진행 중인 파티 모험이 없어요. `!파티모험계속` 으로 다시 불러와주세요.")
            return

        lock = self._lock(key)
        if lock.locked():
            await self.respond_ephemeral(interaction, "GM 아리스가 아직 이야기를 쓰는 중이에요. 잠시만요!")
            return

        async with lock:
            if view.is_finished():
                await self.respond_ephemeral(interaction, "이미 지나간 장면이에요. 최신 장면에서 행동해주세요!")
                return
            adv = self.parties.get(key)
            if adv is None or not adv.is_playing:
                await self.respond_ephemeral(interaction, "진행 중인 파티 모험이 없어요.")
                return
            # 락 대기 중에 턴이 바뀌었을 수 있으므로 다시 확인.
            actor_id = str(interaction.user.id)
            if actor_id != adv.current_actor_id:
                current = adv.current_character
                await self.respond_ephemeral(
                    interaction, f"지금은 **{current.name if current else '?'}** 의 턴이에요!"
                )
                return

            actor = adv.members[actor_id]

            # 이전 장면 버튼 비활성화 + 인터랙션 응답 (버튼 클릭 / 모달 제출 모두 처리)
            for item in view.children:
                item.disabled = True
            try:
                if not interaction.response.is_done():
                    if interaction.message is not None:
                        await interaction.response.edit_message(view=view)
                    else:
                        await interaction.response.defer()
                if interaction.message is None and view.message:
                    await safe_edit_message(view.message, view=view)
            except discord.HTTPException:
                logger.debug("이전 장면 버튼 비활성화 실패 (무시됨)")
            view.stop()

            channel = interaction.channel
            # 전투 중 지정 행동(공격/방어)은 코드가 명중·피해를 굴리므로 별도 d20 판정을 하지 않는다.
            in_combat_action = adv.combat is not None and (choice or {}).get("combat") in ("attack", "defend")
            check = roll_check(actor, stat, dc) if (stat or fate_roll) and not in_combat_action else None

            header = f"🕹️ **{actor.name}**: {action_text}"
            if check:
                header += f"\n{check.display}"
            try:
                await channel.send(header)
            except discord.HTTPException:
                logger.debug("행동 로그 전송 실패 (무시됨)")

            try:
                async with channel.typing():
                    adv, result = await asyncio.to_thread(
                        run_party_turn, adv, actor_id, action_text, check, choice=choice, model=self._model()
                    )
                    self.parties[key] = adv
            except Exception as e:
                logger.error(f"파티 TRPG 턴 처리 실패: {e}", exc_info=True)
                try:
                    await channel.send(
                        f"⚠️ GM 응답 생성에 실패했어요: {str(e)[:100]}\n장면을 다시 열어드릴게요. 같은 행동을 다시 시도할 수 있어요."
                    )
                except discord.HTTPException:
                    pass
                await self.send_party_scene(channel, key, adv)
                return

            await self._autosave(key, adv)

            if result.combat_log:
                try:
                    await channel.send("\n".join(result.combat_log))
                except discord.HTTPException:
                    logger.debug("전투 로그 전송 실패 (무시됨)")

            if not adv.is_playing:
                try:
                    await channel.send(embed=self.party_ending_embed(adv))
                except discord.HTTPException:
                    logger.exception("파티 엔딩 메시지 전송 실패")
                await self._delete_save(key)
                self.parties.pop(key, None)
                self.active_views.pop(key, None)
                self.locks.pop(key, None)
                return

            await self.send_party_scene(channel, key, adv, result=result)

    async def skip_party_turn(self, interaction: discord.Interaction, key: PartyKey, view: PartyAdventureView):
        lock = self._lock(key)
        if lock.locked():
            await self.respond_ephemeral(interaction, "GM 아리스가 아직 이야기를 쓰는 중이에요. 잠시만요!")
            return

        async with lock:
            adv = self.parties.get(key)
            if adv is None or not adv.is_playing:
                await self.respond_ephemeral(interaction, "진행 중인 파티 모험이 없어요.")
                return
            if view.is_finished():
                await self.respond_ephemeral(interaction, "이미 지나간 장면이에요. 최신 장면에서 조작해주세요!")
                return

            skipped = adv.current_character
            adv.advance_turn()
            await self._autosave(key, adv)

            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except discord.HTTPException:
                pass

            try:
                await interaction.channel.send(
                    f"⏭️ **{skipped.name if skipped else '?'}** 의 턴을 건너뛰었어요.",
                    delete_after=10,
                )
            except discord.HTTPException:
                pass
            await self.send_party_scene(interaction.channel, key, adv)

    # ------------------------------------------------------------------ 중단
    async def suspend_party(self, interaction: discord.Interaction, key: PartyKey, view: PartyAdventureView):
        if self._lock(key).locked():
            await self.respond_ephemeral(interaction, "GM 아리스가 이야기를 쓰는 중에는 중단할 수 없어요. 잠시만요!")
            return

        adv = self.parties.get(key)
        for item in view.children:
            item.disabled = True
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=view)
            elif view.message:
                await safe_edit_message(view.message, view=view)
        except discord.HTTPException:
            logger.debug("중단 시 버튼 비활성화 실패 (무시됨)")
        view.stop()

        if adv is not None:
            await self._autosave(key, adv)
            self.parties.pop(key, None)
        self.active_views.pop(key, None)
        try:
            await interaction.channel.send(
                "💾 파티 모험을 저장하고 잠시 쉬어갑니다. `!파티모험계속` 으로 언제든 이어서 할 수 있어요!"
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ 명령어
    @commands.command(name="파티모험", aliases=["멀티모험", "파티trpg"], help="여럿이 함께하는 파티 TRPG 모험을 시작합니다.")
    async def party_start(self, ctx):
        """호스트가 장르를 고르고 로비를 열어 참가자를 모은다."""
        if not is_local_ai_configured():
            await ctx.send(
                "선생님, 로컬 AI 설정이 비어있어요! `TOKEN.env`에 `LOCAL_AI_BASE_URL`, `LOCAL_AI_MODEL`을 설정해주세요.",
                delete_after=15,
            )
            return

        key = self._make_key(ctx.guild, ctx.channel)
        if key in self.lobbies:
            await ctx.send("이 채널에 이미 모집 중인 파티 로비가 있어요! 위의 로비에서 참가해주세요.", delete_after=15)
            return
        adv = self.parties.get(key)
        if adv is not None and adv.is_playing:
            await ctx.send(
                "이 채널에 이미 진행 중인 파티 모험이 있어요! `!파티모험계속` 으로 이어가거나 `!파티모험종료` 로 끝낼 수 있어요.",
                delete_after=15,
            )
            return

        model = self._model()
        try:
            await asyncio.to_thread(check_model_available, model)
        except FileNotFoundError:
            await ctx.send(
                f"⚠️ 모델 `{model}` 을 찾을 수 없어요.\n"
                "`ollama pull` 로 설치하거나 `TOKEN.env`의 `LOCAL_AI_MODEL` 설정을 확인해주세요.",
                delete_after=20,
            )
            return
        except RuntimeError as e:
            await ctx.send(
                f"⚠️ 로컬 AI 서버에 연결할 수 없어요: {str(e)[:100]}\nOllama가 실행 중인지 확인해주세요.",
                delete_after=20,
            )
            return

        has_save = await asyncio.to_thread(self._load_save_sync, key) is not None
        description = (
            "GM 아리스가 진행하는 **협동 파티 TRPG**입니다.\n"
            f"호스트가 장르를 고르면 로비가 열리고, 최대 {PARTY_MAX_MEMBERS}명이 참가할 수 있어요.\n\n"
            + "\n".join(f"{g['emoji']} **{g['label']}** — {g['hint']}" for g in GENRES.values())
        )
        if has_save:
            description += "\n\n⚠️ 이 채널에 저장된 파티 모험이 있어요. 새로 시작하면 기존 세이브를 덮어씁니다. (`!파티모험계속` 으로 이어하기)"

        embed = discord.Embed(title="🎲 파티 TRPG — 새로운 모험", description=description, color=0x9B59B6)
        view = PartyGenreSelectView(self, key, ctx.author.id)
        try:
            msg = await ctx.send(embed=embed, view=view)
            view.message = msg
        except discord.HTTPException:
            logger.exception("파티 모험 시작 화면 전송 실패")

    @commands.command(name="파티모험계속", aliases=["파티계속"], help="저장된 파티 TRPG 모험을 이어서 진행합니다.")
    async def party_continue(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel)
        if self._lock(key).locked():
            await ctx.send("GM 아리스가 아직 이야기를 쓰는 중이에요. 잠시 후 다시 시도해주세요!", delete_after=10)
            return
        adv = self.parties.get(key)

        if adv is None:
            data = await asyncio.to_thread(self._load_save_sync, key)
            if not data:
                await ctx.send("이 채널에 저장된 파티 모험이 없어요. `!파티모험` 으로 새 모험을 시작해주세요!", delete_after=15)
                return
            try:
                adv = PartyAdventure.from_dict(data)
            except Exception as e:
                logger.error(f"파티 TRPG 세이브 로드 실패: {e}", exc_info=True)
                await ctx.send("세이브 데이터를 읽지 못했어요. `!파티모험` 으로 새 모험을 시작해주세요.", delete_after=15)
                return
            self.parties[key] = adv

        if not adv.is_playing:
            self.parties.pop(key, None)
            await self._delete_save(key)
            await ctx.send("이미 끝난 모험이에요. `!파티모험` 으로 새 모험을 시작해주세요!", delete_after=15)
            return

        adv.ensure_current_alive()
        await ctx.send(f"📖 **{adv.title}** — 파티 모험을 이어서 진행합니다!", delete_after=10)
        await self.send_party_scene(ctx.channel, key, adv)

    @commands.command(name="파티모험상태", aliases=["파티상태"], help="진행 중인 파티 TRPG 상태를 확인합니다.")
    async def party_status(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel)
        if key in self.parties:
            async with self._lock(key):
                adv = self.parties.get(key)
                if adv is not None:
                    await ctx.send(embed=self.party_sheet_embed(adv), delete_after=60)
                    return
        adv = None
        data = await asyncio.to_thread(self._load_save_sync, key)
        if data:
            try:
                adv = PartyAdventure.from_dict(data)
            except Exception:
                adv = None
        if adv is None:
            await ctx.send("이 채널에 진행 중인 파티 모험이 없어요. `!파티모험` 으로 시작해주세요!", delete_after=15)
            return
        await ctx.send(embed=self.party_sheet_embed(adv), delete_after=60)

    @commands.command(name="파티모험종료", aliases=["파티종료"], help="진행 중인 파티 TRPG 모험을 종료하고 세이브를 삭제합니다.")
    async def party_end(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel)

        # 로비만 있는 경우: 호스트가 명령어로도 취소할 수 있게 한다.
        lobby = self.lobbies.get(key)
        if lobby is not None:
            is_admin = getattr(ctx.author.guild_permissions, "manage_guild", False) if ctx.guild else True
            if ctx.author.id != lobby.host_id and not is_admin:
                await ctx.send("로비 취소는 호스트나 관리자만 할 수 있어요.", delete_after=10)
                return
            await self.cancel_lobby(key)
            await ctx.send("🛑 파티 로비를 닫았어요.", delete_after=10)
            return

        adv = self.parties.get(key)
        if adv is None:
            data = await asyncio.to_thread(self._load_save_sync, key)
            if data:
                try:
                    adv = PartyAdventure.from_dict(data)
                except Exception:
                    adv = None

        if adv is None:
            await ctx.send("이 채널에 진행 중인 파티 모험이 없어요.", delete_after=10)
            return

        is_admin = getattr(ctx.author.guild_permissions, "manage_guild", False) if ctx.guild else True
        if str(ctx.author.id) != adv.host_id and not is_admin:
            await ctx.send("파티 모험 종료는 호스트나 관리자만 할 수 있어요.", delete_after=10)
            return

        async with self._lock(key):
            self.parties.pop(key, None)
            await self._delete_save(key)
            await self._retire_active_view(key)
        self.locks.pop(key, None)
        await ctx.send("🛑 파티 모험을 종료하고 세이브를 삭제했어요. 다음 모험에서 만나요, 용사님들!", delete_after=15)

    # ------------------------------------------------------------------ Slash Commands
    async def slash_party_start(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.party_start(ctx)

    async def slash_party_continue(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.party_continue(ctx)

    async def slash_party_status(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.party_status(ctx)

    async def slash_party_end(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.party_end(ctx)

    # ------------------------------------------------------------------ Cog 라이프사이클
    async def cog_load(self):
        self.party_group = discord.app_commands.Group(name="파티모험", description="파티(멀티플레이어) TRPG 명령어")
        self.party_group.command(name="시작", description="파티 TRPG 로비를 엽니다 (채널당 1개)")(self.slash_party_start)
        self.party_group.command(name="계속", description="저장된 파티 모험을 이어서 진행합니다")(self.slash_party_continue)
        self.party_group.command(name="상태", description="파티 상태를 확인합니다")(self.slash_party_status)
        self.party_group.command(name="종료", description="파티 모험을 종료하고 세이브를 삭제합니다")(self.slash_party_end)
        self.bot.tree.add_command(self.party_group, override=True)

    async def cog_unload(self):
        try:
            if self.party_group:
                self.bot.tree.remove_command(self.party_group.name)
        except Exception as e:
            logger.error(f"파티 TRPG Slash Commands 제거 중 오류: {e}")


async def setup(bot):
    await bot.add_cog(TRPGParty(bot))
