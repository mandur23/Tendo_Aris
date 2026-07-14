"""TRPG cog 3종(1인용·파티·자유 모험)이 함께 쓰는 공용 UI 컴포넌트."""
import discord

from GameSystem.TRPGEngine import CombatState


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
