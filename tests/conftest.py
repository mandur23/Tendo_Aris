"""pytest 공용 픽스처.

프로젝트 루트를 sys.path 에 추가하고, LLM(Ollama) 호출을 목킹하는
`gm` 픽스처와 재현 가능한 랜덤 시드를 제공한다.

실행: .venv/Scripts/python -m pytest tests -q
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import GameSystem.TRPGEngine as eng  # noqa: E402


@pytest.fixture(autouse=True)
def _seed():
    """모든 테스트에서 주사위 굴림이 재현 가능하도록 시드를 고정한다."""
    random.seed(1234)


@pytest.fixture
def gm(monkeypatch):
    """엔진의 LLM 호출(_chat_json)을 지정한 응답으로 목킹한다.

    사용: gm({"narration": "...", ...}) — 이후 엔진 호출은 이 dict 를 받는다.
    """
    def set_reply(reply: dict):
        monkeypatch.setattr(eng, "_chat_json", lambda content, **kw: dict(reply))
    return set_reply


SCENARIO_REPLY = {
    "title": "테스트 모험",
    "world": "테스트 세계관.",
    "quest": "테스트 퀘스트.",
    "opening": "모험이 시작됐다.",
    "personal_quest": "개인 퀘스트다.",
    "choices": [{"text": "전진한다", "stat": "없음", "dc": 10}],
}


@pytest.fixture
def scenario_reply():
    """시나리오 생성용 표준 목킹 응답."""
    return dict(SCENARIO_REPLY)
