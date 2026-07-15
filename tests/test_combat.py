"""전투 시스템 테스트 — 적 정규화, 선택지, 기계 판정, 솔로 전투 흐름."""
import random

import GameSystem.TRPGEngine as eng
from GameSystem.TRPGEngine import (
    CombatState,
    Enemy,
    TRPGCharacter,
    _combat_from_llm,
    _run_combat_mechanics,
    build_combat_choices,
    generate_scenario,
    play_turn,
)


def test_combat_start_정규화와_상한():
    combat = _combat_from_llm({"enemies": [
        {"name": "고블린", "hp": 999, "ac": 99, "attack": 99, "damage": "9d99+99"},
        {"name": "늑대", "hp": 8, "ac": 13, "attack": 2, "damage": "1d6"},
        {"name": "", "hp": 5},      # 이름 없음 → 무시
        "잘못된항목",                 # dict 아님 → 무시
    ]})
    assert combat is not None and len(combat.enemies) == 2
    gob = combat.enemies[0]
    assert gob.hp <= eng.ENEMY_HP_LIMIT
    assert eng.ENEMY_AC_MIN <= gob.ac <= eng.ENEMY_AC_MAX
    assert gob.attack <= eng.ENEMY_ATTACK_LIMIT
    assert gob.damage == "2d12+5"   # 주사위 상한으로 잘림


def test_combat_start_부적합_입력은_None():
    assert _combat_from_llm(None) is None
    assert _combat_from_llm({"enemies": []}) is None
    assert _combat_from_llm("문자열") is None


def test_전투_선택지는_공격과_방어():
    combat = CombatState(enemies=[
        Enemy(name="A", hp=5, max_hp=5, ac=10, attack=1, damage="1d4"),
        Enemy(name="B", hp=0, max_hp=5, ac=10, attack=1, damage="1d4"),   # 사망 → 제외
    ])
    choices = build_combat_choices(combat)
    attack_targets = [c["target"] for c in choices if c.get("combat") == "attack"]
    assert attack_targets == [0]
    assert choices[-1]["combat"] == "defend"


def test_전투_기계판정은_언젠가_끝난다():
    warrior = TRPGCharacter.create("전사", "warrior")
    combat = CombatState(enemies=[Enemy(name="슬라임", hp=5, max_hp=5, ac=8, attack=1, damage="1d4")])
    for _ in range(60):
        if combat.over or warrior.hp <= 0:
            break
        log, deltas, free = _run_combat_mechanics(
            combat, warrior, {"combat": "attack", "target": 0}, [warrior]
        )
        assert not free
    assert combat.over or warrior.hp <= 0


def test_방어_태세():
    warrior = TRPGCharacter.create("전사", "warrior")
    combat = CombatState(enemies=[Enemy(name="오크", hp=20, max_hp=20, ac=12, attack=3, damage="1d6")])
    log, deltas, free = _run_combat_mechanics(combat, warrior, {"combat": "defend"}, [warrior])
    assert any("방어 태세" in line for line in log)
    assert free is False


def test_솔로_전투_개시와_종료(gm, scenario_reply):
    gm(scenario_reply)
    adv = generate_scenario("fantasy", TRPGCharacter.create("솔로", "warrior"))

    gm({
        "narration": "고블린이 튀어나왔다!",
        "choices": [],
        "hp_change": 0,
        "combat_start": {"enemies": [{"name": "고블린", "hp": 6, "ac": 8, "attack": 1, "damage": "1d4"}]},
        "game_over": False, "victory": False,
    })
    result = play_turn(adv, "동굴에 들어간다", None)
    assert adv.combat is not None
    assert result.combat_log, "전투 시작 로그가 있어야 함"
    assert any(c.get("combat") == "attack" for c in adv.choices)

    # 전투 서술 목킹 (LLM 응답에 narration 만 있으면 됨)
    gm({"narration": "전투가 이어진다.", "choices": [{"text": "쉰다", "stat": "없음", "dc": 10}]})
    random.seed(7)
    turns = 0
    while adv.combat is not None and adv.is_playing and turns < 40:
        attack = next(c for c in adv.choices if c.get("combat") == "attack")
        play_turn(adv, attack["text"], None, choice=attack)
        turns += 1
    assert adv.combat is None or not adv.is_playing


def test_전투상태_직렬화_왕복():
    combat = CombatState(
        enemies=[Enemy(name="유령", hp=10, max_hp=12, ac=11, attack=2, damage="1d6+1")],
        attacker_idx=0,
    )
    restored = CombatState.from_dict(combat.to_dict())
    assert restored.enemies[0].name == "유령"
    assert restored.enemies[0].hp == 10 and restored.enemies[0].max_hp == 12
