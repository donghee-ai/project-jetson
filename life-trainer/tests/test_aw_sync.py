"""lifetrainer.collect.aw_sync 테스트.

이 모듈의 존재 이유: ActivityWatch 의 하트비트 병합 때문에 버킷의 마지막 이벤트는
`id`/`timestamp` 가 고정된 채 `duration` 만 계속 자란다. 겹쳐서 재조회 + (bucket_id, ts)
upsert 로 재폴링해도 행이 늘지 않고 duration 만 갱신돼야 한다 — 그 시나리오가 핵심 테스트다.
"""

from __future__ import annotations

import dataclasses

import pytest

from lifetrainer import db
from lifetrainer.collect.aw_client import AWError, AWEvent
from lifetrainer.collect.aw_sync import SyncResult, bucket_type, sync, upsert_bucket, upsert_events
from lifetrainer.config import load_config


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    yield c
    c.close()


def _cfg(*, overlap_sec=900, backfill_days=7, hosts=()):
    base = load_config()
    aw = dataclasses.replace(base.aw, overlap_sec=overlap_sec, backfill_days=backfill_days, hosts=tuple(hosts))
    return dataclasses.replace(base, aw=aw)


class _FakeClient:
    """AWClient 대역. `.buckets()`/`.events()` 만 흉내낸다 (sync() 가 쓰는 표면 전부)."""

    def __init__(self, buckets: dict, events_by_bucket: dict[str, list[AWEvent]], fail_buckets=frozenset()):
        self._buckets = buckets
        self._events_by_bucket = events_by_bucket
        self._fail_buckets = fail_buckets
        self.calls: list[tuple[str, float | None, float | None]] = []

    def buckets(self):
        return self._buckets

    def events(self, bucket_id, *, start=None, end=None, limit=-1):
        self.calls.append((bucket_id, start, end))
        if bucket_id in self._fail_buckets:
            raise AWError(f"{bucket_id} 서버가 500 을 반환함")
        evs = self._events_by_bucket.get(bucket_id, [])
        # 실제 aw-server 처럼 [start, end] 와 겹치는 이벤트만 돌려준다.
        return [
            e
            for e in evs
            if (start is None or e.ts + e.duration >= start) and (end is None or e.ts <= end)
        ]


WIN_BUCKET = "aw-watcher-window_pc"
AFK_BUCKET = "aw-watcher-afk_pc"


def _win_meta(hostname="pc"):
    return {"id": WIN_BUCKET, "type": "currentwindow", "client": "aw-watcher-window", "hostname": hostname}


# ── bucket_type ──────────────────────────────────────────────────────────


def test_bucket_type_from_id():
    assert bucket_type("aw-watcher-window_pc", {}) == "window"
    assert bucket_type("aw-watcher-afk_pc", {}) == "afk"
    assert bucket_type("aw-watcher-web-chrome_pc", {}) == "web"
    assert bucket_type("some-other-bucket_pc", {}) == "unknown"


def test_bucket_type_falls_back_to_meta():
    assert bucket_type("mystery_pc", {"type": "afkstatus"}) == "afk"


def test_bucket_type_android_is_not_window():
    """폰 앱 세션은 `window` 가 아니라 `android` 로 받는다.

    예전에는 메타 타입 `"currentwindow"` 안의 부분문자열 `window` 에 **우연히**
    걸려 `window` 로 잡혔다. 그러면 폰 이벤트가 노트북 창 이벤트와 같은
    타임라인에 섞인다.
    """
    meta = {"id": "aw-watcher-android", "type": "currentwindow", "hostname": "s24"}
    assert bucket_type("aw-watcher-android", meta) == "android"


def test_bucket_type_android_web_is_web_not_android():
    """`aw-watcher-android-web` 은 `android` 가 아니라 `web` 이다.

    ★ 2026-08-23 실측으로 드러난 버그. 판별 순서가 `android` → `web` 이라
    `"android"` 부분문자열에 먼저 걸려 **`web` 분기에 영원히 도달하지 못했다.**

    영향이 조용했다: 롤업은 URL 을 `dev.buckets["web"]` 에서만 꺼내 슬롯에
    붙이는데 폰의 web 버킷이 항상 비어 있었다. 그래서 `rules.yaml` 의 URL
    규칙(앱 규칙보다 **먼저** 평가되도록 일부러 배치한 것)이 폰 브라우징에
    하나도 걸리지 않았고, 폰 크롬 929분이 앱 이름만으로 뭉뚱그려졌다.
    에러는 나지 않았다 — 그래서 테스트로 못박는다.
    """
    meta = {"id": "aw-watcher-android-web", "type": "currentwindow", "hostname": "s24"}
    assert bucket_type("aw-watcher-android-web", meta) == "web"


def test_bucket_type_android_media_is_android():
    """`aw-watcher-android-media` 는 `android` 로 받는다 — 전용 분기를 두지 않는다.

    미디어 재생 구간은 앱 세션과 겹치므로 `_activity_intervals` 의 `_union` 이
    흡수한다. 합성 회귀 데이터로 이중 계산이 없음을 확인한다.

    이 테스트는 `web` 을 앞으로 옮긴 수정이 `media` 까지 끌고 가지 않았는지
    지킨다 — `"media"` 에는 `web`·`afk`·`unlock` 어느 것도 없어야 한다.
    """
    assert bucket_type("aw-watcher-android-media", {}) == "android"


def test_bucket_type_order_is_specific_before_general():
    """폰 버킷 다섯 개가 각자 제 타입으로 갈린다.

    전부 `"android"` 를 포함하므로, 구체적인 판별이 먼저 오지 않으면 통째로
    `android` 로 뭉개진다. 이 표가 곧 판별 순서의 계약이다.
    """
    assert bucket_type("aw-watcher-android-unlock", {}) == "unlock"
    assert bucket_type("aw-watcher-android-afk", {}) == "afk"
    assert bucket_type("aw-watcher-android-web", {}) == "web"
    assert bucket_type("aw-watcher-android-media", {}) == "android"
    assert bucket_type("aw-watcher-android", {}) == "android"


def test_bucket_type_desktop_window_still_wins_on_same_meta_type():
    """데스크톱 창 워처의 버킷 타입도 `"currentwindow"` 다 — 메타로는 못 가른다.

    그래서 판별은 버킷 ID 로 한다. 이 테스트가 깨지면 노트북이 폰으로 분류된다.
    """
    meta = {"id": WIN_BUCKET, "type": "currentwindow", "hostname": "pc"}
    assert bucket_type("aw-watcher-window_pc", meta) == "window"


def test_bucket_type_unlock_beats_android():
    """`aw-watcher-android-unlock` 은 android/unlock 둘 다에 걸린다 — unlock 이 먼저."""
    meta = {"type": "os.lockscreen.unlocks"}
    assert bucket_type("aw-watcher-android-unlock", meta) == "unlock"
    assert bucket_type("mystery_phone", meta) == "unlock"


def test_bucket_type_synthesized_phone_afk_is_afk():
    """포크가 심는 `aw-watcher-android-afk` 는 노트북과 같은 언어를 쓰라고 만든 것이다.

    android 보다 afk 가 먼저 걸려야 롤업이 이걸 not-afk 구간 원천으로 쓴다.
    """
    meta = {"type": "afkstatus"}
    assert bucket_type("aw-watcher-android-afk", meta) == "afk"


# ── upsert_bucket ────────────────────────────────────────────────────────


def test_upsert_bucket_inserts_then_keeps_first_seen(conn):
    upsert_bucket(conn, WIN_BUCKET, _win_meta())
    row1 = conn.execute("SELECT * FROM aw_bucket WHERE bucket_id=?", (WIN_BUCKET,)).fetchone()
    assert row1["type"] == "window"
    assert row1["host"] == "pc"

    first_seen = row1["first_seen"]
    upsert_bucket(conn, WIN_BUCKET, _win_meta())  # 재호출 -> first_seen 은 유지, last_seen 은 갱신 가능
    row2 = conn.execute("SELECT * FROM aw_bucket WHERE bucket_id=?", (WIN_BUCKET,)).fetchone()
    assert row2["first_seen"] == first_seen
    assert row2["last_seen"] >= first_seen


# ── upsert_events ────────────────────────────────────────────────────────


def test_upsert_events_extracts_dedicated_columns_and_keeps_data_json(conn):
    upsert_bucket(conn, WIN_BUCKET, _win_meta())
    ev = AWEvent(bucket_id=WIN_BUCKET, ts=1000.0, duration=30.0, data={"app": "Code.exe", "title": "a.py"})
    n = upsert_events(conn, [ev])
    assert n == 1
    row = conn.execute("SELECT * FROM aw_event WHERE bucket_id=? AND ts=1000.0", (WIN_BUCKET,)).fetchone()
    assert row["app"] == "Code.exe"
    assert row["title"] == "a.py"
    assert row["ts_end"] == pytest.approx(1030.0)
    assert '"app"' in row["data_json"]


def test_upsert_events_afk_status_column(conn):
    upsert_bucket(conn, AFK_BUCKET, {"type": "afkstatus", "hostname": "pc"})
    ev = AWEvent(bucket_id=AFK_BUCKET, ts=500.0, duration=60.0, data={"status": "not-afk"})
    upsert_events(conn, [ev])
    row = conn.execute("SELECT status FROM aw_event WHERE bucket_id=? AND ts=500.0", (AFK_BUCKET,)).fetchone()
    assert row["status"] == "not-afk"


# ── ★ 핵심 시나리오: 하트비트 병합 재폴링 = 행 증가 없이 duration 만 갱신 ────────


def test_resync_growing_last_event_updates_duration_not_row_count(conn):
    cfg = _cfg(overlap_sec=900, backfill_days=7)
    events_first = [
        AWEvent(bucket_id=WIN_BUCKET, ts=1000.0, duration=50.0, data={"app": "Code.exe", "title": "a.py"}),
        AWEvent(bucket_id=WIN_BUCKET, ts=1100.0, duration=50.0, data={"app": "Code.exe", "title": "b.py"}),
        AWEvent(bucket_id=WIN_BUCKET, ts=1200.0, duration=50.0, data={"app": "Code.exe", "title": "c.py"}),
    ]
    client = _FakeClient({WIN_BUCKET: _win_meta()}, {WIN_BUCKET: events_first})

    result1 = sync(conn, client, cfg, now=2000.0)
    assert result1.errors == []
    count1 = conn.execute("SELECT COUNT(*) FROM aw_event WHERE bucket_id=?", (WIN_BUCKET,)).fetchone()[0]
    assert count1 == 3

    # 커서는 마지막으로 본 이벤트의 시작 시각(ts)이어야 한다 — ts_end(1250)나 now(2000)가 아니다.
    cursor = db.get_state_float(conn, f"aw_cursor:{WIN_BUCKET}")
    assert cursor == pytest.approx(1200.0)

    # 하트비트가 계속 병합돼 마지막 이벤트의 duration 만 자랐다 (id/timestamp 는 그대로).
    events_first[-1] = AWEvent(
        bucket_id=WIN_BUCKET, ts=1200.0, duration=500.0, data={"app": "Code.exe", "title": "c.py"}
    )

    result2 = sync(conn, client, cfg, now=2500.0)
    assert result2.errors == []

    # 행 수는 그대로여야 한다 — 새 이벤트로 조각나면 안 된다.
    count2 = conn.execute("SELECT COUNT(*) FROM aw_event WHERE bucket_id=?", (WIN_BUCKET,)).fetchone()[0]
    assert count2 == 3

    row = conn.execute(
        "SELECT duration, ts_end FROM aw_event WHERE bucket_id=? AND ts=1200.0", (WIN_BUCKET,)
    ).fetchone()
    assert row["duration"] == pytest.approx(500.0)
    assert row["ts_end"] == pytest.approx(1700.0)

    # 두 번째 폴링은 겹쳐서 재조회했어야 한다 (start <= 이전 커서).
    second_call_start = client.calls[-1][1]
    assert second_call_start <= 1200.0


# ── 버킷 하나가 실패해도 나머지는 계속 진행 ────────────────────────────────


def test_one_bucket_failing_does_not_block_others(conn):
    cfg = _cfg()
    ok_bucket = WIN_BUCKET
    bad_bucket = "aw-watcher-afk_pc"
    buckets = {
        ok_bucket: _win_meta(),
        bad_bucket: {"type": "afkstatus", "hostname": "pc"},
    }
    events_by_bucket = {
        ok_bucket: [AWEvent(bucket_id=ok_bucket, ts=100.0, duration=10.0, data={"app": "Code.exe"})],
    }
    client = _FakeClient(buckets, events_by_bucket, fail_buckets={bad_bucket})

    result = sync(conn, client, cfg, now=1000.0)

    assert result.buckets_seen == 2
    assert len(result.errors) == 1
    assert bad_bucket in result.errors[0]

    ok_count = conn.execute("SELECT COUNT(*) FROM aw_event WHERE bucket_id=?", (ok_bucket,)).fetchone()[0]
    assert ok_count == 1
    bad_count = conn.execute("SELECT COUNT(*) FROM aw_event WHERE bucket_id=?", (bad_bucket,)).fetchone()[0]
    assert bad_count == 0


def test_buckets_listing_failure_propagates():
    """버킷 목록 자체를 못 가져오면 동기화 전체가 무의미하므로 예외가 그대로 올라와야 한다."""

    class _AllFailClient:
        def buckets(self):
            raise AWError("연결 실패")

    cfg = _cfg()
    conn_ = db.connect(":memory:")
    db.init_db(conn_)
    try:
        with pytest.raises(AWError):
            sync(conn_, _AllFailClient(), cfg, now=1000.0)
    finally:
        conn_.close()


# ── 호스트 필터 ──────────────────────────────────────────────────────────


def test_host_filter_limits_synced_buckets(conn):
    cfg = _cfg(hosts=("pc-a",))
    buckets = {
        "aw-watcher-window_pc-a": {"hostname": "pc-a"},
        "aw-watcher-window_pc-b": {"hostname": "pc-b"},
    }
    events_by_bucket = {
        "aw-watcher-window_pc-a": [
            AWEvent(bucket_id="aw-watcher-window_pc-a", ts=10.0, duration=5.0, data={"app": "Code.exe"})
        ],
        "aw-watcher-window_pc-b": [
            AWEvent(bucket_id="aw-watcher-window_pc-b", ts=10.0, duration=5.0, data={"app": "Code.exe"})
        ],
    }
    client = _FakeClient(buckets, events_by_bucket)

    result = sync(conn, client, cfg, now=1000.0)

    assert result.buckets_seen == 1
    a_count = conn.execute(
        "SELECT COUNT(*) FROM aw_event WHERE bucket_id='aw-watcher-window_pc-a'"
    ).fetchone()[0]
    b_count = conn.execute(
        "SELECT COUNT(*) FROM aw_event WHERE bucket_id='aw-watcher-window_pc-b'"
    ).fetchone()[0]
    assert a_count == 1
    assert b_count == 0


# ── SyncResult 필드 존재 확인 ────────────────────────────────────────────


def test_sync_result_fields(conn):
    cfg = _cfg()
    client = _FakeClient({WIN_BUCKET: _win_meta()}, {WIN_BUCKET: []})
    result = sync(conn, client, cfg, now=1000.0)
    assert isinstance(result, SyncResult)
    assert result.window_end == pytest.approx(1000.0)
    assert result.window_start <= result.window_end



# ── 자라는 이벤트 (하트비트) ─────────────────────────────────────────────


def test_upsert_keeps_the_longer_duration(conn):
    """같은 `(bucket_id, ts)` 가 다시 오면 **긴 쪽**이 남아야 한다.

    ★ 2026-08-23 실측. AW 는 하트비트로 자라는 중인 이벤트를 성장 단계별로 여러
    벌 돌려준다. 08-23 afk 버킷 하루치에서 465건이 왔는데 고유 timestamp 는 66개,
    39개 그룹이 같은 `ts` 의 다른 길이였다:

        20:19:31  [16.6s, 3716.6s]    ← 62분짜리 연속 작업
        21:21:27  [202s, 207s, 226s]

    예전 upsert 는 `excluded` 를 그대로 써서 **마지막에 온 것이 이겼다.** 순서가
    보장되지 않아 짧은 쪽이 자주 남았고, 노트북 활동 9.4시간(08-17~23)이 사라졌다.
    롤업은 그 구간을 "afk 워처가 침묵한 공백"으로 읽어 폰에 넘겼다 — **에러 없이.**
    """
    upsert_bucket(conn, "aw-watcher-afk_pc", {"type": "afkstatus", "hostname": "pc"})
    grown = AWEvent(bucket_id="aw-watcher-afk_pc", ts=1000.0, duration=3716.0,
                    data={"status": "not-afk"}, event_id=1)
    stale = AWEvent(bucket_id="aw-watcher-afk_pc", ts=1000.0, duration=16.0,
                    data={"status": "not-afk"}, event_id=1)

    # 긴 것 뒤에 짧은 것이 와도 긴 것이 남는다 (예전에는 여기서 16.0 이 됐다)
    upsert_events(conn, [grown, stale])
    row = conn.execute("SELECT duration, ts_end FROM aw_event WHERE ts=1000.0").fetchone()
    assert row[0] == 3716.0, f"짧은 쪽이 이겼다: {row[0]}"
    assert row[1] == 4716.0

    # 순서를 뒤집어도 결과가 같다 — 순서에 의존하지 않는다는 것이 요점이다
    conn.execute("DELETE FROM aw_event")
    upsert_events(conn, [stale, grown])
    row = conn.execute("SELECT duration, ts_end FROM aw_event WHERE ts=1000.0").fetchone()
    assert row[0] == 3716.0
    assert row[1] == 4716.0


def test_upsert_still_updates_the_mutable_fields(conn):
    """길이만 MAX 로 잠근다 — 제목 같은 값은 새 것으로 갱신돼야 한다."""
    upsert_bucket(conn, "aw-watcher-window_pc", {"type": "currentwindow", "hostname": "pc"})
    upsert_events(conn, [AWEvent(bucket_id="aw-watcher-window_pc", ts=1000.0, duration=10.0,
                                 data={"app": "chrome.exe", "title": "옛 제목"}, event_id=1)])
    upsert_events(conn, [AWEvent(bucket_id="aw-watcher-window_pc", ts=1000.0, duration=20.0,
                                 data={"app": "chrome.exe", "title": "새 제목"}, event_id=1)])
    row = conn.execute("SELECT duration, title FROM aw_event WHERE ts=1000.0").fetchone()
    assert row[0] == 20.0
    assert row[1] == "새 제목"


# ── 서버가 잘라 준 판 흡수 (issues/0027) ─────────────────────────────────
#
# aw-server 는 이벤트를 **질의 창에 맞춰 잘라서** 준다. 실측:
#     start=+  0s → ts=11:54:22.687  dur=924.6s  id=24766
#     start=+600s → ts=12:04:22.687  dur=324.6s  id=24766
# 우리 PK 가 (bucket_id, ts) 라 잘린 판마다 새 행이 생겼고, PC afk 버킷이
# 총합 3,400시간 / 합집합 268시간(12.67배)까지 부풀었다.


def _ev(ts, dur, *, eid, bucket="b1", app="Code.exe"):
    return AWEvent(bucket_id=bucket, ts=ts, duration=dur, data={"app": app}, event_id=eid)


def _rows(conn, bucket="b1"):
    return [
        (round(r["ts"], 3), round(r["ts_end"], 3), r["event_id"])
        for r in conn.execute(
            "SELECT ts, ts_end, event_id FROM aw_event WHERE bucket_id=? ORDER BY ts", (bucket,)
        )
    ]


@pytest.fixture()
def bucket(conn):
    upsert_bucket(conn, "b1", {"id": "b1", "type": "afkstatus", "client": "aw-watcher-afk", "hostname": "pc"})
    conn.commit()
    return "b1"


def test_잘린_판이_새_행을_안_만든다(conn, bucket):
    """뒤늦게 온 잘린 판은 원본에 흡수돼야 한다."""
    upsert_events(conn, [_ev(1000.0, 900.0, eid=7)], spans=[])       # 원본 [1000, 1900)
    upsert_events(conn, [_ev(1600.0, 300.0, eid=7)], spans=[])       # 같은 이벤트, 창이 밀린 판

    assert _rows(conn) == [(1000.0, 1900.0, 7)], "잘린 판이 별도 행으로 남았다"


def test_잘린_판이_먼저_와도_합쳐진다(conn, bucket):
    """순서가 뒤집혀도 결과가 같아야 한다 — 폴링 순서는 보장되지 않는다."""
    upsert_events(conn, [_ev(1600.0, 300.0, eid=7)], spans=[])       # 잘린 판이 먼저
    upsert_events(conn, [_ev(1000.0, 900.0, eid=7)], spans=[])       # 원본이 나중

    assert _rows(conn) == [(1000.0, 1900.0, 7)]


def test_재사용된_id_는_안_합친다(conn, bucket):
    """★ 이 검사가 이 기능의 안전장치다.

    `event_id` 는 재사용된다 — `aw-watcher-android` 에서 5,152회 뒤로 점프하고,
    PC afk 에서도 0초짜리가 사라진 뒤 그 id 가 다른 시간대에 다시 쓰인 것이 4건 있다:

        id=7217  08-23 00:32:57 → 00:32:57  (0.0s, not-afk)
        id=7217  08-23 01:04:53 → 01:19:53  (900s, afk)   ← 전혀 다른 이벤트

    id 만 보고 합치면 **가짜 거대 구간**이 생긴다. 겹칠 때만 합쳐야 한다.
    """
    upsert_events(conn, [_ev(1000.0, 0.0, eid=7)], spans=[])         # 0초짜리
    upsert_events(conn, [_ev(5000.0, 900.0, eid=7)], spans=[])       # 한참 뒤, 같은 id

    assert _rows(conn) == [(1000.0, 1000.0, 7), (5000.0, 5900.0, 7)], (
        "떨어진 두 이벤트를 하나로 합쳐 가짜 구간을 만들었다"
    )


def test_id_가_없어도_겹치는_같은_내용은_합친다(conn, bucket):
    """★ 2026-09-07 에 **기대값을 뒤집었다.** 전에는 이 테스트가
    *"id 가 없으면 손대지 않는다"* 를 지켰다 — 그건 `_absorb_clipped_view` 의
    **한계를 적어 둔 것**이지 원하는 동작이 아니었다.

    여기 들어오는 두 행([1000,1900] · [1600,1900])은 **끝이 같고 시작만 다른**
    바로 그 잘린 판의 모양이다. 둘을 남기면 총합이 300초 부풀고,
    `event_id` 가 없다는 이유만으로 그걸 못 고칠 이유가 없다.

    `_absorb_duplicate_publication` 이 내용(app·title·url·status)과 겹침으로 판정하므로
    id 없이도 걸린다. 사본 회귀 검사에서 **모든 버킷의 합집합이 유지됐다** —
    합치는 것은 겹친 부분뿐이므로 정의상 그렇다.
    """
    upsert_events(conn, [_ev(1000.0, 900.0, eid=None)], spans=[])
    upsert_events(conn, [_ev(1600.0, 300.0, eid=None)], spans=[])

    assert _rows(conn) == [(1000.0, 1900.0, None)]


def test_다른_버킷의_같은_id_는_남남이다(conn, bucket):
    """여러 버킷은 event_id 공간을 공유할 수 있다. 반드시 버킷 단위로 묶어야 한다."""
    upsert_bucket(conn, "b2", {"id": "b2", "type": "afkstatus", "client": "aw-watcher-afk", "hostname": "pc"})
    upsert_events(conn, [_ev(1000.0, 900.0, eid=7)], spans=[])
    upsert_events(conn, [_ev(1600.0, 300.0, eid=7, bucket="b2")], spans=[])

    assert _rows(conn, "b1") == [(1000.0, 1900.0, 7)]
    assert _rows(conn, "b2") == [(1600.0, 1900.0, 7)]


# ── 상류 중복 발행 흡수 (2026-09-07 · issues/0027 두 번째 기제) ──────────────
#
# `aw-watcher-afk` 가 자리비움 동안 타임스탬프를 **뒤로 흘려** 서버 병합을 깨뜨린다
# (사본 회귀 표본에서 단조 하강 확인). 상류가 `event_id` 도 새로 매기므로
# `_absorb_clipped_view` 로는 못 잡는다. 여기서 잡는 것은 **내용과 겹침**이다.


def test_같은_내용이_겹치면_하나로_합친다(conn, bucket):
    """드리프트가 만든 모양 — 시작이 몇 ms 다르고 끝은 거의 같다."""
    upsert_events(conn, [_ev(1000.000, 900.0, eid=11)], spans=[])
    upsert_events(conn, [_ev(1000.360, 899.6, eid=12)], spans=[])   # 다른 id, 0.36초 밀림

    rows = _rows(conn)
    assert len(rows) == 1, f"드리프트 중복이 안 합쳐졌다: {rows}"
    assert rows[0][0] == 1000.0 and rows[0][1] == 1900.0, "합집합으로 안 넓혔다"


def test_맞닿기만_하는_것은_두_구간으로_남는다(conn, bucket):
    """★ 안 합쳐야 하는 쪽. 같은 앱을 연달아 쓴 것은 **진짜로 두 구간**이다.

    `_absorb_clipped_view` 는 맞닿는 것도 합치지만(잘린 판은 끝을 공유한다),
    이쪽은 내용만 보고 판정하므로 그 규칙을 그대로 쓰면 정상 데이터를 합쳐 버린다.
    """
    upsert_events(conn, [_ev(1000.0, 600.0, eid=21)], spans=[])   # [1000, 1600]
    upsert_events(conn, [_ev(1600.0, 300.0, eid=22)], spans=[])   # [1600, 1900]

    assert _rows(conn) == [(1000.0, 1600.0, 21), (1600.0, 1900.0, 22)]


def test_내용이_다르면_겹쳐도_안_합친다(conn, bucket):
    """겹침만으로 합치면 서로 다른 창이 하나가 된다. 판정은 **내용 + 겹침** 둘 다다."""
    upsert_events(conn, [_ev(1000.0, 900.0, eid=31, app="Code.exe")], spans=[])
    upsert_events(conn, [_ev(1200.0, 300.0, eid=32, app="firefox.exe")], spans=[])

    assert len(_rows(conn)) == 2


def test_0초_시점_사건은_건드리지_않는다(conn, bucket):
    """★ `aw-watcher-android-unlock` 은 전부 0초다 — **개수 자체가 뜻이다.**

    겹침 판정에 0초를 넣으면 같은 순간의 잠금해제 두 번이 한 번이 된다.
    세는 것을 줄이는 방향이라 조용히 틀린다. 회귀 검사에서도 이 버킷은
    **행 수가 변하지 않아야 한다.**
    """
    upsert_events(conn, [_ev(1000.0, 0.0, eid=41)], spans=[])
    upsert_events(conn, [_ev(1000.0, 0.0, eid=42)], spans=[])
    upsert_events(conn, [_ev(1500.0, 0.0, eid=43)], spans=[])

    assert len(_rows(conn)) == 2, "0초 시점 사건이 합쳐졌다 (같은 ts 는 PK 가 이미 합친다)"


def test_다른_버킷은_섞이지_않는다(conn, bucket):
    from lifetrainer.collect.aw_sync import upsert_bucket

    upsert_bucket(conn, "b2", {"id": "b2", "type": "afkstatus", "client": "aw-watcher-afk", "hostname": "pc"})
    upsert_events(conn, [_ev(1000.0, 900.0, eid=51)], spans=[])
    upsert_events(conn, [_ev(1200.0, 300.0, eid=52, bucket="b2")], spans=[])

    assert len(_rows(conn, "b1")) == 1 and len(_rows(conn, "b2")) == 1
