"""웹 검색 — 질의어를 URL 로 바꾸는 계층. 본문은 여전히 `webfetch.py` 가 읽는다.

## 왜 필요한가

`fetch_url` 은 **URL 을 알아야** 읽는다. 그래서 모델이 도메인을 지어내는 일이
있었다 — "롤체가 뭐야" 에 `valorant.com` 을 열고, 그 뒤 세 턴이 전부 같은 틀린
출처를 인용했다 (`docs/issues/h-0006-the-model-cites-one-source-and-stays-there.md`). 검색은 그 구멍을 메운다.

## 왜 Serper 하나인가

구글 Custom Search JSON API 는 **신규 가입이 닫혔다** (2026-08-19 확인, 기존
고객도 2027-01-01 종료). 대안으로 안내되는 Vertex AI Search 는 도메인 50개 이하
사이트 검색용이고 GCP 과금 계정이 전제다.

한국어 질의는 원래 네이버 검색 API 로 보냈다 — 실패 사례("롤체")가 국내 게임
용어였기 때문이다. **그 경로는 2026-08-25 에 지웠다.** 검색 API 가 NAVER API HUB
로 이관되고 개발자센터 신규 신청이 2026-07-31 에 닫혀 우리는 키를 못 받는다.
코드에 남아 있던 옛 엔드포인트·헤더는 HUB 키로는 401 이 나므로, 누가 설정에
`naver_client_id` 를 채우는 순간 **한국어 질의만 조용히 죽는 함정**이었다
(`HISTORY/2026-08-25-it-worked-because-the-key-was-empty.md`).

    모든 질의  ->  Serper.dev   구글 결과를 JSON 으로. 2,500건 무료

한국어를 버린 것이 아니라 **한국어를 Serper 안에서 처리한다** — `_serper` 가
한글이 섞이면 `gl=kr&hl=ko` 를 붙여 구글 한국/한국어 결과를 받는다. 언어 판정
(`is_korean`)이 남아 있는 이유가 이것이다.

## ★ 이 툴은 질의어를 바깥으로 내보낸다

활동 원본은 로컬에 보관하지만 검색은 외부 전송 경로다.
나가는 것은 **질의어 한 줄**이다. 방어가 두 겹이다.

1. **길이** — `MAX_QUERY_CHARS` 로 자른다. 맥락이 통째로 실리는 것을 막는다
2. **내용** — `search()` 가 나가기 직전에 `trigger.is_personal()` 로 한 번 더 본다

★ 처음엔 1번만 있었다. 그런데 유출은 **길이가 아니라 내용**이었다 — 개인 낱말이
질의어 **앞**에 붙어서, 잘라도 그대로 남았다 (`docs/issues/0001`). 방어를 엉뚱한
축에 걸어두고 막혔다고 믿고 있었다.

## ★ 이 두 겹이 막는 것과 못 막는 것 (`docs/issues/0031`)

여기 오래 **"활동 기록·창 제목·계획은 절대 싣지 않는다"** 라고 적혀 있었다.
그 문장은 코드보다 넓었다. `is_personal` 이 실제로 판정하는 것은 한국어
**자기지칭 어형**이다 (`내/오늘/이번 주` + `계획/일정/할 일`, `달성률|플래너|타임라인`).

    막는다      "어제 뭐 했지" · "내 오늘 일정" · "오늘은?"
                → 자기 기록을 묻는 말. 선주입 경로는 조립 결과도 다시 검사한다

    못 막는다   모델이 계획 제목이나 문서 제목에서 낱말을 뽑아 검색 인자로 넣는 경우.
                기록에서 왔다는 표시가 문자열에 없어 게이트가 판정할 근거가 없다.

지금 이 경로를 붙잡는 것은 `tools.py` 의 검색어 작성 지시다. 모델이 따르지 않아도
울리는 강제 장치는 없다. 강제하려면 질의어를 최근 기록 제목과 대조하는 별도 설계가
필요하다. 그때까지 이 모듈이 약속하는 것은 **질의어 한 줄만 나간다**까지다.

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

SERPER_ENDPOINT = "https://google.serper.dev/search"

MAX_QUERY_CHARS = 120  # 질의어는 바깥으로 나간다. 맥락을 통째로 싣지 못하게 자른다.
MAX_SNIPPET_CHARS = 160  # 툴 결과 길이가 곧 다음 턴의 입력이다
DEFAULT_TIMEOUT_SEC = 10.0

_HANGUL = re.compile(r"[가-힣]")


class SearchError(Exception):
    """검색 실패. 툴 계층이 문장으로 바꿔 모델에 돌려준다."""


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


@dataclass
class SearchResults:
    provider: str  # 'serper' — 라벨과 함께 결과 문자열에 박힌다
    label: str  # 결과 문자열에 박을 한국어 출처 라벨
    query: str
    hits: list[SearchHit] = field(default_factory=list)


def is_korean(text: str) -> bool:
    """한글이 하나라도 있으면 한국어 질의로 본다.

    비율이 아니라 존재로 판정한다. "Qwen3 벤치마크 결과" 처럼 섞인 질의도 답이
    국내 블로그·커뮤니티에 있을 때가 많다.

    ★ 이 판정은 이제 **공급자를 고르는 데 쓰이지 않는다** (공급자는 하나다).
    `_serper` 가 `gl=kr&hl=ko` 를 붙일지만 정한다.
    """
    return bool(_HANGUL.search(text or ""))


def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def providers_available(cfg: "Config") -> dict[str, bool]:
    """설정된 공급자. `lt doctor` 와 툴 게이트가 같은 판정을 쓰게 한다.

    공급자가 하나뿐이어도 **dict 를 유지한다.** 부르는 쪽(`cli.doctor` ·
    `converse._must_refuse_web`)이 `any(...)` 로 읽고 있어서, 나중에 공급자가
    다시 늘어도 그쪽을 안 고친다.
    """
    return {"serper": bool(cfg.search.serper_api_key)}


def _serper(cfg: "Config", query: str, limit: int) -> SearchResults:
    import requests

    # 한국어 질의는 지역·언어를 한국으로 못박는다. Serper 기본값은 구글 미국/영어라
    # "롤체" 같은 국내 용어가 엉뚱한 곳에 안착한다 — 이 툴이 생긴 이유가 그 사고다.
    # 네이버를 쓸 수 없게 된 뒤(HISTORY/2026-08-25-it-worked-because-the-key-was-empty.md) 한국어 질의는 전부 여기로 온다.
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

    # ★ **발송 직전 마지막 관문** (`docs/issues/0001`).
    #
    # 게이트(`trigger.select_tools`)는 사용자의 원문 하나를 보고 툴을 고르는데,
    # **실제로 나가는 문자열은 그 뒤에 다시 조립된다** — `converse._search_query` 가
    # 짧은 후속 발화("오늘은?") 앞에 직전 질문의 낱말("어제","했지")을 붙인다.
    # 그래서 게이트가 판정한 문자열과 나간 문자열이 달랐고, 개인 발화가 새어나갔다.
    #
    # 모든 경로(선주입·모델의 툴 호출·MCP)가 이 함수로 합류하므로, **여기서 막으면
    # 새 호출자가 생겨도 못 빠져나간다.** 게이트와 같은 판정을 쓴다.
    from lifetrainer.llm.trigger import is_personal  # 지연 import — 순환 방지

    if is_personal(query):
        raise SearchError(
            f"이 질문은 사용자 자신의 기록에 대한 것이라 바깥으로 내보내지 않았다 ({query!r}). "
            "검색하지 말고 이미 주어진 활동 기록·계획으로 답하라."
        )

    n = limit if limit is not None else cfg.search.max_results
    n = max(1, min(int(n), 10))

    if not cfg.search.serper_api_key:
        raise SearchError(
            "검색 API 키가 설정돼 있지 않습니다 "
            "(config/lifetrainer.toml 의 [search] serper_api_key)."
        )

    # ★ **보내기 전에** 남긴다. 성공 뒤에만 남기면 실패한 요청이 기록에서 사라지는데,
    #   기록의 목적은 "몇 건 받았나"가 아니라 **무엇이 바깥으로 나갔나** 다
    #   (`docs/issues/0002`). 나간 것은 실패해도 나간 것이다.
    logger.info("바깥으로 나감 — 검색(serper) %r (최대 %d건 요청)", query, n)

    try:
        result = _serper(cfg, query, n)
    except SearchError:
        raise
    except Exception as exc:  # noqa: BLE001 - requests 계열 예외를 한 종류로 모은다
        raise SearchError(f"검색 중 오류: {exc}") from exc

    logger.info("검색(%s) 응답 %d건", result.provider, len(result.hits))
    return result
