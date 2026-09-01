"""ActivityWatch REST API 클라이언트.

`aw-client` 패키지를 쓰지 않는다. `requests` 로 직접 REST 를 친다.
이유(`docs/research/activitywatch.md §6`): PyPI `aw-client==0.5.15` 는
Authorization 헤더를 보낼 코드 경로 자체가 없어 API 키 인증을 지원하지 못한다.
원격 폴러에 필요한 표면(`/buckets/`, `/buckets/<id>/events`)도 단순해서
무거운 의존성(persist-queue, aw-core, tomlkit)을 끌고 올 이유가 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from lifetrainer.timeutil import iso_utc, parse_iso

logger = logging.getLogger(__name__)


class AWError(Exception):
    """ActivityWatch 서버와의 통신 실패를 감싼다."""


@dataclass(frozen=True)
class AWEvent:
    """ActivityWatch 이벤트 하나.

    `ts` 는 시작 시각(epoch UTC), `duration` 은 초 단위 길이다.
    """

    bucket_id: str
    ts: float  # epoch UTC
    duration: float  # 초
    data: dict
    event_id: int | None = None

    @property
    def ts_end(self) -> float:
        """종료 시각 = 시작 + duration."""
        return self.ts + self.duration


class AWClient:
    """ActivityWatch aw-server-rust 의 REST API 를 직접 호출하는 얇은 클라이언트."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout: float = 10.0,
        user_agent: str = "LifeTrainer/0.1",
        session: "requests.Session | None" = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._user_agent = user_agent
        self._session = session if session is not None else requests.Session()

    def _headers(self, *, auth: bool) -> dict[str, str]:
        headers = {"User-Agent": self._user_agent}
        if auth and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _get(self, path: str, *, params: dict[str, Any] | None = None, auth: bool = True) -> Any:
        url = f"{self._base_url}{path}"
        try:
            resp = self._session.get(
                url, params=params, headers=self._headers(auth=auth), timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise AWError(f"ActivityWatch 요청 실패 ({url}): {exc}") from exc

        if resp.status_code >= 400:
            raise AWError(
                f"ActivityWatch 요청이 {resp.status_code} 를 반환함 ({url}): {resp.text[:500]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise AWError(f"ActivityWatch 응답이 JSON 이 아님 ({url}): {exc}") from exc

    def ping(self) -> bool:
        """서버가 살아 있는지 확인한다. 예외를 삼키고 bool 만 반환한다."""
        try:
            self.info()
            return True
        except AWError:
            return False

    def info(self) -> dict:
        """`GET /api/0/info` — 인증 면제 헬스체크 엔드포인트."""
        return self._get("/api/0/info", auth=False)

    def delete_event(self, bucket_id: str, event_id: int) -> None:
        """`DELETE /api/0/buckets/<id>/events/<event_id>` — **한 건씩만** 지울 수 있다.

        aw-server 에 기간 단위 일괄 삭제가 없다
        ([aw-server#55](https://github.com/ActivityWatch/aw-server/issues/55)).
        그래서 프라이빗 구간 정리는 `events()` 로 받아 하나씩 돈다.

        ★ `event_id` 는 **서버 응답에서 온 것**을 써야 한다. 우리 `aw_event.event_id`
          는 스키마 주석이 "참고용, 신뢰하지 않는다"라고 적어 둔 값이다.
        """
        url = f"{self._base_url}/api/0/buckets/{bucket_id}/events/{event_id}"
        try:
            resp = self._session.delete(url, headers=self._headers(auth=True), timeout=self._timeout)
        except requests.RequestException as exc:
            raise AWError(f"ActivityWatch 삭제 실패 ({url}): {exc}") from exc
        # 404 는 이미 없다는 뜻이라 성공으로 친다 — 두 번 돌려도 같은 결과여야 한다.
        if resp.status_code >= 400 and resp.status_code != 404:
            raise AWError(f"ActivityWatch 삭제가 {resp.status_code} 를 반환함 ({url}): {resp.text[:200]}")

    def buckets(self) -> dict[str, dict]:
        """`GET /api/0/buckets/` — {bucket_id: Bucket 메타} 딕셔너리."""
        return self._get("/api/0/buckets/")

    def events(
        self,
        bucket_id: str,
        *,
        start: float | None = None,
        end: float | None = None,
        limit: int = -1,
    ) -> list[AWEvent]:
        """`GET /api/0/buckets/<id>/events` — start/end 는 RFC3339 문자열로 보낸다.

        epoch 숫자를 그대로 주면 서버가 400 을 준다 (조사 문서 §2).
        반환은 항상 ts 오름차순으로 정렬한다.
        """
        params: dict[str, Any] = {"limit": limit}
        if start is not None:
            params["start"] = iso_utc(start)
        if end is not None:
            params["end"] = iso_utc(end)

        raw = self._get(f"/api/0/buckets/{bucket_id}/events", params=params)
        events = [
            AWEvent(
                bucket_id=bucket_id,
                ts=parse_iso(item["timestamp"]),
                duration=float(item.get("duration", 0.0)),
                data=dict(item.get("data") or {}),
                event_id=item.get("id"),
            )
            for item in raw
        ]
        events.sort(key=lambda e: e.ts)
        return events
