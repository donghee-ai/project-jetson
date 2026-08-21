"""웹 검색 — 질의어를 URL 로 바꾸는 계층. 본문은 여전히 `webfetch.py` 가 읽는다.

## 왜 필요한가

`fetch_url` 은 **URL 을 알아야** 읽는다. 그래서 모델이 도메인을 지어내는 일이
있었다 — "롤체가 뭐야" 에 `valorant.com` 을 열고, 그 뒤 세 턴이 전부 같은 틀린
출처를 인용했다 (`docs/known-issues.md §1`). 검색은 그 구멍을 메운다.

## 왜 구글이 아닌가

구글 Custom Search JSON API 는 **신규 가입이 닫혔다** (2026-08-19 확인, 기존
고객도 2027-01-01 종료). 대안으로 안내되는 Vertex AI Search 는 도메인 50개 이하
사이트 검색용이고 GCP 과금 계정이 전제다. 그래서:

    한국어 질의  ->  네이버 검색 API   국내 사이트에 강하다
    그 외        ->  Serper.dev        구글 결과를 JSON 으로. 2,500건 무료

**언어로 고른다.** 실패 사례("롤체")가 국내 게임 용어였다.

★ **네이버는 지금 못 쓴다** — 검색 API 가 NAVER API HUB 로 이관되고 개발자센터
신규 신청이 2026-07-31 에 닫혔다. 아래 `NAVER_ENDPOINT`·헤더는 옛 방식이라 HUB
키로는 401 이 난다 (`docs/known-issues.md §4`). 그래서 실제로는 **Serper 하나로
돌고 있고**, 한국어 질의는 `_pick_provider` 의 폴백을 타고 Serper 로 간다.
그 대신 `_serper` 가 한국어일 때 `gl=kr&hl=ko` 를 붙인다.

## ★ 이 툴은 질의어를 바깥으로 내보낸다

이 프로젝트의 전제는 "데이터가 기기 밖으로 안 나간다" 이고, 검색은 그 예외다.
나가는 것은 **질의어 한 줄뿐**이며 활동 기록·창 제목·계획은 절대 싣지 않는다.
질의어에 개인 정보가 섞이지 않게 길이를 `MAX_QUERY_CHARS` 로 자른다 — 모델이
맥락을 통째로 질의어에 넣는 것을 막는 최소한의 방어다.

## 왜 PoliteSession 을 안 쓰나

`collect/http.py` 는 **크롤러**의 예의(robots.txt·도메인 토큰버킷·조건부 GET)를
강제하는 계층이다. 여기는 키를 발급받고 쿼터를 부여받은 **계약된 API** 라
지배 규범이 robots.txt 가 아니라 이용약관이고, Serper 는 POST 를 쓴다.
그래서 `requests` 를 직접 쓰되 타임아웃·결과 수 상한은 여기서 강제한다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

NAVER_ENDPOINT = "https://openapi.naver.com/v1/search/webkr.json"
SERPER_ENDPOINT = "https://google.serper.dev/search"

MAX_QUERY_CHARS = 120  # 질의어는 바깥으로 나간다. 맥락을 통째로 싣지 못하게 자른다.
MAX_SNIPPET_CHARS = 160  # 툴 결과 길이가 곧 다음 턴의 입력이다
DEFAULT_TIMEOUT_SEC = 10.0

_HANGUL = re.compile(r"[가-힣]")
_TAGS = re.compile(r"<[^>]+>")


class SearchError(Exception):
    """검색 실패. 툴 계층이 문장으로 바꿔 모델에 돌려준다."""


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


@dataclass
class SearchResults:
    provider: str  # 'naver' | 'serper'
    label: str  # 결과 문자열에 박을 한국어 출처 라벨
    query: str
    hits: list[SearchHit] = field(default_factory=list)


def is_korean(text: str) -> bool:
    """한글이 하나라도 있으면 한국어 질의로 본다.

    비율이 아니라 존재로 판정한다. "Qwen3 벤치마크 결과" 처럼 섞인 질의는
    국내 블로그·커뮤니티에 답이 있을 때가 많아 네이버 쪽이 유리하다.
    """
    return bool(_HANGUL.search(text or ""))


def _clean(text: str) -> str:
    """네이버가 검색어에 씌우는 `<b>` 태그와 엔티티를 벗긴다."""
    import html

    return html.unescape(_TAGS.sub("", text or "")).strip()


def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def providers_available(cfg: "Config") -> dict[str, bool]:
    """설정된 공급자. `lt doctor` 와 툴 게이트가 같은 판정을 쓰게 한다."""
    return {
        "naver": bool(cfg.search.naver_client_id and cfg.search.naver_client_secret),
        "serper": bool(cfg.search.serper_api_key),
    }


def _pick_provider(cfg: "Config", query: str) -> str:
    """언어로 고르고, 없으면 있는 쪽으로 넘어간다.

    폴백이 있는 이유: 키를 하나만 넣어둔 상태에서도 검색이 **되기는 해야** 한다.
    다만 어느 쪽으로 검색했는지는 결과 라벨에 그대로 드러난다 — 조용히 대체하고
    출처를 안 밝히면 그것이 곧 지난번 폴백 사고다
    (`HISTORY/2026-08-18-search-fallback-noise.md`).
    """
    have = providers_available(cfg)
    preferred = "naver" if is_korean(query) else "serper"
    if have[preferred]:
        return preferred
    other = "serper" if preferred == "naver" else "naver"
    if have[other]:
        return other
    raise SearchError(
        "검색 API 키가 설정돼 있지 않습니다 "
        "(config/lifetrainer.toml 의 [search] naver_client_id/secret 또는 serper_api_key)."
    )


def _naver(cfg: "Config", query: str, limit: int) -> SearchResults:
    import requests

    resp = requests.get(
        NAVER_ENDPOINT,
        params={"query": query, "display": limit, "sort": "sim"},
        headers={
            "X-Naver-Client-Id": cfg.search.naver_client_id,
            "X-Naver-Client-Secret": cfg.search.naver_client_secret,
            "User-Agent": cfg.collect.user_agent,
        },
        timeout=cfg.search.timeout_sec,
    )
    if resp.status_code != 200:
        raise SearchError(f"네이버 검색 실패 (HTTP {resp.status_code})")
    items = (resp.json() or {}).get("items") or []
    hits = [
        SearchHit(
            title=_truncate(_clean(it.get("title")), 100),
            url=str(it.get("link") or ""),
            snippet=_truncate(_clean(it.get("description")), MAX_SNIPPET_CHARS),
        )
        for it in items
        if it.get("link")
    ]
    return SearchResults(provider="naver", label="네이버 웹 검색", query=query, hits=hits[:limit])


def _serper(cfg: "Config", query: str, limit: int) -> SearchResults:
    import requests

    # 한국어 질의는 지역·언어를 한국으로 못박는다. Serper 기본값은 구글 미국/영어라
    # "롤체" 같은 국내 용어가 엉뚱한 곳에 안착한다 — 이 툴이 생긴 이유가 그 사고다.
    # 네이버를 쓸 수 없게 된 뒤(known-issues §4) 한국어 질의는 전부 여기로 온다.
    locale = {"gl": "kr", "hl": "ko"} if is_korean(query) else {}

    resp = requests.post(
        SERPER_ENDPOINT,
        json={"q": query, "num": limit, **locale},
        headers={
            "X-API-KEY": cfg.search.serper_api_key,
            "Content-Type": "application/json",
            "User-Agent": cfg.collect.user_agent,
        },
        timeout=cfg.search.timeout_sec,
    )
    if resp.status_code != 200:
        raise SearchError(f"Serper 검색 실패 (HTTP {resp.status_code})")
    data = resp.json() or {}
    hits = [
        SearchHit(
            title=_truncate(str(it.get("title") or ""), 100),
            url=str(it.get("link") or ""),
            snippet=_truncate(str(it.get("snippet") or ""), MAX_SNIPPET_CHARS),
        )
        for it in (data.get("organic") or [])
        if it.get("link")
    ]
    return SearchResults(
        provider="serper", label="구글 검색 (Serper 경유)", query=query, hits=hits[:limit]
    )


def search(cfg: "Config", query: str, *, limit: int | None = None) -> SearchResults:
    """질의어 하나를 검색해 결과 목록을 돌려준다. 본문은 읽지 않는다.

    네트워크·형식 오류는 전부 `SearchError` 로 모아 올린다 — 툴 계층이 그것을
    "확인하지 못했다" 문장으로 바꾼다. 여기서 다른 출처로 조용히 대체하지 않는다.
    """
    query = _truncate(query, MAX_QUERY_CHARS)
    if not query:
        raise SearchError("검색어가 비었습니다.")

    n = limit if limit is not None else cfg.search.max_results
    n = max(1, min(int(n), 10))

    provider = _pick_provider(cfg, query)
    try:
        result = _naver(cfg, query, n) if provider == "naver" else _serper(cfg, query, n)
    except SearchError:
        raise
    except Exception as exc:  # noqa: BLE001 - requests 계열 예외를 한 종류로 모은다
        raise SearchError(f"검색 중 오류: {exc}") from exc

    logger.info("검색(%s) %r → %d건", provider, query, len(result.hits))
    return result
