"""lifetrainer.llm.webfetch 테스트.

**네트워크 금지.** DNS 조회(`socket.getaddrinfo`)와 HTTP(`PoliteSession.get`)를
전부 가짜로 갈아끼운다. 실제 주소로 나가는 테스트는 하나도 없다.

이 파일이 지키려는 계약:
- **사설·루프백·Tailscale 주소는 절대 못 연다.** 이 기기의 웹 플래너에는 창 제목이
  담긴 개인 기록이 있고, URL 은 모델이 고른다.
- **리다이렉트도 홉마다 검사한다.** 첫 URL 만 보면 302 한 번에 방어가 무너진다.
"""

from __future__ import annotations

import socket

import pytest

from lifetrainer.llm import webfetch
from lifetrainer.llm.webfetch import (
    WebFetchError,
    assert_public_url,
    collapse_menu_runs,
    html_to_text,
)


@pytest.fixture()
def fake_dns(monkeypatch):
    """호스트명 -> IP 를 테스트가 정하게 한다 (실제 DNS 를 타지 않는다)."""
    table: dict[str, str] = {}

    def _getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        ip = table.get(host, host)  # 등록 안 된 이름은 그대로 IP 로 해석 시도
        try:
            socket.inet_pton(socket.AF_INET6 if ":" in ip else socket.AF_INET, ip)
        except OSError as exc:
            raise socket.gaierror(f"unknown host {host}") from exc
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 443))]

    monkeypatch.setattr(webfetch.socket, "getaddrinfo", _getaddrinfo)
    return table


# ── SSRF 차단 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/v1/models",  # llama-server
        "http://10.0.0.5/",
        "http://192.168.0.1/",
        "http://172.17.0.2/",
        "http://169.254.169.254/latest/meta-data/",  # 클라우드 메타데이터 관용 공격
        "http://[::1]/",
    ],
)
def test_내부_주소는_막는다(url, fake_dns):
    with pytest.raises(WebFetchError, match="공개 인터넷"):
        assert_public_url(url)


def test_Tailscale_주소를_막는다(fake_dns):
    """★ 100.64.0.0/10(CGNAT)은 `is_private` 이 False 다. `is_global` 로 봐야 걸린다.

    이 기기의 웹 플래너가 100.64.0.2:8770 에 있다 — 창 제목이 담긴 개인 기록이다.
    """
    import ipaddress

    assert ipaddress.ip_address("100.64.0.2").is_private is False  # 함정 자체를 고정
    with pytest.raises(WebFetchError, match="공개 인터넷"):
        assert_public_url("http://100.64.0.2:8770/")


def test_이름이_내부_주소로_풀려도_막는다(fake_dns):
    """도메인은 멀쩡해 보여도 A 레코드가 내부를 가리킬 수 있다 (DNS 리바인딩)."""
    fake_dns["evil.example.com"] = "127.0.0.1"
    with pytest.raises(WebFetchError, match="공개 인터넷"):
        assert_public_url("https://evil.example.com/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/", "javascript:alert(1)"])
def test_http_외_스킴은_막는다(url, fake_dns):
    with pytest.raises(WebFetchError, match="http/https"):
        assert_public_url(url)


def test_호스트가_없으면_막는다(fake_dns):
    with pytest.raises(WebFetchError):
        assert_public_url("https:///path")


def test_공개_주소는_통과한다(fake_dns):
    fake_dns["example.com"] = "93.184.216.34"
    assert assert_public_url("https://example.com/a") == "https://example.com/a"


# ── 리다이렉트 홉별 검증 ─────────────────────────────────────────────────


class _FakeResult:
    """`FetchResult` 대역.

    ★ 실물 `PoliteSession` 은 **비-2xx 를 전부 `error="http_<code>"` 로 표시한다**
    (수집 관점에서는 3xx 도 "본문 못 받음"이다). 가짜가 3xx 에 error=None 을 주면
    실물보다 느슨해져서, 리다이렉트를 error 로 먼저 걸러버리는 버그가 통과한다 —
    실제로 그렇게 통과했고 www.valorant.com 이 301 로 죽었다.
    """

    def __init__(self, status=200, headers=None, text=None, body=b"", error=None):  # noqa: ANN001
        self.status = status
        self.headers = headers or {}
        self.text = text
        self.body = body
        if error is None and not (200 <= status < 300):
            error = f"http_{status}"
        self.error = error


def _install_fake_session(monkeypatch, responses: dict):
    """URL -> _FakeResult 매핑으로 PoliteSession 을 대체한다."""
    calls: list[str] = []

    class _FakeSession:
        def __init__(self, cfg, conn):  # noqa: ANN001
            pass

        def get(self, url, **kwargs):  # noqa: ANN001, ANN003
            calls.append(url)
            assert kwargs.get("allow_redirects") is False, "리다이렉트를 자동으로 따라가면 안 된다"
            return responses[url]

    import lifetrainer.collect.http as http_mod

    monkeypatch.setattr(http_mod, "PoliteSession", _FakeSession)
    return calls


def test_리다이렉트_목적지도_검사한다(monkeypatch, fake_dns):
    """공개 도메인이 302 로 내부 주소를 가리키는 고전적 우회."""
    fake_dns["public.example.com"] = "93.184.216.34"
    _install_fake_session(
        monkeypatch,
        {
            "https://public.example.com/": _FakeResult(
                status=302, headers={"Location": "http://100.64.0.2:8770/"}
            )
        },
    )
    with pytest.raises(WebFetchError, match="공개 인터넷"):
        webfetch.fetch_page(object(), object(), "https://public.example.com/")


def test_정상_리다이렉트는_따라간다(monkeypatch, fake_dns):
    fake_dns["a.example.com"] = "93.184.216.34"
    fake_dns["b.example.com"] = "93.184.216.35"
    calls = _install_fake_session(
        monkeypatch,
        {
            "https://a.example.com/": _FakeResult(status=301, headers={"Location": "https://b.example.com/x"}),
            "https://b.example.com/x": _FakeResult(
                status=200, headers={"Content-Type": "text/html"}, text="<html><body><p>도착</p></body></html>"
            ),
        },
    )
    page = webfetch.fetch_page(object(), object(), "https://a.example.com/")
    assert page.text == "도착"
    assert page.url == "https://b.example.com/x"
    assert len(calls) == 2


def test_리다이렉트가_너무_많으면_멈춘다(monkeypatch, fake_dns):
    fake_dns["loop.example.com"] = "93.184.216.34"
    _install_fake_session(
        monkeypatch,
        {"https://loop.example.com/": _FakeResult(status=302, headers={"Location": "https://loop.example.com/"})},
    )
    with pytest.raises(WebFetchError, match="리다이렉트가 너무 많"):
        webfetch.fetch_page(object(), object(), "https://loop.example.com/")


def test_robots_차단은_그대로_알린다(monkeypatch, fake_dns):
    fake_dns["x.example.com"] = "93.184.216.34"
    _install_fake_session(monkeypatch, {"https://x.example.com/": _FakeResult(error="robots_disallowed")})
    with pytest.raises(WebFetchError, match="robots.txt"):
        webfetch.fetch_page(object(), object(), "https://x.example.com/")


def test_읽을_수_없는_형식은_거절한다(monkeypatch, fake_dns):
    fake_dns["pdf.example.com"] = "93.184.216.34"
    _install_fake_session(
        monkeypatch,
        {"https://pdf.example.com/a.pdf": _FakeResult(status=200, headers={"Content-Type": "application/pdf"})},
    )
    with pytest.raises(WebFetchError, match="읽을 수 있는 형식"):
        webfetch.fetch_page(object(), object(), "https://pdf.example.com/a.pdf")


def test_너무_큰_문서는_거절한다(monkeypatch, fake_dns):
    fake_dns["big.example.com"] = "93.184.216.34"
    _install_fake_session(
        monkeypatch,
        {
            "https://big.example.com/": _FakeResult(
                status=200, headers={"Content-Type": "text/html"}, body=b"x" * (webfetch.MAX_BYTES + 1)
            )
        },
    )
    with pytest.raises(WebFetchError, match="너무 큽니다"):
        webfetch.fetch_page(object(), object(), "https://big.example.com/")


def test_본문을_상한까지만_싣고_잘림을_알린다(monkeypatch, fake_dns):
    fake_dns["long.example.com"] = "93.184.216.34"
    long_text = "가나다라마바사아자차카타파하 " * 200
    _install_fake_session(
        monkeypatch,
        {
            "https://long.example.com/": _FakeResult(
                status=200, headers={"Content-Type": "text/html"}, text=f"<html><body><p>{long_text}</p></body></html>"
            )
        },
    )
    page = webfetch.fetch_page(object(), object(), "https://long.example.com/", max_chars=100)
    assert len(page.text) == 100
    assert page.truncated is True


# ── HTML -> 텍스트 ───────────────────────────────────────────────────────


def test_스크립트와_스타일은_버린다():
    title, text = html_to_text(
        "<html><head><title>T</title><style>a{color:red}</style></head>"
        "<body><script>evil()</script><p>본문</p></body></html>"
    )
    assert title == "T"
    assert text == "본문"
    assert "evil" not in text and "color" not in text


def test_블록_태그가_줄을_나눈다():
    _, text = html_to_text("<p>하나</p><p>둘</p>")
    assert text.split("\n\n") == ["하나", "둘"]


def test_깨진_HTML_도_예외를_내지_않는다():
    title, text = html_to_text("<p>열고 안 닫음 <div><span>중첩")
    assert "열고 안 닫음" in text


# ── 메뉴 접기 ────────────────────────────────────────────────────────────


def test_긴_짧은줄_연속은_접힌다():
    """GitHub Trending 의 언어 필터(수백 줄)가 본문을 밀어내던 문제."""
    menu = "\n".join(f"Lang{i}" for i in range(60))
    text = collapse_menu_runs(f"{menu}\n이것은 충분히 긴 실제 본문 문장입니다. 접히면 안 됩니다.")
    assert "줄 생략)" in text
    assert "이것은 충분히 긴 실제 본문 문장입니다" in text
    assert len(text) < 400


def test_접어도_꼬리는_남긴다():
    """메뉴 끝과 본문 시작 사이에 긴 줄이 없으면 본문 첫머리까지 먹힌다."""
    menu = "\n".join(f"Lang{i}" for i in range(60))
    text = collapse_menu_runs(f"{menu}\nowner /\nrepo-name")
    assert "repo-name" in text
    assert "owner /" in text


def test_빈_줄이_목록을_끊지_않는다():
    """블록 태그마다 줄바꿈이 들어가 메뉴가 '낱말\\n\\n낱말' 로 나온다."""
    menu = "\n\n".join(f"Lang{i}" for i in range(60))
    assert "줄 생략)" in collapse_menu_runs(menu)


def test_짧은_목록은_그대로_둔다():
    text = "\n".join(f"항목{i}" for i in range(5))
    assert collapse_menu_runs(text) == text


def test_제목이_긴_목록은_영향받지_않는다():
    """Hacker News 처럼 각 줄이 긴 목록은 접히면 안 된다."""
    lines = [f"{i}. 이것은 해커뉴스 기사 제목처럼 충분히 긴 한 줄입니다 (example.com)" for i in range(40)]
    text = "\n".join(lines)
    assert collapse_menu_runs(text) == text


# ── 인코딩 ───────────────────────────────────────────────────────────────


def test_charset_선언이_없으면_UTF8_로_읽는다(monkeypatch, fake_dns):
    """requests 는 charset 이 없으면 RFC 대로 ISO-8859-1 로 읽어 한글이 깨진다.

    실측: docs.python.org 제목이 'ipaddress â€" IPv4/IPv6…' 로 나왔다.
    """
    fake_dns["nocharset.example.com"] = "93.184.216.34"
    html = "<html><head><title>대한민국 — 위키</title></head><body><p>본문</p></body></html>"
    _install_fake_session(
        monkeypatch,
        {
            "https://nocharset.example.com/": _FakeResult(
                status=200,
                headers={"Content-Type": "text/html"},  # charset 없음
                text=html.encode("utf-8").decode("latin-1"),  # requests 의 오해석을 재현
                body=html.encode("utf-8"),
            )
        },
    )
    page = webfetch.fetch_page(object(), object(), "https://nocharset.example.com/")
    assert page.title == "대한민국 — 위키"


def test_charset_을_선언하면_서버를_믿는다(monkeypatch, fake_dns):
    fake_dns["declared.example.com"] = "93.184.216.34"
    _install_fake_session(
        monkeypatch,
        {
            "https://declared.example.com/": _FakeResult(
                status=200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                text="<html><body><p>서버가 디코드한 본문</p></body></html>",
                body=b"\xff\xfe(\xea\xb9\xa8\xec\xa7\x84 \xeb\xb0\x94\xec\x9d\xb4\xed\x8a\xb8)",
            )
        },
    )
    page = webfetch.fetch_page(object(), object(), "https://declared.example.com/")
    assert page.text == "서버가 디코드한 본문"


# ── 처음 보는 사이트도 동작한다 (허용목록이 없다) ─────────────────────────


@pytest.mark.parametrize(
    "host", ["random-blog.example.org", "docs.somewhere.co.kr", "news.unknown.io"]
)
def test_모르는_도메인도_공개_주소면_읽는다(host, monkeypatch, fake_dns):
    """허용목록(allowlist)은 두지 않는다 — 안전 경계는 IP 이지 도메인이 아니다."""
    fake_dns[host] = "93.184.216.34"
    url = f"https://{host}/page"
    _install_fake_session(
        monkeypatch,
        {url: _FakeResult(status=200, headers={"Content-Type": "text/html"}, body=b"<p>hello</p>")},
    )
    assert webfetch.fetch_page(object(), object(), url).text == "hello"
