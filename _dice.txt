"""자유 행동 판정에서 플레이어가 직접 주사위를 굴리게 하는 UI 헬퍼.

1인 모험 / 파티 모험 / 자유 모험 세 모드가 같은 흐름을 쓰므로 공용으로 둔다.

동작:
- 행동을 선언하면 "🎲 주사위 굴리기" 버튼이 달린 메시지를 보낸다.
- 행동한 본인만 누를 수 있고, 누르면 그 자리에서 판정 결과로 메시지가 바뀐다.
- 제한 시간 안에 누르지 않으면 진행이 멈추지 않도록 자동으로 굴린다.
  (자동이든 수동이든 판정 규칙과 확률은 완전히 동일하다)
"""
import asyncio
import logging
from typing import Optional

import discord

from GameSystem.TRPGEngine import CheckResult, roll_check
from utils.discord_utils import AuthorLockedView, safe_edit_message

logger = logging.getLogger(__name__)

# 이 시간(초) 안에 버튼을 누르지 않으면 자동으로 굴린다.
PLAYER_ROLL_TIMEOUT = 30.0


class PlayerRollView(AuthorLockedView):
    """'🎲 주사위 굴리기' 버튼 하나짜리 View. 행동한 본인만 누를 수 있다."""

    def __init__(self, author_id: int, timeout: float = PLAYER_ROLL_TIMEOUT):
        super().__init__(author_id=author_id, timeout=timeout)
        self.message: Optional[discord.Message] = None
        self.rolled = False

        button = discord.ui.Button(label="🎲 주사위 굴리기", style=discord.ButtonStyle.primary)
        button.callback = self._roll_cb
        self.add_item(button)

    async def _roll_cb(self, interaction: discord.Interaction):
        self.rolled = True
        for item in self.children:
            item.disabled = True
        try:
            # 실제 굴림과 결과 표시는 호출부에서 처리하므로 여기서는 응답만 닫는다.
            await interaction.response.defer()
        except discord.HTTPException:
            logger.debug("주사위 버튼 응답 실패 (무시됨)")
        self.stop()


def _prompt_text(character, stat: Optional[str], dc: int) -> str:
    """굴리기 전에 보여줄 안내 문구 (무엇을, 얼마나 어렵게 판정하는지)."""
    stat_label = f"{stat} 판정" if stat else "운명 판정"
    if stat:
        mod = character.stats.get(stat, 0)
        detail = f"목표 **{dc}** · 보정 {mod:+d}"
    else:
        detail = f"목표 **{dc}** · 보정 없음"
    return (
        f"🎲 **{character.name}** — {stat_label}\n"
        f"{detail}\n"
        "버튼을 눌러 주사위를 굴려주세요!"
    )


async def player_roll_check(
    channel,
    character,
    stat: Optional[str],
    dc: int,
    *,
    author_id: int,
    timeout: float = PLAYER_ROLL_TIMEOUT,
) -> CheckResult:
    """플레이어가 직접 굴리게 한 뒤 판정 결과를 돌려준다.

    버튼 전송이 실패하거나 제한 시간을 넘기면 자동으로 굴린다.
    """
    prompt = _prompt_text(character, stat, dc)
    view = PlayerRollView(author_id=author_id, timeout=timeout)

    try:
        message = await channel.send(prompt, view=view)
    except discord.HTTPException:
        logger.debug("주사위 버튼 전송 실패 — 자동으로 굴립니다.")
        return roll_check(character, stat, dc)

    view.message = message
    # View 자체의 타임아웃에만 기대지 않고 직접 제한을 건다.
    # (버튼이 어떤 이유로 응답하지 않아도 모험 진행이 멈추지 않도록)
    try:
        await asyncio.wait_for(view.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        view.stop()

    # 버튼을 눌렀든 시간이 지났든, 굴림 자체는 같은 규칙으로 처리한다.
    check = roll_check(character, stat, dc)
    tail = "" if view.rolled else "\n⏱️ 시간이 지나 자동으로 굴렸어요."
    try:
        await safe_edit_message(
            message, content=f"**{character.name}** {check.display}{tail}", view=None
        )
    except discord.HTTPException:
        logger.debug("주사위 결과 메시지 수정 실패 (무시됨)")
    return check
