"""TRPG cog 3종(1인용·파티·자유 모험)이 함께 쓰는 공용 UI 컴포넌트."""
import logging

import discord

from GameSystem.TRPGEngine import CombatState, generate_class
from utils.discord_utils import AuthorLockedView
from utils.llm_utils import run_llm

logger = logging.getLogger(__name__)

CLASS_CREATE_VIEW_TIMEOUT = 180.0


def hp_bar(hp: int, max_hp: int, width: int = 10) -> str:
    filled = max(0, min(width, round(width * hp / max_hp))) if max_hp > 0 else 0
    return f"`{'█' * filled}{'░' * (width - filled)}` {hp}/{max_hp}"


def combat_status_lines(combat: CombatState) -> str:
    """장면 임베드에 넣을 적 상태 요약."""
    lines = []
    for enemy in combat.enemies:
        marker = "💀 " if not enemy.alive else ""
        lines.append(
            f"{marker}👹 **{enemy.name}** {hp_bar(enemy.hp, enemy.max_hp)} · 방어 {enemy.ac}"
        )
    return "\n".join(lines)


class CharacterSetupModal(discord.ui.Modal, title="🧙 캐릭터 만들기"):
    """이름·종족·배경을 입력받는 캐릭터 생성 모달. 직업은 버튼으로 미리 골라져 있다."""

    name = discord.ui.TextInput(label="이름", max_length=20)
    race = discord.ui.TextInput(
        label="종족 (선택)",
        placeholder="예: 엘프, 드워프, 수인 — 비워두면 생략",
        max_length=20,
        required=False,
    )
    background = discord.ui.TextInput(
        label="배경 설정 (선택)",
        style=discord.TextStyle.paragraph,
        placeholder="예: 몰락한 가문의 마지막 후계자. 잃어버린 가보를 찾고 있다.",
        max_length=200,
        required=False,
    )

    def __init__(self, *, default_name: str, on_submit_cb):
        super().__init__()
        self.name.default = default_name[:20]
        self._on_submit_cb = on_submit_cb

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.name).strip() or interaction.user.display_name
        await self._on_submit_cb(
            interaction,
            name[:20],
            str(self.race).strip(),
            str(self.background).strip(),
        )


# ------------------------------------------------------------------ 직업 즉석 생성 UI
class ClassConceptModal(discord.ui.Modal, title="✨ 직업 만들기"):
    """원하는 직업 컨셉을 받는다. 비워두면 세계관에 맞춰 알아서 만들어 준다."""

    concept = discord.ui.TextInput(
        label="어떤 직업을 원하시나요? (비워도 됩니다)",
        placeholder="예: 그림자를 다루는 암살자 / 짐승과 대화하는 방랑자",
        required=False,
        max_length=100,
    )

    def __init__(self, *, on_submit_cb):
        super().__init__()
        self._on_submit_cb = on_submit_cb

    async def on_submit(self, interaction: discord.Interaction):
        await self._on_submit_cb(interaction, str(self.concept).strip())


class GeneratedClassView(AuthorLockedView):
    """생성된 직업을 확인하고 시작하거나 다시 만드는 뷰 (본인에게만 보인다)."""

    def __init__(self, *, author_id: int, on_accept, on_retry):
        super().__init__(author_id=author_id, timeout=CLASS_CREATE_VIEW_TIMEOUT)
        self._on_accept = on_accept
        self._on_retry = on_retry

        accept_btn = discord.ui.Button(label="✅ 이 직업으로 시작", style=discord.ButtonStyle.success)
        accept_btn.callback = self._accept_cb
        self.add_item(accept_btn)

        retry_btn = discord.ui.Button(label="🔄 다시 만들기", style=discord.ButtonStyle.secondary)
        retry_btn.callback = self._retry_cb
        self.add_item(retry_btn)

    async def _accept_cb(self, interaction: discord.Interaction):
        self.stop()
        await self._on_accept(interaction)

    async def _retry_cb(self, interaction: discord.Interaction):
        self.stop()
        await self._on_retry(interaction)


def generated_class_embed(spec: dict, concept: str) -> discord.Embed:
    """생성된 직업을 보여주는 임베드."""
    stats = " / ".join(f"{k} +{v}" if v > 0 else f"{k} {v}" for k, v in spec["stats"].items())
    embed = discord.Embed(
        title=f"{spec['emoji']} {spec['label']}",
        description=(f"컨셉: {concept}\n\n" if concept else "")
        + f"HP {spec['hp']}\n{stats}\n소지품: {', '.join(spec['items'])}",
        color=0x2ECC71,
    )
    embed.set_footer(text="마음에 들면 시작하고, 아니면 다시 만들어 보세요!")
    return embed


async def run_class_creation(
    interaction: discord.Interaction,
    *,
    genre_key: str,
    concept: str,
    model,
    on_accept,
):
    """직업을 생성해 본인에게만 보여주고, '시작 / 다시 만들기' 흐름을 처리한다.

    on_accept(interaction, class_key, spec) 는 사용자가 그 직업으로 시작할 때 호출된다.
    '다시 만들기' 를 누르면 같은 컨셉으로 이 과정을 반복한다.
    """
    waiting = "✨ 아리스가 직업을 구상하는 중이에요..."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(waiting, ephemeral=True)
        else:
            await interaction.response.send_message(waiting, ephemeral=True)
    except discord.HTTPException:
        logger.debug("직업 생성 대기 메시지 전송 실패 (무시됨)")

    try:
        class_key, spec = await run_llm(generate_class, genre_key, concept=concept, model=model)
    except Exception as e:
        logger.error(f"직업 생성 실패: {e}", exc_info=True)
        try:
            await interaction.edit_original_response(
                content="직업을 만들지 못했어요. 잠시 후 다시 시도하거나 기존 직업을 골라주세요."
            )
        except discord.HTTPException:
            pass
        return

    async def _accept(accept_itx: discord.Interaction):
        await on_accept(accept_itx, class_key, spec)

    async def _retry(retry_itx: discord.Interaction):
        await run_class_creation(
            retry_itx, genre_key=genre_key, concept=concept, model=model, on_accept=on_accept
        )

    view = GeneratedClassView(author_id=interaction.user.id, on_accept=_accept, on_retry=_retry)
    try:
        await interaction.edit_original_response(content=None, embed=generated_class_embed(spec, concept), view=view)
    except discord.HTTPException:
        logger.debug("생성된 직업 표시 실패 (무시됨)")
