"""파티(멀티플레이어) 모험 엔진 테스트."""
import GameSystem.TRPGEngine as eng
from GameSystem.TRPGEngine import (
    PartyAdventure,
    TRPGCharacter,
    _normalize_party_hp_changes,
    generate_party_scenario,
    play_party_turn,
    roll_check,
)


def make_members():
    return {
        "111": TRPGCharacter.create("알파", "warrior"),
        "222": TRPGCharacter.create("베타", "mage"),
        "333": TRPGCharacter.create("감마", "rogue"),
    }


def make_party(gm, scenario_reply):
    gm(scenario_reply)
    return generate_party_scenario("fantasy", "111", make_members())


def test_시나리오_생성(gm, scenario_reply):
    adv = make_party(gm, scenario_reply)
    assert adv.turn_order == ["111", "222", "333"]
    assert adv.current_actor_id == "111"
    assert adv.host_id == "111"


def test_턴진행_HP이름매칭_아이템_턴순환(gm, scenario_reply):
    adv = make_party(gm, scenario_reply)
    gm({
        "narration": "함정이 발동해 알파가 다쳤고 감마도 스쳤다.",
        "choices": [{"text": "후퇴한다", "stat": "민첩", "dc": 12}],
        "hp_changes": [
            {"name": "알파", "change": -8},
            {"name": "도적 감마", "change": -2},   # 부분 일치 매칭
            {"name": "없는사람", "change": -5},    # 무시
        ],
        "items_add": ["이상한 열쇠"],
        "game_over": False, "victory": False,
    })
    check = roll_check(adv.members["111"], "힘", 12)
    result = play_party_turn(adv, "111", "함정을 조사한다", check)

    assert adv.members["111"].hp == 26 - 8
    assert adv.members["333"].hp == 20 - 2
    assert adv.members["222"].hp == 18
    assert result.hp_changes == {"알파": -8, "감마": -2}
    assert "이상한 열쇠" in adv.members["111"].inventory
    assert "이상한 열쇠" not in adv.members["222"].inventory
    assert adv.current_actor_id == "222", "턴이 다음 멤버로 넘어가야 함"


def test_쓰러진_멤버_턴_스킵(gm, scenario_reply):
    adv = make_party(gm, scenario_reply)
    adv.members["333"].hp = 0
    adv.current_idx = 1          # 베타 턴
    adv.advance_turn()
    assert adv.current_actor_id == "111", "쓰러진 감마를 건너뛰어야 함"
    adv.members["333"].hp = 5
    adv.advance_turn()
    assert adv.current_actor_id == "222"


def test_전멸하면_dead(gm, scenario_reply):
    adv = make_party(gm, scenario_reply)
    for uid in adv.members:
        adv.members[uid].hp = 1
    gm({
        "narration": "폭발이 전원을 덮쳤다.",
        "choices": [],
        "hp_changes": [{"name": n, "change": -10} for n in ("알파", "베타", "감마")],
        "game_over": False, "victory": False,
    })
    result = play_party_turn(adv, adv.current_actor_id, "폭탄을 건드린다", None)
    assert adv.status == "dead"
    assert result.ended and not result.victory


def test_승리(gm, scenario_reply):
    adv = make_party(gm, scenario_reply)
    gm({"narration": "퀘스트 완수!", "choices": [], "hp_changes": [], "victory": True, "game_over": False})
    result = play_party_turn(adv, "111", "봉인을 해제한다", None)
    assert adv.status == "victory" and result.victory


def test_직렬화_왕복과_손상세이브_복구(gm, scenario_reply):
    adv = make_party(gm, scenario_reply)
    adv.members["222"].hp = 0
    adv.current_idx = 1
    data = adv.to_dict()

    restored = PartyAdventure.from_dict(data)
    assert restored.turn_order == adv.turn_order
    assert restored.members["222"].hp == 0
    restored.ensure_current_alive()
    assert restored.current_actor_id != "222"

    broken = dict(data)
    broken["turn_order"] = ["111", "999", "222", "333"]   # 없는 멤버 포함
    broken["current_idx"] = 99
    fixed = PartyAdventure.from_dict(broken)
    assert "999" not in fixed.turn_order
    assert 0 <= fixed.current_idx < len(fixed.turn_order)


def test_hp변화_정규화_상한():
    members = make_members()
    changes = _normalize_party_hp_changes(
        [{"name": "알파", "change": -99}, {"name": "알파", "change": -99}], members
    )
    assert changes == {"111": -eng.HP_CHANGE_LIMIT}
    assert _normalize_party_hp_changes("잘못된타입", members) == {}
    assert _normalize_party_hp_changes([{"name": 123, "change": -5}], members) == {}
