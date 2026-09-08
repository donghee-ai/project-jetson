"""lifetrainer.slackio.notify 테스트. 네트워크 절대 금지 — 가짜 WebClient 를 주입한다.

`SlackNotifier.__init__` 은 계약서상 `cfg` 하나만 받으므로, 테스트에서는
생성 후 `notifier.client` 속성을 페이크로 바꿔치기하는 방식으로 주입한다.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError

from lifetrainer import db
from lifetrainer.slackio import notify as notify_mod
from lifetrainer.slackio.notify import SlackError, SlackNotifier


# ── 픽스처 ────────────────────────────────────────────────────────────


def make_cfg(*, bot_token: str = "xoxb-test", default_channel: str = "C_DEFAULT", mode: str = "notify"):
    slack = SimpleNamespace(
        mode=mode,
        bot_token=bot_token,
        app_token="xapp-test",
        default_channel=default_channel,
        openclaw_config=Path("/nonexistent/openclaw.json"),
    )
    return SimpleNamespace(slack=slack)


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    yield c
    c.close()


class FakeSlackResponse:
    def __init__(self, status_code: int, headers: dict | None = None, data: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __str__(self) -> str:
        # 실제 SlackResponse 는 본문 dict 를 문자열로 보여준다. 이게 없으면
        # `SlackApiError` 메시지에 객체 주소만 찍혀서, 오류 문구를 보고 분기하는
        # 코드가 테스트에서만 다르게 동작한다 (가짜가 실물보다 착해지는 경우).
        return str(self.data)


class FakeUploadResponse:
    """`slack_sdk.web.SlackResponse` 를 흉내낸다 — **dict 가 아니다.**

    실제 SDK 는 dict 가 아니라 이 모양의 객체를 돌려준다. 가짜가 dict 를 돌려주게
    두었더니 `dict(resp)` 가 테스트에서만 통과하고 실기기에서 `ValueError` 로
    터졌다 (`HISTORY/2026-08-17-upload-png-slackresponse.md`). payload 는 `.data`
    에만 있고, 이 객체를 dict() 로 감싸면 실제와 똑같이 실패해야 한다.
    """

    def __init__(self, data: dict) -> None:
        self.data = data

    def __getitem__(self, key):  # SlackResponse 도 첨자 접근을 지원한다
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __iter__(self):
        # 실제 SlackResponse 는 페이지네이션 이터레이터라 dict() 재료로 쓸 수 없다.
        raise TypeError("SlackResponse 를 dict() 로 감싸면 안 된다 — .data 를 쓸 것")


class FakeWebClient:
    """호출을 전부 기록하고, 미리 설정된 결과를 돌려주는 가짜 클라이언트."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.conversations_open_result = {"channel": {"id": "D999"}}
        self.files_upload_v2_result = FakeUploadResponse({"ok": True, "file": {"id": "F123"}})
        self.chat_postMessage_result = {"ok": True, "ts": "1000.001"}
        # (method_name -> 남은 예외 큐) 429 재시도 등을 시뮬레이션할 때 사용
        self._raise_queue: dict[str, list[Exception]] = {}

    def queue_error(self, method: str, exc: Exception) -> None:
        self._raise_queue.setdefault(method, []).append(exc)

    def _maybe_raise(self, method: str) -> None:
        q = self._raise_queue.get(method)
        if q:
            raise q.pop(0)

    def conversations_open(self, **kwargs):
        self.calls.append(("conversations_open", kwargs))
        self._maybe_raise("conversations_open")
        return self.conversations_open_result

    def files_upload_v2(self, **kwargs):
        self.calls.append(("files_upload_v2", kwargs))
        self._maybe_raise("files_upload_v2")
        return self.files_upload_v2_result

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat_postMessage", kwargs))
        self._maybe_raise("chat_postMessage")
        return self.chat_postMessage_result

    @property
    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]


# ── 토큰 없음 -> no-op ───────────────────────────────────────────────


def test_no_token_means_disabled():
    notifier = SlackNotifier(make_cfg(bot_token=""))
    assert notifier.enabled is False
    assert notifier.client is None


def test_disabled_post_is_silent_noop(caplog):
    notifier = SlackNotifier(make_cfg(bot_token=""))
    with caplog.at_level("WARNING"):
        result = notifier.post("hello")
    assert result == ""
    assert "비활성화" in caplog.text


def test_disabled_upload_png_is_silent_noop(tmp_path):
    notifier = SlackNotifier(make_cfg(bot_token=""))
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG")
    result = notifier.upload_png(png, title="t")
    assert result == {}
def test_disabled_notifier_even_with_client_assigned_stays_disabled():
    # 토큰이 없으면 client 를 나중에 채워도 enabled 는 False 여야 한다.
    notifier = SlackNotifier(make_cfg(bot_token=""))
    notifier.client = FakeWebClient()
    assert notifier.enabled is False
    assert notifier.post("hi") == ""


# ── resolve_channel ──────────────────────────────────────────────────


def test_resolve_channel_passthrough_for_c_and_d():
    notifier = SlackNotifier(make_cfg())
    notifier.client = FakeWebClient()
    assert notifier.resolve_channel("C123") == "C123"
    assert notifier.resolve_channel("D456") == "D456"


def test_resolve_channel_empty_falls_back_to_default():
    notifier = SlackNotifier(make_cfg(default_channel="C_DEFAULT"))
    notifier.client = FakeWebClient()
    assert notifier.resolve_channel(None) == "C_DEFAULT"
    assert notifier.resolve_channel("") == "C_DEFAULT"


def test_resolve_channel_converts_user_id_to_dm_channel():
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake
    result = notifier.resolve_channel("U0001")
    assert result == "D999"
    assert fake.call_names == ["conversations_open"]
    assert fake.calls[0][1]["users"] == "U0001"


def test_resolve_channel_caches_user_id_lookup():
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake
    notifier.resolve_channel("U0001")
    notifier.resolve_channel("U0001")
    assert fake.call_names.count("conversations_open") == 1


# ── post_report: 업로드 -> 게시 순서, slack_file 삽입 ─────────────────


def _insert_report_row(conn, **overrides) -> int:
    row = dict(
        kind="daily",
        day="2026-08-16",
        text="오늘 리포트입니다",
        blocks_json="[]",
        png_path=None,
        created_at=1000.0,
    )
    row.update(overrides)
    cur = conn.execute(
        "INSERT INTO report(kind, day, text, blocks_json, png_path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (row["kind"], row["day"], row["text"], row["blocks_json"], row["png_path"], row["created_at"]),
    )
    conn.commit()
    return cur.lastrowid


def test_post_report_uploads_png_before_posting_and_embeds_file_id(conn, tmp_path):
    png = tmp_path / "report.png"
    png.write_bytes(b"\x89PNG\r\n")
    report_id = _insert_report_row(conn, png_path=str(png))

    built_report = SimpleNamespace(
        kind="daily",
        day="2026-08-16",
        text="오늘 리포트입니다",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "요약"}}],
        png_path=png,
        report_id=report_id,
    )

    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake

    ts = notifier.post_report(conn, built_report, channel="C777")

    assert ts == "1000.001"
    # 업로드가 게시보다 먼저 호출돼야 한다
    assert fake.call_names == ["files_upload_v2", "chat_postMessage"]

    posted_blocks = fake.calls[-1][1]["blocks"]
    image_blocks = [b for b in posted_blocks if b["type"] == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["slack_file"] == {"id": "F123"}
    assert "image_url" not in image_blocks[0]

    row = conn.execute("SELECT posted_at, slack_ts, slack_channel FROM report WHERE id = ?", (report_id,)).fetchone()
    assert row["posted_at"] is not None
    assert row["slack_ts"] == "1000.001"
    assert row["slack_channel"] == "C777"


def test_post_report_without_png_skips_upload(conn):
    report_id = _insert_report_row(conn, png_path=None)
    built_report = SimpleNamespace(
        kind="daily",
        day="2026-08-16",
        text="이미지 없는 리포트",
        blocks=[],
        png_path=None,
        report_id=report_id,
    )
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake

    ts = notifier.post_report(conn, built_report, channel="C777")

    assert ts == "1000.001"
    assert fake.call_names == ["chat_postMessage"]


def test_post_report_disabled_is_noop(conn):
    report_id = _insert_report_row(conn)
    built_report = SimpleNamespace(
        kind="daily", day="2026-08-16", text="t", blocks=[], png_path=None, report_id=report_id
    )
    notifier = SlackNotifier(make_cfg(bot_token=""))
    ts = notifier.post_report(conn, built_report)
    assert ts == ""
    row = conn.execute("SELECT posted_at FROM report WHERE id = ?", (report_id,)).fetchone()
    assert row["posted_at"] is None


# ── 429 재시도 ───────────────────────────────────────────────────────


def test_post_retries_on_429_then_succeeds():
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake
    # 두 번 429 로 실패한 뒤 세 번째에 성공 (기본 max_retries=3 이므로 예산 안쪽)
    fake.queue_error("chat_postMessage", SlackApiError("ratelimited", FakeSlackResponse(429, {"Retry-After": "0"})))
    fake.queue_error("chat_postMessage", SlackApiError("ratelimited", FakeSlackResponse(429, {"Retry-After": "0"})))

    ts = notifier.post("hello", channel="C1")

    assert ts == "1000.001"
    assert fake.call_names.count("chat_postMessage") == 3


def test_post_gives_up_after_max_retries_and_raises_slack_error():
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake
    for _ in range(5):
        fake.queue_error(
            "chat_postMessage", SlackApiError("ratelimited", FakeSlackResponse(429, {"Retry-After": "0"}))
        )

    with pytest.raises(SlackError):
        notifier.post("hello", channel="C1")

    # 최초 시도 + 최대 3회 재시도 = 4번을 넘지 않아야 한다
    assert fake.call_names.count("chat_postMessage") == 4


def test_post_non_429_error_wrapped_as_slack_error_without_retry():
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake
    fake.queue_error(
        "chat_postMessage", SlackApiError("not_in_channel", FakeSlackResponse(400, {}, {"error": "not_in_channel"}))
    )

    with pytest.raises(SlackError):
        notifier.post("hello", channel="C1")

    assert fake.call_names.count("chat_postMessage") == 1


# ── alert: 쿨다운 억제 ──────────────────────────────────────────────
def _invalid_blocks_error() -> SlackApiError:
    return SlackApiError(
        "The request to the Slack API failed.",
        FakeSlackResponse(200, {}, {"ok": False, "error": "invalid_blocks"}),
    )


def _image_card() -> list[dict]:
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "플래너"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "달성률"}},
        {"type": "image", "slack_file": {"id": "F123"}, "alt_text": "플래너"},
    ]


@pytest.fixture
def _no_sleep(monkeypatch):
    """경합 재시도 대기(합계 3.5초)를 테스트에서 뺀다. 잰 시간은 코드에 남는다."""
    slept: list[float] = []
    monkeypatch.setattr(notify_mod.time, "sleep", slept.append)
    return slept


def test_image_race_retries_with_the_image_before_giving_up(_no_sleep):
    """★ 사진을 곧장 포기하지 않는다 (2026-08-24).

    한 번 거부됐다고 이미지를 빼 버리면 사용자에게 **사진 없는 카드**가 간다.
    실제로 3일에 7번 그랬고 사용자가 `/view` 를 다시 쳤다. 경합이므로 기다렸다
    다시 하면 통과한다 — 두 번째 시도에서 성공하는 경우.
    """
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake
    fake.queue_error("chat_postMessage", _invalid_blocks_error())

    ts = notifier.post("플래너 — 2026-08-17", blocks=_image_card(), channel="C1")

    assert ts == "1000.001"
    assert fake.call_names == ["chat_postMessage", "chat_postMessage"]
    resent = fake.calls[-1][1]["blocks"]
    assert [b["type"] for b in resent] == ["header", "section", "image"]  # ★ 이미지가 살아 있다
    assert _no_sleep == [0.5]  # 첫 간격만 기다렸다


def test_invalid_blocks_retries_without_image_block(_no_sleep):
    """재시도를 다 써도 거부되면 그때 이미지를 뺀다 — 카드까지 잃지는 않는다."""
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake
    for _ in range(4):  # 최초 1회 + 재시도 3회를 전부 거부
        fake.queue_error("chat_postMessage", _invalid_blocks_error())

    ts = notifier.post("플래너 — 2026-08-17", blocks=_image_card(), channel="C1")

    assert ts == "1000.001"  # 카드는 살아서 나갔다
    assert fake.call_names == ["chat_postMessage"] * 5
    retried = fake.calls[-1][1]["blocks"]
    assert [b["type"] for b in retried] == ["header", "section"]  # 이미지만 빠졌다
    assert _no_sleep == [0.5, 1.0, 2.0]  # 3.5초를 넘기지 않는다


def test_image_race_retry_does_not_swallow_a_different_error(_no_sleep):
    """재시도 중에 다른 오류가 나면 그건 경합이 아니다 — 삼키지 않고 올린다."""
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake
    fake.queue_error("chat_postMessage", _invalid_blocks_error())
    fake.queue_error(
        "chat_postMessage",
        SlackApiError(
            "channel_not_found",
            FakeSlackResponse(200, {}, {"ok": False, "error": "channel_not_found"}),
        ),
    )

    with pytest.raises(SlackError):
        notifier.post("플래너 — 2026-08-17", blocks=_image_card(), channel="C1")


def test_invalid_blocks_without_image_is_not_swallowed():
    """이미지와 무관한 invalid_blocks 는 재시도해도 같다 — 조용히 삼키면 안 된다."""
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake
    fake.queue_error("chat_postMessage", _invalid_blocks_error())

    with pytest.raises(SlackError):
        notifier.post("본문", blocks=[{"type": "header"}], channel="C1")
    assert fake.call_names == ["chat_postMessage"]  # 재시도하지 않았다


def test_other_errors_are_not_retried_without_image():
    notifier = SlackNotifier(make_cfg())
    fake = FakeWebClient()
    notifier.client = fake
    fake.queue_error(
        "chat_postMessage",
        SlackApiError("failed", FakeSlackResponse(200, {}, {"ok": False, "error": "not_in_channel"})),
    )

    with pytest.raises(SlackError):
        notifier.post("본문", blocks=_image_card(), channel="C1")
    assert fake.call_names == ["chat_postMessage"]


# ── PNG 가 두 번 뜨던 문제 (2026-08-19) ───────────────────────────────
#
# `files_upload_v2` 에 channel 을 주면 **슬랙이 그 파일을 채널에 메시지로 게시한다.**
# 거기에 같은 file id 를 image 블록으로 또 실으면 같은 그림이 두 번 뜬다.
# 사용자가 Slack 에서 두 번 봤다고 알려줬다.


def test_upload_for_block_does_not_share_to_channel(tmp_path):
    """블록에 실을 파일은 채널에 공유하지 않는다 — 공유하면 파일 메시지가 따로 생긴다."""
    notifier = SlackNotifier(make_cfg())
    notifier.client = FakeWebClient()
    png = tmp_path / "planner.png"
    png.write_bytes(b"\x89PNG")

    notifier.upload_png(png, title="플래너", channel="C777", share=False)

    kwargs = dict(notifier.client.calls[0][1])
    assert "channel" not in kwargs  # ★ 이게 있으면 파일이 따로 게시된다
    assert kwargs["title"] == "플래너"


def test_upload_with_share_still_posts_to_channel(tmp_path):
    """파일만 던지는 용도(share=True)는 그대로 둔다."""
    notifier = SlackNotifier(make_cfg())
    notifier.client = FakeWebClient()
    png = tmp_path / "planner.png"
    png.write_bytes(b"\x89PNG")

    notifier.upload_png(png, title="플래너", channel="C777", share=True)

    assert dict(notifier.client.calls[0][1])["channel"] == "C777"


def test_post_report_uploads_without_sharing(conn, tmp_path):
    """리포트 카드는 메시지 하나여야 한다 — 업로드가 채널을 건드리면 안 된다."""
    png = tmp_path / "r.png"
    png.write_bytes(b"\x89PNG")
    notifier = SlackNotifier(make_cfg())
    notifier.client = FakeWebClient()
    built_report = SimpleNamespace(
        report_id=None, kind="daily", day="2026-08-19",
        text="본문", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "본문"}}],
        png_path=str(png),
    )

    notifier.post_report(conn, built_report, channel="C777")

    uploads = [kw for name, kw in notifier.client.calls if name == "files_upload_v2"]
    posts = [kw for name, kw in notifier.client.calls if name == "chat_postMessage"]
    assert len(uploads) == 1 and "channel" not in uploads[0]
    assert len(posts) == 1  # 카드 하나
    image_blocks = [b for b in posts[0]["blocks"] if b.get("type") == "image"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["slack_file"]["id"] == "F123"


# ── 지운 기능을 지키던 테스트를 **뒤집었다** ─────────────────────────────
#
# `alert()` 는 2026-09-01 에 지웠다 — 만들어만 두고 호출자가 한 번도 없었고,
# 쿨다운으로 중복을 늦추는 모델이라 CLAUDE.md §1 이 경계하는 "이미 아는 문제를
# 매번 다시" 쪽이었다.
#
# ★ 여기 있던 테스트 4개는 *"alert 가 쿨다운으로 억제한다"* 를 지키고 있었다.
#   **지우기만 하면 다음 사람이 되살려도 아무도 안 막는다.** 그래서 지키는 것을
#   뒤집었다 — 이제 "그 경로가 없다"와 "대신 무엇이 그 일을 하는가"를 지킨다.
#   (팔레트 고르개를 없앨 때와 같은 처방이다.)


def test_운영_알림_경로는_없다():
    notifier = SlackNotifier(make_cfg())
    assert not hasattr(notifier, "alert"), (
        "alert 를 되살렸다. 되살릴 거면 **언제 안 울리나**를 먼저 답해라 — "
        "쿨다운은 중복을 늦출 뿐 멈추지 않는다 (CLAUDE.md §1)"
    )


def test_상태_알림은_daily_check_가_한다():
    """대신 무엇이 그 일을 하는가. **경로를 실물로 가리킨다.**

    이름만 적어 두면 파일이 옮겨져도 아무도 모른다. 자기시험이 있다는 것까지 확인한다 —
    그게 이 경로를 alert 보다 낫게 만드는 이유다.
    """
    import pathlib

    script = pathlib.Path(__file__).resolve().parents[2] / "operate" / "tools" / "daily-check.sh"
    assert script.is_file(), f"상태 알림 경로가 사라졌다: {script}"
    body = script.read_text(encoding="utf-8")
    assert "--self-test" in body, "daily-check.sh 가 자기시험을 잃었다"
    assert "변화 없음 — 알리지 않는다" in body, "상태가 그대로일 때 조용한 성질이 사라졌다"
