"""유틸리티 테스트 — 웹 검색 파서(오프라인 HTML 픽스처), 지식 검색, 한국어 정제."""
import pytest

import utils.knowledge_utils as ku
from utils.llm_utils import extract_json_object, strip_non_korean
from utils.web_search import _parse_html_results, _parse_lite_results, _unwrap_ddg_url

HTML_PAGE = """
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">예시 제목</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">예시 <b>요약</b> 텍스트</a>
  </div>
</div>
<div class="result result--ad">
  <a rel="nofollow" class="result__a" href="https://duckduckgo.com/y.js?ad_domain=ad.com">광고 제목</a>
</div>
"""

LITE_PAGE = """
<tr>
  <td><a rel="nofollow" href="https://example.org/doc" class='result-link'>라이트 제목</a></td>
</tr>
<tr>
  <td class='result-snippet'>라이트 요약입니다.</td>
</tr>
"""


def test_html_파서는_광고를_거르고_리다이렉트를_푼다():
    results = _parse_html_results(HTML_PAGE, max_results=5)
    assert len(results) == 1
    assert results[0]["title"] == "예시 제목"
    assert results[0]["url"] == "https://example.com/page"
    assert "요약" in results[0]["snippet"]


def test_lite_파서():
    results = _parse_lite_results(LITE_PAGE, max_results=5)
    assert len(results) == 1
    assert results[0]["title"] == "라이트 제목"
    assert results[0]["url"] == "https://example.org/doc"
    assert results[0]["snippet"] == "라이트 요약입니다."


def test_ddg_리다이렉트_해제():
    assert _unwrap_ddg_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fx") == "https://a.com/x"
    assert _unwrap_ddg_url("https://direct.com/page") == "https://direct.com/page"


def test_비한국어_문자_제거는_URL을_보존():
    text = "안녕하세요 ね 세계 https://example.com/日本 좋아요 é"
    cleaned = strip_non_korean(text)
    assert "ね" not in cleaned and "é" not in cleaned
    assert "https://example.com/日本" in cleaned    # URL 내부는 보존


def test_json_추출은_코드펜스와_잡음을_복구():
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('응답: {"b": 2} 입니다') == {"b": 2}
    with pytest.raises(ValueError):
        extract_json_object("JSON 없음")


@pytest.fixture
def knowledge_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ku, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(ku, "_cache_signature", None)
    monkeypatch.setattr(ku, "_cache_chunks", [])
    return tmp_path


def test_지식검색_섹션단위_청크와_점수(knowledge_dir):
    (knowledge_dir / "규칙.md").write_text(
        "# 서버 규칙\n\n1. 서로 존중한다.\n2. 음악은 재생 명령어로 신청한다.\n\n"
        "# 게임 개발부\n\n부원은 유즈, 모모이, 미도리, 아리스 네 명이다.\n",
        encoding="utf-8",
    )
    stats = ku.knowledge_stats()
    assert stats["chunks"] == 2, "헤더 단위로 청크가 나뉘어야 함"

    hits = ku.search_knowledge("서버 규칙 알려줘")
    assert hits and "존중" in hits[0]["text"]
    assert ku.search_knowledge("완전히무관한외계어쿼리zzz") == []
    assert ku.search_knowledge("") == []
