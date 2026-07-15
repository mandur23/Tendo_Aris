"""자유 모험(공유 세계) TRPG 명령어 Cog

`!자유모험` 으로 주인이 장르를 고르고 캐릭터를 만들면 하나의 '열린 세계'가 생성된다.
채널의 다른 사용자들은 `!자유모험참가` 나 장면의 🙋 참가 버튼으로 언제든 합류해
각자 자신의 캐릭터와 개인 퀘스트를 가지고 자유롭게 모험한다 (채널당 세계 1개, 최대 6명).

- 파티 모험과 달리 턴 순서가 없다. 멤버 누구든 선택지/자유 행동으로 행동할 수 있다.
- 각자 개인 퀘스트를 받고, 완수하면 GM이 다음 개인 퀘스트를 이어서 준다.
- HP가 0이 된 캐릭터는 동료가 구해줄 때까지 행동할 수 없다 (전원 쓰러지면 필사의 행동 허용).
- 매 행동 자동 저장되며 `!자유모험계속` 으로 이어서 할 수 있다. 세계는 주인이 종료할 때까지 유지된다.

게임 규칙(주사위·HP·인벤토리·퀘스트)은 GameSystem/TRPGEngine.py 의 자유 모험 엔진이 코드로 관리한다.
"""
import asyncio
import functools
import logging
import random
from typing import Dict, Optional, Tuple

import discord
from discord.ext import commands

from GameSystem.TRPGEngine import (
    CLASSES,
    DEFAULT_DC,
    FREE_ACTION_DC,
    GENRES,
    WORLD_MAX_MEMBERS,
    TRPGCharacter,
    WorldActionResult,
    WorldAdventure,
    WorldMember,
    generate_world_join,
    generate_world_quest,
    generate_world_scenario,
    play_world_action,
    roll_check,
)
from cogs.trpg_ui import CharacterSetupModal, combat_status_lines, hp_bar as _hp_bar
from utils.config import LOCAL_AI_MODEL
from utils.discord_utils import AuthorLockedView, safe_defer, safe_edit_message
from utils.file_utils import load_trpg_world_saves, save_trpg_world_saves
from utils.llm_utils import check_model_available, is_local_ai_configured

logger = logging.getLogger(__name__)

# (guild_id_or_0, channel_id) — 자유 모험 세계는 채널당 1개
WorldKey = Tuple[int, int]

SELECT_VIEW_TIMEOUT = 180       # 장르/직업 선택 대기 시간 (초)
ADVENTURE_VIEW_TIMEOUT = 1800   # 장면 버튼 유지 시간 (초). 만료돼도 !자유모험계속 으로 재개 가능.


class WorldGenreSelectView(AuthorLockedView):
    """세계를 여는 사람이 장르를 고르는 버튼 뷰."""

    def __init__(self, cog: "TRPGWorld", key: WorldKey, owner_id: int):
        super().__init__(author_id=owner_id, timeout=SELECT_VIEW_TIMEOUT)
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
        await self.cog.show_owner_class_select(interaction, self.key, genre_key)


class WorldOwnerClassSelectView(AuthorLockedView):
    """세계를 여는 사람이 직업을 고르는 버튼 뷰. 직업을 고르면 캐릭터 모달이 열린다."""

    def __init__(self, cog: "TRPGWorld", key: WorldKey, owner_id: int, genre_key: str):
        super().__init__(author_id=owner_id, timeout=SELECT_VIEW_TIMEOUT)
        self.cog = cog
        self.key = key
        self.genre_key = genre_key

        for class_key, spec in CLASSES.items():
            btn = discord.ui.Button(label=f"{spec['emoji']} {spec['label']}", style=discord.ButtonStyle.primary)
            btn.callback = functools.partial(self._class_cb, class_key=class_key)
            self.add_item(btn)

    async def _class_cb(self, interaction: discord.Interaction, class_key: str):
        origin_message = interaction.message

        async def _submit(modal_itx: discord.Interaction, name: str, race: str, background: str):
            self.stop()
            character = TRPGCharacter.create(name, class_key, race=race, background=background)
            await self.cog.create_world(modal_itx, self.key, self.genre_key, character, origin_message)

        await interaction.response.send_modal(
            CharacterSetupModal(default_name=interaction.user.display_name, on_submit_cb=_submit)
        )


class WorldJoinClassSelectView(AuthorLockedView):
    """합류하려는 사용자가 직업을 고르는 (ephemeral) 버튼 뷰."""

    def __init__(self, cog: "TRPGWorld", key: WorldKey, user_id: int):
        super().__init__(author_id=user_id, timeout=SELECT_VIEW_TIMEOUT)
        self.cog = cog
        self.key = key

        for class_key, spec in CLASSES.items():
            btn = discord.ui.Button(label=f"{spec['emoji']} {spec['label']}", style=discord.ButtonStyle.primary)
            btn.callback = functools.partial(self._class_cb, class_key=class_key)
            self.add_item(btn)

    async def _class_cb(self, interaction: discord.Interaction, class_key: str):
        async def _submit(modal_itx: discord.Interaction, name: str, race: str, background: str):
            self.stop()
            character = TRPGCharacter.create(name, class_key, race=race, background=background)
            await self.cog.join_world(modal_itx, self.key, character)

        await interaction.response.send_modal(
            CharacterSetupModal(default_name=interaction.user.display_name, on_submit_cb=_submit)
        )


class WorldFreeActionModal(discord.ui.Modal, title="✍️ 자유 행동"):
    """선택지에 없는 행동을 직접 입력받는 모달. 운명 판정(d20, 보정 없음)이 따라붙는다."""

    action = discord.ui.TextInput(
        label="무엇을 하시겠습니까?",
        placeholder="예: 술집 주인에게 소문에 대해 캐묻는다",
        max_length=150,
    )

    def __init__(self, cog: "TRPGWorld", key: WorldKey, view: "WorldAdventureView"):
        super().__init__()
        self.cog = cog
        self.key = key
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        action_text = str(self.action).strip()
        if not action_text:
            await self.cog.respond_ephemeral(interaction, "행동 내용이 비어있어요.")
            return
        await self.cog.process_world_action(
            interaction, self.key, action_text,
            stat=None, dc=FREE_ACTION_DC, fate_roll=True, view=self.view,
        )


class WorldAdventureView(discord.ui.View):
    """현재 장면의 선택지 버튼 + 자유 행동/참가/시트/중단 컨트롤.

    턴 순서가 없으므로 선택지·자유 행동은 멤버 누구나 쓸 수 있다.
    참가는 비멤버 누구나, 중단(저장)은 세계 주인만 누를 수 있다.
    """

    def __init__(self, cog: "TRPGWorld", key: WorldKey, adv: WorldAdventure):
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

        join_btn = discord.ui.Button(label="🙋 참가", style=discord.ButtonStyle.primary, row=2)
        join_btn.callback = self._join_cb
        self.add_item(join_btn)

        sheet_btn = discord.ui.Button(label="🧾 시트", style=discord.ButtonStyle.secondary, row=2)
        sheet_btn.callback = self._sheet_cb
        self.add_item(sheet_btn)

        suspend_btn = discord.ui.Button(label="💾 중단(주인)", style=discord.ButtonStyle.danger, row=2)
        suspend_btn.callback = self._suspend_cb
        self.add_item(suspend_btn)

    async def _check_actor(self, interaction: discord.Interaction) -> Optional[WorldAdventure]:
        """행동 가능한 멤버인지 확인한다. 아니면 안내 후 None."""
        adv = self.cog.worlds.get(self.key)
        if adv is None:
            await self.cog.respond_ephemeral(interaction, "세계 정보를 찾을 수 없어요. `!자유모험계속` 으로 다시 불러와주세요.")
            return None
        member = adv.members.get(str(interaction.user.id))
        if member is None:
            await self.cog.respond_ephemeral(interaction, "아직 이 세계의 모험가가 아니에요! 🙋 참가 버튼으로 합류할 수 있어요.")
            return None
        # 쓰러진 캐릭터는 동료가 구해줄 때까지 행동 불가. 단, 전원 쓰러졌다면 필사의 행동을 허용한다.
        if member.character.hp <= 0 and adv.alive_ids():
            await self.cog.respond_ephemeral(
                interaction, f"**{member.character.name}** 은(는) 쓰러져 있어요... 동료가 구해줄 때까지 기다려주세요!"
            )
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
        await self.cog.process_world_action(
            interaction, self.key, choice["text"],
            stat=choice.get("stat"), dc=choice.get("dc", DEFAULT_DC), fate_roll=False, view=self,
            choice=choice,
        )

    async def _free_cb(self, interaction: discord.Interaction):
        adv = await self._check_actor(interaction)
        if adv is None:
            return
        await interaction.response.send_modal(WorldFreeActionModal(self.cog, self.key, self))

    async def _join_cb(self, interaction: discord.Interaction):
        await self.cog.offer_join(interaction, self.key)

    async def _sheet_cb(self, interaction: discord.Interaction):
        adv = self.cog.worlds.get(self.key)
        if adv is None:
            await self.cog.respond_ephemeral(interaction, "세계 정보를 찾을 수 없어요.")
            return
        try:
            embed = self.cog.world_sheet_embed(adv)
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
        except discord.HTTPException:
            logger.exception("세계 시트 전송 실패")

    async def _suspend_cb(self, interaction: discord.Interaction):
        adv = self.cog.worlds.get(self.key)
        if adv is not None and str(interaction.user.id) != adv.owner_id:
            await self.cog.respond_ephemeral(interaction, "중단(저장)은 세계의 주인만 할 수 있어요.")
            return
        await self.cog.suspend_world(interaction, self.key, self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            await safe_edit_message(self.message, view=self)


class TRPGWorld(commands.Cog):
    """자유 모험(공유 세계) TRPG — GM 아리스가 진행하는 열린 세계 텍스트 어드벤처."""

    def __init__(self, bot):
        self.bot = bot
        self.world_group = None
        self.worlds: Dict[WorldKey, WorldAdventure] = {}
        self.locks: Dict[WorldKey, asyncio.Lock] = {}
        self.active_views: Dict[WorldKey, WorldAdventureView] = {}

    # ------------------------------------------------------------------ 공통 유틸
    @staticmethod
    def _make_key(guild: Optional[discord.Guild], channel) -> WorldKey:
        guild_id = guild.id if guild else 0
        return (guild_id, getattr(channel, "id", 0))

    @staticmethod
    def _key_str(key: WorldKey) -> str:
        return f"{key[0]}:{key[1]}"

    def _model(self) -> str:
        """ChatAI Cog가 모델 자동 전환을 했다면 그 모델을 함께 사용한다."""
        chat_cog = self.bot.get_cog("ChatAI")
        return getattr(chat_cog, "active_model", None) or LOCAL_AI_MODEL

    def _lock(self, key: WorldKey) -> asyncio.Lock:
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
    def _save_sync(self, key: WorldKey, adv_dict: dict):
        saves = load_trpg_world_saves()
        saves[self._key_str(key)] = adv_dict
        save_trpg_world_saves(saves)

    def _delete_save_sync(self, key: WorldKey):
        saves = load_trpg_world_saves()
        if saves.pop(self._key_str(key), None) is not None:
            save_trpg_world_saves(saves)

    def _load_save_sync(self, key: WorldKey) -> Optional[dict]:
        return load_trpg_world_saves().get(self._key_str(key))

    async def _autosave(self, key: WorldKey, adv: WorldAdventure):
        try:
            await asyncio.to_thread(self._save_sync, key, adv.to_dict())
        except Exception as e:
            logger.error(f"자유 모험 자동 저장 실패: {e}")

    async def _delete_save(self, key: WorldKey):
        try:
            await asyncio.to_thread(self._delete_save_sync, key)
        except Exception as e:
            logger.error(f"자유 모험 세이브 삭제 실패: {e}")

    async def _get_or_load_world(self, key: WorldKey) -> Optional[WorldAdventure]:
        """메모리의 세계를 반환하고, 없으면 디스크 세이브에서 복원한다."""
        adv = self.worlds.get(key)
        if adv is not None:
            return adv
        data = await asyncio.to_thread(self._load_save_sync, key)
        if not data:
            return None
        try:
            adv = WorldAdventure.from_dict(data)
        except Exception as e:
            logger.error(f"자유 모험 세이브 로드 실패: {e}", exc_info=True)
            return None
        self.worlds[key] = adv
        return adv

    # ------------------------------------------------------------------ 임베드
    def _member_status_lines(self, adv: WorldAdventure) -> str:
        lines = []
        for uid, member in adv.members.items():
            char = member.character
            marker = "💀 " if char.hp <= 0 else ""
            owner_mark = " 👑" if uid == adv.owner_id else ""
            lines.append(f"{marker}{char.job_emoji} **{char.name}** ({char.job}){owner_mark} {_hp_bar(char.hp, char.max_hp)}")
        return "\n".join(lines) if lines else "아직 아무도 없음"

    def world_scene_embed(
        self,
        adv: WorldAdventure,
        *,
        result: Optional[WorldActionResult] = None,
        new_quest: str = "",
        opening: bool = False,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{adv.genre_emoji} {adv.title} — {adv.turn}번째 이야기",
            description=adv.scene[:4000],
            color=0x1ABC9C,
        )
        if opening and adv.world:
            embed.add_field(name="🌍 세계관", value=adv.world[:1024], inline=False)

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
            if result.quest_completed:
                value = "🎉 개인 퀘스트를 완수했어요!"
                if new_quest:
                    value += f"\n🎯 다음 퀘스트: {new_quest}"
                embed.add_field(name="🏅 퀘스트", value=value[:1024], inline=False)

        if adv.combat is not None:
            embed.add_field(name="⚔️ 전투 중!", value=combat_status_lines(adv.combat)[:1024], inline=False)

        embed.add_field(name="👥 모험가들", value=self._member_status_lines(adv)[:1024], inline=False)
        footer = "턴 순서 없음 — 멤버 누구든 버튼으로 행동하거나 ✍️ 자유 행동으로 직접 입력하세요. 🙋 참가로 합류!"
        if adv.combat is not None:
            footer = "⚔️ 전투 중! 공격/방어 버튼이나 ✍️ 자유 행동(물약·도주·꾀)으로 싸우세요."
        embed.set_footer(text=footer)
        return embed

    def world_sheet_embed(self, adv: WorldAdventure) -> discord.Embed:
        embed = discord.Embed(
            title=f"👥 모험가 시트 — {adv.genre_emoji} {adv.title}",
            description=f"{adv.genre_label} · 누적 {adv.turn}번의 행동 · {len(adv.members)}/{WORLD_MAX_MEMBERS}명",
            color=0x3498DB,
        )
        for uid, member in adv.members.items():
            char = member.character
            status = " (쓰러짐 💀)" if char.hp <= 0 else ""
            owner_mark = " 👑" if uid == adv.owner_id else ""
            inventory = ", ".join(char.inventory) if char.inventory else "비어 있음"
            race_part = f"종족: {char.race}\n" if char.race else ""
            quest_part = f"🎯 {member.quest}" if member.quest else "🎯 (다음 퀘스트를 기다리는 중)"
            embed.add_field(
                name=f"{char.job_emoji} {char.name} — {char.job}{status}{owner_mark}",
                value=(
                    f"HP {_hp_bar(char.hp, char.max_hp)}\n"
                    f"{race_part}"
                    f"능력치: {char.stats_line()}\n"
                    f"소지품: {inventory}\n"
                    f"{quest_part} (완수 {member.quests_done}회)"
                )[:1024],
                inline=False,
            )
        return embed

    def world_ending_embed(self, adv: WorldAdventure) -> discord.Embed:
        embed = discord.Embed(
            title="🌅 세계의 문이 닫혔다",
            description=adv.scene[:4000],
            color=0x95A5A6,
        )
        member_lines = "\n".join(
            f"{m.character.job_emoji} {m.character.name} ({m.character.job}) — 퀘스트 완수 {m.quests_done}회"
            for m in adv.members.values()
        )
        embed.add_field(
            name="모험 기록",
            value=f"{adv.genre_emoji} {adv.title} · 누적 {adv.turn}번의 행동\n{member_lines}"[:1024],
            inline=False,
        )
        embed.set_footer(text="`!자유모험` 으로 새로운 세계를 열 수 있어요.")
        return embed

    # ------------------------------------------------------------------ 뷰 관리
    async def _retire_active_view(self, key: WorldKey):
        """이전 장면 뷰를 중지·비활성화해 오래된 메시지에서 중복 조작을 막는다."""
        old = self.active_views.pop(key, None)
        if old is None or old.is_finished():
            return
        old.stop()
        for item in old.children:
            item.disabled = True
        if old.message:
            await safe_edit_message(old.message, view=old)

    async def send_world_scene(
        self,
        channel: discord.abc.Messageable,
        key: WorldKey,
        adv: WorldAdventure,
        *,
        result: Optional[WorldActionResult] = None,
        new_quest: str = "",
        opening: bool = False,
    ):
        await self._retire_active_view(key)
        embed = self.world_scene_embed(adv, result=result, new_quest=new_quest, opening=opening)
        view = WorldAdventureView(self, key, adv)
        try:
            msg = await channel.send(embed=embed, view=view)
            view.message = msg
            self.active_views[key] = view
        except discord.HTTPException:
            logger.exception("자유 모험 장면 전송 실패")

    # ------------------------------------------------------------------ 세계 생성 플로우
    async def show_owner_class_select(self, interaction: discord.Interaction, key: WorldKey, genre_key: str):
        genre = GENRES[genre_key]
        embed = discord.Embed(
            title=f"{genre['emoji']} {genre['label']} 세계 — 직업 선택",
            description=(
                f"{genre['hint']}.\n\n"
                "첫 모험가(주인)의 직업을 선택하세요! 직업을 고르면 이름·종족·배경을 입력할 수 있어요."
            ),
            color=0x1ABC9C,
        )
        for spec in CLASSES.values():
            stats = " / ".join(f"{k} +{v}" if v > 0 else f"{k} {v}" for k, v in spec["stats"].items())
            embed.add_field(
                name=f"{spec['emoji']} {spec['label']}",
                value=f"HP {spec['hp']}\n{stats}\n소지품: {', '.join(spec['items'])}",
                inline=True,
            )
        view = WorldOwnerClassSelectView(self, key, interaction.user.id, genre_key)
        try:
            await interaction.response.edit_message(embed=embed, view=view)
            view.message = interaction.message
        except discord.HTTPException:
            logger.exception("직업 선택 화면 표시 실패")

    async def create_world(
        self,
        interaction: discord.Interaction,
        key: WorldKey,
        genre_key: str,
        character: TRPGCharacter,
        origin_message: Optional[discord.Message],
    ):
        lock = self._lock(key)
        if lock.locked():
            await self.respond_ephemeral(interaction, "이미 세계 생성이 진행 중이에요. 잠시만요!")
            return

        genre = GENRES[genre_key]
        progress = discord.Embed(
            title=f"{genre['emoji']} {genre['label']} 세계 창조 중...",
            description=(
                "🖋️ GM 아리스가 열린 세계를 창조하는 중입니다...\n"
                "로컬 AI 성능에 따라 수십 초 정도 걸릴 수 있어요."
            ),
            color=0x1ABC9C,
        )
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.HTTPException:
            pass
        if origin_message is not None:
            await safe_edit_message(origin_message, embed=progress, view=None)

        async with lock:
            existing = self.worlds.get(key)
            if existing is not None and existing.is_playing:
                return

            try:
                async with interaction.channel.typing():
                    adv = await asyncio.to_thread(
                        generate_world_scenario,
                        genre_key,
                        str(interaction.user.id),
                        character,
                        model=self._model(),
                    )
            except FileNotFoundError:
                await self._fail_progress(
                    interaction, origin_message,
                    f"⚠️ 모델 `{self._model()}` 을 찾을 수 없어요.\n"
                    "`ollama pull` 로 설치하거나 `TOKEN.env`의 `LOCAL_AI_MODEL` 설정을 확인해주세요.",
                )
                return
            except Exception as e:
                logger.error(f"자유 모험 세계 생성 실패: {e}", exc_info=True)
                await self._fail_progress(
                    interaction, origin_message,
                    f"⚠️ 세계 창조에 실패했어요: {str(e)[:100]}\n`!자유모험` 으로 다시 시도해주세요.",
                )
                return

            self.worlds[key] = adv
            await self._autosave(key, adv)

            if origin_message is not None:
                finish = discord.Embed(
                    title=f"{genre['emoji']} {genre['label']} 세계 창조 완료!",
                    description="열린 세계가 깨어났습니다. 아래에서 모험이 시작됩니다.",
                    color=0x2ECC71,
                )
                await safe_edit_message(origin_message, embed=finish, view=None)

            member = adv.members.get(str(interaction.user.id))
            if member is not None and member.quest:
                try:
                    await interaction.channel.send(
                        f"🎯 **{member.character.name}** 의 개인 퀘스트: {member.quest}"
                    )
                except discord.HTTPException:
                    pass
            await self.send_world_scene(interaction.channel, key, adv, opening=True)

    async def _fail_progress(
        self,
        interaction: discord.Interaction,
        origin_message: Optional[discord.Message],
        description: str,
    ):
        """'창조 중...' 메시지를 실패 안내로 바꾼다. 편집이 안 되면 채널에 안내를 보낸다."""
        embed = discord.Embed(title="🌍 자유 모험 TRPG", description=description, color=0xE74C3C)
        edited = False
        if origin_message is not None:
            edited = await safe_edit_message(origin_message, embed=embed, view=None)
        if not edited:
            try:
                await interaction.channel.send(description, delete_after=20)
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------ 합류 플로우
    async def offer_join(self, interaction: discord.Interaction, key: WorldKey):
        """직업 선택 뷰를 ephemeral로 보여 합류 절차를 시작한다."""
        adv = await self._get_or_load_world(key)
        if adv is None or not adv.is_playing:
            await self.respond_ephemeral(interaction, "이 채널에 열려 있는 세계가 없어요. `!자유모험` 으로 새 세계를 열 수 있어요!")
            return
        if str(interaction.user.id) in adv.members:
            await self.respond_ephemeral(interaction, "이미 이 세계의 모험가예요! 장면 버튼으로 바로 행동할 수 있어요.")
            return
        if len(adv.members) >= WORLD_MAX_MEMBERS:
            await self.respond_ephemeral(interaction, f"세계가 가득 찼어요! (최대 {WORLD_MAX_MEMBERS}명)")
            return

        view = WorldJoinClassSelectView(self, key, interaction.user.id)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("합류할 캐릭터의 직업을 선택하세요!", view=view, ephemeral=True)
            else:
                await interaction.followup.send("합류할 캐릭터의 직업을 선택하세요!", view=view, ephemeral=True)
        except discord.HTTPException:
            logger.exception("합류 직업 선택 표시 실패")

    async def join_world(self, interaction: discord.Interaction, key: WorldKey, character: TRPGCharacter):
        lock = self._lock(key)
        if lock.locked():
            await self.respond_ephemeral(interaction, "GM 아리스가 아직 이야기를 쓰는 중이에요. 잠시 후 다시 시도해주세요!")
            return

        async with lock:
            adv = await self._get_or_load_world(key)
            if adv is None or not adv.is_playing:
                await self.respond_ephemeral(interaction, "이 채널에 열려 있는 세계가 없어요.")
                return
            user_id = str(interaction.user.id)
            if user_id in adv.members:
                await self.respond_ephemeral(interaction, "이미 이 세계의 모험가예요!")
                return
            if len(adv.members) >= WORLD_MAX_MEMBERS:
                await self.respond_ephemeral(interaction, f"세계가 가득 찼어요! (최대 {WORLD_MAX_MEMBERS}명)")
                return

            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass

            try:
                async with interaction.channel.typing():
                    join_data = await asyncio.to_thread(
                        generate_world_join, adv, character, model=self._model()
                    )
            except Exception as e:
                logger.error(f"자유 모험 합류 서술 생성 실패: {e}", exc_info=True)
                await self.respond_ephemeral(
                    interaction, f"⚠️ 합류 서술 생성에 실패했어요: {str(e)[:100]}\n잠시 후 다시 시도해주세요."
                )
                return

            adv.members[user_id] = WorldMember(character=character, quest=join_data["quest"])
            adv.add_event(f"{character.name} ({character.job}) 이(가) 세계에 합류했다")
            await self._autosave(key, adv)

            lines = [f"🙋 **{character.name}** ({character.job}) 이(가) 세계에 합류했어요!", "", join_data["arrival"]]
            if join_data["quest"]:
                lines.append(f"\n🎯 **{character.name}** 의 개인 퀘스트: {join_data['quest']}")
            try:
                await interaction.channel.send("\n".join(lines))
            except discord.HTTPException:
                logger.debug("합류 안내 전송 실패 (무시됨)")
            await self.send_world_scene(interaction.channel, key, adv)

    # ------------------------------------------------------------------ 행동 진행
    async def process_world_action(
        self,
        interaction: discord.Interaction,
        key: WorldKey,
        action_text: str,
        *,
        stat: Optional[str],
        dc: int,
        fate_roll: bool,
        view: WorldAdventureView,
        choice: Optional[dict] = None,
    ):
        adv = self.worlds.get(key)
        if adv is None or not adv.is_playing:
            await self.respond_ephemeral(interaction, "진행 중인 세계가 없어요. `!자유모험계속` 으로 다시 불러와주세요.")
            return

        lock = self._lock(key)
        if lock.locked():
            await self.respond_ephemeral(interaction, "GM 아리스가 아직 이야기를 쓰는 중이에요. 잠시만요!")
            return

        async with lock:
            # 더블 클릭 대비: 락 대기 중에 같은 뷰의 다른 클릭이 먼저 처리됐다면 중단.
            if view.is_finished():
                await self.respond_ephemeral(interaction, "이미 지나간 장면이에요. 최신 장면에서 행동해주세요!")
                return
            adv = self.worlds.get(key)
            if adv is None or not adv.is_playing:
                await self.respond_ephemeral(interaction, "진행 중인 세계가 없어요.")
                return
            actor_id = str(interaction.user.id)
            member = adv.members.get(actor_id)
            if member is None:
                await self.respond_ephemeral(interaction, "아직 이 세계의 모험가가 아니에요!")
                return
            actor = member.character
            if actor.hp <= 0 and adv.alive_ids():
                await self.respond_ephemeral(
                    interaction, f"**{actor.name}** 은(는) 쓰러져 있어요... 동료가 구해줄 때까지 기다려주세요!"
                )
                return

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
                    result = await asyncio.to_thread(
                        play_world_action, adv, actor_id, action_text, check, choice=choice, model=self._model()
                    )
            except Exception as e:
                logger.error(f"자유 모험 행동 처리 실패: {e}", exc_info=True)
                try:
                    await channel.send(
                        f"⚠️ GM 응답 생성에 실패했어요: {str(e)[:100]}\n장면을 다시 열어드릴게요. 같은 행동을 다시 시도할 수 있어요."
                    )
                except discord.HTTPException:
                    pass
                await self.send_world_scene(channel, key, adv)
                return

            if result.combat_log:
                try:
                    await channel.send("\n".join(result.combat_log))
                except discord.HTTPException:
                    logger.debug("전투 로그 전송 실패 (무시됨)")

            # 개인 퀘스트를 완수했다면 다음 퀘스트를 이어서 생성한다 (실패해도 진행은 계속).
            new_quest = ""
            if result.quest_completed:
                try:
                    async with channel.typing():
                        new_quest = await asyncio.to_thread(
                            generate_world_quest, adv, actor, model=self._model()
                        )
                except Exception as e:
                    logger.error(f"다음 개인 퀘스트 생성 실패: {e}", exc_info=True)
                    new_quest = ""
                if new_quest:
                    member.quest = new_quest
                    adv.add_event(f"{actor.name} 이(가) 새 개인 퀘스트를 받았다: {new_quest[:60]}")

            await self._autosave(key, adv)
            await self.send_world_scene(channel, key, adv, result=result, new_quest=new_quest)

    # ------------------------------------------------------------------ 중단
    async def suspend_world(self, interaction: discord.Interaction, key: WorldKey, view: WorldAdventureView):
        if self._lock(key).locked():
            await self.respond_ephemeral(interaction, "GM 아리스가 이야기를 쓰는 중에는 중단할 수 없어요. 잠시만요!")
            return

        adv = self.worlds.get(key)
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
            self.worlds.pop(key, None)
        self.active_views.pop(key, None)
        try:
            await interaction.channel.send(
                "💾 세계를 저장하고 잠시 문을 닫습니다. `!자유모험계속` 으로 언제든 다시 열 수 있어요!"
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ 명령어
    @commands.command(name="자유모험", aliases=["월드모험", "자유trpg"], help="여럿이 드나드는 자유 모험(공유 세계) TRPG를 시작합니다.")
    async def world_start(self, ctx):
        """주인이 장르·직업·캐릭터를 정해 새 공유 세계를 연다."""
        if not is_local_ai_configured():
            await ctx.send(
                "선생님, 로컬 AI 설정이 비어있어요! `TOKEN.env`에 `LOCAL_AI_BASE_URL`, `LOCAL_AI_MODEL`을 설정해주세요.",
                delete_after=15,
            )
            return

        key = self._make_key(ctx.guild, ctx.channel)
        adv = self.worlds.get(key)
        if adv is not None and adv.is_playing:
            await ctx.send(
                "이 채널에 이미 열려 있는 세계가 있어요! `!자유모험참가` 로 합류하거나 `!자유모험계속` 으로 이어갈 수 있어요.",
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
            "GM 아리스가 진행하는 **자유 모험(공유 세계)** TRPG입니다.\n"
            "턴 순서 없이 각자 개인 퀘스트를 가지고 자유롭게 모험해요.\n"
            f"세계가 열리면 최대 {WORLD_MAX_MEMBERS}명까지 언제든 합류할 수 있어요.\n\n"
            + "\n".join(f"{g['emoji']} **{g['label']}** — {g['hint']}" for g in GENRES.values())
        )
        if has_save:
            description += "\n\n⚠️ 이 채널에 저장된 세계가 있어요. 새로 열면 기존 세이브를 덮어씁니다. (`!자유모험계속` 으로 이어하기)"

        embed = discord.Embed(title="🌍 자유 모험 TRPG — 새로운 세계", description=description, color=0x1ABC9C)
        view = WorldGenreSelectView(self, key, ctx.author.id)
        try:
            msg = await ctx.send(embed=embed, view=view)
            view.message = msg
        except discord.HTTPException:
            logger.exception("자유 모험 시작 화면 전송 실패")

    @commands.command(name="자유모험참가", aliases=["월드참가", "자유참가"], help="이 채널의 자유 모험 세계에 합류합니다.")
    async def world_join_cmd(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel)
        adv = await self._get_or_load_world(key)
        if adv is None or not adv.is_playing:
            await ctx.send("이 채널에 열려 있는 세계가 없어요. `!자유모험` 으로 새 세계를 열 수 있어요!", delete_after=15)
            return
        if str(ctx.author.id) in adv.members:
            await ctx.send("이미 이 세계의 모험가예요! 장면 버튼으로 바로 행동할 수 있어요.", delete_after=10)
            return
        if len(adv.members) >= WORLD_MAX_MEMBERS:
            await ctx.send(f"세계가 가득 찼어요! (최대 {WORLD_MAX_MEMBERS}명)", delete_after=10)
            return

        view = WorldJoinClassSelectView(self, key, ctx.author.id)
        try:
            msg = await ctx.send(f"{ctx.author.mention} 합류할 캐릭터의 직업을 선택하세요!", view=view)
            view.message = msg
        except discord.HTTPException:
            logger.exception("합류 직업 선택 표시 실패")

    @commands.command(name="자유모험계속", aliases=["월드계속", "자유계속"], help="저장된 자유 모험 세계를 이어서 진행합니다.")
    async def world_continue(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel)
        if self._lock(key).locked():
            await ctx.send("GM 아리스가 아직 이야기를 쓰는 중이에요. 잠시 후 다시 시도해주세요!", delete_after=10)
            return

        adv = await self._get_or_load_world(key)
        if adv is None:
            await ctx.send("이 채널에 저장된 세계가 없어요. `!자유모험` 으로 새 세계를 열어주세요!", delete_after=15)
            return
        if not adv.is_playing:
            self.worlds.pop(key, None)
            await self._delete_save(key)
            await ctx.send("이미 닫힌 세계예요. `!자유모험` 으로 새 세계를 열어주세요!", delete_after=15)
            return

        await ctx.send(f"📖 **{adv.title}** — 세계의 문이 다시 열립니다!", delete_after=10)
        await self.send_world_scene(ctx.channel, key, adv)

    @commands.command(name="자유모험상태", aliases=["월드상태", "자유상태"], help="자유 모험 세계의 모험가 시트를 확인합니다.")
    async def world_status(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel)
        adv = await self._get_or_load_world(key)
        if adv is None:
            await ctx.send("이 채널에 진행 중인 세계가 없어요. `!자유모험` 으로 시작해주세요!", delete_after=15)
            return
        await ctx.send(embed=self.world_sheet_embed(adv), delete_after=60)

    @commands.command(name="자유모험이탈", aliases=["월드이탈", "자유이탈"], help="자유 모험 세계에서 내 캐릭터를 이탈시킵니다.")
    async def world_leave(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel)
        async with self._lock(key):
            adv = await self._get_or_load_world(key)
            if adv is None or not adv.is_playing:
                await ctx.send("이 채널에 열려 있는 세계가 없어요.", delete_after=10)
                return
            user_id = str(ctx.author.id)
            member = adv.members.pop(user_id, None)
            if member is None:
                await ctx.send("이 세계의 모험가가 아니에요.", delete_after=10)
                return

            # 마지막 모험가가 떠나면 세계도 닫는다.
            if not adv.members:
                self.worlds.pop(key, None)
                await self._delete_save(key)
                await self._retire_active_view(key)
                await ctx.send(
                    f"🚪 **{member.character.name}** 이(가) 세계를 떠났어요. 마지막 모험가가 떠나 세계의 문이 닫혔습니다.",
                    delete_after=20,
                )
                return

            # 주인이 떠나면 남은 첫 모험가에게 주인 자리를 넘긴다.
            notice = f"🚪 **{member.character.name}** 이(가) 세계를 떠났어요."
            if user_id == adv.owner_id:
                adv.owner_id = next(iter(adv.members))
                new_owner = adv.members[adv.owner_id].character
                notice += f" 세계의 주인이 **{new_owner.name}** 에게 넘어갔어요. 👑"
            adv.add_event(f"{member.character.name} 이(가) 세계를 떠났다")
            await self._autosave(key, adv)
            await ctx.send(notice, delete_after=20)

    @commands.command(name="자유모험종료", aliases=["월드종료", "자유종료"], help="자유 모험 세계를 닫고 세이브를 삭제합니다.")
    async def world_end(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel)
        adv = await self._get_or_load_world(key)
        if adv is None:
            await ctx.send("이 채널에 진행 중인 세계가 없어요.", delete_after=10)
            return

        is_admin = getattr(ctx.author.guild_permissions, "manage_guild", False) if ctx.guild else True
        if str(ctx.author.id) != adv.owner_id and not is_admin:
            await ctx.send("세계 종료는 세계의 주인이나 관리자만 할 수 있어요.", delete_after=10)
            return

        async with self._lock(key):
            adv.status = "closed"
            self.worlds.pop(key, None)
            await self._delete_save(key)
            await self._retire_active_view(key)
        self.locks.pop(key, None)
        try:
            await ctx.send(embed=self.world_ending_embed(adv))
        except discord.HTTPException:
            await ctx.send("🌅 세계를 닫고 세이브를 삭제했어요. 다음 세계에서 만나요!", delete_after=15)

    # ------------------------------------------------------------------ Slash Commands
    async def slash_world_start(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.world_start(ctx)

    async def slash_world_join(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.world_join_cmd(ctx)

    async def slash_world_continue(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.world_continue(ctx)

    async def slash_world_status(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.world_status(ctx)

    async def slash_world_leave(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.world_leave(ctx)

    async def slash_world_end(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.world_end(ctx)

    # ------------------------------------------------------------------ Cog 라이프사이클
    async def cog_load(self):
        self.world_group = discord.app_commands.Group(name="자유모험", description="자유 모험(공유 세계) TRPG 명령어")
        self.world_group.command(name="시작", description="새 공유 세계를 엽니다 (채널당 1개)")(self.slash_world_start)
        self.world_group.command(name="참가", description="이 채널의 세계에 합류합니다")(self.slash_world_join)
        self.world_group.command(name="계속", description="저장된 세계를 이어서 진행합니다")(self.slash_world_continue)
        self.world_group.command(name="상태", description="모험가 시트를 확인합니다")(self.slash_world_status)
        self.world_group.command(name="이탈", description="세계에서 내 캐릭터를 이탈시킵니다")(self.slash_world_leave)
        self.world_group.command(name="종료", description="세계를 닫고 세이브를 삭제합니다")(self.slash_world_end)
        self.bot.tree.add_command(self.world_group, override=True)

    async def cog_unload(self):
        try:
            if self.world_group:
                self.bot.tree.remove_command(self.world_group.name)
        except Exception as e:
            logger.error(f"자유 모험 Slash Commands 제거 중 오류: {e}")


async def setup(bot):
    await bot.add_cog(TRPGWorld(bot))
