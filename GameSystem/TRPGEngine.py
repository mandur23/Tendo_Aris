"""AI 생성형 TRPG 게임 엔진 (Discord 비의존)

게임 상태(캐릭터 시트·HP·인벤토리·진행 기록)는 코드가 관리하고,
로컬 LLM(Ollama)은 장면 서술과 선택지 생성만 담당한다.
주사위 판정도 코드에서 굴려 결과만 LLM에 전달하므로 규칙 일관성이 유지된다.

모든 LLM 호출 함수는 동기(blocking)이며, 호출부에서 asyncio.to_thread 로 감싸야 한다.
"""
import logging
import random
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

    @classmethod
    def create(cls, name: str, class_key: str) -> "TRPGCharacter":
        spec = CLASSES[class_key]
        return cls(
            name=name,
            job=spec["label"],
            job_emoji=spec["emoji"],
            stats=dict(spec["stats"]),
            hp=spec["hp"],
            max_hp=spec["hp"],
            inventory=list(spec["items"]),
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
        return (
            f"이름: {self.name} / 직업: {self.job} / HP: {self.hp}/{self.max_hp}\n"
            f"능력치: {self.stats_line()}\n"
            f"소지품: {inv}"
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "job": self.job,
            "job_emoji": self.job_emoji,
            "stats": dict(self.stats),
            "hp": self.hp,
            "max_hp": self.max_hp,
            "inventory": list(self.inventory),
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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TRPGAdventure":
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
def _build_turn_prompt(adv: TRPGAdventure, action_text: str, check: Optional[CheckResult]) -> str:
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
        '  "game_over": false,\n'
        '  "victory": false\n'
        "}\n"
        "규칙: hp_change는 -10~10 사이 정수로, 피해를 입으면 음수, 회복하면 양수, 그 외에는 0. "
        "items_add/items_remove는 실제 변화가 있을 때만 채웁니다. "
        "victory는 퀘스트를 최종 달성한 경우에만 true, game_over는 죽음 등으로 모험이 끝난 경우에만 true로 합니다."
    )
    return "\n\n".join(lines)


def play_turn(
    adv: TRPGAdventure,
    action_text: str,
    check: Optional[CheckResult],
    *,
    model: Optional[str] = None,
) -> TurnResult:
    """플레이어 행동 한 턴을 처리하고 어드벤처 상태를 갱신한다."""
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
    adv.choices = _normalize_choices(data.get("choices"))
    adv.turn += 1

    return TurnResult(
        narration=narration,
        hp_change=hp_change,
        items_added=items_added,
        items_removed=items_removed,
        ended=not adv.is_playing,
        victory=adv.status == "victory",
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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PartyAdventure":
        members = {
            str(uid): TRPGCharacter.from_dict(char_data)
            for uid, char_data in data.get("members", {}).items()
        }
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


def _build_party_turn_prompt(
    adv: PartyAdventure,
    actor: TRPGCharacter,
    action_text: str,
    check: Optional[CheckResult],
) -> str:
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
        '  "game_over": false,\n'
        '  "victory": false\n'
        "}\n"
        "규칙: hp_changes 의 change 는 -10~10 사이 정수이며, 이번 장면에서 실제로 피해를 입거나 "
        "회복한 캐릭터만 포함합니다 (없으면 빈 배열). "
        f"items_add/items_remove 는 행동한 캐릭터({actor.name})의 소지품 변화만 적습니다. "
        "victory 는 파티가 퀘스트를 최종 달성한 경우에만 true, "
        "game_over 는 파티가 전멸하는 등 모험이 끝난 경우에만 true 로 합니다."
    )
    return "\n\n".join(lines)


def play_party_turn(
    adv: PartyAdventure,
    actor_id: str,
    action_text: str,
    check: Optional[CheckResult],
    *,
    model: Optional[str] = None,
) -> PartyTurnResult:
    """파티 멤버 한 명의 행동 턴을 처리하고 어드벤처 상태를 갱신한다."""
    actor = adv.members.get(str(actor_id))
    if actor is None:
        raise ValueError("파티에 없는 플레이어의 행동입니다.")

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
    adv.choices = _normalize_choices(data.get("choices"))
    adv.turn += 1
    if adv.is_playing:
        adv.advance_turn()

    return PartyTurnResult(
        narration=narration,
        hp_changes=hp_changes,
        items_added=items_added,
        items_removed=items_removed,
        ended=not adv.is_playing,
        victory=adv.status == "victory",
    )
