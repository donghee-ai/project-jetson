"""LLM 이 웹 페이지를 읽을 수 있게 해주는 계층 — 안전장치가 본체다.

## 왜 안전장치가 먼저인가

이 툴의 URL 은 **모델이 고른다.** 그리고 모델은 우리가 수집한 문서(외부 RSS)를
읽고 답한다. 즉 **외부에서 흘러들어온 문자열이 요청 대상이 될 수 있다.**
이 기기에는 밖으로 새면 안 되는 것이 붙어 있다:

    100.64.0.2:8770   웹 플래너 (창 제목이 담긴 개인 활동 기록)
    127.0.0.1:8080       llama-server
    127.0.0.1:18081      OpenClaw 게이트웨이
    100.64.0.3:35600  노트북 ActivityWatch

그래서 "공개 인터넷 주소가 아니면 안 간다" 를 먼저 세우고 기능을 얹는다.

## ★ `is_private` 로 막으면 안 된다

파이썬 실측:

    ipaddress.ip_address("100.64.0.2").is_private  ->  False   ← 젯슨 웹 플래너
    ipaddress.ip_address("100.64.0.2").is_global   ->  False

Tailscale 이 쓰는 100.64.0.0/10(CGNAT)은 `is_private` 에 안 잡힌다. **`is_global`
이 참인 주소만 허용**해야 사설·루프백·링크로컬·예약·CGNAT 이 한 번에 걸린다.

## 리다이렉트를 자동으로 따라가면 안 되는 이유

첫 URL 만 검사하고 `allow_redirects=True` 로 두면, 공개 도메인이 302 로
`http://100.64.0.2:8770` 을 가리키는 순간 방어가 무의미해진다. 홉마다 다시
검증한다(`PoliteSession.get(allow_redirects=False)`).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    import sqlite3

    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 3
MAX_BYTES = 2_000_000  # 2MB 넘는 문서는 읽을 가치보다 비용이 크다
DEFAULT_MAX_CHARS = 4000  # 계약서 §0: LLM 입력 3~5K 로 끊는다
FETCH_TIMEOUT_SEC = 15.0

_ALLOWED_SCHEMES = frozenset({"http", "https"})
# 텍스트로 바꿔 읽을 수 있는 것만. PDF·이미지·바이너리는 거부한다.
_ALLOWED_CONTENT = ("text/html", "text/plain", "application/xhtml", "application/json", "text/xml", "application/xml")

_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "head", "template", "iframe"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer"}
)

_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")

# 메뉴·필터 목록 접기 기준 (collapse_menu_runs). 짧은 줄이 이만큼 연속되면 목록으로 본다.
MENU_RUN_LINES = 25
MENU_LINE_CHARS = 30
MENU_KEEP_TAIL = 6  # 접을 때 남길 꼬리 줄 수 (본문 첫머리 보호)


class WebFetchError(Exception):
    """가져오기 실패. 메시지가 그대로 모델에게 툴 결과로 전달된다."""


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str
    truncated: bool


def assert_public_url(url: str) -> str:
    """URL 이 공개 인터넷을 가리키는지 검사하고, 통과하면 그대로 돌려준다.

    실패는 전부 `WebFetchError` — 어디서 막혔는지 사용자가 알 수 있게 이유를 담는다.
    """
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise WebFetchError(f"http/https 주소만 열 수 있습니다 (받은 것: {parts.scheme or '없음'!r})")
    host = parts.hostname
    if not host:
        raise WebFetchError(f"주소에서 호스트를 찾지 못했습니다: {url!r}")

    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise WebFetchError(f"주소를 찾을 수 없습니다 ({host}): {exc}") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise WebFetchError(f"주소를 찾을 수 없습니다: {host}")

    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:  # pragma: no cover - getaddrinfo 가 준 값이라 사실상 없음
            raise WebFetchError(f"해석할 수 없는 주소입니다: {raw}") from None
        # ★ is_private 가 아니라 is_global 로 본다 (모듈 docstring 참고).
        if not ip.is_global:
            raise WebFetchError(
                f"공개 인터넷 주소가 아니라 열 수 없습니다 ({host} → {raw}). "
                "이 기기 안이나 사설망·Tailscale 주소는 대화 툴로 접근하지 않습니다."
            )
    return url


class _TextExtractor(HTMLParser):
    """태그를 걷어내고 본문 텍스트만 남긴다.

    beautifulsoup 을 안 쓴다 — 상주 메모리 300MB 제약이 있고, 여기서 필요한 것은
    "태그 제거 + 블록 단위 줄바꿈" 뿐이라 표준 라이브러리로 충분하다.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        # `head` 는 _SKIP_TAGS 라 title 은 위에서 따로 건진다.
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data)

    def result(self) -> tuple[str, str]:
        text = _WS_RE.sub(" ", "".join(self.parts))
        text = "\n".join(line.strip() for line in text.split("\n"))
        return self.title.strip(), _NL_RE.sub("\n\n", text).strip()


def collapse_menu_runs(
    text: str, *, min_run: int = MENU_RUN_LINES, max_len: int = MENU_LINE_CHARS
) -> str:
    """짧은 줄이 길게 이어지는 구간(=메뉴·필터 목록)을 한 줄 요약으로 접는다.

    태그로는 못 거른다. 예컨대 GitHub Trending 의 언어 필터는 `<details>` 안에 있는데,
    `details` 를 통째로 버리면 다른 사이트의 접힌 **본문**까지 잃는다. 대신 모양으로
    판단한다 — 사람이 읽는 문장은 25줄씩 연속으로 30자 미만이지 않다.

    실측: 이 필터 하나로 GitHub Trending 본문이 15,000자 → 2,000자대가 되고,
    잘라내기 전에 실제 저장소 목록이 들어온다. Hacker News 처럼 제목이 긴 목록은
    영향을 받지 않는다.
    """
    lines = text.split("\n")
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        content = [line for line in run if line.strip()]
        if len(content) >= min_run:
            # 꼬리 몇 줄은 남긴다. 메뉴가 끝나고 본문이 시작될 때 그 경계에 긴 줄이
            # 없으면(예: GitHub Trending 은 언어 목록 바로 뒤에 "owner /", "repo" 가
            # 짧은 줄로 온다) 본문 첫머리까지 같이 접혀버린다.
            kept = content[-MENU_KEEP_TAIL:]
            out.append(f"(목록 {len(content) - len(kept)}줄 생략)")
            out.extend(kept)
        else:
            out.extend(run)
        run.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            # 항목 사이의 빈 줄은 목록을 끊지 않는다. 블록 태그마다 줄바꿈이 들어가서
            # 메뉴 한 항목이 "낱말\n\n낱말" 로 나오는데, 여기서 끊으면 아무것도 못 접는다.
            (run if run else out).append(line)
            continue
        if len(stripped) <= max_len:
            run.append(line)
            continue
        flush()
        out.append(line)
    flush()
    return _NL_RE.sub("\n\n", "\n".join(out)).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """(제목, 본문 텍스트). 파싱이 깨져도 예외를 내지 않고 있는 만큼 돌려준다."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - 깨진 HTML 때문에 대화가 죽으면 안 된다
        logger.debug("HTML 파싱 중 예외(무시하고 부분 결과 사용): %s", exc)
    title, text = parser.result()
    return title, collapse_menu_runs(text)


def fetch_page(
    cfg: "Config",
    conn: "sqlite3.Connection",
    url: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> FetchedPage:
    """URL 을 안전하게 가져와 텍스트로 돌려준다. 리다이렉트는 홉마다 재검증한다."""
    from lifetrainer.collect.http import PoliteSession

    session = PoliteSession(cfg, conn)
    current = assert_public_url(url)

    for hop in range(MAX_REDIRECTS + 1):
        result = session.get(
            current,
            timeout=FETCH_TIMEOUT_SEC,
            use_state=False,  # 대화용 1회성 조회 — ETag 캐시 상태를 남기지 않는다
            allow_redirects=False,
        )
        if result.error == "robots_disallowed":
            raise WebFetchError(f"이 사이트의 robots.txt 가 접근을 막고 있습니다: {current}")

        # ★ 리다이렉트 판정을 `result.error` 검사보다 **먼저** 한다.
        # PoliteSession 은 비-2xx 를 전부 `error="http_<code>"` 로 표시하므로(수집용
        # 관점에서는 3xx 도 "본문 못 받음"이다), 순서를 바꾸면 301 한 번에 통째로
        # 실패한다. 실제로 www.valorant.com 이 그렇게 죽었다.
        if 300 <= result.status < 400:
            location = (result.headers or {}).get("Location") or (result.headers or {}).get("location")
            if not location:
                raise WebFetchError(f"리다이렉트에 목적지가 없습니다 ({result.status}): {current}")
            if hop >= MAX_REDIRECTS:
                raise WebFetchError(f"리다이렉트가 너무 많습니다({MAX_REDIRECTS}회 초과): {url}")
            # ★ 홉마다 다시 검증한다. 여기서 안 막으면 SSRF 방어가 무의미해진다.
            current = assert_public_url(urljoin(current, location))
            continue

        if result.error:
            raise WebFetchError(f"가져오지 못했습니다 ({current}): {result.error}")

        if result.status >= 400:
            raise WebFetchError(f"서버가 {result.status} 를 반환했습니다: {current}")

        content_type = str((result.headers or {}).get("Content-Type", "")).lower()
        if content_type and not any(c in content_type for c in _ALLOWED_CONTENT):
            raise WebFetchError(f"읽을 수 있는 형식이 아닙니다 ({content_type.split(';')[0]}): {current}")

        body = result.body or b""
        if len(body) > MAX_BYTES:
            raise WebFetchError(f"문서가 너무 큽니다 ({len(body) // 1024}KB). 더 구체적인 주소를 주세요.")

        # ★ 서버가 charset 을 선언하지 않으면 requests 는 RFC 대로 ISO-8859-1 로 읽는다.
        # 요즘 페이지는 사실상 UTF-8 이라 그대로 두면 한글·기호가 깨진다(실측:
        # docs.python.org 제목이 'â€"' 로 나왔다). 선언이 있으면 서버를 믿고,
        # 없으면 UTF-8 로 직접 디코드한다.
        if "charset=" in content_type:
            raw = result.text if result.text is not None else body.decode("utf-8", errors="replace")
        elif body:
            raw = body.decode("utf-8", errors="replace")
        else:
            raw = result.text or ""
        if "html" in content_type or raw.lstrip()[:1] == "<":
            title, text = html_to_text(raw)
        else:
            title, text = "", raw.strip()

        if not text:
            raise WebFetchError(f"본문에서 읽을 텍스트를 찾지 못했습니다: {current}")

        truncated = len(text) > max_chars
        return FetchedPage(url=current, title=title, text=text[:max_chars], truncated=truncated)

    raise WebFetchError(f"리다이렉트가 너무 많습니다: {url}")  # pragma: no cover - 위 루프에서 처리
