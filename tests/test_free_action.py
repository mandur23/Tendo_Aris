"""자유 행동의 판정 능력치 결정 테스트 (키워드 → LLM → 운명 판정 폴백)."""
import pytest

import GameSystem.TRPGEngine as eng
from GameSystem.TRPGEngine import (
    DC_MAX,
    DC_MIN,
    DEFAULT_DC,
    FREE_ACTION_DC,
    TRPGCharacter,
    _keyword_dc,
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


def test_난이도_단서가_없으면_기본_난이도():
    assert _keyword_dc("자물쇠를 딴다") == DEFAULT_DC
    assert _keyword_dc("") == DEFAULT_DC


@pytest.mark.parametrize("action", [
    "낡은 자물쇠를 딴다",
    "작은 상자를 살짝 연다",
    "가벼운 나뭇가지를 치운다",
])
def test_쉬운_단서면_난이도가_내려간다(action):
    assert _keyword_dc(action) < DEFAULT_DC


@pytest.mark.parametrize("action", [
    "굳게 잠긴 문을 부순다",
    "거대한 바위를 밀어 옮긴다",
    "삼엄한 경비를 뚫고 몰래 잠입한다",
])
def test_어려운_단서면_난이도가_올라간다(action):
    assert _keyword_dc(action) > DEFAULT_DC


def test_반대_단서는_서로_상쇄된다():
    # 거대한(+3) + 천천히(-2) = +1
    assert _keyword_dc("거대한 바위를 천천히 밀어본다") == DEFAULT_DC + 1


def test_같은_등급_단서가_여러개여도_한번만_반영():
    once = _keyword_dc("거대한 바위를 부순다")
    twice = _keyword_dc("거대한 성문과 거대한 바위를 부순다")
    assert once == twice


def test_난이도는_항상_허용_범위_안():
    아주쉬움 = _keyword_dc("낡고 부서진 작은 자물쇠를 천천히 살짝 딴다")
    아주어려움 = _keyword_dc("삼엄한 경비 속 거대한 강철 성문을 단숨에 필사적으로 부순다")
    assert DC_MIN <= 아주쉬움 <= DC_MAX
    assert DC_MIN <= 아주어려움 <= DC_MAX
    assert 아주쉬움 < DEFAULT_DC < 아주어려움


def test_키워드_판정에도_난이도가_반영된다(monkeypatch):
    """키워드로 능력치를 찾은 경우에도 LLM 없이 난이도가 달라져야 한다."""
    def boom(*a, **kw):
        raise AssertionError("키워드로 판단 가능하면 LLM을 부르면 안 됨")

    monkeypatch.setattr(eng, "ollama_chat_sync", boom)

    쉬움_stat, 쉬움_dc = judge_free_action("낡은 자물쇠를 살짝 딴다")
    어려움_stat, 어려움_dc = judge_free_action("굳게 잠긴 강철문을 단숨에 부순다")

    assert 쉬움_stat == "민첩" and 어려움_stat == "힘"
    assert 쉬움_dc < DEFAULT_DC < 어려움_dc


def test_판정에_캐릭터_능력치_보정이_실제로_반영된다():
    """도적(민첩+3)이 자물쇠를 따면 민첩 보정을 받아야 한다."""
    from GameSystem.TRPGEngine import roll_check

    rogue = TRPGCharacter.create("도적", "rogue")
    stat, dc = judge_free_action("자물쇠를 딴다", rogue)
    check = roll_check(rogue, stat, dc)
    assert check.stat == "민첩"
    assert check.mod == 3
    assert "민첩 판정" in check.display
