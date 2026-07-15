"""자유 모험(공유 세계) 엔진 테스트."""
import random

from GameSystem.TRPGEngine import (
    CombatState,
    Enemy,
    TRPGCharacter,
    WorldAdventure,
    WorldMember,
    generate_world_join,
    generate_world_quest,
    generate_world_scenario,
    play_world_action,
)


def make_world(gm):
    gm({
        "title": "열린 왕국", "world": "누구나 오가는 왕국.",
        "opening": "아렌이 성문에 도착했다.",
        "personal_quest": "사라진 스승의 행방을 찾아라.",
        "choices": [{"text": "성문을 살핀다", "stat": "지능", "dc": 11}],
    })
    char = TRPGCharacter.create("아렌", "mage", race="엘프", background="추방된 연구자")
    return generate_world_scenario("fantasy", "10", char)


def test_세계_생성과_개인퀘스트(gm):
    adv = make_world(gm)
    assert adv.owner_id == "10"
    assert adv.members["10"].quest.startswith("사라진")
    assert adv.members["10"].character.race == "엘프"


def test_합류_서술과_퀘스트(gm):
    adv = make_world(gm)
    gm({"arrival": "숲에서 도적 카일이 나타났다.", "personal_quest": "가보를 되찾아라."})
    join = generate_world_join(adv, TRPGCharacter.create("카일", "rogue"))
    assert "카일" in join["arrival"] and join["quest"]


def test_행동_퀘스트완수_타인HP_아이템(gm):
    adv = make_world(gm)
    adv.members["20"] = WorldMember(character=TRPGCharacter.create("카일", "rogue"), quest="가보 찾기")
    gm({
        "narration": "아렌이 스승의 단서를 찾아냈다.",
        "choices": [{"text": "쉬어간다", "stat": "없음", "dc": 10}],
        "hp_changes": [{"name": "카일", "change": -3}],
        "items_add": ["스승의 일기"],
        "quest_complete": True,
    })
    result = play_world_action(adv, "10", "단서를 조사한다", None)

    assert result.quest_completed
    assert adv.members["10"].quests_done == 1
    assert adv.members["10"].quest == "", "완수 후 다음 퀘스트 대기 상태"
    assert adv.members["20"].character.hp == 20 - 3
    assert "스승의 일기" in adv.members["10"].character.inventory
    assert adv.events, "퀘스트 완수 이벤트가 다음 프롬프트용으로 남아야 함"
    assert adv.last_actor_id == "10"


def test_다음_개인퀘스트_발급(gm):
    adv = make_world(gm)
    gm({"personal_quest": "왕궁의 음모를 파헤쳐라."})
    quest = generate_world_quest(adv, adv.members["10"].character)
    assert quest.startswith("왕궁")


def test_전투_개시와_종료(gm):
    adv = make_world(gm)
    gm({
        "narration": "늑대 무리가 덮쳐왔다!", "choices": [],
        "combat_start": {"enemies": [{"name": "늑대", "hp": 6, "ac": 9, "attack": 1, "damage": "1d4"}]},
        "quest_complete": False,
    })
    result = play_world_action(adv, "10", "숲을 수색한다", None)
    assert adv.combat is not None and result.combat_log

    gm({"narration": "전투가 이어진다.", "choices": [{"text": "쉰다", "stat": "없음", "dc": 10}]})
    random.seed(11)
    turns = 0
    while adv.combat is not None and turns < 40:
        attack = next(c for c in adv.choices if c.get("combat") == "attack")
        play_world_action(adv, "10", attack["text"], None, choice=attack)
        turns += 1
    assert adv.combat is None


def test_직렬화_왕복(gm):
    adv = make_world(gm)
    adv.members["10"].quests_done = 3
    adv.combat = CombatState(enemies=[Enemy(name="유령", hp=10, max_hp=12, ac=11, attack=2, damage="1d6+1")])
    restored = WorldAdventure.from_dict(adv.to_dict())
    assert restored.owner_id == "10"
    assert restored.members["10"].quests_done == 3
    assert restored.combat is not None and restored.combat.enemies[0].name == "유령"
