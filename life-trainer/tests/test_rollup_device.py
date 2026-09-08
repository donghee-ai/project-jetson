"""롤업의 기기 차원과 중재 — **마지막 상호작용이 그 시간을 소유한다**.

폰이 들어오기 전에는 롤업이 버킷을 기기 구분 없이 타입별로 합쳤다. 그대로 폰을
붙이면 두 가지가 조용히 깨진다:

1. 폰의 창 이벤트가 **노트북의 afk 구간**으로 클리핑돼 전부 `away` 가 된다
   (폰을 쓰는 동안 노트북은 당연히 afk 다)
2. 두 기기 활동이 한 슬롯에서 합산돼 **하루가 24시간을 넘는다**

2번은 [08-17 겹침 부풀림](../HISTORY/2026-08-17-aw-overlap-inflation.md)과 같은
부류이고, 합성 데이터로는 원리적으로 안 잡히는 종류였다. 그래서 여기서는 불변식
(`슬롯 활동 합 ≤ 슬롯 길이`)을 **직접 단언한다**.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest

from lifetrainer import db, timeutil
from lifetrainer.rollup.classify import Classifier
from lifetrainer.rollup.rollup import rollup_day
from lifetrainer.config import load_config

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"
DAY = "2026-01-05"

PC = "DESKTOP-TEST"
PHONE = "phone-example"


@pytest.fixture()
def cfg(tmp_path):
    return dataclasses.replace(load_config(), db_path=tmp_path / "lt.db")


@pytest.fixture()
def conn(cfg):
    c = db.connect(cfg.db_path)
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(scope="module")
def classifier() -> Classifier:
    return Classifier.from_yaml(RULES_PATH)


# ── 픽스처 ────────────────────────────────────────────────────────────────


def _bucket(conn, bucket_id: str, btype: str, device: str, kind: str) -> None:
    device_id = db.upsert_device(conn, device, kind=kind, hostname=device)
    conn.execute(
        "INSERT INTO aw_bucket(bucket_id, host, client, type, hostname, device_id, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
        (bucket_id, device, btype, btype, device, device_id),
    )


def _event(conn, bucket_id, ts, ts_end, *, app=None, title=None, status=None) -> None:
    conn.execute(
        "INSERT INTO aw_event(bucket_id, ts, ts_end, duration, event_id, app, title, url, status, "
        "data_json, synced_at) VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, ?, '{}', ?)",
        (bucket_id, ts, ts_end, ts_end - ts, app, title, status, time.time()),
    )


def _setup(conn, *, phone_afk: bool = False) -> None:
    _bucket(conn, "pc-window", "window", PC, "laptop")
    _bucket(conn, "pc-afk", "afk", PC, "laptop")
    _bucket(conn, "ph-android", "android", PHONE, "phone")
    if phone_afk:
        _bucket(conn, "ph-afk", "afk", PHONE, "phone")


def _slot_bounds(cfg, slot: int) -> tuple[float, float]:
    return timeutil.slot_bounds(DAY, slot, cfg.tz, cfg.rollup.slot_minutes)


def _breakdown(conn, slot: int) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        "SELECT category, app, seconds FROM slot_breakdown WHERE day = ? AND slot = ?", (DAY, slot)
    ).fetchall()
    return {(r["category"], r["app"] or ""): r["seconds"] for r in rows}


def _slot(conn, slot: int):
    return conn.execute("SELECT * FROM slot WHERE day = ? AND slot = ?", (DAY, slot)).fetchone()


# ── 틀어둔 미디어 vs 실제 작업 (2026-08-25) ───────────────────────────────
#
# 실제 사고의 구조만 합성한 회귀 사례다. 폰 영상이 연속 재생되는 동안 afk 워처는
# 침묵했고, 노트북은 not-afk 로 상호작용을 보고했다. 보고 공백을 앱 세션으로 메운
# 폰이 슬롯을 가져가면 안 된다.


def test_playing_media_does_not_beat_a_device_that_was_touched(conn, cfg, classifier):
    """폰 afk 가 침묵한 구간의 앱 세션은 **노트북의 not-afk 를 못 이긴다.**"""
    _setup(conn, phone_afk=True)
    s, e = _slot_bounds(cfg, 60)

    # 노트북: 슬롯 내내 코딩하고 afk 가 not-afk 라고 말한다
    _event(conn, "pc-window", s, e, app="Code.exe", title="rollup.py")
    _event(conn, "pc-afk", s, e, status="not-afk")
    # 폰: 유튜브가 더 늦게 시작해 슬롯 끝까지 이어진다.
    _event(conn, "ph-android", s + 120, e, app="com.google.android.youtube", title="영상")
    # ★ afk 워처는 **살아 있다**(아침에 보고했다). 다만 이 슬롯에서는 Doze 로 침묵했다.
    #   빈 버킷과 구별해야 한다 — 워처가 아예 없는 기기는 앱 세션 말고 판단 재료가 없다.
    morning_s, morning_e = _slot_bounds(cfg, 10)
    _event(conn, "ph-afk", morning_s, morning_e, status="not-afk")

    rollup_day(conn, cfg, classifier, DAY)
    bd = _breakdown(conn, 60)
    coding = sum(v for (cat, _app), v in bd.items() if cat == "coding")
    phone = sum(v for (_cat, app), v in bd.items() if "youtube" in app)
    assert coding > phone, f"코딩이 이겨야 한다 (coding={coding}, phone={phone})"
    assert coding >= 540, "슬롯의 대부분을 노트북이 가져가야 한다"


def test_phone_still_wins_when_it_is_the_only_device_awake(conn, cfg, classifier):
    """★ 폰이 항상 지면 안 된다. 경쟁자가 없으면 보고 공백 세션도 그대로 이긴다.

    이동 중·자기 전 폰 사용이 이 경우다 (`HISTORY/2026-08-25-a-video-left-playing-outranked-the-work.md` 의 '고친 것').
    """
    _setup(conn, phone_afk=True)
    s, e = _slot_bounds(cfg, 61)
    _event(conn, "pc-afk", s, e, status="afk")          # 노트북은 자리비움이라고 **말했다**
    _event(conn, "ph-android", s, e, app="com.google.android.youtube", title="영상")

    rollup_day(conn, cfg, classifier, DAY)
    bd = _breakdown(conn, 61)
    phone = sum(v for (_cat, app), v in bd.items() if "youtube" in app)
    assert phone >= 540, f"폰이 가져가야 한다 (phone={phone})"


def test_touched_phone_still_beats_the_laptop(conn, cfg, classifier):
    """진짜 기기 전환은 그대로 동작한다 — 폰이 **만졌다고 말하면** 늦게 시작한 쪽이 이긴다."""
    _setup(conn, phone_afk=True)
    s, e = _slot_bounds(cfg, 62)
    _event(conn, "pc-window", s, e, app="Code.exe", title="rollup.py")
    _event(conn, "pc-afk", s, e, status="not-afk")
    _event(conn, "ph-android", s + 120, e, app="com.kakao.talk", title="카카오톡")
    _event(conn, "ph-afk", s + 120, e, status="not-afk")   # ★ 폰도 만졌다

    rollup_day(conn, cfg, classifier, DAY)
    bd = _breakdown(conn, 62)
    phone = sum(v for (_cat, app), v in bd.items() if "kakao" in app)
    assert phone >= 400, f"나중에 만진 폰이 그 시간을 가져가야 한다 (phone={phone})"


# ── 폰만 있을 때 ──────────────────────────────────────────────────────────


def test_phone_alone_is_not_all_away(conn, cfg, classifier):
    """★ 회귀의 핵심. 안드로이드엔 AFK 워처가 없어서, 예전 구조라면 전부 away 가 됐다."""
    s, e = _slot_bounds(cfg, 60)
    _setup(conn)
    _event(conn, "ph-android", s, e, app="com.kakao.talk")

    rollup_day(conn, cfg, classifier, DAY)

    row = _slot(conn, 60)
    assert row["active_sec"] == pytest.approx(e - s)
    assert ("away", "com.kakao.talk") not in _breakdown(conn, 60)


def test_phone_afk_bucket_is_authoritative_when_present(conn, cfg, classifier):
    """포크가 화면·터치로 afk 를 합성해 주면 그것이 정본이다 — 앱 세션 추론보다 우선한다."""
    s, e = _slot_bounds(cfg, 60)
    _setup(conn, phone_afk=True)
    _event(conn, "ph-android", s, e, app="com.kakao.talk")
    # 앱 세션은 슬롯 내내지만, 화면은 앞 절반만 켜져 있었다.
    _event(conn, "ph-afk", s, s + 300, status="not-afk")
    _event(conn, "ph-afk", s + 300, e, status="afk")

    rollup_day(conn, cfg, classifier, DAY)

    assert _slot(conn, 60)["active_sec"] == pytest.approx(300.0)


# ── 중재: 마지막 상호작용이 이긴다 ─────────────────────────────────────────


def test_phone_left_on_while_working_on_pc_goes_to_pc(conn, cfg, classifier):
    """폰을 켜둔 채 노트북에서 작업 → **노트북**. (노트북 활동이 더 늦게 시작했다)"""
    s, e = _slot_bounds(cfg, 60)
    _setup(conn)
    _event(conn, "ph-android", s, e, app="com.google.youtube")  # 슬롯 내내 켜둠
    _event(conn, "pc-afk", s + 60, e, status="not-afk")  # 1분 뒤부터 노트북 사용
    _event(conn, "pc-window", s + 60, e, app="Code.exe", title="main.py")

    rollup_day(conn, cfg, classifier, DAY)

    bd = _breakdown(conn, 60)
    # 노트북이 켜진 뒤로는 전부 노트북이다.
    assert bd.get(("coding", "Code.exe")) == pytest.approx(e - s - 60)
    # 노트북이 아직 활동 전이던 첫 1분만 폰이 갖는다 — 그때는 폰이 유일한 활동이었다.
    phone_sec = sum(sec for (_, app), sec in bd.items() if app == "com.google.youtube")
    assert phone_sec == pytest.approx(60.0)


def test_touching_phone_during_pc_work_moves_to_phone(conn, cfg, classifier):
    """노트북 작업 중 폰을 만짐 → 그 구간은 **폰**, 이후 다시 노트북."""
    s, e = _slot_bounds(cfg, 60)
    _setup(conn)
    _event(conn, "pc-afk", s, e, status="not-afk")
    _event(conn, "pc-window", s, e, app="Code.exe", title="main.py")
    # 슬롯 중간에 폰을 6분 만졌다 (5분 흡수 임계값보다 길다).
    _event(conn, "ph-android", s + 120, s + 480, app="com.kakao.talk")

    rollup_day(conn, cfg, classifier, DAY)

    bd = _breakdown(conn, 60)
    phone_sec = sum(sec for (_, app), sec in bd.items() if app == "com.kakao.talk")
    assert phone_sec == pytest.approx(360.0)
    # 폰이 가져간 만큼 노트북에서 빠진다 — 두 번 세지 않는다.
    assert bd.get(("coding", "Code.exe")) == pytest.approx((e - s) - 360.0)


def test_short_phone_use_is_absorbed_into_pc(conn, cfg, classifier):
    """★ 흡수가 중재보다 먼저다.

    "코딩 → 폰 3분 → 코딩" 은 코딩으로 이어져야 한다 (CLAUDE.md 기본값).
    흡수가 정한 구간을 중재가 폰에게 되돌려주면 둘이 싸운다.
    """
    s, e = _slot_bounds(cfg, 60)
    _setup(conn)
    # 노트북 창이 3분 끊긴다 (= 폰 만지는 동안).
    _event(conn, "pc-window", s, s + 120, app="Code.exe", title="main.py")
    _event(conn, "pc-window", s + 300, e, app="Code.exe", title="main.py")
    _event(conn, "pc-afk", s, s + 120, status="not-afk")
    _event(conn, "pc-afk", s + 120, s + 300, status="afk")
    _event(conn, "pc-afk", s + 300, e, status="not-afk")
    _event(conn, "ph-android", s + 120, s + 300, app="com.kakao.talk")

    rollup_day(conn, cfg, classifier, DAY)

    bd = _breakdown(conn, 60)
    assert bd.get(("coding", "Code.exe")) == pytest.approx(e - s)
    assert ("phone", "com.kakao.talk") not in bd
    assert bd.get(("etc", "com.kakao.talk")) is None


# ── 불변식 ────────────────────────────────────────────────────────────────


def test_two_devices_never_exceed_slot_length(conn, cfg, classifier):
    """★ 08-17 부풀림과 같은 부류. 기기 차원이 없으면 여기서 두 배가 된다."""
    s, e = _slot_bounds(cfg, 60)
    _setup(conn)
    _event(conn, "pc-afk", s, e, status="not-afk")
    _event(conn, "pc-window", s, e, app="Code.exe", title="main.py")
    _event(conn, "ph-android", s, e, app="com.kakao.talk")

    rollup_day(conn, cfg, classifier, DAY)

    row = _slot(conn, 60)
    assert row["active_sec"] <= (e - s) + 1e-6
    assert sum(_breakdown(conn, 60).values()) <= (e - s) + 1e-6


def test_full_day_never_exceeds_24h(conn, cfg, classifier):
    """하루 전체로도 확인한다 — 슬롯마다 맞아도 합에서 어긋날 수 있다."""
    day_start, day_end = timeutil.day_bounds(DAY, cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)
    _setup(conn)
    _event(conn, "pc-afk", day_start, day_end, status="not-afk")
    _event(conn, "pc-window", day_start, day_end, app="Code.exe", title="main.py")
    _event(conn, "ph-android", day_start, day_end, app="com.kakao.talk")

    result = rollup_day(conn, cfg, classifier, DAY)

    assert result.active_sec <= (day_end - day_start) + 1e-6
    total = conn.execute(
        "SELECT COALESCE(SUM(seconds), 0) AS s FROM slot_breakdown WHERE day = ?", (DAY,)
    ).fetchone()["s"]
    assert total <= (day_end - day_start) + 1e-6


# ── 기존 동작 보존 ────────────────────────────────────────────────────────


def test_device_id_null_buckets_still_roll_up(conn, cfg, classifier):
    """마이그레이션 이전에 만들어진 `device_id` NULL 버킷도 계속 집계된다."""
    s, e = _slot_bounds(cfg, 60)
    conn.execute(
        "INSERT INTO aw_bucket(bucket_id, host, client, type, hostname, first_seen, last_seen) "
        "VALUES ('old-window', 'oldpc', 'window', 'window', 'oldpc', 0, 0)"
    )
    conn.execute(
        "INSERT INTO aw_bucket(bucket_id, host, client, type, hostname, first_seen, last_seen) "
        "VALUES ('old-afk', 'oldpc', 'afk', 'afk', 'oldpc', 0, 0)"
    )
    _event(conn, "old-afk", s, e, status="not-afk")
    _event(conn, "old-window", s, e, app="Code.exe", title="main.py")

    rollup_day(conn, cfg, classifier, DAY)

    assert _breakdown(conn, 60).get(("coding", "Code.exe")) == pytest.approx(e - s)


# ── afk 보고 공백 (2026-08-23) ────────────────────────────────────────────
# ★ 이 규칙은 **폰 전용**이다. 노트북에 적용했더니 잠든 시간이 코딩으로 찍혔다.


def _dev(kind, *, window=(), afk=(), android=()):
    from lifetrainer.rollup.rollup import _DeviceDay

    d = _DeviceDay(key=1, kind=kind)
    d.buckets["window"] = [{"ts": s, "ts_end": e} for s, e in window]
    d.buckets["android"] = [{"ts": s, "ts_end": e} for s, e in android]
    d.buckets["afk"] = [{"ts": s, "ts_end": e, "status": st} for s, e, st in afk]
    return d


def test_노트북은_afk_공백을_활동으로_보지_않는다():
    """★ 익명 회귀 사례: 자는 동안 VS Code 를 켜뒀는데 afk 타임라인에 정규화가 만든
    구멍이 있었고, 그 구멍이 코딩으로 찍혔다.

    노트북의 워처는 상주한다 — 공백은 워처가 죽어서가 아니라 **우리 쪽 정규화**가
    남긴 빈틈이다. 그것을 활동의 증거로 삼으면 안 된다.
    """
    from lifetrainer.rollup.rollup import _activity_intervals

    dev = _dev("laptop", window=[(0, 10_000)], afk=[(0, 2_000, "afk")])
    assert _activity_intervals(dev) == [], "afk 공백을 활동으로 읽었다"


def test_폰은_afk_공백을_활동으로_본다():
    """폰의 워처는 Doze 에서 멈추고 앱 세션은 소급 기록된다 — 공백에선 세션이 증거다."""
    from lifetrainer.rollup.rollup import _activity_intervals

    dev = _dev("phone", android=[(0, 10_000)], afk=[(0, 2_000, "afk")])
    got = _activity_intervals(dev)
    assert got == [(2_000, 10_000)], f"폰의 보고 공백이 활동으로 안 잡혔다: {got}"


def test_노트북도_not_afk_는_그대로_활동이다():
    from lifetrainer.rollup.rollup import _activity_intervals

    dev = _dev("laptop", window=[(0, 10_000)], afk=[(0, 3_000, "not-afk"), (3_000, 10_000, "afk")])
    assert _activity_intervals(dev) == [(0, 3_000)]


# ── 폰 미디어 제목 (2026-08-25) ────────────────────────────────────────────
#
# 안드로이드 앱 세션이 주는 title 은 **앱 이름**이라 유튜브를 두 시간 봐도
# 전부 "YouTube" 였다. `aw-watcher-android-media` 가 곡·영상 제목을 따로
# 보고하므로 같은 앱의 세션에 얹는다.
#
# ★ 그 워처의 버킷은 `type='android'` 로 등록돼 있고 이름만 `-media` 다.
#   타입만 보면 재생 상태 전환(대부분 길이 0)이 앱 세션과 같은 통에 섞인다.


def test_media_title_replaces_the_app_label(conn, cfg, classifier):
    _setup(conn)
    _bucket(conn, "ph-android-media", "android", PHONE, "phone")  # ★ 타입이 android 다
    s, e = _slot_bounds(cfg, 70)
    _event(conn, "ph-android", s, e, app="com.google.android.youtube", title="YouTube")
    _event(conn, "ph-android-media", s + 60, s + 60, app="com.google.android.youtube", title="어떤 영상 제목")

    rollup_day(conn, cfg, classifier, DAY)
    row = _slot(conn, 70)
    assert row["top_app"] == "com.google.android.youtube"
    assert row["top_title"] == "어떤 영상 제목"


def test_media_title_does_not_leak_across_apps(conn, cfg, classifier):
    """백그라운드 음악이 다른 앱 세션의 제목을 바꾸면 '무엇을 했나'가 거짓이 된다."""
    _setup(conn)
    _bucket(conn, "ph-android-media", "android", PHONE, "phone")
    s, e = _slot_bounds(cfg, 71)
    _event(conn, "ph-android", s, e, app="com.kakao.talk", title="카카오톡")
    _event(conn, "ph-android-media", s + 60, s + 60, app="com.google.android.youtube", title="영상 제목")

    rollup_day(conn, cfg, classifier, DAY)
    assert _slot(conn, 71)["top_title"] == "카카오톡"


def test_media_events_are_not_counted_as_app_sessions(conn, cfg, classifier):
    """미디어 버킷만 있고 앱 세션이 없으면 그 슬롯은 활동이 아니다."""
    _setup(conn)
    _bucket(conn, "ph-android-media", "android", PHONE, "phone")
    s, e = _slot_bounds(cfg, 72)
    _event(conn, "ph-android-media", s, e, app="com.google.android.youtube", title="영상")

    rollup_day(conn, cfg, classifier, DAY)
    assert not _breakdown(conn, 72), "재생 상태 이벤트가 활동으로 세어지면 안 된다"


# ── 폰 웹 제목 (2026-09-04) ────────────────────────────────────────────────
#
# 안드로이드 앱 세션이 주는 title 은 **앱 이름**이라 크롬으로 뭘 봐도 "Chrome" 이었다.
# 웹 버킷에 페이지 제목이 들어와 있는데(폰 앱이 09-03 부터 채운다) `contribs` 에는
# window 이벤트만 들어가고 `top_title` 은 거기서 뽑히므로 오는 길이 없었다.


def _web_event(conn, bucket_id, ts, ts_end, *, url, title) -> None:
    """`_event` 는 url 을 NULL 로 박으므로 웹 이벤트는 따로 넣는다."""
    conn.execute(
        "INSERT INTO aw_event(bucket_id, ts, ts_end, duration, event_id, app, title, url, "
        "status, data_json, synced_at) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, NULL, '{}', ?)",
        (bucket_id, ts, ts_end, ts_end - ts, title, url, time.time()),
    )


def test_웹_제목이_앱_이름_자리를_대신한다(conn, cfg, classifier):
    """`"Chrome"` 이 144칸에 백 번 찍히면 그 칸들은 아무것도 말하지 않는다."""
    _setup(conn)
    _bucket(conn, "ph-web", "web", PHONE, "phone")
    s, e = _slot_bounds(cfg, 72)
    _event(conn, "ph-android", s, e, app="com.android.chrome", title="Chrome")
    _web_event(conn, "ph-web", s, e, url="m.example-forum.com/best", title="인기글 - 예시 커뮤니티")

    rollup_day(conn, cfg, classifier, DAY)
    assert _slot(conn, 72)["top_title"] == "인기글 - 예시 커뮤니티"


def test_웹_제목이_비면_원래_제목을_그대로_둔다(conn, cfg, classifier):
    """짧은 전환 이벤트는 url 만 있고 제목이 아직 안 붙는다. 빈 것으로 덮으면 잃는다."""
    _setup(conn)
    _bucket(conn, "ph-web", "web", PHONE, "phone")
    s, e = _slot_bounds(cfg, 73)
    _event(conn, "ph-android", s, e, app="com.android.chrome", title="Chrome")
    _web_event(conn, "ph-web", s, e, url="m.example-forum.com/best", title="")

    rollup_day(conn, cfg, classifier, DAY)
    assert _slot(conn, 73)["top_title"] == "Chrome"


def test_웹_제목은_브라우저가_아닌_앱에_새지_않는다(conn, cfg, classifier):
    """백그라운드 탭 제목이 카카오톡 세션에 붙으면 '무엇을 했나'가 거짓이 된다."""
    _setup(conn)
    _bucket(conn, "ph-web", "web", PHONE, "phone")
    s, e = _slot_bounds(cfg, 74)
    _event(conn, "ph-android", s, e, app="com.kakao.talk", title="카카오톡")
    _web_event(conn, "ph-web", s, e, url="m.example-forum.com/best", title="예시 커뮤니티")

    rollup_day(conn, cfg, classifier, DAY)
    assert _slot(conn, 74)["top_title"] == "카카오톡"


def test_웹_제목은_집계를_움직이지_않는다(conn, cfg, classifier):
    """★ 표시용이다. `classify` 에 먹이면 카테고리가 움직여 집계 원천이 바뀐다.

    집계 원천은 `slot_breakdown` 이고 `slot` 의 winner-takes-all 은 시각화 전용이다
    (CLAUDE.md). 이 테스트가 그 경계를 지킨다 — 깨지면 제목이 분류로 샌 것이다.
    """
    _setup(conn)
    _bucket(conn, "ph-web", "web", PHONE, "phone")
    s, e = _slot_bounds(cfg, 75)
    _event(conn, "ph-android", s, e, app="com.android.chrome", title="Chrome")
    # 제목만으로 다른 카테고리로 끌려갈 만한 문자열을 일부러 쓴다.
    _web_event(conn, "ph-web", s, e, url="m.example-forum.com/best", title="github pull request 코딩")

    rollup_day(conn, cfg, classifier, DAY)
    with_web = _breakdown(conn, 75)

    conn.execute("DELETE FROM aw_event WHERE bucket_id = 'ph-web'")
    rollup_day(conn, cfg, classifier, DAY)
    assert _breakdown(conn, 75) == with_web, "웹 제목이 분류를 움직였다"
