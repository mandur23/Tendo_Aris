"""자유 행동의 판정 능력치 결정 테스트 (키워드 → LLM → 운명 판정 폴백)."""
import pytest

import GameSystem.TRPGEngine as eng
from GameSystem.TRPGEngine import (
    DEFAULT_DC,
    FREE_ACTION_DC,
    TRPGCharacter,
    _keyword_stat,
    judge_free_action,
)


@pytest.mark.parametrize("action,expected", [
    ("자물쇠를 조심스럽게 딴다", "민첩"),
    ("몰래 뒤로 다가간다", "민첩"),
    ("문을 힘껏 부순다", "힘"),
    ("바위를 밀어 옮긴다", "힘"),
    ("벽의 문양을 자세히 관찰한다", "지능"),
    ("고대 문자를 해독한다", "지능"),
    ("상인에게 소문을 캐묻는다", "매력"),
    ("경비병을 설득해 통과한다", "매력"),
])
def test_키워드로_능력치를_고른다(action, expected):
    assert _keyword_stat(action) == expected


def test_키워드가_없으면_None():
    assert _keyword_stat("음") is None
    assert _keyword_stat("") is None


def test_키워드_매칭이면_LLM을_호출하지_않는다(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("키워드로 판단 가능하면 LLM을 부르면 안 됨")

    monkeypatch.setattr(eng, "ollama_chat_sync", boom)
    stat, dc = judge_free_action("자물쇠를 딴다", TRPGCharacter.create("도적", "rogue"))
    assert stat == "민첩"
    assert dc == DEFAULT_DC


def test_애매하면_LLM_판단을_사용(monkeypatch):
    monkeypatch.setattr(
        eng, "ollama_chat_sync",
        lambda *a, **kw: '{"stat": "지능", "dc": 15}',
    )
    stat, dc = judge_free_action("이 상황을 어떻게든 해본다")
    assert stat == "지능" and dc == 15


def test_LLM이_없음이라고_하면_운명판정(monkeypatch):
    monkeypatch.setattr(eng, "ollama_chat_sync", lambda *a, **kw: '{"stat": "없음", "dc": 10}')
    stat, dc = judge_free_action("가만히 서 있는다")
    assert stat is None
    assert dc <= FREE_ACTION_DC


def test_LLM_실패해도_운명판정으로_진행(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("Ollama 연결 실패")

    monkeypatch.setattr(eng, "ollama_chat_sync", boom)
    stat, dc = judge_free_action("이 상황을 어떻게든 해본다")
    assert stat is None and dc == FREE_ACTION_DC


def test_LLM_난이도는_8에서_18로_보정(monkeypatch):
    monkeypatch.setattr(eng, "ollama_chat_sync", lambda *a, **kw: '{"stat": "힘", "dc": 99}')
    assert judge_free_action("무언가를 시도한다")[1] == 18

    monkeypatch.setattr(eng, "ollama_chat_sync", lambda *a, **kw: '{"stat": "힘", "dc": -5}')
    assert judge_free_action("무언가를 시도한다")[1] == 8

    monkeypatch.setattr(eng, "ollama_chat_sync", lambda *a, **kw: '{"stat": "힘", "dc": "이상한값"}')
    assert judge_free_action("무언가를 시도한다")[1] == DEFAULT_DC


def test_use_llm_False면_호출하지_않는다(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("use_llm=False 면 호출하면 안 됨")

    monkeypatch.setattr(eng, "ollama_chat_sync", boom)
    assert judge_free_action("이 상황을 어떻게든 해본다", use_llm=False) == (None, FREE_ACTION_DC)


def test_판정에_캐릭터_능력치_보정이_실제로_반영된다():
    """도적(민첩+3)이 자물쇠를 따면 민첩 보정을 받아야 한다."""
    from GameSystem.TRPGEngine import roll_check

    rogue = TRPGCharacter.create("도적", "rogue")
    stat, dc = judge_free_action("자물쇠를 딴다", rogue)
    check = roll_check(rogue, stat, dc)
    assert check.stat == "민첩"
    assert check.mod == 3
    assert "민첩 판정" in check.display
