"""AI 생성형 TRPG 명령어 Cog

`!모험` 으로 장르와 직업을 고르면 로컬 LLM(Ollama)이 세계관·퀘스트·장면을
생성하는 1인용 TRPG 를 진행한다. 캐릭터 시트·HP·인벤토리·주사위 판정은
GameSystem/TRPGEngine.py 가 코드로 관리하고, LLM 은 서술과 선택지 생성만 담당한다.

GM 페르소나는 '아리스'. 매 턴 자동 저장되며 `!모험계속` 으로 이어서 할 수 있다.
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
    TRPGAdventure,
    TRPGCharacter,
    TurnResult,
    generate_scenario,
    play_turn,
    roll_check,
    roll_dice,
)
from cogs.trpg_ui import CharacterSetupModal, combat_status_lines, hp_bar as _hp_bar
from utils.config import LOCAL_AI_MODEL
from utils.discord_utils import AuthorLockedView, safe_defer, safe_edit_message
from utils.file_utils import load_trpg_saves, save_trpg_saves
from utils.llm_utils import check_model_available, is_local_ai_configured

logger = logging.getLogger(__name__)

# (guild_id_or_0, channel_id, user_id)
SessionKey = Tuple[int, int, int]

SELECT_VIEW_TIMEOUT = 180       # 장르/직업 선택 대기 시간 (초)
ADVENTURE_VIEW_TIMEOUT = 1800   # 장면 버튼 유지 시간 (초). 만료돼도 !모험계속 으로 재개 가능.


class GenreSelectView(AuthorLockedView):
    """새 모험의 장르를 고르는 버튼 뷰."""

    def __init__(self, cog: "TRPG", key: SessionKey):
        super().__init__(author_id=key[2], timeout=SELECT_VIEW_TIMEOUT)
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
        await self.cog.show_class_select(interaction, self.key, genre_key)


class ClassSelectView(AuthorLockedView):
    """주인공 직업을 고르는 버튼 뷰."""

    def __init__(self, cog: "TRPG", key: SessionKey, genre_key: str):
        super().__init__(author_id=key[2], timeout=SELECT_VIEW_TIMEOUT)
        self.cog = cog
        self.key = key
        self.genre_key = genre_key

        for class_key, spec in CLASSES.items():
            btn = discord.ui.Button(label=f"{spec['emoji']} {spec['label']}", style=discord.ButtonStyle.primary)
            btn.callback = functools.partial(self._class_cb, class_key=class_key)
            self.add_item(btn)

    async def _class_cb(self, interaction: discord.Interaction, class_key: str):
        async def _submit(modal_itx: discord.Interaction, name: str, race: str, background: str):
            self.stop()
            character = TRPGCharacter.create(name, class_key, race=race, background=background)
            await self.cog.start_adventure(modal_itx, self.key, self.genre_key, character)

        await interaction.response.send_modal(
            CharacterSetupModal(default_name=interaction.user.display_name, on_submit_cb=_submit)
        )


class FreeActionModal(discord.ui.Modal, title="✍️ 자유 행동"):
    """선택지에 없는 행동을 직접 입력받는 모달. 운명 판정(d20, 보정 없음)이 따라붙는다."""

    action = discord.ui.TextInput(
        label="무엇을 하시겠습니까?",
        placeholder="예: 상인에게 소문에 대해 캐묻는다",
        max_length=150,
    )

    def __init__(self, cog: "TRPG", key: SessionKey, view: "AdventureView"):
        super().__init__()
        self.cog = cog
        self.key = key
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        action_text = str(self.action).strip()
        if not action_text:
            await self.cog.respond_ephemeral(interaction, "행동 내용이 비어있어요.")
            return
        await self.cog.process_action(
            interaction, self.key, action_text,
            stat=None, dc=FREE_ACTION_DC, fate_roll=True, view=self.view,
        )


class AdventureView(AuthorLockedView):
    """현재 장면의 선택지 버튼 + 자유 행동/캐릭터/중단 컨트롤."""

    def __init__(self, cog: "TRPG", key: SessionKey, adv: TRPGAdventure):
        super().__init__(author_id=key[2], timeout=ADVENTURE_VIEW_TIMEOUT)
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

        sheet_btn = discord.ui.Button(label="🧾 캐릭터", style=discord.ButtonStyle.secondary, row=2)
        sheet_btn.callback = self._sheet_cb
        self.add_item(sheet_btn)

        suspend_btn = discord.ui.Button(label="💾 중단(저장)", style=discord.ButtonStyle.danger, row=2)
        suspend_btn.callback = self._suspend_cb
        self.add_item(suspend_btn)

    async def _choice_cb(self, interaction: discord.Interaction, index: int):
        adv = self.cog.adventures.get(self.key)
        if adv is None or index >= len(adv.choices):
            await self.cog.respond_ephemeral(interaction, "모험 정보를 찾을 수 없어요. `!모험계속` 으로 다시 불러와주세요.")
            return
        choice = adv.choices[index]
        await self.cog.process_action(
            interaction, self.key, choice["text"],
            stat=choice.get("stat"), dc=choice.get("dc", DEFAULT_DC), fate_roll=False, view=self,
            choice=choice,
        )

    async def _free_cb(self, interaction: discord.Interaction):
        adv = self.cog.adventures.get(self.key)
        if adv is None:
            await self.cog.respond_ephemeral(interaction, "모험 정보를 찾을 수 없어요. `!모험계속` 으로 다시 불러와주세요.")
            return
        await interaction.response.send_modal(FreeActionModal(self.cog, self.key, self))

    async def _sheet_cb(self, interaction: discord.Interaction):
        adv = self.cog.adventures.get(self.key)
        if adv is None:
            await self.cog.respond_ephemeral(interaction, "모험 정보를 찾을 수 없어요.")
            return
        try:
            embed = self.cog.character_embed(adv)
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
        except discord.HTTPException:
            logger.exception("캐릭터 시트 전송 실패")

    async def _suspend_cb(self, interaction: discord.Interaction):
        await self.cog.suspend_adventure(interaction, self.key, self)


class TRPG(commands.Cog):
    """AI 생성형 TRPG — GM 아리스가 진행하는 1인용 텍스트 어드벤처."""

    def __init__(self, bot):
        self.bot = bot
        self.trpg_group = None
        self.adventures: Dict[SessionKey, TRPGAdventure] = {}
        self.locks: Dict[SessionKey, asyncio.Lock] = {}
        self.active_views: Dict[SessionKey, AdventureView] = {}

    # ------------------------------------------------------------------ 공통 유틸
    @staticmethod
    def _make_key(guild: Optional[discord.Guild], channel, user) -> SessionKey:
        guild_id = guild.id if guild else 0
        return (guild_id, getattr(channel, "id", 0), user.id)

    @staticmethod
    def _key_str(key: SessionKey) -> str:
        return f"{key[0]}:{key[1]}:{key[2]}"

    def _model(self) -> str:
        """ChatAI Cog가 모델 자동 전환을 했다면 그 모델을 함께 사용한다."""
        chat_cog = self.bot.get_cog("ChatAI")
        return getattr(chat_cog, "active_model", None) or LOCAL_AI_MODEL

    def _lock(self, key: SessionKey) -> asyncio.Lock:
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
    def _save_sync(self, key: SessionKey, adv_dict: dict):
        saves = load_trpg_saves()
        saves[self._key_str(key)] = adv_dict
        save_trpg_saves(saves)

    def _delete_save_sync(self, key: SessionKey):
        saves = load_trpg_saves()
        if saves.pop(self._key_str(key), None) is not None:
            save_trpg_saves(saves)

    def _load_save_sync(self, key: SessionKey) -> Optional[dict]:
        return load_trpg_saves().get(self._key_str(key))

    async def _autosave(self, key: SessionKey, adv: TRPGAdventure):
        try:
            await asyncio.to_thread(self._save_sync, key, adv.to_dict())
        except Exception as e:
            logger.error(f"TRPG 자동 저장 실패: {e}")

    async def _delete_save(self, key: SessionKey):
        try:
            await asyncio.to_thread(self._delete_save_sync, key)
        except Exception as e:
            logger.error(f"TRPG 세이브 삭제 실패: {e}")

    # ------------------------------------------------------------------ 임베드
    def adventure_embed(
        self,
        adv: TRPGAdventure,
        *,
        result: Optional[TurnResult] = None,
        opening: bool = False,
    ) -> discord.Embed:
        char = adv.character
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
            if result.hp_change:
                changes.append(f"HP {'+' if result.hp_change > 0 else ''}{result.hp_change}")
            if result.items_added:
                changes.append(f"획득: {', '.join(result.items_added)}")
            if result.items_removed:
                changes.append(f"상실: {', '.join(result.items_removed)}")
            if changes:
                embed.add_field(name="📦 변화", value="\n".join(changes)[:1024], inline=False)

        if adv.combat is not None:
            embed.add_field(name="⚔️ 전투 중!", value=combat_status_lines(adv.combat)[:1024], inline=False)

        embed.add_field(name=char.summary_line(), value=_hp_bar(char.hp, char.max_hp), inline=False)

        footer = "버튼으로 행동을 고르거나 ✍️ 자유 행동으로 직접 입력하세요."
        if adv.combat is not None:
            footer = "⚔️ 전투 중! 공격/방어 버튼이나 ✍️ 자유 행동(물약·도주·꾀)으로 싸우세요."
        elif not opening and adv.quest:
            footer = f"🎯 {adv.quest[:140]}\n{footer}"
        embed.set_footer(text=footer[:2048])
        return embed

    def character_embed(self, adv: TRPGAdventure) -> discord.Embed:
        char = adv.character
        embed = discord.Embed(
            title=f"{char.job_emoji} {char.name} — {char.job}",
            color=0x3498DB,
        )
        embed.add_field(name="HP", value=_hp_bar(char.hp, char.max_hp), inline=False)
        if char.race:
            embed.add_field(name="종족", value=char.race, inline=True)
        embed.add_field(name="능력치", value=char.stats_line(), inline=False)
        if char.background:
            embed.add_field(name="배경", value=char.background[:1024], inline=False)
        inventory = "\n".join(f"- {item}" for item in char.inventory) if char.inventory else "비어 있음"
        embed.add_field(name="소지품", value=inventory[:1024], inline=False)
        embed.add_field(
            name="모험 정보",
            value=f"{adv.genre_emoji} {adv.title} ({adv.genre_label}) · {adv.turn}턴 진행",
            inline=False,
        )
        return embed

    def ending_embed(self, adv: TRPGAdventure) -> discord.Embed:
        if adv.status == "victory":
            title, color = "🏆 퀘스트 완수!", 0xF1C40F
        elif adv.status == "dead":
            title, color = "💀 당신은 쓰러졌다...", 0x992D22
        else:
            title, color = "🌒 모험이 막을 내렸다", 0x95A5A6

        embed = discord.Embed(title=title, description=adv.scene[:4000], color=color)
        embed.add_field(
            name="모험 기록",
            value=f"{adv.genre_emoji} {adv.title} · 총 {adv.turn}턴 · {adv.character.summary_line()}",
            inline=False,
        )
        embed.set_footer(text="`!모험` 으로 새로운 모험을 시작할 수 있어요.")
        return embed

    async def _retire_active_view(self, key: SessionKey):
        """이전 장면 뷰를 중지·비활성화해 오래된 메시지에서 중복 조작을 막는다."""
        old = self.active_views.pop(key, None)
        if old is None or old.is_finished():
            return
        old.stop()
        for item in old.children:
            item.disabled = True
        if old.message:
            await safe_edit_message(old.message, view=old)

    async def send_adventure(
        self,
        channel: discord.abc.Messageable,
        key: SessionKey,
        adv: TRPGAdventure,
        *,
        result: Optional[TurnResult] = None,
        opening: bool = False,
    ):
        await self._retire_active_view(key)
        embed = self.adventure_embed(adv, result=result, opening=opening)
        view = AdventureView(self, key, adv)
        try:
            msg = await channel.send(embed=embed, view=view)
            view.message = msg
            self.active_views[key] = view
        except discord.HTTPException:
            logger.exception("모험 장면 전송 실패")

    # ------------------------------------------------------------------ 시작 플로우
    async def show_class_select(self, interaction: discord.Interaction, key: SessionKey, genre_key: str):
        genre = GENRES[genre_key]
        embed = discord.Embed(
            title=f"{genre['emoji']} {genre['label']} 모험 — 직업 선택",
            description=f"{genre['hint']}.\n\n주인공의 직업을 선택하세요! 직업을 고르면 이름·종족·배경을 입력할 수 있어요.",
            color=0x9B59B6,
        )
        for spec in CLASSES.values():
            stats = " / ".join(f"{k} +{v}" if v > 0 else f"{k} {v}" for k, v in spec["stats"].items())
            embed.add_field(
                name=f"{spec['emoji']} {spec['label']}",
                value=f"HP {spec['hp']}\n{stats}\n소지품: {', '.join(spec['items'])}",
                inline=True,
            )
        view = ClassSelectView(self, key, genre_key)
        try:
            await interaction.response.edit_message(embed=embed, view=view)
            view.message = interaction.message
        except discord.HTTPException:
            logger.exception("직업 선택 화면 표시 실패")

    async def start_adventure(self, interaction: discord.Interaction, key: SessionKey, genre_key: str, character: TRPGCharacter):
        lock = self._lock(key)
        if lock.locked():
            await self.respond_ephemeral(interaction, "이미 모험 생성이 진행 중이에요. 잠시만요!")
            return

        genre = GENRES[genre_key]
        progress = discord.Embed(
            title=f"{genre['emoji']} {genre['label']} 모험 준비 중...",
            description=(
                "🖋️ GM 아리스가 세계를 창조하는 중입니다...\n"
                "로컬 AI 성능에 따라 수십 초 정도 걸릴 수 있어요."
            ),
            color=0x9B59B6,
        )
        try:
            await interaction.response.edit_message(embed=progress, view=None)
        except discord.HTTPException:
            logger.exception("모험 생성 대기 화면 표시 실패")

        async with lock:
            # 더블 클릭 등으로 이미 모험이 만들어졌다면 중복 생성하지 않는다.
            existing = self.adventures.get(key)
            if existing is not None and existing.is_playing:
                return

            try:
                async with interaction.channel.typing():
                    adv = await asyncio.to_thread(
                        generate_scenario, genre_key, character, model=self._model()
                    )
            except FileNotFoundError:
                await self._fail_progress(
                    interaction,
                    f"⚠️ 모델 `{self._model()}` 을 찾을 수 없어요.\n"
                    "`ollama pull` 로 설치하거나 `TOKEN.env`의 `LOCAL_AI_MODEL` 설정을 확인해주세요.",
                )
                return
            except Exception as e:
                logger.error(f"TRPG 시나리오 생성 실패: {e}", exc_info=True)
                await self._fail_progress(
                    interaction,
                    f"⚠️ 세계 생성에 실패했어요: {str(e)[:100]}\n`!모험` 으로 다시 시도해주세요.",
                )
                return

            self.adventures[key] = adv
            await self._autosave(key, adv)
            await self._finish_progress(interaction, genre)
            await self.send_adventure(interaction.channel, key, adv, opening=True)

    async def _fail_progress(self, interaction: discord.Interaction, description: str):
        """'준비 중...' 메시지를 실패 안내로 바꾼다. 편집이 안 되면 채널에 안내를 보낸다."""
        embed = discord.Embed(title="🎲 AI TRPG", description=description, color=0xE74C3C)
        edited = False
        if interaction.message:
            edited = await safe_edit_message(interaction.message, embed=embed, view=None)
        if not edited:
            try:
                await interaction.channel.send(description, delete_after=20)
            except discord.HTTPException:
                pass

    async def _finish_progress(self, interaction: discord.Interaction, genre: dict):
        """'준비 중...' 메시지를 생성 완료 안내로 바꾼다."""
        if not interaction.message:
            return
        embed = discord.Embed(
            title=f"{genre['emoji']} {genre['label']} 모험 생성 완료!",
            description="용사 아리스, 출진! 아래에서 모험이 시작됩니다.",
            color=0x2ECC71,
        )
        await safe_edit_message(interaction.message, embed=embed, view=None)

    # ------------------------------------------------------------------ 턴 진행
    async def process_action(
        self,
        interaction: discord.Interaction,
        key: SessionKey,
        action_text: str,
        *,
        stat: Optional[str],
        dc: int,
        fate_roll: bool,
        view: AdventureView,
        choice: Optional[dict] = None,
    ):
        adv = self.adventures.get(key)
        if adv is None or not adv.is_playing:
            await self.respond_ephemeral(interaction, "진행 중인 모험이 없어요. `!모험계속` 으로 다시 불러와주세요.")
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
            # 락 대기 중에 !모험종료 등으로 모험이 사라졌을 수 있으므로 다시 확인.
            adv = self.adventures.get(key)
            if adv is None or not adv.is_playing:
                await self.respond_ephemeral(interaction, "진행 중인 모험이 없어요.")
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
            check = roll_check(adv.character, stat, dc) if (stat or fate_roll) and not in_combat_action else None

            header = f"🕹️ **{adv.character.name}**: {action_text}"
            if check:
                header += f"\n{check.display}"
            try:
                await channel.send(header)
            except discord.HTTPException:
                logger.debug("행동 로그 전송 실패 (무시됨)")

            try:
                async with channel.typing():
                    result = await asyncio.to_thread(
                        play_turn, adv, action_text, check, choice=choice, model=self._model()
                    )
            except Exception as e:
                logger.error(f"TRPG 턴 처리 실패: {e}", exc_info=True)
                try:
                    await channel.send(
                        f"⚠️ GM 응답 생성에 실패했어요: {str(e)[:100]}\n장면을 다시 열어드릴게요. 같은 행동을 다시 시도할 수 있어요."
                    )
                except discord.HTTPException:
                    pass
                await self.send_adventure(channel, key, adv)
                return

            await self._autosave(key, adv)

            if result.combat_log:
                try:
                    await channel.send("\n".join(result.combat_log))
                except discord.HTTPException:
                    logger.debug("전투 로그 전송 실패 (무시됨)")

            if not adv.is_playing:
                try:
                    await channel.send(embed=self.ending_embed(adv))
                except discord.HTTPException:
                    logger.exception("엔딩 메시지 전송 실패")
                await self._delete_save(key)
                self.adventures.pop(key, None)
                self.active_views.pop(key, None)
                self.locks.pop(key, None)
                return

            await self.send_adventure(channel, key, adv, result=result)

    # ------------------------------------------------------------------ 중단
    async def suspend_adventure(self, interaction: discord.Interaction, key: SessionKey, view: AdventureView):
        # 턴 처리 도중 중단하면 저장 시점이 꼬일 수 있으므로 막는다.
        if self._lock(key).locked():
            await self.respond_ephemeral(interaction, "GM 아리스가 이야기를 쓰는 중에는 중단할 수 없어요. 잠시만요!")
            return

        adv = self.adventures.get(key)
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
            self.adventures.pop(key, None)
        self.active_views.pop(key, None)
        try:
            await interaction.channel.send(
                "💾 모험을 저장하고 잠시 쉬어갑니다. `!모험계속` 으로 언제든 이어서 할 수 있어요!"
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ 명령어
    @commands.command(name="모험", aliases=["trpg", "모험시작"], help="AI 생성형 TRPG 모험을 시작합니다.")
    async def trpg_start(self, ctx):
        """장르 선택부터 시작하는 새 모험. 진행 중이면 안내만 한다."""
        if not is_local_ai_configured():
            await ctx.send(
                "선생님, 로컬 AI 설정이 비어있어요! `TOKEN.env`에 `LOCAL_AI_BASE_URL`, `LOCAL_AI_MODEL`을 설정해주세요.",
                delete_after=15,
            )
            return

        key = self._make_key(ctx.guild, ctx.channel, ctx.author)
        adv = self.adventures.get(key)
        if adv is not None and adv.is_playing:
            await ctx.send(
                "이미 진행 중인 모험이 있어요! `!모험계속` 으로 이어가거나 `!모험종료` 로 끝낼 수 있어요.",
                delete_after=15,
            )
            return

        # 장르/직업 선택 UI를 보여주기 전에 모델 존재 여부를 확인한다.
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
                f"⚠️ 로컬 AI 서버에 연결할 수 없어요: {str(e)[:100]}\n"
                "Ollama가 실행 중인지 확인해주세요.",
                delete_after=20,
            )
            return

        has_save = await asyncio.to_thread(self._load_save_sync, key) is not None
        description = (
            "GM 아리스가 세계관·퀘스트·이야기를 즉석에서 생성하는 1인용 TRPG입니다.\n"
            "장르를 선택하면 모험이 시작됩니다!\n\n"
            + "\n".join(f"{g['emoji']} **{g['label']}** — {g['hint']}" for g in GENRES.values())
        )
        if has_save:
            description += "\n\n⚠️ 저장된 모험이 있어요. 새 모험을 시작하면 기존 세이브를 덮어씁니다. (`!모험계속` 으로 이어하기)"

        embed = discord.Embed(title="🎲 AI TRPG — 새로운 모험", description=description, color=0x9B59B6)
        view = GenreSelectView(self, key)
        try:
            msg = await ctx.send(embed=embed, view=view)
            view.message = msg
        except discord.HTTPException:
            logger.exception("모험 시작 화면 전송 실패")

    @commands.command(name="모험계속", aliases=["trpg계속", "모험재개"], help="저장된 TRPG 모험을 이어서 진행합니다.")
    async def trpg_continue(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel, ctx.author)
        if self._lock(key).locked():
            await ctx.send("GM 아리스가 아직 이야기를 쓰는 중이에요. 잠시 후 다시 시도해주세요!", delete_after=10)
            return
        adv = self.adventures.get(key)

        if adv is None:
            data = await asyncio.to_thread(self._load_save_sync, key)
            if not data:
                await ctx.send("저장된 모험이 없어요. `!모험` 으로 새 모험을 시작해주세요!", delete_after=15)
                return
            try:
                adv = TRPGAdventure.from_dict(data)
            except Exception as e:
                logger.error(f"TRPG 세이브 로드 실패: {e}", exc_info=True)
                await ctx.send("세이브 데이터를 읽지 못했어요. `!모험` 으로 새 모험을 시작해주세요.", delete_after=15)
                return
            self.adventures[key] = adv

        if not adv.is_playing:
            self.adventures.pop(key, None)
            await self._delete_save(key)
            await ctx.send("이미 끝난 모험이에요. `!모험` 으로 새 모험을 시작해주세요!", delete_after=15)
            return

        await ctx.send(f"📖 **{adv.title}** — 모험을 이어서 진행합니다!", delete_after=10)
        await self.send_adventure(ctx.channel, key, adv)

    @commands.command(name="모험상태", aliases=["trpg상태"], help="진행 중인 TRPG 캐릭터 상태를 확인합니다.")
    async def trpg_status(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel, ctx.author)
        adv = self.adventures.get(key)
        if adv is None:
            data = await asyncio.to_thread(self._load_save_sync, key)
            if data:
                try:
                    adv = TRPGAdventure.from_dict(data)
                except Exception:
                    adv = None
        if adv is None:
            await ctx.send("진행 중인 모험이 없어요. `!모험` 으로 시작해주세요!", delete_after=15)
            return
        await ctx.send(embed=self.character_embed(adv), delete_after=60)

    @commands.command(name="주사위", aliases=["roll", "굴림"], help="TRPG 주사위를 굴립니다. 예: !주사위 2d6+3 (기본 d20)")
    async def dice_command(self, ctx, *, notation: str = "d20"):
        try:
            roll = roll_dice(notation)
        except ValueError as e:
            await ctx.send(f"⚠️ {e}", delete_after=10)
            return
        await ctx.send(f"**{ctx.author.display_name}** 님의 굴림 — {roll.display}")

    @commands.command(name="모험종료", aliases=["trpg종료"], help="진행 중인 TRPG 모험을 종료하고 세이브를 삭제합니다.")
    async def trpg_end(self, ctx):
        key = self._make_key(ctx.guild, ctx.channel, ctx.author)
        # 턴 처리 도중 종료하면 자동 저장이 세이브를 되살릴 수 있으므로 락으로 순서를 보장한다.
        async with self._lock(key):
            had_memory = self.adventures.pop(key, None) is not None
            had_save = await asyncio.to_thread(self._load_save_sync, key) is not None
            if not had_memory and not had_save:
                await ctx.send("진행 중인 모험이 없어요.", delete_after=10)
                return
            await self._delete_save(key)
            await self._retire_active_view(key)
        self.locks.pop(key, None)
        await ctx.send("🛑 모험을 종료하고 세이브를 삭제했어요. 다음 모험에서 만나요, 용사님!", delete_after=15)

    # ------------------------------------------------------------------ Slash Commands
    async def slash_trpg_start(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.trpg_start(ctx)

    async def slash_trpg_continue(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.trpg_continue(ctx)

    async def slash_trpg_status(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.trpg_status(ctx)

    async def slash_trpg_end(self, interaction: discord.Interaction):
        await safe_defer(interaction)
        ctx = await self.bot.get_context(interaction)
        await self.trpg_end(ctx)

    async def slash_dice(self, interaction: discord.Interaction, 표기: str = "d20"):
        try:
            roll = roll_dice(표기)
        except ValueError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"**{interaction.user.display_name}** 님의 굴림 — {roll.display}")

    # ------------------------------------------------------------------ Cog 라이프사이클
    async def cog_load(self):
        self.trpg_group = discord.app_commands.Group(name="모험", description="AI 생성형 TRPG 명령어")
        self.trpg_group.command(name="시작", description="AI가 생성하는 TRPG 모험을 시작합니다")(self.slash_trpg_start)
        self.trpg_group.command(name="계속", description="저장된 모험을 이어서 진행합니다")(self.slash_trpg_continue)
        self.trpg_group.command(name="상태", description="캐릭터 상태를 확인합니다")(self.slash_trpg_status)
        self.trpg_group.command(name="종료", description="모험을 종료하고 세이브를 삭제합니다")(self.slash_trpg_end)
        self.trpg_group.command(name="주사위", description="TRPG 주사위를 굴립니다 (예: 2d6+3, 기본 d20)")(self.slash_dice)
        self.bot.tree.add_command(self.trpg_group, override=True)

    async def cog_unload(self):
        try:
            if self.trpg_group:
                self.bot.tree.remove_command(self.trpg_group.name)
        except Exception as e:
            logger.error(f"TRPG Slash Commands 제거 중 오류: {e}")


async def setup(bot):
    await bot.add_cog(TRPG(bot))
