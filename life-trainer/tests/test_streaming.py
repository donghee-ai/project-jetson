"""스트리밍 — 생성 중인 답을 조각으로 흘린다.

★ 지키려는 성질 넷:
  ① 조립본이 비스트리밍 응답과 **같다** (호출부가 조립을 다시 하지 않는다)
  ② 툴 콜 조각은 사용자에게 안 나간다
  ③ thinking 이 켜져 있으면 스트리밍하지 않는다 (사고 텍스트 유출)
  ④ Slack 갱신이 실패해도 답을 잃지 않는다
"""

from __future__ import annotations

import json

from lifetrainer.llm.client import _consume_stream


class _Resp:
    def __init__(self, chunks):
        self._chunks = chunks

    def iter_lines(self, decode_unicode=False):  # noqa: ANN001, FBT002
        for c in self._chunks:
            line = f"data: {json.dumps(c, ensure_ascii=False)}"
            yield line.encode("utf-8") if not decode_unicode else line
        yield b"data: [DONE]"


def _delta(**d):
    return {"choices": [{"delta": d}]}


def test_본문_조각을_이어_붙인다():
    got = []
    raw = _consume_stream(_Resp([_delta(content="젯"), _delta(content="슨 "), _delta(content="오린")]), got.append)
    assert raw["choices"][0]["message"]["content"] == "젯슨 오린"
    assert got == ["젯", "슨 ", "오린"]


def test_한글이_깨지지_않는다():
    """SSE 에 charset 이 없으면 requests 가 ISO-8859-1 로 준다 — 실측으로 깨졌다."""
    got = []
    raw = _consume_stream(_Resp([_delta(content="제이슨 오린은 미국의")]), got.append)
    assert raw["choices"][0]["message"]["content"] == "제이슨 오린은 미국의"


def test_툴콜_조각은_사용자에게_안_나간다():
    got = []
    raw = _consume_stream(
        _Resp(
            [
                _delta(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "search_docs"}}]),
                _delta(tool_calls=[{"index": 0, "function": {"arguments": '{"query":'}}]),
                _delta(tool_calls=[{"index": 0, "function": {"arguments": '"jetson"}'}}]),
            ]
        ),
        got.append,
    )
    assert got == []  # 화면에 아무것도 안 나간다
    call = raw["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "search_docs"
    assert json.loads(call["function"]["arguments"]) == {"query": "jetson"}


def test_툴콜_인자가_순서대로_이어진다():
    """서버가 인자를 여러 조각으로 쪼갠다. 순서가 어긋나면 JSON 이 깨진다."""
    raw = _consume_stream(
        _Resp(
            [
                _delta(tool_calls=[{"index": 1, "function": {"arguments": '{"a":'}}]),
                _delta(tool_calls=[{"index": 0, "function": {"name": "first"}}]),
                _delta(tool_calls=[{"index": 1, "function": {"arguments": "1}"}}]),
                _delta(tool_calls=[{"index": 1, "function": {"name": "second"}}]),
            ]
        ),
        lambda _s: None,
    )
    calls = raw["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["first", "second"]
    assert json.loads(calls[1]["function"]["arguments"]) == {"a": 1}


def test_깨진_줄_하나가_턴_전체를_버리지_않는다():
    class _Broken(_Resp):
        def iter_lines(self, decode_unicode=False):  # noqa: ANN001, FBT002
            yield b"data: {not json"
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}'
            yield b"data: [DONE]"

    raw = _consume_stream(_Broken([]), lambda _s: None)
    assert raw["choices"][0]["message"]["content"] == "ok"


def test_usage_를_주워_담는다():
    raw = _consume_stream(
        _Resp([_delta(content="a"), {"choices": [], "usage": {"prompt_tokens": 7}}]),
        lambda _s: None,
    )
    assert raw["usage"]["prompt_tokens"] == 7


# ── Slack 쪽 ──────────────────────────────────────────────────────────────


class _FakeSlack:
    def __init__(self, fail_update=False):
        self.posts, self.updates, self._fail = [], [], fail_update

    def chat_postMessage(self, channel, text, **kw):  # noqa: ANN001
        self.posts.append(text)
        return {"ts": "111.222"}

    def chat_update(self, channel, ts, text):  # noqa: ANN001
        if self._fail:
            raise RuntimeError("rate limited")
        self.updates.append(text)


def test_최종본이_중간_글을_덮어쓴다():
    """재생성·반복 감지가 본문을 바꾼다. 중간 글을 그대로 두면 안 된다."""
    from lifetrainer.slackio.app import _Streamer

    slack = _FakeSlack()
    s = _Streamer(slack, "C1", None)
    s.on_progress("초안입니다")
    assert s.finish("최종 답입니다") is True
    assert slack.updates[-1] == "최종 답입니다"


def test_라운드_시작의_빈_문자열은_무시한다():
    from lifetrainer.slackio.app import _Streamer

    slack = _FakeSlack()
    s = _Streamer(slack, "C1", None)
    s.on_progress("")
    assert slack.posts == []


def test_갱신이_실패하면_평소_경로로_넘긴다():
    """스트리밍은 편의 기능이다. 이것 때문에 답을 잃으면 안 된다."""
    from lifetrainer.slackio.app import _Streamer

    slack = _FakeSlack(fail_update=True)
    s = _Streamer(slack, "C1", None)
    s.on_progress("일부")
    assert s.finish("최종") is False  # 호출부가 _safe_say 로 답한다


def test_갱신_간격을_지킨다():
    """chat.update 는 Tier 3 다. 촘촘히 치면 429 로 갱신이 아예 끊긴다."""
    from lifetrainer.slackio import app as A

    slack = _FakeSlack()
    s = A._Streamer(slack, "C1", None)
    s.on_progress("첫 글")          # 게시
    s.on_progress("첫 글 더 길게")   # 간격 안이라 무시
    assert len(slack.posts) == 1 and slack.updates == []
