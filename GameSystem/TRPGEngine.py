"""AI 생성형 TRPG 게임 엔진 (Discord 비의존)

게임 상태(캐릭터 시트·HP·인벤토리·진행 기록)는 코드가 관리하고,
로컬 LLM(Ollama)은 장면 서술과 선택지 생성만 담당한다.
주사위 판정도 코드에서 굴려 결과만 LLM에 전달하므로 규칙 일관성이 유지된다.

모든 LLM 호출 함수는 동기(blocking)이며, 호출부에서 asyncio.to_thread 로 감싸야 한다.
"""
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from utils.llm_utils import extract_json_object, ollama_chat_sync, strip_non_korean

logger = logging.getLogger(__name__)

STATS = ("힘", "민첩", "지능", "매력")
DEFAULT_DC = 12
FREE_ACTION_DC = 11          # 자유 행동은 스탯 보정 없는 '운명 판정'으로 처리
MAX_CHOICES = 4
CHOICE_TEXT_MAX = 48         # 버튼 라벨 제한(80자)을 감안한 선택지 텍스트 길이
NARRATION_MAX = 1500
INVENTORY_MAX = 12
RECENT_TURNS_IN_PROMPT = 3   # 프롬프트에 전문을 싣는 최근 턴 수
LOG_LINES_IN_PROMPT = 12     # 프롬프트에 싣는 한 줄 요약 기록 수
HP_CHANGE_LIMIT = 12         # LLM이 제안하는 턴당 HP 변화 상한

SCENARIO_TEMPERATURE = 0.9   # 세계 생성은 다양성을 위해 온도를 높인다
TURN_TEMPERATURE = 0.75

GENRES: Dict[str, Dict[str, str]] = {
    "fantasy": {
        "label": "판타지",
        "emoji": "⚔️",
        "hint": "검과 마법, 던전과 몬스터가 있는 중세 판타지 세계",
    },
    "wuxia": {
        "label": "무협",
        "emoji": "🐉",
        "hint": "강호의 문파와 무공, 협객과 비급이 있는 무협 세계",
    },
    "sf": {
        "label": "SF 우주",
        "emoji": "🚀",
        "hint": "우주선과 외계 행성, 인공지능이 있는 SF 세계",
    },
    "horror": {
        "label": "현대 호러",
        "emoji": "👻",
        "hint": "현대 도시의 괴담과 미스터리, 서서히 조여오는 공포",
    },
    "cyberpunk": {
        "label": "사이버펑크",
        "emoji": "🌃",
        "hint": "거대 기업과 해커, 네온 불빛의 사이버펑크 도시",
    },
}

CLASSES: Dict[str, Dict] = {
    "warrior": {
        "label": "전사",
        "emoji": "🛡️",
        "stats": {"힘": 3, "민첩": 1, "지능": 0, "매력": 1},
        "hp": 26,
        "items": ["낡은 검", "나무 방패", "회복 물약"],
    },
    "mage": {
        "label": "마법사",
        "emoji": "🔮",
        "stats": {"힘": 0, "민첩": 1, "지능": 3, "매력": 1},
        "hp": 18,
        "items": ["수습 지팡이", "주문서", "회복 물약"],
    },
    "rogue": {
        "label": "도적",
        "emoji": "🗡️",
        "stats": {"힘": 1, "민첩": 3, "지능": 1, "매력": 0},
        "hp": 20,
        "items": ["단검", "자물쇠 따개", "회복 물약"],
    },
    "bard": {
        "label": "음유시인",
        "emoji": "🎻",
        "stats": {"힘": 0, "민첩": 1, "지능": 1, "매력": 3},
        "hp": 20,
        "items": ["류트", "화려한 망토", "회복 물약"],
    },
}

# LLM 응답이 깨졌을 때 게임이 멈추지 않도록 쓰는 기본 선택지.
FALLBACK_CHOICES: List[Dict] = [
    {"text": "주변을 주의 깊게 살펴본다", "stat": "지능", "dc": 11},
    {"text": "조심스럽게 앞으로 나아간다", "stat": None, "dc": DEFAULT_DC},
]

GM_SYSTEM_PROMPT = (
    "당신은 한국어 TRPG의 게임 마스터(GM)입니다. 이름은 '아리스'이며, 레트로 RPG를 사랑하는 열정적인 GM입니다.\n"
    "플레이어의 행동과 주사위 판정 결과를 바탕으로 장면을 서술하고 다음 선택지를 제시합니다.\n"
    "\n"
    "[서술 규칙]\n"
    "- 반드시 자연스러운 한국어로만 작성합니다. 한자·일본어 가나·키릴 등 외국 문자는 한 글자도 쓰지 않습니다.\n"
    "- 플레이어를 '당신'이라고 부르며, 2인칭 시점으로 서술합니다.\n"
    "- 장면 서술은 3~7문장으로 생생하되 간결하게 씁니다.\n"
    "- 주사위 판정 결과를 절대 뒤집지 않습니다. 성공이면 성공으로, 실패면 실패로 서술합니다.\n"
    "- 대성공이면 기대 이상의 성과를, 대실패면 상황이 악화되는 전개를 서술합니다.\n"
    "- 세계관·퀘스트·지금까지의 기록과 모순되지 않게 전개합니다.\n"
    "- 이야기가 정체되지 않도록 매 턴 새로운 정보나 사건을 하나씩 제시합니다.\n"
    "\n"
    "[출력 형식]\n"
    "- 반드시 유효한 JSON 객체 하나만 출력합니다. JSON 밖에 다른 텍스트를 쓰지 않습니다.\n"
    "- 선택지(choices)는 2~4개, 각 선택지 텍스트는 25자 이내로 씁니다.\n"
    "- 선택지의 stat은 힘/민첩/지능/매력 중 판정이 필요한 스탯, 판정이 필요 없는 안전한 행동이면 \"없음\"으로 씁니다.\n"
    "- dc는 판정 난이도로 8(쉬움)~18(매우 어려움) 사이 정수입니다."
)

GM_PARTY_SYSTEM_PROMPT = (
    "당신은 한국어 TRPG의 게임 마스터(GM)입니다. 이름은 '아리스'이며, 레트로 RPG를 사랑하는 열정적인 GM입니다.\n"
    "여러 명의 플레이어가 한 파티로 함께 모험합니다. 각 턴마다 정해진 한 명이 행동하며,\n"
    "당신은 그 행동과 주사위 판정 결과를 바탕으로 장면을 서술하고 다음 선택지를 제시합니다.\n"
    "\n"
    "[서술 규칙]\n"
    "- 반드시 자연스러운 한국어로만 작성합니다. 한자·일본어 가나·키릴 등 외국 문자는 한 글자도 쓰지 않습니다.\n"
    "- 각 캐릭터는 이름으로 지칭합니다. 파티 전체가 같은 장면에 함께 있습니다.\n"
    "- 이번 턴에 행동한 캐릭터를 중심으로 서술하되, 다른 파티원의 가벼운 반응을 곁들여도 됩니다.\n"
    "- 행동하지 않은 캐릭터의 중요한 행동이나 판정을 마음대로 결정하지 않습니다.\n"
    "- 장면 서술은 3~7문장으로 생생하되 간결하게 씁니다.\n"
    "- 주사위 판정 결과를 절대 뒤집지 않습니다. 성공이면 성공으로, 실패면 실패로 서술합니다.\n"
    "- 대성공이면 기대 이상의 성과를, 대실패면 상황이 악화되는 전개를 서술합니다.\n"
    "- 세계관·퀘스트·지금까지의 기록과 모순되지 않게 전개합니다.\n"
    "- HP가 0이 되어 쓰러진 캐릭터는 회복되기 전까지 행동할 수 없는 상태로 서술합니다.\n"
    "- 이야기가 정체되지 않도록 매 턴 새로운 정보나 사건을 하나씩 제시합니다.\n"
    "\n"
    "[출력 형식]\n"
    "- 반드시 유효한 JSON 객체 하나만 출력합니다. JSON 밖에 다른 텍스트를 쓰지 않습니다.\n"
    "- 선택지(choices)는 2~4개, 각 선택지 텍스트는 25자 이내로, 다음 턴 캐릭터가 할 만한 행동으로 씁니다.\n"
    "- 선택지의 stat은 힘/민첩/지능/매력 중 판정이 필요한 스탯, 판정이 필요 없는 안전한 행동이면 \"없음\"으로 씁니다.\n"
    "- dc는 판정 난이도로 8(쉬움)~18(매우 어려움) 사이 정수입니다.\n"
    "- hp_changes 에는 이번 장면에서 실제로 피해나 회복이 있었던 캐릭터만 캐릭터 이름과 정수 변화량으로 적습니다."
)


# ------------------------------------------------------------------ 주사위 판정
@dataclass
class CheckResult:
    """d20 판정 결과. stat 이 None 이면 보정 없는 '운명 판정'."""

    stat: Optional[str]
    roll: int
    mod: int
    dc: int

    @property
    def total(self) -> int:
        return self.roll + self.mod

    @property
    def success(self) -> bool:
        if self.roll == 20:
            return True
        if self.roll == 1:
            return False
        return self.total >= self.dc

    @property
    def band(self) -> str:
        if self.roll == 20:
            return "대성공"
        if self.roll == 1:
            return "대실패"
        return "성공" if self.success else "실패"

    @property
    def display(self) -> str:
        stat_label = f"{self.stat} 판정" if self.stat else "운명 판정"
        mod_part = f" {'+' if self.mod >= 0 else ''}{self.mod}" if self.stat else ""
        return f"🎲 {stat_label}: d20({self.roll}){mod_part} = {self.total} / 목표 {self.dc} → **{self.band}**"

    def prompt_text(self) -> str:
        stat_label = f"{self.stat} 판정" if self.stat else "운명 판정"
        return f"{stat_label} 결과: {self.band} (주사위 {self.roll}, 총합 {self.total}, 목표 난이도 {self.dc})"


def roll_check(character: "TRPGCharacter", stat: Optional[str], dc: int = DEFAULT_DC) -> CheckResult:
    """d20 + 스탯 보정 판정을 굴린다. stat 이 None 이면 보정 없이 굴린다."""
    mod = character.stats.get(stat, 0) if stat else 0
    return CheckResult(stat=stat, roll=random.randint(1, 20), mod=mod, dc=dc)


# ------------------------------------------------------------------ 범용 주사위 (NdM+K 표기)
DICE_NOTATION_RE = re.compile(
    r"^\s*(?P<count>\d*)\s*[dD]\s*(?P<sides>\d+)\s*(?:(?P<sign>[+-])\s*(?P<mod>\d+))?\s*$"
)
DICE_MAX_COUNT = 20
DICE_MAX_SIDES = 1000


@dataclass
class DiceRoll:
    """범용 주사위 굴림 결과. 예: 2d6+3 → rolls=[4, 2], modifier=3, total=9."""

    notation: str
    rolls: List[int]
    modifier: int

    @property
    def total(self) -> int:
        return sum(self.rolls) + self.modifier

    @property
    def display(self) -> str:
        rolls_text = " + ".join(str(r) for r in self.rolls)
        mod_text = ""
        if self.modifier > 0:
            mod_text = f" + {self.modifier}"
        elif self.modifier < 0:
            mod_text = f" - {-self.modifier}"
        return f"🎲 {self.notation}: ({rolls_text}){mod_text} = **{self.total}**"


def roll_dice(notation: str) -> DiceRoll:
    """'2d6+3', 'd20', '3D8-2' 같은 TRPG 주사위 표기를 굴린다.

    잘못된 표기나 범위 초과는 ValueError 를 던진다.
    """
    match = DICE_NOTATION_RE.match(notation or "")
    if not match:
        raise ValueError("주사위 표기를 이해하지 못했어요. 예: `d20`, `2d6+3`, `3d8-2`")

    count = int(match.group("count") or 1)
    sides = int(match.group("sides"))
    modifier = int(match.group("mod") or 0)
    if match.group("sign") == "-":
        modifier = -modifier

    if not (1 <= count <= DICE_MAX_COUNT):
        raise ValueError(f"주사위 개수는 1~{DICE_MAX_COUNT}개까지만 굴릴 수 있어요.")
    if not (2 <= sides <= DICE_MAX_SIDES):
        raise ValueError(f"주사위 면 수는 2~{DICE_MAX_SIDES}면까지만 가능해요.")

    normalized = f"{count}d{sides}"
    if modifier > 0:
        normalized += f"+{modifier}"
    elif modifier < 0:
        normalized += str(modifier)

    return DiceRoll(
        notation=normalized,
        rolls=[random.randint(1, sides) for _ in range(count)],
        modifier=modifier,
    )


# ==================================================================== 전투 시스템
# 전투는 코드가 전권을 갖는다: 적 스탯 관리, 명중 굴림(d20+보정 vs 방어),
# 피해 굴림(1d8+보정, 치명타는 2d8)을 모두 코드가 굴리고,
# LLM은 확정된 판정 로그를 받아 서술로만 옮긴다.
COMBAT_MAX_ENEMIES = 3
ENEMY_HP_LIMIT = 45
ENEMY_AC_MIN, ENEMY_AC_MAX = 8, 18
ENEMY_ATTACK_LIMIT = 6
ENEMY_DAMAGE_FALLBACK = "1d6"
PLAYER_BASE_DEFENSE = 10     # 플레이어 방어 = 10 + 민첩 (+방어 태세 보너스)
DEFEND_BONUS = 4
PLAYER_DAMAGE_DIE = 8        # 플레이어 피해 = 1d8 + 공격 스탯 (치명타 2d8)
ATTACK_STATS = ("힘", "민첩", "지능")   # 가장 높은 스탯으로 공격한다 (직업 특기 자동 반영)


@dataclass
class Enemy:
    """코드가 관리하는 적 스탯 블록. damage 는 NdM+K 주사위 표기."""

    name: str
    hp: int
    max_hp: int
    ac: int
    attack: int
    damage: str

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def apply_hp(self, delta: int) -> int:
        before = self.hp
        self.hp = max(0, min(self.max_hp, self.hp + delta))
        return self.hp - before

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "ac": self.ac,
            "attack": self.attack,
            "damage": self.damage,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Enemy":
        max_hp = int(data.get("max_hp", 1))
        return cls(
            name=data.get("name", "적"),
            hp=max(0, min(max_hp, int(data.get("hp", 1)))),
            max_hp=max_hp,
            ac=int(data.get("ac", 12)),
            attack=int(data.get("attack", 2)),
            damage=data.get("damage", ENEMY_DAMAGE_FALLBACK),
        )


@dataclass
class CombatState:
    """진행 중인 전투 상태. 적 반격은 라운드 로빈으로 한 번에 1체씩 이뤄진다."""

    enemies: List[Enemy]
    attacker_idx: int = 0

    def alive_enemies(self) -> List[Enemy]:
        return [e for e in self.enemies if e.alive]

    @property
    def over(self) -> bool:
        return not self.alive_enemies()

    def next_attacker(self) -> Optional[Enemy]:
        """다음으로 반격할 살아있는 적을 라운드 로빈으로 고른다."""
        count = len(self.enemies)
        for step in range(count):
            idx = (self.attacker_idx + step) % count
            if self.enemies[idx].alive:
                self.attacker_idx = (idx + 1) % count
                return self.enemies[idx]
        return None

    def to_dict(self) -> dict:
        return {
            "enemies": [e.to_dict() for e in self.enemies],
            "attacker_idx": self.attacker_idx,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CombatState":
        return cls(
            enemies=[Enemy.from_dict(e) for e in data.get("enemies", [])],
            attacker_idx=int(data.get("attacker_idx", 0)),
        )


def _normalize_enemy_damage(raw) -> str:
    """LLM이 제안한 적 피해 주사위 표기를 검증·상한 적용한다."""
    if not isinstance(raw, str):
        return ENEMY_DAMAGE_FALLBACK
    match = DICE_NOTATION_RE.match(raw)
    if not match:
        return ENEMY_DAMAGE_FALLBACK
    count = max(1, min(2, int(match.group("count") or 1)))
    sides = max(2, min(12, int(match.group("sides"))))
    mod = max(0, min(5, int(match.group("mod") or 0)))
    if match.group("sign") == "-":
        mod = 0
    notation = f"{count}d{sides}"
    if mod:
        notation += f"+{mod}"
    return notation


def _combat_from_llm(raw) -> Optional[CombatState]:
    """LLM의 combat_start 필드를 정규화해 CombatState 를 만든다. 부적합하면 None."""
    if isinstance(raw, dict):
        raw = raw.get("enemies")
    if not isinstance(raw, list):
        return None
    enemies: List[Enemy] = []
    for item in raw[:COMBAT_MAX_ENEMIES]:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name", ""), 20)
        if not name:
            continue
        try:
            hp = int(item.get("hp", 10))
        except (TypeError, ValueError):
            hp = 10
        hp = max(1, min(ENEMY_HP_LIMIT, hp))
        try:
            ac = int(item.get("ac", 12))
        except (TypeError, ValueError):
            ac = 12
        ac = max(ENEMY_AC_MIN, min(ENEMY_AC_MAX, ac))
        try:
            attack = int(item.get("attack", 2))
        except (TypeError, ValueError):
            attack = 2
        attack = max(0, min(ENEMY_ATTACK_LIMIT, attack))
        enemies.append(Enemy(
            name=name, hp=hp, max_hp=hp, ac=ac, attack=attack,
            damage=_normalize_enemy_damage(item.get("damage")),
        ))
    return CombatState(enemies=enemies) if enemies else None


def build_combat_choices(combat: CombatState) -> List[Dict]:
    """전투 중 선택지는 코드가 생성한다: 살아있는 적별 공격 + 방어."""
    choices: List[Dict] = []
    for idx, enemy in enumerate(combat.enemies):
        if not enemy.alive:
            continue
        choices.append({
            "text": f"⚔️ {enemy.name} 공격", "stat": None, "dc": DEFAULT_DC,
            "combat": "attack", "target": idx,
        })
        if len(choices) >= MAX_CHOICES - 1:
            break
    choices.append({"text": "🛡️ 방어", "stat": None, "dc": DEFAULT_DC, "combat": "defend"})
    return choices


def _combat_start_log(combat: CombatState) -> List[str]:
    lines = ["⚔️ 전투 시작!"]
    for enemy in combat.enemies:
        lines.append(f"- {enemy.name}: HP {enemy.hp} / 방어 {enemy.ac} / 공격 +{enemy.attack} ({enemy.damage})")
    return lines


def _attack_profile(char: "TRPGCharacter") -> tuple:
    """공격에 쓸 스탯과 보정치. 힘/민첩/지능 중 가장 높은 것을 쓴다."""
    best = max(ATTACK_STATS, key=lambda s: char.stats.get(s, 0))
    return best, char.stats.get(best, 0)


def _resolve_player_attack(char: "TRPGCharacter", enemy: Enemy) -> List[str]:
    """플레이어의 공격을 판정·적용한다. 명중: d20+보정 vs 방어, 피해: 1d8+보정 (치명타 2d8)."""
    stat, mod = _attack_profile(char)
    roll = random.randint(1, 20)
    total = roll + mod
    crit = roll == 20
    hit = crit or (roll != 1 and total >= enemy.ac)
    if crit:
        band = "치명타!"
    elif roll == 1:
        band = "대실패"
    else:
        band = "명중" if hit else "빗나감"
    lines = [f"⚔️ {char.name} 의 공격({stat}) → {enemy.name}: d20({roll})+{mod}={total} / 방어 {enemy.ac} → **{band}**"]
    if hit:
        dice = [random.randint(1, PLAYER_DAMAGE_DIE) for _ in range(2 if crit else 1)]
        damage = max(1, sum(dice) + mod)
        enemy.apply_hp(-damage)
        dice_text = "+".join(str(d) for d in dice)
        line = (
            f"💥 피해 {len(dice)}d{PLAYER_DAMAGE_DIE}({dice_text})+{mod} = {damage}"
            f" → {enemy.name} HP {enemy.hp}/{enemy.max_hp}"
        )
        if not enemy.alive:
            line += " 💀 처치!"
        lines.append(line)
    return lines


def _resolve_enemy_attack(
    combat: CombatState,
    participants: List["TRPGCharacter"],
    defending: set,
) -> tuple:
    """적 1체(라운드 로빈)의 반격을 판정·적용한다. (로그, {이름: HP변화}) 반환."""
    alive_targets = [c for c in participants if c.hp > 0]
    enemy = combat.next_attacker()
    if enemy is None or not alive_targets:
        return [], {}
    target = random.choice(alive_targets)
    defense = PLAYER_BASE_DEFENSE + target.stats.get("민첩", 0)
    if target.name in defending:
        defense += DEFEND_BONUS
    roll = random.randint(1, 20)
    total = roll + enemy.attack
    crit = roll == 20
    hit = crit or (roll != 1 and total >= defense)
    if crit:
        band = "치명타!"
    elif roll == 1:
        band = "대실패"
    else:
        band = "명중" if hit else "빗나감"
    lines = [f"🗡️ {enemy.name} 의 공격 → {target.name}: d20({roll})+{enemy.attack}={total} / 방어 {defense} → **{band}**"]
    deltas: Dict[str, int] = {}
    if hit:
        damage = roll_dice(enemy.damage).total
        if crit:
            damage *= 2
        damage = max(1, damage)
        applied = target.apply_hp(-damage)
        if applied:
            deltas[target.name] = applied
        line = f"💥 피해 {enemy.damage}{' ×2' if crit else ''} = {damage} → {target.name} HP {target.hp}/{target.max_hp}"
        if target.hp <= 0:
            line += " 💀 쓰러짐!"
        lines.append(line)
    return lines, deltas


def _run_combat_mechanics(
    combat: CombatState,
    actor: "TRPGCharacter",
    choice: Optional[dict],
    participants: List["TRPGCharacter"],
) -> tuple:
    """전투 한 턴의 기계 판정을 모두 처리한다. (로그, {이름: HP변화}, 자유행동 여부) 반환."""
    log: List[str] = []
    deltas: Dict[str, int] = {}
    kind = (choice or {}).get("combat")
    free = kind not in ("attack", "defend")
    defending: set = set()

    if kind == "attack":
        target = None
        idx = choice.get("target")
        if isinstance(idx, int) and 0 <= idx < len(combat.enemies) and combat.enemies[idx].alive:
            target = combat.enemies[idx]
        else:
            alive = combat.alive_enemies()
            target = alive[0] if alive else None
        if target is not None:
            log.extend(_resolve_player_attack(actor, target))
    elif kind == "defend":
        defending.add(actor.name)
        log.append(f"🛡️ {actor.name} 이(가) 방어 태세를 갖췄다 (이번 적 공격에 방어 +{DEFEND_BONUS})")

    if combat.over:
        log.append("🏆 모든 적을 물리쳤다!")
    else:
        wave_log, wave_deltas = _resolve_enemy_attack(combat, participants, defending)
        log.extend(wave_log)
        for name, delta in wave_deltas.items():
            deltas[name] = deltas.get(name, 0) + delta
    return log, deltas, free


def _combat_prompt_block(combat: CombatState) -> str:
    alive = combat.alive_enemies()
    if not alive:
        return "[전투 상황] 모든 적이 쓰러졌다."
    lines = [f"- {e.name}: HP {e.hp}/{e.max_hp}" for e in alive]
    return "[전투 상황] 남은 적:\n" + "\n".join(lines)


def _build_combat_narration_prompt(
    context_lines: List[str],
    combat: CombatState,
    *,
    action_line: str,
    check: Optional["CheckResult"],
    log_lines: List[str],
    combat_over: bool,
    players_down: bool,
    free_action: bool,
) -> str:
    lines = list(context_lines)
    lines.append(_combat_prompt_block(combat))
    lines.append(f"[이번 행동] {action_line}")
    if check is not None:
        lines.append(f"[판정 결과] {check.prompt_text()}")
    lines.append(
        "[전투 판정 — 코드로 이미 확정됨. 절대 뒤집거나 새로운 피해를 만들지 마세요]\n"
        + "\n".join(log_lines)
    )
    if combat_over:
        lines.append("[전투 종료] 모든 적이 쓰러졌습니다. 전투의 마무리와 그 직후 상황을 서술하세요.")
    if players_down:
        lines.append("[위급] 아군이 모두 쓰러졌습니다. 상황에 맞게 서술하세요.")

    schema = ['  "narration": "위 판정 결과를 그대로 반영한 전투 장면 서술 (3~6문장)"']
    if free_action:
        schema.append('  "hp_changes": [{"name": "캐릭터 이름", "change": 5}]')
        schema.append('  "items_add": ["새로 얻은 아이템"]')
        schema.append('  "items_remove": ["잃거나 사용한 아이템"]')
    if combat_over:
        schema.append('  "choices": [{"text": "선택지 행동 (25자 이내)", "stat": "힘|민첩|지능|매력|없음", "dc": 12}]')
    lines.append(
        "다음 JSON 형식으로만 답하세요:\n{\n" + ",\n".join(schema) + "\n}"
        + ("\n규칙: hp_changes 는 자유 행동의 결과(물약 사용 등)로 아군 HP가 변한 경우에만 적습니다. "
           "적의 HP는 코드가 관리하므로 절대 변경을 제안하지 마세요." if free_action else "")
    )
    return "\n\n".join(lines)


def _combat_chat(prompt: str, *, model: Optional[str], system: str) -> dict:
    """전투 서술용 LLM 호출. 실패해도 전투가 멈추지 않도록 빈 dict 를 반환한다."""
    try:
        return _chat_json(prompt, model=model, temperature=TURN_TEMPERATURE, system=system)
    except Exception as e:
        logger.error(f"전투 서술 생성 실패, 판정 로그로 대체합니다: {e}")
        return {}


COMBAT_START_SCHEMA_LINE = (
    '  "combat_start": {"enemies": [{"name": "적 이름", "hp": 12, "ac": 12, "attack": 3, "damage": "1d6+1"}]},\n'
)
COMBAT_START_RULE = (
    "combat_start 는 이번 장면에서 실제로 전투가 벌어지는 경우에만 넣습니다 "
    f"(적 1~{COMBAT_MAX_ENEMIES}체, hp는 {ENEMY_HP_LIMIT} 이하, ac는 {ENEMY_AC_MIN}~{ENEMY_AC_MAX}, "
    f"attack은 0~{ENEMY_ATTACK_LIMIT}, damage는 1d4~2d12 표기). 전투가 없으면 combat_start 를 생략합니다. "
)


# ------------------------------------------------------------------ 캐릭터
@dataclass
class TRPGCharacter:
    name: str
    job: str
    job_emoji: str
    stats: Dict[str, int]
    hp: int
    max_hp: int
    inventory: List[str] = field(default_factory=list)
    race: str = ""               # 종족 (예: 엘프, 드워프). 비어 있으면 표기 생략.
    background: str = ""         # 배경 설정 — GM이 역할 연기(성격·말투·과거)에 반영한다.

    @classmethod
    def create(
        cls,
        name: str,
        class_key: str,
        *,
        race: str = "",
        background: str = "",
    ) -> "TRPGCharacter":
        spec = CLASSES[class_key]
        return cls(
            name=name,
            job=spec["label"],
            job_emoji=spec["emoji"],
            stats=dict(spec["stats"]),
            hp=spec["hp"],
            max_hp=spec["hp"],
            inventory=list(spec["items"]),
            race=race.strip(),
            background=background.strip(),
        )

    def apply_hp(self, delta: int) -> int:
        """HP 변화를 적용하고 실제 변화량을 반환한다."""
        before = self.hp
        self.hp = max(0, min(self.max_hp, self.hp + delta))
        return self.hp - before

    def stats_line(self) -> str:
        return " / ".join(f"{k} {'+' if v >= 0 else ''}{v}" for k, v in self.stats.items())

    def summary_line(self) -> str:
        return f"{self.job_emoji} {self.name} ({self.job}) — HP {self.hp}/{self.max_hp}"

    def prompt_text(self) -> str:
        inv = ", ".join(self.inventory) if self.inventory else "없음"
        race_part = f" / 종족: {self.race}" if self.race else ""
        lines = [
            f"이름: {self.name} / 직업: {self.job}{race_part} / HP: {self.hp}/{self.max_hp}",
            f"능력치: {self.stats_line()}",
            f"소지품: {inv}",
        ]
        if self.background:
            lines.append(f"배경: {self.background}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "job": self.job,
            "job_emoji": self.job_emoji,
            "stats": dict(self.stats),
            "hp": self.hp,
            "max_hp": self.max_hp,
            "inventory": list(self.inventory),
            "race": self.race,
            "background": self.background,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TRPGCharacter":
        return cls(
            name=data["name"],
            job=data["job"],
            job_emoji=data.get("job_emoji", "🎲"),
            stats=dict(data.get("stats", {})),
            hp=int(data.get("hp", 1)),
            max_hp=int(data.get("max_hp", 1)),
            inventory=list(data.get("inventory", [])),
            race=data.get("race", ""),
            background=data.get("background", ""),
        )


# ------------------------------------------------------------------ 모험 상태
@dataclass
class TRPGAdventure:
    genre_key: str
    title: str
    world: str
    quest: str
    character: TRPGCharacter
    scene: str
    choices: List[Dict] = field(default_factory=list)
    turn: int = 0
    log: List[str] = field(default_factory=list)         # 오래된 턴의 한 줄 요약
    recent: List[Dict] = field(default_factory=list)     # 최근 턴 전문 (프롬프트용)
    status: str = "playing"                              # playing / victory / dead / over
    combat: Optional[CombatState] = None                 # 진행 중인 전투 (없으면 None)

    @property
    def genre_label(self) -> str:
        return GENRES.get(self.genre_key, {}).get("label", self.genre_key)

    @property
    def genre_emoji(self) -> str:
        return GENRES.get(self.genre_key, {}).get("emoji", "🎲")

    @property
    def is_playing(self) -> bool:
        return self.status == "playing"

    def record_turn(self, action: str, result_band: str, narration: str) -> None:
        self.recent.append({
            "action": action,
            "result": result_band,
            "narration": narration[:400],
        })
        if len(self.recent) > RECENT_TURNS_IN_PROMPT:
            oldest = self.recent.pop(0)
            line = f"{oldest['action']} → {oldest['result']}"
            self.log.append(line[:80])
            if len(self.log) > LOG_LINES_IN_PROMPT:
                self.log = self.log[-LOG_LINES_IN_PROMPT:]

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "genre_key": self.genre_key,
            "title": self.title,
            "world": self.world,
            "quest": self.quest,
            "character": self.character.to_dict(),
            "scene": self.scene,
            "choices": list(self.choices),
            "turn": self.turn,
            "log": list(self.log),
            "recent": list(self.recent),
            "status": self.status,
            "combat": self.combat.to_dict() if self.combat else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TRPGAdventure":
        combat_data = data.get("combat")
        return cls(
            genre_key=data["genre_key"],
            title=data.get("title", "이름 없는 모험"),
            world=data.get("world", ""),
            quest=data.get("quest", ""),
            character=TRPGCharacter.from_dict(data["character"]),
            scene=data.get("scene", ""),
            choices=list(data.get("choices", [])) or list(FALLBACK_CHOICES),
            turn=int(data.get("turn", 0)),
            log=list(data.get("log", [])),
            recent=list(data.get("recent", [])),
            status=data.get("status", "playing"),
            combat=CombatState.from_dict(combat_data) if combat_data else None,
        )


@dataclass
class TurnResult:
    """한 턴 처리 결과. 상태 변화는 이미 어드벤처에 적용되어 있다."""

    narration: str
    hp_change: int
    items_added: List[str]
    items_removed: List[str]
    ended: bool
    victory: bool
    combat_log: List[str] = field(default_factory=list)   # 코드가 확정한 전투 판정 로그


# ------------------------------------------------------------------ LLM 응답 정규화
def _clean_text(value, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    return strip_non_korean(value.strip())[:max_len].strip()


def _normalize_choices(raw) -> List[Dict]:
    choices: List[Dict] = []
    if not isinstance(raw, list):
        raw = []
    for item in raw[:MAX_CHOICES]:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text", ""), CHOICE_TEXT_MAX)
        if not text:
            continue
        stat = item.get("stat")
        if stat not in STATS:
            stat = None
        try:
            dc = int(item.get("dc", DEFAULT_DC))
        except (TypeError, ValueError):
            dc = DEFAULT_DC
        dc = max(8, min(18, dc))
        choices.append({"text": text, "stat": stat, "dc": dc})
    return choices or [dict(c) for c in FALLBACK_CHOICES]


def _normalize_items(raw, limit: int = 3) -> List[str]:
    if not isinstance(raw, list):
        return []
    items = []
    for item in raw[:limit]:
        cleaned = _clean_text(item, 24)
        if cleaned:
            items.append(cleaned)
    return items


def _normalize_hp_change(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(-HP_CHANGE_LIMIT, min(HP_CHANGE_LIMIT, value))


def _chat_json(
    user_content: str,
    *,
    model: Optional[str],
    temperature: float,
    system: str = GM_SYSTEM_PROMPT,
) -> dict:
    """GM 시스템 프롬프트로 LLM을 호출하고 JSON 객체를 파싱한다. 실패 시 1회 재시도."""
    messages = [{"role": "user", "content": user_content}]
    reply = ollama_chat_sync(
        messages,
        system=system,
        model=model,
        temperature=temperature,
        format_json=True,
    )
    try:
        return extract_json_object(reply)
    except ValueError:
        logger.warning("TRPG GM 응답 JSON 파싱 실패, 재시도합니다. 응답 일부: %s", reply[:120])

    retry_messages = messages + [
        {"role": "assistant", "content": reply},
        {"role": "user", "content": "출력이 유효한 JSON이 아니었습니다. 지시한 형식의 JSON 객체 하나만 다시 출력하세요."},
    ]
    reply = ollama_chat_sync(
        retry_messages,
        system=system,
        model=model,
        temperature=temperature,
        format_json=True,
    )
    try:
        return extract_json_object(reply)
    except ValueError as e:
        raise RuntimeError("GM(AI) 응답을 해석하지 못했습니다. 다시 시도해주세요.") from e


# ------------------------------------------------------------------ 시나리오 생성
def generate_scenario(
    genre_key: str,
    character: TRPGCharacter,
    *,
    model: Optional[str] = None,
) -> TRPGAdventure:
    """장르와 캐릭터를 바탕으로 새 모험(세계관·퀘스트·첫 장면)을 생성한다."""
    genre = GENRES[genre_key]
    user_content = (
        "새 TRPG 모험을 생성하세요.\n"
        f"장르: {genre['label']} — {genre['hint']}\n"
        f"[주인공]\n{character.prompt_text()}\n"
        "\n"
        "다음 JSON 형식으로만 답하세요:\n"
        "{\n"
        '  "title": "모험 제목 (20자 이내)",\n'
        '  "world": "세계관 소개 (3~4문장)",\n'
        '  "quest": "주인공이 달성해야 할 목표 퀘스트 (1~2문장)",\n'
        '  "opening": "첫 장면 서술 (4~7문장, 주인공을 \'당신\'으로 지칭, 퀘스트를 받게 되는 상황)",\n'
        '  "choices": [{"text": "선택지 행동 (25자 이내)", "stat": "힘|민첩|지능|매력|없음", "dc": 12}]\n'
        "}"
    )
    data = _chat_json(user_content, model=model, temperature=SCENARIO_TEMPERATURE)

    title = _clean_text(data.get("title", ""), 40) or f"{genre['label']} 모험"
    world = _clean_text(data.get("world", ""), 700)
    quest = _clean_text(data.get("quest", ""), 300)
    opening = _clean_text(data.get("opening", ""), NARRATION_MAX)
    if not opening:
        raise RuntimeError("GM(AI)이 첫 장면을 생성하지 못했습니다. 다시 시도해주세요.")

    return TRPGAdventure(
        genre_key=genre_key,
        title=title,
        world=world,
        quest=quest,
        character=character,
        scene=opening,
        choices=_normalize_choices(data.get("choices")),
        turn=1,
    )


# ------------------------------------------------------------------ 턴 진행
def _solo_context_lines(adv: TRPGAdventure) -> List[str]:
    lines = [
        f"[세계관] {adv.world}",
        f"[퀘스트] {adv.quest}",
        f"[캐릭터]\n{adv.character.prompt_text()}",
    ]
    if adv.log:
        lines.append("[지난 모험 기록]\n" + "\n".join(f"- {entry}" for entry in adv.log))
    # 마지막 recent 항목의 서술은 [현재 장면]과 같으므로 행동/결과만 싣고 본문은 중복시키지 않는다.
    for entry in adv.recent[:-1]:
        lines.append(
            f"[이전 턴] 행동: {entry['action']} ({entry['result']})\n{entry['narration']}"
        )
    if adv.recent:
        last = adv.recent[-1]
        lines.append(f"[직전 행동] {last['action']} ({last['result']})")
    lines.append(f"[현재 장면]\n{adv.scene}")
    return lines


def _build_turn_prompt(adv: TRPGAdventure, action_text: str, check: Optional[CheckResult]) -> str:
    lines = _solo_context_lines(adv)
    lines.append(f"[플레이어 행동] {action_text}")
    if check is not None:
        lines.append(f"[판정 결과] {check.prompt_text()}")
    else:
        lines.append("[판정 결과] 판정 없음 (안전한 행동, 실패 없이 진행)")
    lines.append(
        "\n판정 결과에 충실하게 다음 장면을 서술하고, 다음 JSON 형식으로만 답하세요:\n"
        "{\n"
        '  "narration": "다음 장면 서술 (3~7문장)",\n'
        '  "choices": [{"text": "선택지 행동 (25자 이내)", "stat": "힘|민첩|지능|매력|없음", "dc": 12}],\n'
        '  "hp_change": 0,\n'
        '  "items_add": ["새로 얻은 아이템"],\n'
        '  "items_remove": ["잃거나 사용한 아이템"],\n'
        + COMBAT_START_SCHEMA_LINE +
        '  "game_over": false,\n'
        '  "victory": false\n'
        "}\n"
        "규칙: hp_change는 -10~10 사이 정수로, 피해를 입으면 음수, 회복하면 양수, 그 외에는 0. "
        "items_add/items_remove는 실제 변화가 있을 때만 채웁니다. "
        + COMBAT_START_RULE +
        "victory는 퀘스트를 최종 달성한 경우에만 true, game_over는 죽음 등으로 모험이 끝난 경우에만 true로 합니다."
    )
    return "\n\n".join(lines)


def _play_solo_combat_turn(
    adv: TRPGAdventure,
    action_text: str,
    check: Optional[CheckResult],
    choice: Optional[dict],
    model: Optional[str],
) -> TurnResult:
    """전투 중 한 턴: 판정은 코드가 확정하고 LLM은 서술만 한다."""
    combat = adv.combat
    char = adv.character
    mech_log, deltas, free = _run_combat_mechanics(combat, char, choice, [char])
    combat_over = combat.over
    players_down = char.hp <= 0

    prompt = _build_combat_narration_prompt(
        _solo_context_lines(adv),
        combat,
        action_line=f"{char.name}: {action_text}",
        check=check if free else None,
        log_lines=mech_log,
        combat_over=combat_over,
        players_down=players_down,
        free_action=free,
    )
    data = _combat_chat(prompt, model=model, system=GM_SYSTEM_PROMPT)
    narration = _clean_text(data.get("narration", ""), NARRATION_MAX) or "\n".join(mech_log)

    hp_change = deltas.get(char.name, 0)
    items_added: List[str] = []
    items_removed: List[str] = []
    if free and data:
        for _, delta in _normalize_party_hp_changes(data.get("hp_changes"), {"solo": char}).items():
            hp_change += char.apply_hp(delta)
        for item in _normalize_items(data.get("items_add")):
            if len(char.inventory) >= INVENTORY_MAX:
                break
            if item not in char.inventory:
                char.inventory.append(item)
                items_added.append(item)
        for item in _normalize_items(data.get("items_remove")):
            if item in char.inventory:
                char.inventory.remove(item)
                items_removed.append(item)

    if char.hp <= 0:
        adv.status = "dead"

    if combat_over:
        adv.combat = None
        adv.choices = _normalize_choices(data.get("choices"))
    else:
        adv.choices = build_combat_choices(combat)

    result_band = check.band if (check and free) else "전투"
    adv.record_turn(action_text, result_band, narration)
    adv.scene = narration
    adv.turn += 1

    return TurnResult(
        narration=narration,
        hp_change=hp_change,
        items_added=items_added,
        items_removed=items_removed,
        ended=not adv.is_playing,
        victory=False,
        combat_log=mech_log,
    )


def play_turn(
    adv: TRPGAdventure,
    action_text: str,
    check: Optional[CheckResult],
    *,
    choice: Optional[dict] = None,
    model: Optional[str] = None,
) -> TurnResult:
    """플레이어 행동 한 턴을 처리하고 어드벤처 상태를 갱신한다."""
    if adv.combat is not None:
        return _play_solo_combat_turn(adv, action_text, check, choice, model)

    data = _chat_json(
        _build_turn_prompt(adv, action_text, check),
        model=model,
        temperature=TURN_TEMPERATURE,
    )

    narration = _clean_text(data.get("narration", ""), NARRATION_MAX)
    if not narration:
        raise RuntimeError("GM(AI)이 장면 서술을 생성하지 못했습니다. 다시 시도해주세요.")

    hp_change = adv.character.apply_hp(_normalize_hp_change(data.get("hp_change")))

    items_added: List[str] = []
    for item in _normalize_items(data.get("items_add")):
        if len(adv.character.inventory) >= INVENTORY_MAX:
            break
        if item not in adv.character.inventory:
            adv.character.inventory.append(item)
            items_added.append(item)

    items_removed: List[str] = []
    for item in _normalize_items(data.get("items_remove")):
        if item in adv.character.inventory:
            adv.character.inventory.remove(item)
            items_removed.append(item)

    victory = bool(data.get("victory"))
    game_over = bool(data.get("game_over"))
    if adv.character.hp <= 0:
        adv.status = "dead"
    elif victory:
        adv.status = "victory"
    elif game_over:
        adv.status = "over"

    result_band = check.band if check else "진행"
    adv.record_turn(action_text, result_band, narration)
    adv.scene = narration
    adv.turn += 1

    combat_log: List[str] = []
    if adv.is_playing:
        combat = _combat_from_llm(data.get("combat_start"))
        if combat is not None:
            adv.combat = combat
            combat_log = _combat_start_log(combat)
            adv.choices = build_combat_choices(combat)
        else:
            adv.choices = _normalize_choices(data.get("choices"))
    else:
        adv.choices = _normalize_choices(data.get("choices"))

    return TurnResult(
        narration=narration,
        hp_change=hp_change,
        items_added=items_added,
        items_removed=items_removed,
        ended=not adv.is_playing,
        victory=adv.status == "victory",
        combat_log=combat_log,
    )


# ==================================================================== 파티(멀티플레이어) 모험
PARTY_MAX_MEMBERS = 4


def _party_member_line(char: TRPGCharacter) -> str:
    """파티 목록/프롬프트용 캐릭터 한 줄 요약."""
    inv = ", ".join(char.inventory) if char.inventory else "없음"
    line = (
        f"{char.name} ({char.job}) — HP {char.hp}/{char.max_hp}, "
        f"능력치: {char.stats_line()}, 소지품: {inv}"
    )
    if char.hp <= 0:
        line += " [쓰러짐]"
    return line


@dataclass
class PartyAdventure:
    """여러 플레이어가 함께 진행하는 파티 모험 상태.

    members 는 참가 순서를 유지하는 dict(user_id 문자열 -> 캐릭터)이며,
    턴은 turn_order 를 따라 돌아가고 HP 0인 멤버는 건너뛴다.
    """

    genre_key: str
    title: str
    world: str
    quest: str
    host_id: str
    members: Dict[str, TRPGCharacter]
    scene: str
    choices: List[Dict] = field(default_factory=list)
    turn: int = 0
    turn_order: List[str] = field(default_factory=list)
    current_idx: int = 0
    log: List[str] = field(default_factory=list)
    recent: List[Dict] = field(default_factory=list)
    status: str = "playing"                              # playing / victory / dead / over
    combat: Optional[CombatState] = None                 # 진행 중인 전투 (없으면 None)

    def __post_init__(self):
        if not self.turn_order:
            self.turn_order = list(self.members.keys())

    @property
    def genre_label(self) -> str:
        return GENRES.get(self.genre_key, {}).get("label", self.genre_key)

    @property
    def genre_emoji(self) -> str:
        return GENRES.get(self.genre_key, {}).get("emoji", "🎲")

    @property
    def is_playing(self) -> bool:
        return self.status == "playing"

    @property
    def current_actor_id(self) -> Optional[str]:
        if not self.turn_order:
            return None
        return self.turn_order[self.current_idx % len(self.turn_order)]

    @property
    def current_character(self) -> Optional[TRPGCharacter]:
        actor_id = self.current_actor_id
        return self.members.get(actor_id) if actor_id else None

    def alive_ids(self) -> List[str]:
        return [uid for uid in self.turn_order if self.members[uid].hp > 0]

    def advance_turn(self) -> None:
        """다음 생존 멤버에게 턴을 넘긴다. 생존자가 없으면 그대로 둔다."""
        count = len(self.turn_order)
        if count == 0:
            return
        for step in range(1, count + 1):
            candidate = (self.current_idx + step) % count
            if self.members[self.turn_order[candidate]].hp > 0:
                self.current_idx = candidate
                return

    def ensure_current_alive(self) -> None:
        """현재 턴 멤버가 쓰러져 있으면 다음 생존 멤버로 턴을 옮긴다 (세이브 로드 대비)."""
        char = self.current_character
        if char is not None and char.hp <= 0 and self.alive_ids():
            self.advance_turn()

    def record_turn(self, actor_name: str, action: str, result_band: str, narration: str) -> None:
        self.recent.append({
            "actor": actor_name,
            "action": action,
            "result": result_band,
            "narration": narration[:400],
        })
        if len(self.recent) > RECENT_TURNS_IN_PROMPT:
            oldest = self.recent.pop(0)
            line = f"{oldest.get('actor', '?')}: {oldest['action']} → {oldest['result']}"
            self.log.append(line[:80])
            if len(self.log) > LOG_LINES_IN_PROMPT:
                self.log = self.log[-LOG_LINES_IN_PROMPT:]

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "genre_key": self.genre_key,
            "title": self.title,
            "world": self.world,
            "quest": self.quest,
            "host_id": self.host_id,
            "members": {uid: char.to_dict() for uid, char in self.members.items()},
            "scene": self.scene,
            "choices": list(self.choices),
            "turn": self.turn,
            "turn_order": list(self.turn_order),
            "current_idx": self.current_idx,
            "log": list(self.log),
            "recent": list(self.recent),
            "status": self.status,
            "combat": self.combat.to_dict() if self.combat else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PartyAdventure":
        members = {
            str(uid): TRPGCharacter.from_dict(char_data)
            for uid, char_data in data.get("members", {}).items()
        }
        combat_data = data.get("combat")
        adv = cls(
            genre_key=data["genre_key"],
            title=data.get("title", "이름 없는 모험"),
            world=data.get("world", ""),
            quest=data.get("quest", ""),
            host_id=str(data.get("host_id", "")),
            members=members,
            scene=data.get("scene", ""),
            choices=list(data.get("choices", [])) or [dict(c) for c in FALLBACK_CHOICES],
            turn=int(data.get("turn", 0)),
            turn_order=[str(uid) for uid in data.get("turn_order", [])],
            current_idx=int(data.get("current_idx", 0)),
            log=list(data.get("log", [])),
            recent=list(data.get("recent", [])),
            status=data.get("status", "playing"),
            combat=CombatState.from_dict(combat_data) if combat_data else None,
        )
        # 세이브가 손상되어 turn_order 에 없는 멤버가 있어도 게임이 멈추지 않게 정리한다.
        adv.turn_order = [uid for uid in adv.turn_order if uid in adv.members]
        if not adv.turn_order:
            adv.turn_order = list(adv.members.keys())
        adv.current_idx %= max(1, len(adv.turn_order))
        return adv


@dataclass
class PartyTurnResult:
    """파티 한 턴 처리 결과. 상태 변화는 이미 어드벤처에 적용되어 있다."""

    narration: str
    hp_changes: Dict[str, int]           # 캐릭터 이름 -> 실제 적용된 HP 변화량
    items_added: List[str]
    items_removed: List[str]
    ended: bool
    victory: bool
    combat_log: List[str] = field(default_factory=list)   # 코드가 확정한 전투 판정 로그


def generate_party_scenario(
    genre_key: str,
    host_id: str,
    members: Dict[str, TRPGCharacter],
    *,
    model: Optional[str] = None,
) -> PartyAdventure:
    """장르와 파티 구성원을 바탕으로 새 파티 모험(세계관·퀘스트·첫 장면)을 생성한다."""
    genre = GENRES[genre_key]
    party_lines = "\n".join(f"- {_party_member_line(char)}" for char in members.values())
    user_content = (
        f"새 TRPG 모험을 생성하세요. 이번 모험은 {len(members)}명이 한 파티로 함께 진행합니다.\n"
        f"장르: {genre['label']} — {genre['hint']}\n"
        f"[파티]\n{party_lines}\n"
        "\n"
        "다음 JSON 형식으로만 답하세요:\n"
        "{\n"
        '  "title": "모험 제목 (20자 이내)",\n'
        '  "world": "세계관 소개 (3~4문장)",\n'
        '  "quest": "파티가 함께 달성해야 할 공동 목표 퀘스트 (1~2문장)",\n'
        '  "opening": "첫 장면 서술 (4~7문장, 파티 전원이 함께 등장해 퀘스트를 받는 상황, 각 캐릭터를 이름으로 지칭)",\n'
        '  "choices": [{"text": "선택지 행동 (25자 이내)", "stat": "힘|민첩|지능|매력|없음", "dc": 12}]\n'
        "}"
    )
    data = _chat_json(
        user_content,
        model=model,
        temperature=SCENARIO_TEMPERATURE,
        system=GM_PARTY_SYSTEM_PROMPT,
    )

    title = _clean_text(data.get("title", ""), 40) or f"{genre['label']} 모험"
    world = _clean_text(data.get("world", ""), 700)
    quest = _clean_text(data.get("quest", ""), 300)
    opening = _clean_text(data.get("opening", ""), NARRATION_MAX)
    if not opening:
        raise RuntimeError("GM(AI)이 첫 장면을 생성하지 못했습니다. 다시 시도해주세요.")

    return PartyAdventure(
        genre_key=genre_key,
        title=title,
        world=world,
        quest=quest,
        host_id=str(host_id),
        members=members,
        scene=opening,
        choices=_normalize_choices(data.get("choices")),
        turn=1,
    )


def _normalize_party_hp_changes(raw, members: Dict[str, TRPGCharacter]) -> Dict[str, int]:
    """LLM이 제안한 hp_changes 목록을 {user_id: 변화량} 으로 정규화한다."""
    if not isinstance(raw, list):
        return {}
    name_to_uid = {char.name: uid for uid, char in members.items()}
    changes: Dict[str, int] = {}
    for item in raw[:PARTY_MAX_MEMBERS * 2]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        uid = name_to_uid.get(name)
        if uid is None:
            # 호칭이 덧붙는 경우("전사 알렉스" 등)를 대비한 부분 일치
            uid = next(
                (u for n, u in name_to_uid.items() if n and (n in name or name in n)),
                None,
            )
        if uid is None:
            continue
        delta = _normalize_hp_change(item.get("change"))
        if delta:
            changes[uid] = max(
                -HP_CHANGE_LIMIT, min(HP_CHANGE_LIMIT, changes.get(uid, 0) + delta)
            )
    return changes


def _party_context_lines(adv: PartyAdventure) -> List[str]:
    lines = [
        f"[세계관] {adv.world}",
        f"[퀘스트] {adv.quest}",
        "[파티 상태]\n" + "\n".join(f"- {_party_member_line(char)}" for char in adv.members.values()),
    ]
    if adv.log:
        lines.append("[지난 모험 기록]\n" + "\n".join(f"- {entry}" for entry in adv.log))
    # 마지막 recent 항목의 서술은 [현재 장면]과 같으므로 행동/결과만 싣고 본문은 중복시키지 않는다.
    for entry in adv.recent[:-1]:
        lines.append(
            f"[이전 턴] {entry.get('actor', '?')}의 행동: {entry['action']} ({entry['result']})\n{entry['narration']}"
        )
    if adv.recent:
        last = adv.recent[-1]
        lines.append(f"[직전 행동] {last.get('actor', '?')}: {last['action']} ({last['result']})")
    lines.append(f"[현재 장면]\n{adv.scene}")
    return lines


def _build_party_turn_prompt(
    adv: PartyAdventure,
    actor: TRPGCharacter,
    action_text: str,
    check: Optional[CheckResult],
) -> str:
    lines = _party_context_lines(adv)
    lines.append(f"[이번 턴 행동] {actor.name} ({actor.job})의 행동: {action_text}")
    if check is not None:
        lines.append(f"[판정 결과] {check.prompt_text()}")
    else:
        lines.append("[판정 결과] 판정 없음 (안전한 행동, 실패 없이 진행)")
    lines.append(
        "\n판정 결과에 충실하게 다음 장면을 서술하고, 다음 JSON 형식으로만 답하세요:\n"
        "{\n"
        '  "narration": "다음 장면 서술 (3~7문장)",\n'
        '  "choices": [{"text": "선택지 행동 (25자 이내)", "stat": "힘|민첩|지능|매력|없음", "dc": 12}],\n'
        '  "hp_changes": [{"name": "캐릭터 이름", "change": -3}],\n'
        '  "items_add": ["새로 얻은 아이템"],\n'
        '  "items_remove": ["잃거나 사용한 아이템"],\n'
        + COMBAT_START_SCHEMA_LINE +
        '  "game_over": false,\n'
        '  "victory": false\n'
        "}\n"
        "규칙: hp_changes 의 change 는 -10~10 사이 정수이며, 이번 장면에서 실제로 피해를 입거나 "
        "회복한 캐릭터만 포함합니다 (없으면 빈 배열). "
        f"items_add/items_remove 는 행동한 캐릭터({actor.name})의 소지품 변화만 적습니다. "
        + COMBAT_START_RULE +
        "victory 는 파티가 퀘스트를 최종 달성한 경우에만 true, "
        "game_over 는 파티가 전멸하는 등 모험이 끝난 경우에만 true 로 합니다."
    )
    return "\n\n".join(lines)


def _play_party_combat_turn(
    adv: PartyAdventure,
    actor_id: str,
    action_text: str,
    check: Optional[CheckResult],
    choice: Optional[dict],
    model: Optional[str],
) -> PartyTurnResult:
    """파티 전투 중 한 턴: 판정은 코드가 확정하고 LLM은 서술만 한다."""
    combat = adv.combat
    actor = adv.members[str(actor_id)]
    participants = list(adv.members.values())
    mech_log, deltas, free = _run_combat_mechanics(combat, actor, choice, participants)
    combat_over = combat.over
    players_down = not adv.alive_ids()

    prompt = _build_combat_narration_prompt(
        _party_context_lines(adv),
        combat,
        action_line=f"{actor.name} ({actor.job}): {action_text}",
        check=check if free else None,
        log_lines=mech_log,
        combat_over=combat_over,
        players_down=players_down,
        free_action=free,
    )
    data = _combat_chat(prompt, model=model, system=GM_PARTY_SYSTEM_PROMPT)
    narration = _clean_text(data.get("narration", ""), NARRATION_MAX) or "\n".join(mech_log)

    hp_changes = dict(deltas)
    items_added: List[str] = []
    items_removed: List[str] = []
    if free and data:
        for uid, delta in _normalize_party_hp_changes(data.get("hp_changes"), adv.members).items():
            applied = adv.members[uid].apply_hp(delta)
            if applied:
                name = adv.members[uid].name
                hp_changes[name] = hp_changes.get(name, 0) + applied
        for item in _normalize_items(data.get("items_add")):
            if len(actor.inventory) >= INVENTORY_MAX:
                break
            if item not in actor.inventory:
                actor.inventory.append(item)
                items_added.append(item)
        for item in _normalize_items(data.get("items_remove")):
            if item in actor.inventory:
                actor.inventory.remove(item)
                items_removed.append(item)

    if not adv.alive_ids():
        adv.status = "dead"                 # 파티 전멸

    if combat_over:
        adv.combat = None
        adv.choices = _normalize_choices(data.get("choices"))
    else:
        adv.choices = build_combat_choices(combat)

    result_band = check.band if (check and free) else "전투"
    adv.record_turn(actor.name, action_text, result_band, narration)
    adv.scene = narration
    adv.turn += 1
    if adv.is_playing:
        adv.advance_turn()

    return PartyTurnResult(
        narration=narration,
        hp_changes=hp_changes,
        items_added=items_added,
        items_removed=items_removed,
        ended=not adv.is_playing,
        victory=False,
        combat_log=mech_log,
    )


def play_party_turn(
    adv: PartyAdventure,
    actor_id: str,
    action_text: str,
    check: Optional[CheckResult],
    *,
    choice: Optional[dict] = None,
    model: Optional[str] = None,
) -> PartyTurnResult:
    """파티 멤버 한 명의 행동 턴을 처리하고 어드벤처 상태를 갱신한다."""
    actor = adv.members.get(str(actor_id))
    if actor is None:
        raise ValueError("파티에 없는 플레이어의 행동입니다.")

    if adv.combat is not None:
        return _play_party_combat_turn(adv, actor_id, action_text, check, choice, model)

    data = _chat_json(
        _build_party_turn_prompt(adv, actor, action_text, check),
        model=model,
        temperature=TURN_TEMPERATURE,
        system=GM_PARTY_SYSTEM_PROMPT,
    )

    narration = _clean_text(data.get("narration", ""), NARRATION_MAX)
    if not narration:
        raise RuntimeError("GM(AI)이 장면 서술을 생성하지 못했습니다. 다시 시도해주세요.")

    # HP 변화는 파티 전원 대상 (LLM이 이름으로 지목), 아이템 변화는 행동자만 대상.
    hp_changes: Dict[str, int] = {}
    for uid, delta in _normalize_party_hp_changes(data.get("hp_changes"), adv.members).items():
        applied = adv.members[uid].apply_hp(delta)
        if applied:
            hp_changes[adv.members[uid].name] = applied

    items_added: List[str] = []
    for item in _normalize_items(data.get("items_add")):
        if len(actor.inventory) >= INVENTORY_MAX:
            break
        if item not in actor.inventory:
            actor.inventory.append(item)
            items_added.append(item)

    items_removed: List[str] = []
    for item in _normalize_items(data.get("items_remove")):
        if item in actor.inventory:
            actor.inventory.remove(item)
            items_removed.append(item)

    victory = bool(data.get("victory"))
    game_over = bool(data.get("game_over"))
    if not adv.alive_ids():
        adv.status = "dead"                 # 파티 전멸
    elif victory:
        adv.status = "victory"
    elif game_over:
        adv.status = "over"

    result_band = check.band if check else "진행"
    adv.record_turn(actor.name, action_text, result_band, narration)
    adv.scene = narration
    adv.turn += 1
    if adv.is_playing:
        adv.advance_turn()

    combat_log: List[str] = []
    combat = _combat_from_llm(data.get("combat_start")) if adv.is_playing else None
    if combat is not None:
        adv.combat = combat
        combat_log = _combat_start_log(combat)
        adv.choices = build_combat_choices(combat)
    else:
        adv.choices = _normalize_choices(data.get("choices"))

    return PartyTurnResult(
        narration=narration,
        hp_changes=hp_changes,
        items_added=items_added,
        items_removed=items_removed,
        ended=not adv.is_playing,
        victory=adv.status == "victory",
        combat_log=combat_log,
    )


# ==================================================================== 자유 모험(공유 세계) 모드
WORLD_MAX_MEMBERS = 6
WORLD_QUEST_MAX = 200        # 개인 퀘스트 길이 상한
WORLD_EVENTS_MAX = 6         # 프롬프트에 주입할 최근 사건 수 상한

GM_WORLD_SYSTEM_PROMPT = (
    "당신은 한국어 TRPG의 게임 마스터(GM)입니다. 이름은 '아리스'이며, 레트로 RPG를 사랑하는 열정적인 GM입니다.\n"
    "하나의 살아있는 세계에서 여러 플레이어가 각자 자신의 캐릭터와 개인 목표(개인 퀘스트)를 가지고\n"
    "자유롭게 모험합니다. 정해진 턴 순서는 없으며, 그때그때 행동한 캐릭터를 중심으로 이야기가 진행됩니다.\n"
    "\n"
    "[서술 규칙]\n"
    "- 반드시 자연스러운 한국어로만 작성합니다. 한자·일본어 가나·키릴 등 외국 문자는 한 글자도 쓰지 않습니다.\n"
    "- 각 캐릭터는 이름으로 지칭합니다. 모든 캐릭터가 같은 장면을 공유합니다.\n"
    "- 이번에 행동한 캐릭터를 중심으로 서술하되, 같은 장면의 다른 캐릭터의 가벼운 반응을 곁들여도 됩니다.\n"
    "- 행동하지 않은 캐릭터의 중요한 행동이나 판정을 마음대로 결정하지 않습니다.\n"
    "- 캐릭터들이 서로 돕는 것(협력)도, 각자 자기 목표를 좇는 것도 모두 자연스럽게 허용합니다.\n"
    "- 각 캐릭터의 종족과 배경 설정을 서술과 NPC의 반응에 적극 반영합니다 (역할 연기 존중).\n"
    "- 실패한 판정도 이야기를 멈추지 않고 새로운 상황을 만들어 전개를 이어갑니다.\n"
    "- 전투가 벌어지면 combat_start 로 적을 선언합니다. 전투 판정과 피해는 코드가 관리합니다.\n"
    "- 주사위 판정 결과를 절대 뒤집지 않습니다. 성공이면 성공으로, 실패면 실패로 서술합니다.\n"
    "- 대성공이면 기대 이상의 성과를, 대실패면 상황이 악화되는 전개를 서술합니다.\n"
    "- 세계관과 지금까지의 기록에 모순되지 않게 전개하고, 매번 새로운 정보나 사건을 하나씩 제시합니다.\n"
    "- HP가 0이 되어 쓰러진 캐릭터는 동료가 구하거나 치료할 수 있는 상태로 서술합니다.\n"
    "\n"
    "[출력 형식]\n"
    "- 반드시 유효한 JSON 객체 하나만 출력합니다. JSON 밖에 다른 텍스트를 쓰지 않습니다.\n"
    "- 선택지(choices)는 2~4개, 각 선택지 텍스트는 25자 이내로, 지금 상황에서 누구든 이어서 할 만한 행동으로 씁니다.\n"
    "- 선택지의 stat은 힘/민첩/지능/매력 중 판정이 필요한 스탯, 판정이 필요 없는 안전한 행동이면 \"없음\"으로 씁니다.\n"
    "- dc는 판정 난이도로 8(쉬움)~18(매우 어려움) 사이 정수입니다.\n"
    "- hp_changes 에는 이번 장면에서 실제로 피해나 회복이 있었던 캐릭터만 캐릭터 이름과 정수 변화량으로 적습니다.\n"
    "- quest_complete 는 이번 행동으로 '행동한 캐릭터의 개인 퀘스트'가 최종 달성된 경우에만 true 로 합니다."
)


@dataclass
class WorldMember:
    """자유 모험 세계에 참가 중인 한 플레이어의 상태."""

    character: TRPGCharacter
    quest: str = ""              # 개인 퀘스트
    quests_done: int = 0         # 완료한 개인 퀘스트 수

    def to_dict(self) -> dict:
        return {
            "character": self.character.to_dict(),
            "quest": self.quest,
            "quests_done": self.quests_done,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorldMember":
        return cls(
            character=TRPGCharacter.from_dict(data["character"]),
            quest=data.get("quest", ""),
            quests_done=int(data.get("quests_done", 0)),
        )


@dataclass
class WorldAdventure:
    """여러 플레이어가 개인 자격으로 드나드는 공유 세계 상태.

    파티 모드와 달리 턴 순서가 없고, 각자 개인 퀘스트를 가진다.
    세계는 종료 명령 전까지 계속 유지된다.
    """

    genre_key: str
    title: str
    world: str
    owner_id: str                                    # 세계를 연 사람 (종료 권한)
    members: Dict[str, WorldMember]                  # user_id -> 멤버 (참가 순서 유지)
    scene: str
    choices: List[Dict] = field(default_factory=list)
    turn: int = 0                                    # 누적 행동 수
    log: List[str] = field(default_factory=list)
    recent: List[Dict] = field(default_factory=list)
    events: List[str] = field(default_factory=list)  # 합류/이탈/퀘스트 완료 등 최근 사건
    last_actor_id: str = ""
    status: str = "playing"                          # playing / closed
    combat: Optional[CombatState] = None             # 진행 중인 전투 (없으면 None)

    @property
    def genre_label(self) -> str:
        return GENRES.get(self.genre_key, {}).get("label", self.genre_key)

    @property
    def genre_emoji(self) -> str:
        return GENRES.get(self.genre_key, {}).get("emoji", "🎲")

    @property
    def is_playing(self) -> bool:
        return self.status == "playing"

    def alive_ids(self) -> List[str]:
        return [uid for uid, member in self.members.items() if member.character.hp > 0]

    def add_event(self, text: str) -> None:
        self.events.append(text[:120])
        if len(self.events) > WORLD_EVENTS_MAX:
            self.events = self.events[-WORLD_EVENTS_MAX:]

    def record_action(self, actor_name: str, action: str, result_band: str, narration: str) -> None:
        self.recent.append({
            "actor": actor_name,
            "action": action,
            "result": result_band,
            "narration": narration[:400],
        })
        if len(self.recent) > RECENT_TURNS_IN_PROMPT:
            oldest = self.recent.pop(0)
            line = f"{oldest.get('actor', '?')}: {oldest['action']} → {oldest['result']}"
            self.log.append(line[:80])
            if len(self.log) > LOG_LINES_IN_PROMPT:
                self.log = self.log[-LOG_LINES_IN_PROMPT:]

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "genre_key": self.genre_key,
            "title": self.title,
            "world": self.world,
            "owner_id": self.owner_id,
            "members": {uid: member.to_dict() for uid, member in self.members.items()},
            "scene": self.scene,
            "choices": list(self.choices),
            "turn": self.turn,
            "log": list(self.log),
            "recent": list(self.recent),
            "events": list(self.events),
            "last_actor_id": self.last_actor_id,
            "status": self.status,
            "combat": self.combat.to_dict() if self.combat else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorldAdventure":
        combat_data = data.get("combat")
        return cls(
            genre_key=data["genre_key"],
            title=data.get("title", "이름 없는 세계"),
            world=data.get("world", ""),
            owner_id=str(data.get("owner_id", "")),
            members={
                str(uid): WorldMember.from_dict(member_data)
                for uid, member_data in data.get("members", {}).items()
            },
            scene=data.get("scene", ""),
            choices=list(data.get("choices", [])) or [dict(c) for c in FALLBACK_CHOICES],
            turn=int(data.get("turn", 0)),
            log=list(data.get("log", [])),
            recent=list(data.get("recent", [])),
            events=list(data.get("events", [])),
            last_actor_id=str(data.get("last_actor_id", "")),
            status=data.get("status", "playing"),
            combat=CombatState.from_dict(combat_data) if combat_data else None,
        )


@dataclass
class WorldActionResult:
    """자유 모험 행동 한 번의 처리 결과. 상태 변화는 이미 세계에 적용되어 있다."""

    narration: str
    hp_changes: Dict[str, int]     # 캐릭터 이름 -> 실제 적용된 HP 변화량
    items_added: List[str]
    items_removed: List[str]
    quest_completed: bool          # 행동자의 개인 퀘스트 달성 여부
    combat_log: List[str] = field(default_factory=list)   # 코드가 확정한 전투 판정 로그


def _world_members_block(adv: WorldAdventure) -> str:
    lines = []
    for member in adv.members.values():
        line = f"- {_party_member_line(member.character)}"
        if member.quest:
            line += f"\n  개인 퀘스트: {member.quest}"
        lines.append(line)
    return "\n".join(lines) if lines else "- (아직 아무도 없음)"


def generate_world_scenario(
    genre_key: str,
    owner_id: str,
    character: TRPGCharacter,
    *,
    model: Optional[str] = None,
) -> WorldAdventure:
    """장르와 첫 캐릭터를 바탕으로 새 공유 세계(세계관·첫 장면·개인 퀘스트)를 생성한다."""
    genre = GENRES[genre_key]
    user_content = (
        "새 TRPG 세계를 생성하세요. 이 세계는 여러 플레이어가 각자 개인 자격으로 드나드는 "
        "'열린 세계'이며, 지금 첫 번째 모험가가 도착했습니다.\n"
        f"장르: {genre['label']} — {genre['hint']}\n"
        f"[첫 모험가]\n{character.prompt_text()}\n"
        "\n"
        "다음 JSON 형식으로만 답하세요:\n"
        "{\n"
        '  "title": "세계(모험) 제목 (20자 이내)",\n'
        '  "world": "세계관 소개 (3~4문장, 여러 모험가가 오갈 수 있는 무대)",\n'
        '  "opening": "첫 장면 서술 (4~7문장, 첫 모험가가 세계에 도착하는 장면, 캐릭터를 이름으로 지칭)",\n'
        '  "personal_quest": "이 캐릭터만의 개인 퀘스트 (1~2문장)",\n'
        '  "choices": [{"text": "선택지 행동 (25자 이내)", "stat": "힘|민첩|지능|매력|없음", "dc": 12}]\n'
        "}"
    )
    data = _chat_json(
        user_content,
        model=model,
        temperature=SCENARIO_TEMPERATURE,
        system=GM_WORLD_SYSTEM_PROMPT,
    )

    title = _clean_text(data.get("title", ""), 40) or f"{genre['label']} 세계"
    world = _clean_text(data.get("world", ""), 700)
    opening = _clean_text(data.get("opening", ""), NARRATION_MAX)
    quest = _clean_text(data.get("personal_quest", ""), WORLD_QUEST_MAX)
    if not opening:
        raise RuntimeError("GM(AI)이 첫 장면을 생성하지 못했습니다. 다시 시도해주세요.")

    return WorldAdventure(
        genre_key=genre_key,
        title=title,
        world=world,
        owner_id=str(owner_id),
        members={str(owner_id): WorldMember(character=character, quest=quest)},
        scene=opening,
        choices=_normalize_choices(data.get("choices")),
        turn=1,
    )


def generate_world_join(
    adv: WorldAdventure,
    character: TRPGCharacter,
    *,
    model: Optional[str] = None,
) -> Dict[str, str]:
    """새 모험가의 등장 서술과 개인 퀘스트를 생성한다. {'arrival', 'quest'} 반환."""
    user_content = (
        "이 세계에 새로운 모험가가 등장합니다. 등장 장면과 개인 퀘스트를 만들어주세요.\n"
        f"[세계관] {adv.world}\n"
        f"[현재 장면] {adv.scene[:400]}\n"
        f"[기존 등장인물]\n{_world_members_block(adv)}\n"
        f"[새 모험가]\n{character.prompt_text()}\n"
        "\n"
        "다음 JSON 형식으로만 답하세요:\n"
        "{\n"
        '  "arrival": "새 모험가가 현재 장면에 자연스럽게 등장하는 서술 (2~3문장, 이름으로 지칭)",\n'
        '  "personal_quest": "이 캐릭터만의 개인 퀘스트 (1~2문장, 기존 인물들의 퀘스트와 겹치지 않게)"\n'
        "}"
    )
    data = _chat_json(
        user_content,
        model=model,
        temperature=TURN_TEMPERATURE,
        system=GM_WORLD_SYSTEM_PROMPT,
    )
    arrival = _clean_text(data.get("arrival", ""), 600)
    quest = _clean_text(data.get("personal_quest", ""), WORLD_QUEST_MAX)
    if not arrival:
        arrival = f"{character.name} 이(가) 모험에 합류했다."
    return {"arrival": arrival, "quest": quest}


def generate_world_quest(
    adv: WorldAdventure,
    character: TRPGCharacter,
    *,
    model: Optional[str] = None,
) -> str:
    """개인 퀘스트를 완료한 캐릭터에게 다음 개인 퀘스트를 생성한다."""
    user_content = (
        f"{character.name} 이(가) 방금 개인 퀘스트를 완수했습니다. "
        "이 세계에서 이어질 다음 개인 퀘스트 하나를 만들어주세요.\n"
        f"[세계관] {adv.world}\n"
        f"[현재 장면] {adv.scene[:400]}\n"
        f"[캐릭터]\n{character.prompt_text()}\n"
        "\n"
        '다음 JSON 형식으로만 답하세요:\n{"personal_quest": "다음 개인 퀘스트 (1~2문장)"}'
    )
    data = _chat_json(
        user_content,
        model=model,
        temperature=TURN_TEMPERATURE,
        system=GM_WORLD_SYSTEM_PROMPT,
    )
    return _clean_text(data.get("personal_quest", ""), WORLD_QUEST_MAX)


def _world_context_lines(adv: WorldAdventure) -> List[str]:
    lines = [
        f"[세계관] {adv.world}",
        f"[등장인물(플레이어)]\n{_world_members_block(adv)}",
    ]
    if adv.events:
        lines.append("[최근 사건]\n" + "\n".join(f"- {event}" for event in adv.events))
    if adv.log:
        lines.append("[지난 기록]\n" + "\n".join(f"- {entry}" for entry in adv.log))
    for entry in adv.recent[:-1]:
        lines.append(
            f"[이전 행동] {entry.get('actor', '?')}: {entry['action']} ({entry['result']})\n{entry['narration']}"
        )
    if adv.recent:
        last = adv.recent[-1]
        lines.append(f"[직전 행동] {last.get('actor', '?')}: {last['action']} ({last['result']})")
    lines.append(f"[현재 장면]\n{adv.scene}")
    return lines


def _build_world_action_prompt(
    adv: WorldAdventure,
    actor_member: WorldMember,
    action_text: str,
    check: Optional[CheckResult],
) -> str:
    actor = actor_member.character
    lines = _world_context_lines(adv)
    quest_note = f" (개인 퀘스트: {actor_member.quest})" if actor_member.quest else ""
    lines.append(f"[이번 행동] {actor.name} ({actor.job}){quest_note}의 행동: {action_text}")
    if check is not None:
        lines.append(f"[판정 결과] {check.prompt_text()}")
    else:
        lines.append("[판정 결과] 판정 없음 (안전한 행동, 실패 없이 진행)")
    lines.append(
        "\n판정 결과에 충실하게 다음 장면을 서술하고, 다음 JSON 형식으로만 답하세요:\n"
        "{\n"
        '  "narration": "다음 장면 서술 (3~7문장)",\n'
        '  "choices": [{"text": "선택지 행동 (25자 이내)", "stat": "힘|민첩|지능|매력|없음", "dc": 12}],\n'
        '  "hp_changes": [{"name": "캐릭터 이름", "change": -3}],\n'
        '  "items_add": ["새로 얻은 아이템"],\n'
        '  "items_remove": ["잃거나 사용한 아이템"],\n'
        + COMBAT_START_SCHEMA_LINE +
        '  "quest_complete": false\n'
        "}\n"
        "규칙: hp_changes 의 change 는 -10~10 사이 정수이며, 이번 장면에서 실제로 피해를 입거나 "
        "회복한 캐릭터만 포함합니다 (없으면 빈 배열). "
        f"items_add/items_remove 는 행동한 캐릭터({actor.name})의 소지품 변화만 적습니다. "
        + COMBAT_START_RULE +
        f"quest_complete 는 이번 행동으로 {actor.name} 의 개인 퀘스트가 최종 달성된 경우에만 true 로 합니다."
    )
    return "\n\n".join(lines)


def _play_world_combat_action(
    adv: WorldAdventure,
    actor_id: str,
    action_text: str,
    check: Optional[CheckResult],
    choice: Optional[dict],
    model: Optional[str],
) -> WorldActionResult:
    """자유 모험 전투 중 행동 한 번: 판정은 코드가 확정하고 LLM은 서술만 한다."""
    combat = adv.combat
    actor_member = adv.members[str(actor_id)]
    actor = actor_member.character
    participants = [member.character for member in adv.members.values()]
    mech_log, deltas, free = _run_combat_mechanics(combat, actor, choice, participants)
    combat_over = combat.over
    players_down = not adv.alive_ids()

    prompt = _build_combat_narration_prompt(
        _world_context_lines(adv),
        combat,
        action_line=f"{actor.name} ({actor.job}): {action_text}",
        check=check if free else None,
        log_lines=mech_log,
        combat_over=combat_over,
        players_down=players_down,
        free_action=free,
    )
    data = _combat_chat(prompt, model=model, system=GM_WORLD_SYSTEM_PROMPT)
    narration = _clean_text(data.get("narration", ""), NARRATION_MAX) or "\n".join(mech_log)

    hp_changes = dict(deltas)
    items_added: List[str] = []
    items_removed: List[str] = []
    if free and data:
        member_chars = {uid: member.character for uid, member in adv.members.items()}
        for uid, delta in _normalize_party_hp_changes(data.get("hp_changes"), member_chars).items():
            applied = member_chars[uid].apply_hp(delta)
            if applied:
                name = member_chars[uid].name
                hp_changes[name] = hp_changes.get(name, 0) + applied
        for item in _normalize_items(data.get("items_add")):
            if len(actor.inventory) >= INVENTORY_MAX:
                break
            if item not in actor.inventory:
                actor.inventory.append(item)
                items_added.append(item)
        for item in _normalize_items(data.get("items_remove")):
            if item in actor.inventory:
                actor.inventory.remove(item)
                items_removed.append(item)

    if combat_over:
        adv.combat = None
        adv.choices = _normalize_choices(data.get("choices"))
    else:
        adv.choices = build_combat_choices(combat)

    result_band = check.band if (check and free) else "전투"
    adv.record_action(actor.name, action_text, result_band, narration)
    adv.scene = narration
    adv.turn += 1
    adv.last_actor_id = str(actor_id)

    return WorldActionResult(
        narration=narration,
        hp_changes=hp_changes,
        items_added=items_added,
        items_removed=items_removed,
        quest_completed=False,
        combat_log=mech_log,
    )


def play_world_action(
    adv: WorldAdventure,
    actor_id: str,
    action_text: str,
    check: Optional[CheckResult],
    *,
    choice: Optional[dict] = None,
    model: Optional[str] = None,
) -> WorldActionResult:
    """자유 모험에서 한 플레이어의 행동을 처리하고 세계 상태를 갱신한다."""
    actor_member = adv.members.get(str(actor_id))
    if actor_member is None:
        raise ValueError("이 세계에 참가하지 않은 플레이어의 행동입니다.")
    actor = actor_member.character

    if adv.combat is not None:
        return _play_world_combat_action(adv, actor_id, action_text, check, choice, model)

    data = _chat_json(
        _build_world_action_prompt(adv, actor_member, action_text, check),
        model=model,
        temperature=TURN_TEMPERATURE,
        system=GM_WORLD_SYSTEM_PROMPT,
    )

    narration = _clean_text(data.get("narration", ""), NARRATION_MAX)
    if not narration:
        raise RuntimeError("GM(AI)이 장면 서술을 생성하지 못했습니다. 다시 시도해주세요.")

    # 프롬프트에 반영된 사건은 여기서 소진시킨다. 이 뒤에 추가되는 사건
    # (퀘스트 완수 등)은 남아서 다음 행동의 프롬프트에 실린다.
    adv.events = []

    member_chars = {uid: member.character for uid, member in adv.members.items()}
    hp_changes: Dict[str, int] = {}
    for uid, delta in _normalize_party_hp_changes(data.get("hp_changes"), member_chars).items():
        applied = member_chars[uid].apply_hp(delta)
        if applied:
            hp_changes[member_chars[uid].name] = applied

    items_added: List[str] = []
    for item in _normalize_items(data.get("items_add")):
        if len(actor.inventory) >= INVENTORY_MAX:
            break
        if item not in actor.inventory:
            actor.inventory.append(item)
            items_added.append(item)

    items_removed: List[str] = []
    for item in _normalize_items(data.get("items_remove")):
        if item in actor.inventory:
            actor.inventory.remove(item)
            items_removed.append(item)

    quest_completed = bool(data.get("quest_complete")) and bool(actor_member.quest)
    if quest_completed:
        actor_member.quests_done += 1
        adv.add_event(f"{actor.name} 이(가) 개인 퀘스트를 완수했다: {actor_member.quest[:60]}")
        actor_member.quest = ""

    result_band = check.band if check else "진행"
    adv.record_action(actor.name, action_text, result_band, narration)
    adv.scene = narration
    adv.turn += 1
    adv.last_actor_id = str(actor_id)

    combat_log: List[str] = []
    combat = _combat_from_llm(data.get("combat_start"))
    if combat is not None:
        adv.combat = combat
        combat_log = _combat_start_log(combat)
        adv.choices = build_combat_choices(combat)
    else:
        adv.choices = _normalize_choices(data.get("choices"))

    return WorldActionResult(
        narration=narration,
        hp_changes=hp_changes,
        items_added=items_added,
        items_removed=items_removed,
        quest_completed=quest_completed,
        combat_log=combat_log,
    )
