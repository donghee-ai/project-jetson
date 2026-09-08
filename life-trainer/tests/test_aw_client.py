"""lifetrainer.collect.aw_client 테스트.

네트워크를 전혀 타지 않는다 — `requests.Session` 을 흉내내는 페이크 객체를 주입한다.
"""

from __future__ import annotations

import json

import pytest
import requests

from lifetrainer.collect.aw_client import AWClient, AWError, AWEvent
from lifetrainer.timeutil import parse_iso


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text if text else json.dumps(json_body) if json_body is not None else ""

    def json(self):
        if self._json_body is None:
            raise ValueError("본문이 JSON 이 아님")
        return self._json_body


class _FakeSession:
    """`requests.Session` 대역. `routes` 딕셔너리(path -> FakeResponse|callable)로 응답을 정한다."""

    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}
        self.calls: list[dict] = []
        self.raise_on: set[str] = set()

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        for path, resp in self.routes.items():
            if path in url:
                if path in self.raise_on:
                    raise requests.exceptions.ConnectionError("연결 실패(fake)")
                if callable(resp):
                    return resp(params)
                return resp
        return _FakeResponse(status_code=404, text="not found")


BASE = "http://100.64.0.5:5600"


# ── info / ping — 인증 면제 ─────────────────────────────────────────────


def test_info_hits_info_endpoint_without_auth_header():
    session = _FakeSession({"/api/0/info": _FakeResponse(json_body={"version": "0.14.0b3"})})
    client = AWClient(BASE, api_key="secret-key", session=session)
    info = client.info()
    assert info["version"] == "0.14.0b3"
    call = session.calls[0]
    assert call["url"] == f"{BASE}/api/0/info"
    assert "Authorization" not in call["headers"]


def test_ping_true_on_success():
    session = _FakeSession({"/api/0/info": _FakeResponse(json_body={"version": "x"})})
    client = AWClient(BASE, session=session)
    assert client.ping() is True


def test_ping_false_swallows_exception():
    session = _FakeSession({"/api/0/info": _FakeResponse(status_code=500, text="boom")})
    client = AWClient(BASE, session=session)
    assert client.ping() is False


# ── 인증 헤더 ────────────────────────────────────────────────────────────


def test_buckets_sends_bearer_auth_when_api_key_set():
    session = _FakeSession({"/api/0/buckets/": _FakeResponse(json_body={})})
    client = AWClient(BASE, api_key="secret-key", session=session)
    client.buckets()
    call = session.calls[0]
    assert call["headers"]["Authorization"] == "Bearer secret-key"


def test_buckets_no_auth_header_when_api_key_empty():
    session = _FakeSession({"/api/0/buckets/": _FakeResponse(json_body={})})
    client = AWClient(BASE, api_key="", session=session)
    client.buckets()
    call = session.calls[0]
    assert "Authorization" not in call["headers"]


def test_buckets_returns_dict():
    payload = {"aw-watcher-window_pc": {"id": "aw-watcher-window_pc", "type": "currentwindow"}}
    session = _FakeSession({"/api/0/buckets/": _FakeResponse(json_body=payload)})
    client = AWClient(BASE, session=session)
    assert client.buckets() == payload


# ── events — RFC3339 start/end, 정렬 ────────────────────────────────────


def test_events_sends_start_end_as_rfc3339_not_epoch():
    captured = {}

    def _handler(params):
        captured.update(params)
        return _FakeResponse(json_body=[])

    session = _FakeSession({"/events": _handler})
    client = AWClient(BASE, session=session)
    client.events("aw-watcher-window_pc", start=1755305000.0, end=1755308600.0, limit=100)

    assert isinstance(captured["start"], str)
    assert isinstance(captured["end"], str)
    # RFC3339 문자열이 다시 같은 epoch 로 파싱돼야 한다 (숫자를 그대로 보내지 않았다는 증거)
    assert parse_iso(captured["start"]) == pytest.approx(1755305000.0)
    assert parse_iso(captured["end"]) == pytest.approx(1755308600.0)
    assert captured["limit"] == 100


def test_events_parses_and_sorts_ascending():
    raw = [
        {"id": 2, "timestamp": "2026-08-16T01:00:10.000000Z", "duration": 5.0, "data": {"app": "b.exe"}},
        {"id": 1, "timestamp": "2026-08-16T01:00:00.000000Z", "duration": 10.0, "data": {"app": "a.exe"}},
    ]
    session = _FakeSession({"/events": _FakeResponse(json_body=raw)})
    client = AWClient(BASE, session=session)
    events = client.events("aw-watcher-window_pc")

    assert [e.event_id for e in events] == [1, 2]
    assert events[0].data["app"] == "a.exe"
    assert events[0].ts < events[1].ts
    assert isinstance(events[0], AWEvent)
    assert events[0].ts_end == pytest.approx(events[0].ts + events[0].duration)


def test_events_bucket_id_and_url_path():
    session = _FakeSession({"/api/0/buckets/my-bucket/events": _FakeResponse(json_body=[])})
    client = AWClient(BASE, session=session)
    client.events("my-bucket")
    assert session.calls[0]["url"] == f"{BASE}/api/0/buckets/my-bucket/events"


# ── 오류 처리 ────────────────────────────────────────────────────────────


def test_http_error_raises_aw_error():
    session = _FakeSession({"/api/0/buckets/": _FakeResponse(status_code=500, text="server error")})
    client = AWClient(BASE, session=session)
    with pytest.raises(AWError):
        client.buckets()


def test_connection_error_raises_aw_error():
    session = _FakeSession({"/api/0/buckets/": _FakeResponse(json_body={})})
    session.raise_on.add("/api/0/buckets/")
    client = AWClient(BASE, session=session)
    with pytest.raises(AWError):
        client.buckets()


def test_non_json_response_raises_aw_error():
    session = _FakeSession({"/api/0/buckets/": _FakeResponse(status_code=200, text="not json at all")})
    client = AWClient(BASE, session=session)
    with pytest.raises(AWError):
        client.buckets()
