"""chat_ai(아리스 대화)의 TRPG 컨텍스트 빌더·시스템 프롬프트 조합 테스트."""
from cogs.chat_ai import ChatAI
from GameSystem.TRPGEngine import (
    CombatState,
    Enemy,
    TRPGAdventure,
    TRPGCharacter,
    WorldMember,
    generate_party_scenario,
    generate_world_scenario,
)


def test_extra_system_조합():
    cog = ChatAI(None)
    captured = {}

    def fake_request(model, messages, extra_system=""):
        captured["extra_system"] = extra_system
        captured["messages"] = messages
        return "테스트 응답"

    cog._request_chat_korean = fake_request
    reply = cog._call_local_ai_sync(
        "질문", "유저", history=[], summary="지난 요약",
        extra_contexts=["[A블록]", "", "[B블록]"],
    )
    assert reply == "테스트 응답"
    assert "지난 요약" in captured["extra_system"]
    assert "[A블록]" in captured["extra_system"] and "[B블록]" in captured["extra_system"]
    assert captured["messages"][-1]["content"] == "(유저) 질문"

    # 요약·컨텍스트가 없으면 extra_system 은 빈 문자열
    cog._call_local_ai_sync("질문2", "유저")
    assert captured["extra_system"] == ""


def test_솔로_컨텍스트_블록():
    adv = TRPGAdventure.from_dict({
        "genre_key": "fantasy", "title": "잊혀진 왕국", "world": "왕국 아르텐.",
        "quest": "성검을 되찾아라.", "scene": "유적 입구다.", "turn": 5,
        "log": ["상인에게 정보를 얻었다 → 성공"], "recent": [], "status": "playing",
        "character": TRPGCharacter.create("용사", "warrior").to_dict(),
    })
    block = ChatAI._build_trpg_context(adv)
    assert "잊혀진 왕국" in block and "아르텐" in block and "성검" in block and "용사" in block


def test_파티_컨텍스트_블록(gm, scenario_reply):
    gm(scenario_reply)
    adv = generate_party_scenario("fantasy", "1", {
        "1": TRPGCharacter.create("알파", "warrior"),
        "2": TRPGCharacter.create("베타", "mage"),
    })
    adv.members["2"].hp = 0
    block = ChatAI._build_party_trpg_context(adv, "2")
    assert "파티 2명" in block
    assert "← 선생님의 캐릭터" in block and "(쓰러짐)" in block
    assert "현재 턴: 알파" in block


def test_자유모험_컨텍스트_블록(gm):
    gm({
        "title": "열린 왕국", "world": "누구나 오가는 왕국.", "opening": "아렌이 도착했다.",
        "personal_quest": "스승을 찾아라.", "choices": [{"text": "x", "stat": "없음", "dc": 10}],
    })
    adv = generate_world_scenario(
        "fantasy", "10",
        TRPGCharacter.create("아렌", "mage", race="엘프", background="추방된 연구자"),
    )
    adv.members["20"] = WorldMember(
        character=TRPGCharacter.create("카일", "rogue"), quest="가보를 되찾아라.", quests_done=2,
    )
    adv.members["20"].character.hp = 0
    adv.combat = CombatState(enemies=[Enemy(name="늑대", hp=4, max_hp=6, ac=9, attack=1, damage="1d4")])

    block = ChatAI._build_world_trpg_context(adv, "20")
    assert "열린 왕국" in block
    assert "← 선생님의 캐릭터" in block and "(쓰러짐)" in block
    assert "가보를 되찾아라" in block and "완수 2개" in block
    assert "늑대 HP 4/6" in block and "엘프" in block
