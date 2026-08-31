"""예의 바른 HTTP 계층 — "같은 사이트 10번 방문" 문제를 구조적으로 없앤다.

설계서(`docs/life-trainer-design.md` §6)를 그대로 코드로 옮긴 것이다:

1. **도메인별 토큰 버킷.** 전역 큐가 아니라 도메인별로 최소 간격을 강제한다
   (동시 연결 1 은 도메인별 락으로 보장).
2. **조건부 GET.** `url_state` 의 `etag`/`last_modified` 를 요청 헤더로 보내고,
   304 면 본문 없이 끝낸다 — 재방문 대부분이 여기서 공짜가 된다.
3. **적응형 주기.** 3회 연속 무변경이면 주기를 2배로, 변경이 감지되면 절반으로.
4. **지수 백오프.** 실패가 쌓이면 다음 시도까지의 간격을 지수적으로 늘리고,
   `max_fail_before_disable` 을 넘기면 사실상 재시도를 멈춘다.
5. **robots.txt 준수.** `robots_cache` 에 도메인당 하루 캐시하고, robots.txt 를
   가져오는 요청 자체도 같은 도메인 레이트리밋을 통과시킨다.

수집기(`feeds.py`, `arxiv.py`)는 `requests` 를 직접 쓰지 않고 전부 이 클래스를 통과한다.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from lifetrainer.collect.dedupe import sha256_hex
from lifetrainer.config import Config

logger = logging.getLogger(__name__)

# 적응형 주기 상한/하한 (초)
ADAPTIVE_MAX_INTERVAL_SEC = 7 * 86400
ADAPTIVE_MIN_INTERVAL_SEC = 600
UNCHANGED_STREAK_THRESHOLD = 3

# 실패 백오프 상한 (초) 및 기본 단위
BACKOFF_BASE_SEC = 60.0
BACKOFF_MAX_SEC = 24 * 3600.0
# max_fail_before_disable 을 넘으면 사실상 비활성화하기 위해 아주 멀리 미룬다.
DISABLE_PUSH_SEC = 180 * 86400.0

DEFAULT_TIMEOUT_SEC = 15.0
DEFAULT_URL_INTERVAL_SEC = 21600  # schema.sql 의 url_state.interval_sec 기본값과 동일
ROBOTS_CACHE_TTL_SEC = 86400.0


@dataclass
class FetchResult:
    """`PoliteSession.get()` 의 결과. 예외 대신 이 안의 `error` 로 실패를 전달한다."""

    url: str
    status: int  # 304 면 not_modified. robots 차단·예외는 0.
    body: bytes | None
    text: str | None
    headers: dict
    from_cache: bool  # 304 로 본문 없이 끝난 경우 True
    elapsed_ms: int
    error: str | None = None


def _domain_of(url: str) -> str:
    """URL 에서 레이트리밋 키로 쓸 도메인(호스트, 포트 제외)을 뽑는다."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return ""
    return (host or "").lower()


class PoliteSession:
    """도메인별 토큰 버킷 + 조건부 GET + robots.txt 를 강제하는 HTTP 계층.

    수집기는 requests 를 직접 쓰지 않는다. 전부 이 클래스를 통과한다.
    """

    def __init__(self, cfg: Config, conn, *, session: "requests.Session | None" = None) -> None:
        self._cfg = cfg
        self._conn = conn
        # 테스트에서 실제 네트워크를 타지 않도록 세션을 주입할 수 있게 한다.
        self._session = session if session is not None else requests.Session()
        self._last_request: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

    # ── 도메인 레이트리밋 ────────────────────────────────────────────────

    def _min_interval_for(self, domain: str) -> float:
        if domain.endswith("arxiv.org"):
            return self._cfg.collect.arxiv_min_interval_sec
        return self._cfg.collect.per_domain_min_interval_sec

    def _throttle(self, domain: str) -> None:
        """도메인당 최소 간격을 강제한다. 동시 연결도 도메인별 락으로 1로 제한한다."""
        if not domain:
            return
        lock = self._locks[domain]
        lock.acquire()
        try:
            min_interval = self._min_interval_for(domain)
            last = self._last_request.get(domain)
            now = time.monotonic()
            if last is not None:
                wait = min_interval - (now - last)
                if wait > 0:
                    time.sleep(wait)
            self._last_request[domain] = time.monotonic()
        finally:
            lock.release()

    # ── robots.txt ──────────────────────────────────────────────────────

    def allowed(self, url: str) -> bool:
        """robots.txt 상 이 URL 을 가져올 수 있는지 (전역 설정 `cfg.collect.respect_robots` 기준).

        `robots_cache` 에 도메인당 캐시한다. 소스 단위로 이 판단을 우회해야 하면
        (예: arXiv 공식 API) `get()` 의 `respect_robots` 인자를 쓴다 — 이 메서드 자체는
        항상 전역 설정을 기준으로 답한다.
        """
        if not self._cfg.collect.respect_robots:
            return True
        return self._robots_permits(url)

    def _robots_permits(self, url: str) -> bool:
        """robots.txt 규칙만으로 판단한다 (전역 `respect_robots` 설정과 무관하게)."""
        domain = _domain_of(url)
        if not domain:
            return True
        parser = self._get_robots_parser(domain, url)
        try:
            return parser.can_fetch(self._cfg.collect.user_agent, url)
        except Exception as exc:  # noqa: BLE001 - robots 판정 실패는 관대하게(허용)
            logger.debug("robots.txt 판정 실패, 허용으로 처리 (%s): %s", url, exc)
            return True

    def _get_robots_parser(self, domain: str, sample_url: str) -> urllib.robotparser.RobotFileParser:
        now = time.time()
        row = self._conn.execute(
            "SELECT body, expires_at FROM robots_cache WHERE domain = ?", (domain,)
        ).fetchone()
        if row is not None and row["expires_at"] > now:
            body = row["body"] or ""
        else:
            scheme = urlsplit(sample_url).scheme or "https"
            robots_url = f"{scheme}://{domain}/robots.txt"
            body, status = self._fetch_robots_body(robots_url, domain)
            self._conn.execute(
                "INSERT INTO robots_cache(domain, body, fetched_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET body=excluded.body, fetched_at=excluded.fetched_at, "
                "expires_at=excluded.expires_at, status=excluded.status",
                (domain, body, now, now + ROBOTS_CACHE_TTL_SEC, status),
            )
        parser = urllib.robotparser.RobotFileParser()
        parser.parse((body or "").splitlines())
        return parser

    def _fetch_robots_body(self, robots_url: str, domain: str) -> tuple[str, int]:
        """robots.txt 자체도 도메인 레이트리밋을 통과시켜 가져온다."""
        self._throttle(domain)
        try:
            resp = self._session.get(
                robots_url,
                headers={"User-Agent": self._cfg.collect.user_agent},
                timeout=DEFAULT_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001 - robots 못 가져와도 전체 흐름은 계속
            logger.debug("robots.txt 요청 실패, 전체 허용으로 처리 (%s): %s", robots_url, exc)
            return "", 0
        if resp.status_code == 200:
            try:
                return resp.text, resp.status_code
            except Exception:  # noqa: BLE001 - 인코딩 문제
                return "", resp.status_code
        # 404 등 robots.txt 가 없으면 관례상 전체 허용으로 취급한다.
        return "", resp.status_code

    # ── 조건부 GET / 적응형 주기 / 백오프 상태 ─────────────────────────────

    def _load_url_state(self, url: str, domain: str):
        row = self._conn.execute("SELECT * FROM url_state WHERE url = ?", (url,)).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT OR IGNORE INTO url_state(url, domain, next_fetch_at, interval_sec) "
                "VALUES (?, ?, 0, ?)",
                (url, domain, DEFAULT_URL_INTERVAL_SEC),
            )
            row = self._conn.execute("SELECT * FROM url_state WHERE url = ?", (url,)).fetchone()
        return row

    def _on_not_modified(self, url: str, state) -> None:
        now = time.time()
        unchanged_streak = int(state["unchanged_streak"]) + 1
        interval = int(state["interval_sec"]) or DEFAULT_URL_INTERVAL_SEC
        if unchanged_streak >= UNCHANGED_STREAK_THRESHOLD:
            interval = min(interval * 2, ADAPTIVE_MAX_INTERVAL_SEC)
        self._conn.execute(
            "UPDATE url_state SET last_fetched=?, next_fetch_at=?, last_status=304, "
            "fail_count=0, unchanged_streak=?, interval_sec=? WHERE url=?",
            (now, now + interval, unchanged_streak, interval, url),
        )

    def _on_success(self, url: str, resp: "requests.Response", body: bytes, state) -> None:
        now = time.time()
        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        content_hash = sha256_hex(body) if body else None
        prev_hash = state["content_hash"]
        changed = content_hash != prev_hash
        interval = int(state["interval_sec"]) or DEFAULT_URL_INTERVAL_SEC

        if changed:
            interval = max(interval // 2, ADAPTIVE_MIN_INTERVAL_SEC)
            unchanged_streak = 0
        else:
            unchanged_streak = int(state["unchanged_streak"]) + 1
            if unchanged_streak >= UNCHANGED_STREAK_THRESHOLD:
                interval = min(interval * 2, ADAPTIVE_MAX_INTERVAL_SEC)

        self._conn.execute(
            "UPDATE url_state SET last_fetched=?, next_fetch_at=?, etag=?, last_modified=?, "
            "content_hash=?, last_status=?, fail_count=0, unchanged_streak=?, interval_sec=? "
            "WHERE url=?",
            (
                now,
                now + interval,
                etag,
                last_modified,
                content_hash,
                resp.status_code,
                unchanged_streak,
                interval,
                url,
            ),
        )

    def _on_failure(self, url: str, state, status: int | None) -> None:
        now = time.time()
        fail_count = int(state["fail_count"]) + 1
        delay = min(BACKOFF_BASE_SEC * (2 ** (fail_count - 1)), BACKOFF_MAX_SEC)
        next_fetch_at = now + delay
        if fail_count > self._cfg.collect.max_fail_before_disable:
            next_fetch_at = now + DISABLE_PUSH_SEC
        self._conn.execute(
            "UPDATE url_state SET last_fetched=?, next_fetch_at=?, last_status=?, fail_count=? "
            "WHERE url=?",
            (now, next_fetch_at, status, fail_count, url),
        )

    # ── 메인 진입점 ─────────────────────────────────────────────────────

    def get(
        self,
        url: str,
        *,
        headers: dict | None = None,
        timeout: float | None = None,
        use_state: bool = True,
        respect_robots: bool | None = None,
        allow_redirects: bool = True,
    ) -> FetchResult:
        """URL 을 예의 바르게 가져온다. 예외를 올리지 않고 전부 `FetchResult.error` 에 담는다.

        `respect_robots` 로 호출부가 소스 단위 정책을 넘길 수 있다. `None`(기본값)이면
        전역 설정(`cfg.collect.respect_robots`)을 따른다. 명시적으로 `True`/`False` 를
        주면 그 값이 이 호출에 한해 전역 설정을 덮어쓴다 (예: arXiv 공식 API 예외).

        `allow_redirects=False` 는 **홉마다 목적지를 직접 검증해야 하는 호출부**를 위한
        것이다(`llm/webfetch.py`). 자동으로 따라가면 첫 URL 만 검사한 SSRF 방어가
        리다이렉트 한 번에 무력화된다 — 공개 도메인이 사설/Tailscale 주소로 넘길 수 있다.
        """
        start = time.monotonic()
        domain = _domain_of(url)
        req_headers = dict(headers or {})
        req_headers.setdefault("User-Agent", self._cfg.collect.user_agent)

        effective_respect_robots = (
            self._cfg.collect.respect_robots if respect_robots is None else respect_robots
        )
        if effective_respect_robots and not self._robots_permits(url):
            logger.info("robots.txt 에 의해 요청 차단: %s", url)
            return FetchResult(
                url=url,
                status=0,
                body=None,
                text=None,
                headers={},
                from_cache=False,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error="robots_disallowed",
            )

        state = self._load_url_state(url, domain) if use_state else None
        if state is not None:
            if state["etag"]:
                req_headers["If-None-Match"] = state["etag"]
            if state["last_modified"]:
                req_headers["If-Modified-Since"] = state["last_modified"]

        self._throttle(domain)

        try:
            resp = self._session.get(
                url,
                headers=req_headers,
                timeout=timeout or DEFAULT_TIMEOUT_SEC,
                allow_redirects=allow_redirects,
            )
        except Exception as exc:  # noqa: BLE001 - 타임아웃/리다이렉트/연결오류 전부 여기로
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("요청 실패 (%s): %s", url, error)
            if state is not None:
                self._on_failure(url, state, status=None)
            return FetchResult(
                url=url,
                status=0,
                body=None,
                text=None,
                headers={},
                from_cache=False,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=error,
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code == 304:
            if state is not None:
                self._on_not_modified(url, state)
            return FetchResult(
                url=url,
                status=304,
                body=None,
                text=None,
                headers=dict(resp.headers),
                from_cache=True,
                elapsed_ms=elapsed_ms,
                error=None,
            )

        if 200 <= resp.status_code < 300:
            body = resp.content
            try:
                text = resp.text
            except Exception as exc:  # noqa: BLE001 - 인코딩 오류 방어
                logger.debug("응답 디코딩 실패 (%s): %s", url, exc)
                text = None
            if state is not None:
                self._on_success(url, resp, body, state)
            return FetchResult(
                url=url,
                status=resp.status_code,
                body=body,
                text=text,
                headers=dict(resp.headers),
                from_cache=False,
                elapsed_ms=elapsed_ms,
                error=None,
            )

        # 4xx / 5xx
        if state is not None:
            self._on_failure(url, state, status=resp.status_code)
        return FetchResult(
            url=url,
            status=resp.status_code,
            body=None,
            text=None,
            headers=dict(resp.headers),
            from_cache=False,
            elapsed_ms=elapsed_ms,
            error=f"http_{resp.status_code}",
        )
