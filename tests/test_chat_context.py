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

    def fake_request(model, messages, extra_system="", turn_context=""):
        captured["extra_system"] = extra_system
        captured["turn_context"] = turn_context
        captured["messages"] = messages
        return "테스트 응답"

    cog._request_chat_korean = fake_request
    reply = cog._call_local_ai_sync(
        "질문", "유저", history=[], summary="지난 요약",
        extra_contexts=["[A블록]", "", "[B블록]"],
    )
    assert reply == "테스트 응답"
    # 요약은 system, 매 턴 바뀌는 참고 자료는 turn_context 로 분리 (프롬프트 캐시 보존)
    assert "지난 요약" in captured["extra_system"]
    assert "[A블록]" not in captured["extra_system"]
    assert "[A블록]" in captured["turn_context"] and "[B블록]" in captured["turn_context"]
    assert captured["messages"][-1]["content"] == "(유저) 질문"

    # 요약·컨텍스트가 없으면 둘 다 빈 문자열
    cog._call_local_ai_sync("질문2", "유저")
    assert captured["extra_system"] == ""
    assert captured["turn_context"] == ""


def test_턴_컨텍스트는_마지막_사용자_메시지에만_붙는다():
    history = [
        {"role": "user", "content": "(유저) 이전 질문"},
        {"role": "assistant", "content": "이전 답"},
        {"role": "user", "content": "(유저) 새 질문"},
    ]
    result = ChatAI._attach_turn_context(history, "[웹 검색 결과]")
    # 앞선 히스토리는 그대로여야 캐시 재사용 구간이 유지된다
    assert result[:2] == history[:2]
    assert result[-1]["content"].startswith("[웹 검색 결과]")
    assert result[-1]["content"].endswith("(유저) 새 질문")
    # 원본 리스트는 변경하지 않는다 (세션 히스토리 오염 방지)
    assert history[-1]["content"] == "(유저) 새 질문"
    # 컨텍스트가 없으면 그대로
    assert ChatAI._attach_turn_context(history, "") == history


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


def test_요약은_락_밖에서_진행되고_새_메시지를_보존한다():
    import asyncio
    import cogs.chat_ai as chat_ai_module
    from cogs.chat_ai import SESSION_COMPACT_TRIGGER, SESSION_HISTORY_MAX_TURNS, _ChatSession

    async def scenario():
        cog = ChatAI(None)
        release = asyncio.Event()

        async def fake_run_llm(func, old_summary, overflow):
            await release.wait()
            return f"요약({len(overflow)})"

        original = chat_ai_module.run_llm
        chat_ai_module.run_llm = fake_run_llm
        try:
            session = _ChatSession(use_tts=False)
            for i in range(SESSION_COMPACT_TRIGGER + 2):
                session.history.append({"role": "user", "content": f"m{i}"})
            before = len(session.history)

            await cog._maybe_compact_history(session)
            assert session.compacting
            # 요약 대기 중에도 세션 락은 비어 있어 다음 메시지를 바로 처리할 수 있다
            assert not session.lock.locked()
            # 중복 요약은 시작하지 않는다
            await cog._maybe_compact_history(session)
            assert len(cog._background_tasks) == 1

            # 요약 도중 도착한 새 메시지
            session.add_user("새 메시지")
            release.set()
            await asyncio.gather(*cog._background_tasks)
        finally:
            chat_ai_module.run_llm = original

        keep = SESSION_HISTORY_MAX_TURNS * 2
        assert not session.compacting
        assert session.summary == f"요약({before - keep})"
        assert len(session.history) == keep + 1
        assert session.history[-1]["content"] == "새 메시지"

    asyncio.run(scenario())
