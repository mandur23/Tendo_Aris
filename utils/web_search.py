"""키 없는 웹 검색 유틸리티 (DuckDuckGo HTML)

외부 의존성 없이 urllib 만으로 DuckDuckGo 검색 결과(제목/URL/요약)를 가져온다.
로컬 LLM 대화에서 최신 정보가 필요할 때 검색 결과를 프롬프트 컨텍스트로
주입(RAG)하는 용도로 사용한다.

html.duckduckgo.com 을 먼저 시도하고, 실패하면 lite.duckduckgo.com 으로
폴백한다. 모든 함수는 동기(blocking)이며, 호출부에서 asyncio.to_thread 로
감싸야 한다.
"""
import html as html_module
import logging
import re
from typing import Dict, List
from urllib import error, parse, request

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")

# html.duckduckgo.com: 결과마다 <div class="result ..."> 블록 안에
# <a class="result__a" href="...">제목</a> 과 <a class="result__snippet" ...>요약</a> 이 있다.
# result__body 등 내부 div 에서 끊기지 않도록 'result' 뒤에 단어 경계를 요구한다.
_HTML_BLOCK_SPLIT_RE = re.compile(r'<div class="result\b')
_HTML_LINK_RE = re.compile(
    r'<a\b(?=[^>]*result__a)[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    flags=re.DOTALL | re.IGNORECASE,
)
_HTML_SNIPPET_RE = re.compile(
    r"<(?P<tag>a|div|span)\b(?=[^>]*result__snippet)[^>]*>(?P<snippet>.*?)</(?P=tag)>",
    flags=re.DOTALL | re.IGNORECASE,
)

# lite.duckduckgo.com: <a rel="nofollow" href="..." class='result-link'>제목</a> 다음
# 행에 <td class='result-snippet'>요약</td> 이 온다.
_LITE_LINK_RE = re.compile(
    r"<a\b(?=[^>]*class=['\"]?result-link)[^>]*href=['\"](?P<href>[^'\"]+)['\"][^>]*>(?P<title>.*?)</a>",
    flags=re.DOTALL | re.IGNORECASE,
)
_LITE_SNIPPET_RE = re.compile(
    r"<td\b(?=[^>]*result-snippet)[^>]*>(?P<snippet>.*?)</td>",
    flags=re.DOTALL | re.IGNORECASE,
)


def _clean_html_text(fragment: str) -> str:
    """태그 제거 + HTML 엔티티 해제 + 공백 정리."""
    text = _TAG_RE.sub("", fragment)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _unwrap_ddg_url(href: str) -> str:
    """DuckDuckGo 리다이렉트 링크(//duckduckgo.com/l/?uddg=...)에서 실제 URL을 꺼낸다."""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = parse.urlparse(href)
        if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            uddg = parse.parse_qs(parsed.query).get("uddg", [""])[0]
            if uddg:
                return uddg
    except ValueError:
        pass
    return href


def _is_ad_url(url: str) -> bool:
    return "duckduckgo.com/y.js" in url or "ad_domain=" in url


def _fetch(url: str, timeout: int) -> str:
    req = request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
        },
        method="GET",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _make_result(href: str, title_fragment: str, snippet_fragment: str) -> Dict[str, str]:
    url = _unwrap_ddg_url(html_module.unescape(href))
    title = _clean_html_text(title_fragment)
    snippet = _clean_html_text(snippet_fragment)
    if not title or not url.startswith("http") or _is_ad_url(url):
        return {}
    return {"title": title[:120], "url": url[:300], "snippet": snippet[:300]}


def _parse_html_results(page: str, max_results: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    for block in _HTML_BLOCK_SPLIT_RE.split(page)[1:]:
        # 블록 첫머리(클래스 속성 잔여분)에 광고 표식이 있으면 건너뛴다.
        if "result--ad" in block[:200]:
            continue
        link_match = _HTML_LINK_RE.search(block)
        if not link_match:
            continue
        snippet_match = _HTML_SNIPPET_RE.search(block)
        result = _make_result(
            link_match.group("href"),
            link_match.group("title"),
            snippet_match.group("snippet") if snippet_match else "",
        )
        if result:
            results.append(result)
        if len(results) >= max_results:
            break
    return results


def _parse_lite_results(page: str, max_results: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    link_matches = list(_LITE_LINK_RE.finditer(page))
    for idx, match in enumerate(link_matches):
        # 요약은 이 링크와 다음 링크 사이 구간에서만 찾는다 (다른 결과의 요약 오염 방지).
        segment_end = link_matches[idx + 1].start() if idx + 1 < len(link_matches) else len(page)
        snippet_match = _LITE_SNIPPET_RE.search(page, match.end(), segment_end)
        result = _make_result(
            match.group("href"),
            match.group("title"),
            snippet_match.group("snippet") if snippet_match else "",
        )
        if result:
            results.append(result)
        if len(results) >= max_results:
            break
    return results


def search_web(query: str, max_results: int = 3, timeout: int = 10) -> List[Dict[str, str]]:
    """DuckDuckGo 에서 검색해 [{'title', 'url', 'snippet'}] 목록을 반환한다.

    네트워크/파싱 실패 시 예외 대신 빈 리스트를 반환한다 (대화가 막히지 않도록).
    """
    query = (query or "").strip()
    if not query:
        return []
    max_results = max(1, min(8, max_results))
    encoded = parse.urlencode({"q": query, "kl": "kr-kr"})
    attempts = (
        (f"https://html.duckduckgo.com/html/?{encoded}", _parse_html_results),
        (f"https://lite.duckduckgo.com/lite/?{encoded}", _parse_lite_results),
    )
    for url, parser in attempts:
        host = parse.urlparse(url).netloc
        try:
            page = _fetch(url, timeout)
        except (error.URLError, TimeoutError, OSError) as e:
            logger.warning(f"웹 검색 요청 실패({host}): {e}")
            continue
        try:
            results = parser(page, max_results)
        except Exception as e:
            logger.warning(f"웹 검색 결과 파싱 실패({host}): {e}")
            continue
        if results:
            return results
        logger.debug(f"웹 검색 결과 없음({host}): {query!r}")
    return []
