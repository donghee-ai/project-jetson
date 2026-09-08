"""프라이빗 모드 — 그 시간을 재지 않기로 한 것.

**이 파일은 반환값이 아니라 테이블을 본다.** 함수가 "걸렀다"고 말하는 것과
`aw_event` 에 행이 없는 것은 다르고, 우리가 지켜야 하는 것은 후자다.

특히 `data_json` 은 원본 `data` 를 **통째로** 갖는다(`collect/aw_sync.py`). 컬럼만
비우는 마스킹은 구멍이라, 창 제목이 어디에도 없다는 것을 매번 문자열로 확인한다.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from lifetrainer import db, privacy
from lifetrainer.collect.aw_client import AWEvent
from lifetrainer.collect.aw_sync import sync, upsert_bucket, upsert_events
from lifetrainer.config import load_config
from lifetrainer.web import auth
from lifetrainer.web.app import create_app

SECRET_TITLE = "은행 계좌 이체 — 신한 12345678"
BUCKET = "aw-watcher-window_pc"
T0 = 1_800_000_000.0  # 2027-01-15 근처. 고정값이라 테스트가 시계를 안 탄다


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    return dataclasses.replace(
        base,
        data_dir=tmp_path,
        db_path=tmp_path / "lt.db",
        ingest=dataclasses.replace(base.ingest, enabled=True, secret="test-ingest-secret"),
    )


@pytest.fixture()
def conn(cfg):
    c = db.open_db(cfg)
    upsert_bucket(c, BUCKET, {"id": BUCKET, "type": "currentwindow", "client": "aw-watcher-window", "hostname": "pc"})
    c.commit()
    yield c
    c.close()


def _ev(ts: float, duration: float, title: str = SECRET_TITLE) -> AWEvent:
    return AWEvent(
        bucket_id=BUCKET, ts=ts, duration=duration,
        data={"app": "chrome.exe", "title": title}, event_id=None,
    )


def _live(conn) -> int:
    """읽는 쪽이 보는 이벤트 수 (`aw_event` 만. 휴지통은 안 센다)."""
    return int(conn.execute("SELECT count(*) AS c FROM aw_event").fetchone()["c"])


def _all_text(conn, *, live_only: bool = False) -> str:
    """기록을 한 덩어리 문자열로. 어느 컬럼에 새든 잡힌다.

    ★ `live_only` 가 이 파일에서 제일 조심할 구분이다 (2026-09-05).
      **지우기는 `purged_event` 로 옮기는 것**이라, 지운 제목이 `forget()` 전까지
      디스크에 있다. 되돌리기를 갖기 위해 치른 대가이고, 감추면 안 된다.

      · `live_only=True`  → 읽는 쪽이 보는 것 (`aw_event`). "화면에서 사라졌나"
      · 기본(False)      → 휴지통까지. "정말 없어졌나"
    """
    rows = [dict(r) for r in conn.execute("SELECT * FROM aw_event").fetchall()]
    if not live_only:
        rows += [dict(r) for r in conn.execute("SELECT * FROM purged_event").fetchall()]
    return json.dumps(rows, ensure_ascii=False)


# ── 1. 저장 관문 ──────────────────────────────────────────────────────────


def test_활성_구간의_이벤트는_저장되지_않고_제목도_안_남는다(conn):
    privacy.begin(conn, 30, now=T0)
    n = upsert_events(conn, [_ev(T0 + 60, 300)])

    assert n == 0
    assert conn.execute("SELECT count(*) AS c FROM aw_event").fetchone()["c"] == 0
    assert SECRET_TITLE not in _all_text(conn), "제목이 어딘가에 남았다"


def test_거르기가_기본값이라_인자를_빼먹어도_막힌다(conn):
    """`spans` 를 안 넘기면 스스로 조회한다 — 실패 방향이 '막는 쪽'이어야 한다."""
    privacy.begin(conn, 30, now=T0)
    upsert_events(conn, [_ev(T0 + 60, 300)])          # spans 인자 없음
    assert conn.execute("SELECT count(*) AS c FROM aw_event").fetchone()["c"] == 0


def test_걸친_이벤트는_잘려서_들어간다(conn):
    """통째로 버리면 조용한 대량 손실이 된다 — afk 는 긴 단일 구간이 될 수 있다."""
    privacy.begin(conn, 10, now=T0)                    # [T0, T0+600)
    upsert_events(conn, [_ev(T0 - 300, 1200)])         # [T0-300, T0+900)

    rows = conn.execute("SELECT ts, ts_end FROM aw_event ORDER BY ts").fetchall()
    assert [(r["ts"] - T0, r["ts_end"] - T0) for r in rows] == [(-300.0, 0.0), (600.0, 900.0)]
    assert SECRET_TITLE in _all_text(conn), "걸친 조각은 남아야 한다 (프라이빗 밖 시간)"


def test_재폴링해도_MAX_ts_end_가_경계를_안_넘는다(conn):
    """`ts_end = MAX(ts_end, excluded.ts_end)` 는 **줄이지 못한다.**

    한 번이라도 안 자른 값이 들어가면 MAX 가 그걸 영구히 잡는다. 그래서 자르기는
    `upsert_events` **안에서** 매번 일어나야 한다.
    """
    privacy.begin(conn, 10, now=T0)                    # [T0, T0+600)
    upsert_events(conn, [_ev(T0 - 300, 200)])          # [T0-300, T0-100) — 프라이빗 밖
    upsert_events(conn, [_ev(T0 - 300, 1200)])         # 하트비트로 자란 같은 이벤트

    # 구간 **밖**의 꼬리([T0+600, T0+900))는 남는 게 맞다. 볼 것은 "구간 안에 몇 초가
    # 들어왔나"다 — 0이어야 한다. MAX(ts_end) 로 보면 정상 꼬리를 누출로 오해한다.
    inside = sum(
        max(0.0, min(r["ts_end"], T0 + 600) - max(r["ts"], T0))
        for r in conn.execute("SELECT ts, ts_end FROM aw_event")
    )
    assert inside == 0.0, f"프라이빗 구간 안에 {inside:.0f}초가 들어왔다"


def test_끄면_그_뒤_이벤트는_정상_저장된다(conn):
    privacy.begin(conn, 60, now=T0)
    privacy.end_now(conn, now=T0 + 600)

    upsert_events(conn, [_ev(T0 + 700, 100, title="정상 창")])
    assert conn.execute("SELECT count(*) AS c FROM aw_event").fetchone()["c"] == 1


def test_끝난_구간도_계속_막는다(conn):
    """★ "지금 켜져 있나"로 판정하면 안 된다.

    PC 는 `overlap_sec`(900초)만큼 겹쳐 다시 읽고, 커서를 잃으면 `backfill_days`(7일)
    까지 되돌아온다. 프라이빗이 끝나는 순간 다음 sync 가 그 구간을 통째로 실어 온다.
    """
    privacy.begin(conn, 10, now=T0)
    privacy.end_now(conn, now=T0 + 600)

    upsert_events(conn, [_ev(T0 + 60, 120)])           # 한참 뒤에 재폴링된 옛 이벤트
    assert conn.execute("SELECT count(*) AS c FROM aw_event").fetchone()["c"] == 0


def test_길이_0_이벤트는_구간_밖이면_그대로_들어간다(conn):
    """★ 이 한 줄이 09-03~09-04 에 unlock 을 통째로 삼켰다.

    unlock 은 **전부** duration 0 이고, 창·미디어 워처도 순간 전환을 0초로 낸다.
    구간 빼기는 `p[1] > p[0]` 인 조각만 돌려주므로, 길이 0 이벤트는 프라이빗과
    겹치지 않아도 결과에서 사라졌다 — 잘린 것이 아니라 **없어진** 것이다.

    구간이 하나라도 있으면(끝난 것이라도) 거르기가 도니까, 09-03 15:14 에 첫 타일을
    누른 뒤로 젯슨에 들어온 길이 0 이벤트가 한 건도 없었다.
    """
    privacy.begin(conn, 10, now=T0)
    privacy.end_now(conn, now=T0 + 600)

    n = upsert_events(conn, [_ev(T0 + 5000, 0, title="잠금 해제")])

    assert n == 1
    assert conn.execute("SELECT count(*) AS c FROM aw_event").fetchone()["c"] == 1


def test_길이_0_이벤트도_구간_안이면_막힌다(conn):
    """점이라고 봐주지 않는다 — 프라이빗 중에 폰을 몇 번 열었는지도 안 남긴다."""
    privacy.begin(conn, 10, now=T0)

    n = upsert_events(conn, [_ev(T0 + 60, 0)])

    assert n == 0
    assert conn.execute("SELECT count(*) AS c FROM aw_event").fetchone()["c"] == 0
    assert SECRET_TITLE not in _all_text(conn)


def test_길이_0_이벤트의_경계는_구간_빼기와_같다(conn):
    """반열림 `[s, e)` — 시작에 딱 걸친 점은 안, 끝에 딱 걸친 점은 밖이다.

    길이 있는 이벤트를 자를 때와 같은 규칙이어야 한다. 여기만 달라지면 프라이빗
    경계에서 한 건이 있다 없다 하는데, 그건 나중에 어느 쪽이 맞는지 못 가린다.
    """
    privacy.begin(conn, 10, now=T0)                    # [T0, T0+600)

    assert upsert_events(conn, [_ev(T0, 0)]) == 0              # 시작 = 안
    assert upsert_events(conn, [_ev(T0 + 600, 0)]) == 1        # 끝 = 밖

# ── 2. 커서 — 막았다고 커서가 멈추면 안 된다 ─────────────────────────────


class _Client:
    def __init__(self, events):
        self._events = events

    def buckets(self):
        return {BUCKET: {"id": BUCKET, "type": "currentwindow", "client": "aw-watcher-window", "hostname": "pc"}}

    def events(self, bucket_id, *, start=None, end=None, limit=-1):
        return [
            e for e in self._events
            if (start is None or e.ts + e.duration >= start) and (end is None or e.ts <= end)
        ]


def test_전부_걸러져도_PC_커서는_전진한다(conn, cfg):
    """커서는 **가져온 목록**의 `max(ev.ts)` 로 계산한다 — 저장 결과가 아니다.

    "저장한 게 없으면 커서를 안 옮긴다"로 바꾸는 순간 overlap 재조회가 영원히 같은
    구간을 다시 읽는다. 그 성질을 여기서 못 박는다.
    """
    privacy.begin(conn, 120, now=T0)
    client = _Client([_ev(T0 + 60, 300), _ev(T0 + 400, 300)])

    sync(conn, client, cfg, now=T0 + 7200)
    cursor_1 = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?", (f"aw_cursor:{BUCKET}",)
    ).fetchone()

    assert cursor_1 is not None, "커서가 안 생겼다 — 다음 sync 가 같은 구간을 다시 읽는다"
    assert conn.execute("SELECT count(*) AS c FROM aw_event").fetchone()["c"] == 0

    sync(conn, client, cfg, now=T0 + 7300)             # 2회차도 조용해야 한다
    assert conn.execute("SELECT count(*) AS c FROM aw_event").fetchone()["c"] == 0


# ── 3. 소급 삭제 ──────────────────────────────────────────────────────────


def test_purge_는_구간을_남겨_되살아남을_막는다(conn, cfg):
    upsert_events(conn, [_ev(T0, 600)], spans=[])       # 프라이빗 없이 들어간 상태
    assert SECRET_TITLE in _all_text(conn)

    privacy.purge(conn, cfg, T0, T0 + 600, now=T0 + 900)

    # ★ 2026-09-05 부터 **표시 삭제**다. 읽는 쪽에서는 사라지지만 행은 남는다 —
    #   되돌리기를 갖기 위한 대가이고, `lt private forget` 이 진짜로 지운다.
    assert _live(conn) == 0
    assert SECRET_TITLE not in _all_text(conn, live_only=True)
    assert SECRET_TITLE in _all_text(conn), "휴지통에도 없다 — 되돌릴 수 없다"

    upsert_events(conn, [_ev(T0, 600)])                 # 다음 sync 가 다시 실어 온다면
    assert _live(conn) == 0, "구간을 안 남기면 overlap 재조회가 되살린다"


def test_purge_는_걸친_이벤트를_잘라서_남긴다(conn, cfg):
    upsert_events(conn, [_ev(T0 - 300, 1200)], spans=[])
    privacy.purge(conn, cfg, T0, T0 + 600, now=T0 + 900)

    rows = conn.execute("SELECT ts, ts_end FROM aw_event ORDER BY ts").fetchall()
    assert [(r["ts"] - T0, r["ts_end"] - T0) for r in rows] == [(-300.0, 0.0), (600.0, 900.0)]


def test_purge_dry_run_은_아무것도_안_바꾼다(conn, cfg):
    upsert_events(conn, [_ev(T0, 600)], spans=[])
    before = _all_text(conn)

    report = privacy.purge(conn, cfg, T0, T0 + 600, dry_run=True, now=T0 + 900)

    assert report.dry_run and report.events == 1
    assert report.seconds == pytest.approx(600.0)
    assert _all_text(conn) == before
    assert conn.execute("SELECT count(*) AS c FROM private_span").fetchone()["c"] == 0


def test_purge_는_논리적_하루로_재롤업_날짜를_고른다(cfg):
    """달력 날짜가 아니다 — 이 앱의 하루는 06시에 시작한다."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(cfg.timezone)
    dawn = datetime(2026, 9, 1, 2, 30, tzinfo=tz).timestamp()
    assert privacy.affected_days(cfg, dawn, dawn + 3600) == ["2026-08-31"]


# ── 3-B. 삭제한 시간은 화면에서 자리비움이다 (2026-09-04) ────────────────
#
# 프라이빗 토글(`kind='live'`)과 소급 삭제(`kind='purge'`)는 **이벤트를 자르는 데는
# 똑같이** 쓰인다. 가르는 것은 **화면 표시**뿐이다:
#
#   live  → '프라이빗' 으로 그린다. 일부러 안 잰 시간이라고 말하는 게 맞다
#   purge → '자리비움' 으로 그린다. 지운 사람의 뜻은 "없애 달라" 이지
#           "여기 뭔가 있었다고 표시해 달라" 가 아니다
#
# 이 구분이 무너지면 삭제가 **자기 흔적을 남기는** 삭제가 된다.


def _rollup(conn, cfg, day: str):
    from lifetrainer.rollup.classify import Classifier
    from lifetrainer.rollup.rollup import rollup_day

    rollup_day(conn, cfg, Classifier.from_yaml(cfg.rollup.rules_path), day)
    return [
        (r["category"], r["private_sec"])
        for r in conn.execute(
            "SELECT category, private_sec FROM slot WHERE day = ? ORDER BY slot", (day,)
        ).fetchall()
    ]


def _day_of(cfg, ts: float) -> str:
    from zoneinfo import ZoneInfo

    from lifetrainer import timeutil

    return timeutil.day_str(ts, ZoneInfo(cfg.timezone), boundary_hour=cfg.rollup.day_boundary_hour)


def test_삭제한_구간은_격자에서_자리비움이다(conn, cfg):
    """★ 이 파일에서 제일 중요한 것. 지운 시간이 '프라이빗' 이라고 적히면 안 된다.

    `category` 만이 아니라 `private_sec` 도 0 이어야 한다 — 자리비움처럼 보이는데
    프라이빗 초가 붙어 있으면 그 칸 하나로 다 새어 나간다.
    """
    upsert_events(conn, [_ev(T0, 600)], spans=[])
    privacy.purge(conn, cfg, T0, T0 + 3600, now=T0 + 3600)

    slots = _rollup(conn, cfg, _day_of(cfg, T0))
    touched = [(cat, priv) for cat, priv in slots if cat != cfg.rollup.no_data_category]

    assert touched, "지운 구간이 어떤 칸으로도 안 나타났다"
    assert all(cat == cfg.rollup.afk_category for cat, _ in touched), touched
    assert all(priv == 0 for _, priv in touched), "자리비움인데 프라이빗 초가 붙어 있다"


def test_토글로_켠_구간은_그대로_프라이빗이다(conn, cfg):
    """반대쪽. 삭제를 자리비움으로 바꾸면서 토글까지 같이 감추면 안 된다."""
    now = T0
    privacy.begin(conn, 60.0, source="test", now=now)

    slots = _rollup(conn, cfg, _day_of(cfg, now + 60))
    assert any(cat == "private" for cat, _ in slots), "토글 구간이 프라이빗으로 안 그려졌다"


def test_삭제는_토글과_똑같이_이벤트를_막는다(conn, cfg):
    """표시만 다르다. **막는 힘은 같아야 한다** — 여기가 갈리면 삭제가 헛것이 된다."""
    privacy.purge(conn, cfg, T0, T0 + 600, now=T0 + 900)
    upsert_events(conn, [_ev(T0, 600)])
    assert conn.execute("SELECT count(*) AS c FROM aw_event").fetchone()["c"] == 0


def test_load_spans_는_종류로_고를_수_있다(conn, cfg):
    """`kinds` 바인딩 순서를 한 번 어긋뜨렸다 — 조용히 틀린 구간을 준다."""
    privacy.begin(conn, 30.0, source="test", now=T0)
    privacy.purge(conn, cfg, T0 + 7200, T0 + 8000, now=T0 + 8000)

    assert len(privacy.load_spans(conn)) == 2
    assert len(privacy.load_spans(conn, kinds=("live",))) == 1
    assert len(privacy.load_spans(conn, kinds=("purge",))) == 1
    assert privacy.load_spans(conn, kinds=("purge",))[0][0] == pytest.approx(T0 + 7200)


# ── 3-C. 되돌리기 (2026-09-05) ────────────────────────────────────────────
#
# 사람이 물었다 — "삭제한거 되돌릴 수 있지?" **아니었다.**
# `privacy.load_spans` 는 *"revoked 는 제외한다 — 잘못 켠 것을 취소할 수 있어야 한다"*
# 고 적어 두고 있었는데 **`revoked = 1` 을 만드는 코드가 저장소에 0개**였다.
# 게다가 purge 가 행을 진짜 DELETE 해서, 구간을 취소해도 기록은 안 돌아왔다.
#
# 이제 지우기는 **표시**고, `undo` 가 되돌리고, `forget` 만이 진짜 지운다.


def test_지운_것은_사라져_보이지만_행은_남아_있다(conn, cfg):
    upsert_events(conn, [_ev(T0, 600)], spans=[])
    privacy.purge(conn, cfg, T0, T0 + 600, now=T0 + 900)

    assert privacy.trash_count(conn)[0] == 1, "휴지통으로 안 갔다"
    assert _live(conn) == 0, "읽는 쪽에서 안 사라졌다"
    assert SECRET_TITLE not in _all_text(conn, live_only=True)


def test_undo_가_지운_것을_되살린다(conn, cfg):
    upsert_events(conn, [_ev(T0, 600)], spans=[])
    report = privacy.purge(conn, cfg, T0, T0 + 600, now=T0 + 900)

    assert privacy.undo(conn, report.span_id) == 1
    assert _live(conn) == 1
    assert SECRET_TITLE in _all_text(conn)


def test_undo_는_구간_표시도_풀어야_한다(conn, cfg):
    """★ 여기가 빠지면 **되돌린 것이 조용히 다시 사라진다.**

    구간이 살아 있으면 다음 sync 의 `clip_events` 가 그 시간을 또 자른다.
    되살아난 행은 남아 있어도 새로 들어오는 것은 계속 막히고, 사람은 이유를 모른다.
    """
    upsert_events(conn, [_ev(T0, 600)], spans=[])
    report = privacy.purge(conn, cfg, T0, T0 + 600, now=T0 + 900)
    privacy.undo(conn, report.span_id)

    assert privacy.load_spans(conn) == [], "구간이 안 풀렸다"
    upsert_events(conn, [_ev(T0 + 600, 600, title="이후")])
    assert _live(conn) == 2


def test_undo_는_걸친_이벤트의_조각을_치운다(conn, cfg):
    """★ 조각을 안 지우면 되살아난 원본과 **같은 시간을 두 번 센다.**"""
    upsert_events(conn, [_ev(T0 - 300, 1200)], spans=[])
    report = privacy.purge(conn, cfg, T0, T0 + 600, now=T0 + 900)
    assert _live(conn) == 2, "조각 둘이 남아 있어야 하는 표본이다"

    privacy.undo(conn, report.span_id)

    rows = conn.execute("SELECT ts, ts_end FROM aw_event ORDER BY ts").fetchall()
    assert [(r["ts"] - T0, r["ts_end"] - T0) for r in rows] == [(-300.0, 900.0)], rows


def test_undo_는_인자가_없으면_가장_최근_삭제를_되돌린다(conn, cfg):
    """오클릭 직후가 대부분이다. 번호를 찾아 오라고 하면 안 쓴다."""
    upsert_events(conn, [_ev(T0, 600), _ev(T0 + 3600, 600, title="나중")], spans=[])
    privacy.purge(conn, cfg, T0, T0 + 600, now=T0 + 900)
    second = privacy.purge(conn, cfg, T0 + 3600, T0 + 4200, now=T0 + 4500)

    privacy.undo(conn)

    alive = _all_text(conn, live_only=True)
    assert "나중" in alive, "가장 최근 삭제가 안 돌아왔다"
    assert SECRET_TITLE not in alive, "먼저 지운 것까지 같이 돌아왔다"
    assert second.span_id is not None


def test_forget_뒤에는_되돌릴_수_없다(conn, cfg):
    """★ `forget` 만이 되돌릴 수 없다. 그래서 **타이머가 안 부른다.**

    되돌리기가 "됐다" 고 답하면서 아무것도 안 돌아오면 그게 더 나쁘다 — 0 을 반환한다.
    """
    upsert_events(conn, [_ev(T0, 600)], spans=[])
    report = privacy.purge(conn, cfg, T0, T0 + 600, now=T0 + 900)

    assert privacy.forget(conn) == 1
    assert privacy.trash_count(conn)[0] == 0
    assert privacy.undo(conn, report.span_id) == 0, "없는 것을 되살렸다고 말하면 안 된다"


def test_지운_구간은_집계에도_안_들어간다(conn, cfg):
    """행이 남아 있으니 **롤업이 그걸 세면** 지운 의미가 없다."""
    upsert_events(conn, [_ev(T0, 600)], spans=[])
    privacy.purge(conn, cfg, T0, T0 + 3600, now=T0 + 3600)

    slots = _rollup(conn, cfg, _day_of(cfg, T0))
    assert all(cat != "coding" for cat, _ in slots), slots


# ── 4. 웹 API ─────────────────────────────────────────────────────────────


@pytest.fixture()
def client(cfg):
    app = create_app(cfg)
    app.config.update(TESTING=True)
    db.open_db(cfg).close()
    return app.test_client()


def test_웹_토글_왕복(client, cfg):
    assert client.get("/api/private").get_json()["active"] is False

    on = client.post("/api/private", json={"minutes": 5}).get_json()
    assert on["active"] is True
    assert on["until_ts"] - on["server_ts"] == pytest.approx(300, abs=2)
    assert on["poll_sec"] == cfg.private.poll_sec

    off = client.delete("/api/private").get_json()
    assert off["active"] is False and off["until_ts"] is None


def test_상한을_넘는_minutes_는_400(client, cfg):
    assert client.post("/api/private", json={"minutes": cfg.private.max_minutes + 1}).status_code == 400
    assert client.post("/api/private", json={"minutes": 0}).status_code == 400
    assert client.post("/api/private", json={"minutes": "매우"}).status_code == 400


def test_읽기전용_모드에서도_프라이빗은_눌린다(cfg):
    """표시용 운영 스위치 때문에 프라이버시 제어를 못 하는 건 방향이 반대다."""
    ro = dataclasses.replace(cfg, web=dataclasses.replace(cfg.web, read_only=True))
    db.open_db(ro).close()
    c = create_app(ro).test_client()

    assert c.post("/api/private", json={"minutes": 5}).status_code == 200
    assert c.delete("/api/private").status_code == 200


def test_purge_는_confirm_없이_400_이고_dry_run_은_200(client):
    without = client.post("/api/private/purge", json={"minutes": 10})
    assert without.status_code == 400
    assert without.get_json()["events"] == 0        # 보고서를 같이 준다

    preview = client.post("/api/private/purge", json={"minutes": 10, "dry_run": True})
    assert preview.status_code == 200 and preview.get_json()["dry_run"] is True

    assert client.post("/api/private/purge", json={"minutes": 10, "confirm": True}).status_code == 200


# ── 5. 엔드포인트 API (PC 헬퍼 · 폰 타일) ────────────────────────────────


def _signed_get(client, cfg, *, device="pc", ts=T0, nonce="n1"):
    sig = auth.sign_ingest(cfg.ingest.secret, device, ts, nonce, b"")
    return client.get(
        "/ingest/private",
        headers={"Authorization": f"{auth.INGEST_AUTH_SCHEME} {device}:{int(ts)}:{nonce}:{sig}"},
    )


def test_ingest_private_는_서명을_요구한다(client, cfg, monkeypatch):
    monkeypatch.setattr("time.time", lambda: T0)
    assert client.get("/ingest/private").status_code == 401
    assert _signed_get(client, cfg).status_code == 200


def test_조회_폴링은_nonce_행을_쌓지_않는다(client, cfg, monkeypatch):
    """15초마다 폴링하면 하루 5,760행이다. 조회는 재전송해도 같은 답이라 nonce 가 필요 없다."""
    monkeypatch.setattr("time.time", lambda: T0)
    conn = db.open_db(cfg)
    before = conn.execute("SELECT count(*) AS c FROM sync_state").fetchone()["c"]

    for i in range(5):
        assert _signed_get(client, cfg, nonce=f"n{i}").status_code == 200

    after = conn.execute("SELECT count(*) AS c FROM sync_state").fetchone()["c"]
    conn.close()
    assert after == before, f"조회 {5}번에 sync_state 가 {after - before}행 늘었다"


def _signed_post(client, cfg, payload, *, device="pc", ts=T0, nonce="p1"):
    """서명 POST. `/ingest/aw` 쪽 `_post()` 와 같은 형태다 — 서명은 **본문 바이트**에 건다."""
    body = json.dumps(payload).encode("utf-8")
    sig = auth.sign_ingest(cfg.ingest.secret, device, ts, nonce, body)
    return client.post(
        "/ingest/private",
        data=body,
        headers={
            "Authorization": f"{auth.INGEST_AUTH_SCHEME} {device}:{int(ts)}:{nonce}:{sig}",
            "Content-Type": "application/json",
        },
    )


def test_끄기는_off_와_end_둘_다_받는다(client, cfg, monkeypatch):
    """★ 문서는 `end`, 코드는 `off` 였다. 그 불일치가 **끄기를 켜기로 뒤집었다.**

    `else` 가 나머지 전부를 켜기로 받았고 `privacy.begin` 은 켜져 있으면 연장까지 한다.
    2026-09-03 에 폰 타일로 껐는데 60분이 새로 켜졌다.
    """
    monkeypatch.setattr("time.time", lambda: T0)
    for i, key in enumerate(("off", "end")):
        assert _signed_post(client, cfg, {"minutes": 5}, nonce=f"on{i}").get_json()["active"]
        got = _signed_post(client, cfg, {key: True}, nonce=f"off{i}").get_json()
        assert got["active"] is False, f"{key} 로 안 꺼졌다: {got}"


def test_모르는_본문은_400_이지_켜기가_아니다(client, cfg, monkeypatch):
    """오타 하나가 정반대 동작이 되면 안 된다 — 끄려던 요청이 켜기가 됐다."""
    monkeypatch.setattr("time.time", lambda: T0)
    for i, body in enumerate(({"typo": 1}, {"off": False}, {"minutse": 5})):
        r = _signed_post(client, cfg, body, nonce=f"bad{i}")
        assert r.status_code == 400, f"{body} 가 400 이 아니다 ({r.status_code})"
    assert _signed_get(client, cfg, nonce="chk").get_json()["active"] is False, (
        "400 을 냈는데 프라이빗이 켜져 있다"
    )


def test_빈_본문은_기존대로_기본값_켜기다(client, cfg, monkeypatch):
    """생략은 모호한 것이 아니다 — 이미 그 계약으로 나가 있다."""
    monkeypatch.setattr("time.time", lambda: T0)
    got = _signed_post(client, cfg, {}, nonce="empty").get_json()
    assert got["active"] is True
    assert got["until_ts"] == T0 + cfg.private.default_minutes * 60.0


def test_같은_상태를_웹과_엔드포인트가_똑같이_말한다(client, cfg, monkeypatch):
    """진실의 원천은 젯슨 하나다 — 두 라우트가 갈리면 헬퍼와 화면이 어긋난다."""
    monkeypatch.setattr("time.time", lambda: T0)
    client.post("/api/private", json={"minutes": 5})

    web = client.get("/api/private").get_json()
    endpoint = _signed_get(client, cfg, nonce="n9").get_json()
    assert web["active"] == endpoint["active"] is True
    assert web["until_ts"] == endpoint["until_ts"]


# ── 6. 마이그레이션 ───────────────────────────────────────────────────────


def test_006_은_두_번_돌아도_파생_테이블을_안_비운다(cfg):
    conn = db.open_db(cfg)
    conn.execute(
        "INSERT INTO slot(day, slot, start_ts, category, active_sec, afk_sec, gap_sec, winner_sec, updated_at) "
        "VALUES ('2026-09-01', 0, 0, 'coding', 600, 0, 0, 600, 0)"
    )
    conn.commit()

    conn.execute("DELETE FROM meta WHERE key = 'migration:006_private_span'")
    assert db.migrate(conn, cfg) == 1
    assert db.migrate(conn, cfg) == 0

    assert conn.execute("SELECT count(*) AS c FROM slot").fetchone()["c"] == 1, (
        "006 은 새 테이블과 기본값 0 인 컬럼뿐이라 재계산이 필요 없다"
    )
    conn.close()


# ── 5. 블럭 단위 삭제 · 웹 되돌리기 (2026-09-05) ──────────────────────────
#
# 전에는 "지금부터 N분" 뿐이라 **어제 오후의 그 한 칸**을 지울 방법이 없었다.
# 격자에서 칸을 골라 지운다.


def _day_str(cfg, ts: float) -> str:
    from zoneinfo import ZoneInfo

    from lifetrainer import timeutil

    return timeutil.day_str(ts, ZoneInfo(cfg.timezone), boundary_hour=cfg.rollup.day_boundary_hour)


def test_웹에서_슬롯_구간을_지운다(client, cfg, conn):
    """슬롯 좌표로 지운다 — 브라우저가 epoch 를 계산하면 변환이 두 곳이 된다."""
    from zoneinfo import ZoneInfo

    from lifetrainer import timeutil

    day = _day_str(cfg, T0)
    day_start, _ = timeutil.day_bounds(
        day, ZoneInfo(cfg.timezone), boundary_hour=cfg.rollup.day_boundary_hour
    )
    slot_sec = cfg.rollup.slot_minutes * 60.0
    slot = int((T0 - day_start) // slot_sec)
    upsert_events(conn, [_ev(day_start + slot * slot_sec, slot_sec)], spans=[])
    assert _live(conn) == 1

    r = client.post(
        "/api/private/purge",
        json={"day": day, "start_slot": slot, "end_slot": slot + 1, "confirm": True},
    )
    assert r.status_code == 200, r.get_json()
    assert _live(conn) == 0
    assert privacy.trash_count(conn)[0] == 1


def test_웹_되돌리기가_지운_것을_되살린다(client, cfg, conn):
    from zoneinfo import ZoneInfo

    from lifetrainer import timeutil

    day = _day_str(cfg, T0)
    day_start, _ = timeutil.day_bounds(
        day, ZoneInfo(cfg.timezone), boundary_hour=cfg.rollup.day_boundary_hour
    )
    slot_sec = cfg.rollup.slot_minutes * 60.0
    slot = int((T0 - day_start) // slot_sec)
    upsert_events(conn, [_ev(day_start + slot * slot_sec, slot_sec)], spans=[])

    client.post("/api/private/purge",
                json={"day": day, "start_slot": slot, "end_slot": slot + 1, "confirm": True})
    r = client.post("/api/private/undo", json={})

    assert r.status_code == 200 and r.get_json()["restored"] == 1
    assert _live(conn) == 1
    assert SECRET_TITLE in _all_text(conn, live_only=True)


def test_되돌릴_것이_없으면_404_다(client):
    """★ 200 으로 "되돌렸다" 고 답하면 안 된다 — 아무 일도 안 했는데 성공으로 읽힌다."""
    r = client.post("/api/private/undo", json={})
    assert r.status_code == 404


def test_슬롯_범위가_거꾸로면_400_이다(client, cfg):
    """끝이 시작보다 앞이면 지울 구간이 없다. 조용히 통과시키면 아무것도 안 지우고 성공한다."""
    day = _day_str(cfg, T0)
    r = client.post("/api/private/purge",
                    json={"day": day, "start_slot": 10, "end_slot": 5, "confirm": True})
    assert r.status_code == 400


# ── 6. 삭제가 프라이빗을 이긴다 (2026-09-07) ──────────────────────────
#
# 사람이 짚었다 — "프라이빗 기록은 삭제가 안 되더라."
#
# ★ 프라이빗 구간에는 **이벤트가 아예 없다.** 저장 관문이 들어올 때 잘라냈기 때문이다.
#   그래서 그 칸에 "기록 지우기" 를 눌러도 지울 행이 없고, `private_span` 이 남아
#   칸은 계속 프라이빗이었다 — 사람 눈에는 삭제가 안 먹는 것으로 보인다.
#
# 뜻으로 보면 둘은 다른 요청이다:
#   프라이빗 = "이 시간은 가려 달라"  → 가렸다는 **표시가 남는다**
#   삭제     = "이 시간을 없애 달라"  → **흔적도 남기지 않는다**(자리비움)


def test_프라이빗_구간을_지우면_자리비움이_된다(conn, cfg):
    """★ 사람이 짚은 그 버그. 지웠는데 화면이 안 바뀌면 안 먹은 것으로 읽힌다."""
    now = T0
    privacy.begin(conn, 60.0, source="test", now=now)
    day = _day_of(cfg, now + 60)
    assert any(cat == "private" for cat, _ in _rollup(conn, cfg, day)), "표본이 안 만들어졌다"

    privacy.purge(conn, cfg, now, now + 3600.0, now=now + 3600.0)
    slots = _rollup(conn, cfg, day)

    assert all(cat != "private" for cat, _ in slots), "지웠는데 프라이빗이 남았다"
    assert any(cat == cfg.rollup.afk_category for cat, _ in slots), "자리비움으로 안 바뀌었다"


def test_되돌리면_프라이빗_표시가_돌아온다(conn, cfg):
    """★ 따로 기록해 두지 않는다 — `undo` 가 purge 구간을 취소하면 프라이빗이 저절로 산다.

    삭제분을 어딘가에 적어 뒀다가 복원하는 구조였으면, 그 기록과 실제가 갈리는 순간
    되돌리기가 조용히 틀린 답을 낸다.
    """
    now = T0
    privacy.begin(conn, 60.0, source="test", now=now)
    day = _day_of(cfg, now + 60)
    report = privacy.purge(conn, cfg, now, now + 3600.0, now=now + 3600.0)
    assert all(cat != "private" for cat, _ in _rollup(conn, cfg, day))

    privacy.undo(conn, report.span_id)

    assert any(cat == "private" for cat, _ in _rollup(conn, cfg, day)), "프라이빗이 안 돌아왔다"


def test_삭제_구간_밖의_프라이빗은_그대로다(conn, cfg):
    """★ 안 지워져야 하는 쪽. 10분을 지웠다고 60분짜리 프라이빗이 통째로 풀리면 안 된다."""
    now = T0
    privacy.begin(conn, 60.0, source="test", now=now)
    day = _day_of(cfg, now + 60)

    privacy.purge(conn, cfg, now, now + 600.0, now=now + 3600.0)   # 앞 10분만
    slots = _rollup(conn, cfg, day)

    assert any(cat == "private" for cat, _ in slots), "구간 밖 프라이빗까지 풀렸다"
    assert any(cat == cfg.rollup.afk_category for cat, _ in slots), "지운 10분이 안 바뀌었다"
