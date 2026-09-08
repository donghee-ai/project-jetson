"""lifetrainer.rollup.rollup 테스트.

전부 합성 픽스처를 aw_event/aw_bucket/manual_entry 에 직접 INSERT 해서 만든다
(다른 에이전트의 synthetic.py 에 의존하지 않는다). 네트워크 호출 없음.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest

from zoneinfo import ZoneInfo

from lifetrainer import db, timeutil
from lifetrainer.config import load_config
from lifetrainer.rollup.classify import Classifier, normalize_fingerprint
from lifetrainer.rollup.rollup import rollup_day

RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"
DAY = "2026-01-05"


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    # 다른 필드는 실제 프로젝트 기본값 그대로 쓰고, DB 경로만 테스트 전용 임시 파일로 바꾼다.
    return dataclasses.replace(base, db_path=tmp_path / "lt.db")


@pytest.fixture()
def conn(cfg):
    c = db.connect(cfg.db_path)
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(scope="module")
def classifier() -> Classifier:
    return Classifier.from_yaml(RULES_PATH)


# ── 픽스처 삽입 헬퍼 ─────────────────────────────────────────────────────


def _insert_bucket(conn, bucket_id: str, btype: str) -> None:
    conn.execute(
        "INSERT INTO aw_bucket(bucket_id, host, client, type, hostname, first_seen, last_seen) "
        "VALUES (?, 'test-host', ?, ?, 'test-host', 0, 0)",
        (bucket_id, btype, btype),
    )


def _insert_event(
    conn,
    bucket_id: str,
    ts: float,
    ts_end: float,
    *,
    app: str | None = None,
    title: str | None = None,
    url: str | None = None,
    status: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO aw_event(bucket_id, ts, ts_end, duration, event_id, app, title, url, status, data_json, synced_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, '{}', ?)
        """,
        (bucket_id, ts, ts_end, ts_end - ts, app, title, url, status, time.time()),
    )


def _insert_manual(conn, start_ts: float, end_ts: float, category: str, subcategory: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO manual_entry(start_ts, end_ts, category, subcategory, note, source, actor, revoked, created_at)
        VALUES (?, ?, ?, ?, NULL, 'cli', 'test', 0, ?)
        """,
        (start_ts, end_ts, category, subcategory, time.time()),
    )


def _setup_buckets(conn) -> None:
    _insert_bucket(conn, "window_test", "window")
    _insert_bucket(conn, "afk_test", "afk")
    _insert_bucket(conn, "web_test", "web")


def _slot_row(conn, day: str, slot: int):
    return conn.execute("SELECT * FROM slot WHERE day = ? AND slot = ?", (day, slot)).fetchone()


def _breakdown_rows(conn, day: str, slot: int):
    return conn.execute(
        "SELECT category, app, seconds FROM slot_breakdown WHERE day = ? AND slot = ? ORDER BY category, app",
        (day, slot),
    ).fetchall()


# ── 시나리오 1: 코딩 500s + 브라우징 100s 한 슬롯 ─────────────────────────


def test_slot_split_between_two_categories(conn, cfg, classifier):
    _setup_buckets(conn)
    tz = cfg.tz
    slot = 50
    s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)

    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, s + 500, app="Code.exe", title="main.py")
    _insert_event(conn, "window_test", s + 500, e, app="chrome.exe", title="Example Domain")
    _insert_event(conn, "web_test", s + 500, e, url="https://example.com/")

    result = rollup_day(conn, cfg, classifier, DAY)
    assert result.slots_written == cfg.slots_per_day

    row = _slot_row(conn, DAY, slot)
    assert row["category"] == "coding"
    assert row["winner_sec"] == pytest.approx(500.0)
    assert row["active_sec"] == pytest.approx(600.0)

    breakdown = {(r["category"], r["app"]): r["seconds"] for r in _breakdown_rows(conn, DAY, slot)}
    assert breakdown[("coding", "Code.exe")] == pytest.approx(500.0)
    assert breakdown[("browsing", "chrome.exe")] == pytest.approx(100.0)


# ── 시나리오 2: AFK 클리핑 ────────────────────────────────────────────────


def test_afk_clipping_moves_time_to_away(conn, cfg, classifier):
    _setup_buckets(conn)
    tz = cfg.tz
    slot = 60
    s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)

    # 창은 슬롯 내내 Code.exe 지만, 뒤 300초는 afk 였다.
    _insert_event(conn, "afk_test", s, s + 300, status="not-afk")
    _insert_event(conn, "afk_test", s + 300, e, status="afk")
    _insert_event(conn, "window_test", s, e, app="Code.exe", title="main.py")

    result = rollup_day(conn, cfg, classifier, DAY)
    assert result.slots_written == cfg.slots_per_day

    row = _slot_row(conn, DAY, slot)
    assert row["active_sec"] == pytest.approx(300.0)
    assert row["afk_sec"] == pytest.approx(300.0)
    # coding 과 away 가 정확히 300/300 으로 타이 -> classifier.order() 순으로 coding 이 이긴다.
    assert row["category"] == "coding"
    assert row["winner_sec"] == pytest.approx(300.0)

    breakdown = {(r["category"], r["app"]): r["seconds"] for r in _breakdown_rows(conn, DAY, slot)}
    assert breakdown[("coding", "Code.exe")] == pytest.approx(300.0)
    assert breakdown[("away", "Code.exe")] == pytest.approx(300.0)


# ── 시나리오 3: 이벤트가 전혀 없는 슬롯 ───────────────────────────────────


def test_empty_slot_becomes_off(conn, cfg, classifier):
    _setup_buckets(conn)
    slot = 70

    result = rollup_day(conn, cfg, classifier, DAY)
    assert result.slots_written == cfg.slots_per_day

    row = _slot_row(conn, DAY, slot)
    assert row["category"] == "off"
    assert row["source"] == "none"
    assert row["active_sec"] == pytest.approx(0.0)
    assert _breakdown_rows(conn, DAY, slot) == []


# ── 시나리오 4: 수동 입력이 슬롯을 덮으면 가중치 2배로 이긴다 ────────────


def test_manual_entry_wins_with_double_weight(conn, cfg, classifier):
    _setup_buckets(conn)
    tz = cfg.tz
    slot = 80
    s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)

    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, e, app="Code.exe", title="main.py")  # coding 600s (raw)
    _insert_manual(conn, s, s + 400, category="ops", subcategory="meeting")  # ops 400s * 2 = 800 (승자 결정용)

    result = rollup_day(conn, cfg, classifier, DAY)
    assert result.slots_written == cfg.slots_per_day

    row = _slot_row(conn, DAY, slot)
    assert row["category"] == "ops"
    assert row["subcategory"] == "meeting"
    assert row["source"] == "manual"

    # breakdown 은 정직해야 한다 — 가중치가 저장된 초에 반영되면 안 된다.
    breakdown = {(r["category"], r["app"]): r["seconds"] for r in _breakdown_rows(conn, DAY, slot)}
    assert breakdown[("coding", "Code.exe")] == pytest.approx(600.0)
    assert breakdown[("ops", "")] == pytest.approx(400.0)


# ── 시나리오 5: 멱등성 ────────────────────────────────────────────────────


def test_rollup_day_is_idempotent(conn, cfg, classifier):
    _setup_buckets(conn)
    tz = cfg.tz
    slot = 90
    s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)

    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, s + 350, app="Code.exe", title="main.py")
    _insert_event(conn, "window_test", s + 350, e, app="Slack.exe", title="#general")

    # updated_at 은 실행 시각을 찍는 부기(bookkeeping) 필드라 자연히 호출마다 달라진다 —
    # 의미 있는 값(카테고리/초 등)의 재현성을 보려는 것이므로 now 를 고정해 비교한다.
    fixed_now = 1_700_000_000.0

    result1 = rollup_day(conn, cfg, classifier, DAY, now=fixed_now)
    slots_1 = conn.execute("SELECT * FROM slot WHERE day = ? ORDER BY slot", (DAY,)).fetchall()
    breakdown_1 = conn.execute(
        "SELECT * FROM slot_breakdown WHERE day = ? ORDER BY slot, category, app", (DAY,)
    ).fetchall()

    result2 = rollup_day(conn, cfg, classifier, DAY, now=fixed_now)
    slots_2 = conn.execute("SELECT * FROM slot WHERE day = ? ORDER BY slot", (DAY,)).fetchall()
    breakdown_2 = conn.execute(
        "SELECT * FROM slot_breakdown WHERE day = ? ORDER BY slot, category, app", (DAY,)
    ).fetchall()

    assert result1.slots_written == result2.slots_written
    assert result1.breakdown_rows == result2.breakdown_rows
    assert len(slots_1) == len(slots_2)
    assert [tuple(r) for r in slots_1] == [tuple(r) for r in slots_2]
    assert [tuple(r) for r in breakdown_1] == [tuple(r) for r in breakdown_2]


# ── 시나리오 6: 슬롯 경계를 걸치는 이벤트 ─────────────────────────────────


def test_event_spanning_slot_boundary_is_split(conn, cfg, classifier):
    _setup_buckets(conn)
    tz = cfg.tz
    slot_a, slot_b = 100, 101
    s0, e0 = timeutil.slot_bounds(DAY, slot_a, tz, cfg.rollup.slot_minutes)
    s1, e1 = timeutil.slot_bounds(DAY, slot_b, tz, cfg.rollup.slot_minutes)
    assert e0 == s1  # 인접 슬롯이어야 테스트 의미가 있다

    _insert_event(conn, "afk_test", s0, e1, status="not-afk")
    # e0 기준으로 앞 200초, 뒤 300초에 걸치는 단일 이벤트.
    _insert_event(conn, "window_test", e0 - 200, e0 + 300, app="Code.exe", title="main.py")

    rollup_day(conn, cfg, classifier, DAY)

    row_a = _slot_row(conn, DAY, slot_a)
    row_b = _slot_row(conn, DAY, slot_b)

    breakdown_a = {(r["category"], r["app"]): r["seconds"] for r in _breakdown_rows(conn, DAY, slot_a)}
    breakdown_b = {(r["category"], r["app"]): r["seconds"] for r in _breakdown_rows(conn, DAY, slot_b)}

    assert breakdown_a[("coding", "Code.exe")] == pytest.approx(200.0)
    assert breakdown_b[("coding", "Code.exe")] == pytest.approx(300.0)
    assert row_a["category"] == "coding"
    assert row_b["category"] == "coding"


# ── 시나리오 7: 규칙에 안 걸린 창이 unclassified 에 누적 ─────────────────


def test_unmatched_window_accumulates_unclassified(conn, cfg, classifier):
    _setup_buckets(conn)
    tz = cfg.tz
    slot = 110
    s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)

    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, e, app="TotallyUnknownApp.exe", title="Mystery Window")

    result = rollup_day(conn, cfg, classifier, DAY)
    assert result.unclassified_seen == 1

    row = conn.execute(
        "SELECT * FROM unclassified WHERE app = 'TotallyUnknownApp.exe'"
    ).fetchone()
    assert row is not None
    assert row["seconds_total"] == pytest.approx(600.0)
    assert row["hits"] == 1
    assert row["title_sample"] == "Mystery Window"

    slot_row = _slot_row(conn, DAY, slot)
    assert slot_row["category"] == "unknown"


# ── unclassified_day: 재롤업 멱등성 / LLM 태깅 보존 / 날짜 합산 ──────────


def test_unclassified_day_idempotent_across_three_rollups(conn, cfg, classifier):
    """같은 날을 세 번 다시 롤업해도 unclassified.seconds_total 과 unclassified_day 가 부풀지 않는다."""
    _setup_buckets(conn)
    tz = cfg.tz
    slot = 115
    s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)

    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, e, app="MysteryApp.exe", title="Something")

    for _ in range(3):
        rollup_day(conn, cfg, classifier, DAY, now=1_700_000_000.0)

    fp = normalize_fingerprint("MysteryApp.exe", "Something")
    row = conn.execute("SELECT * FROM unclassified WHERE fingerprint = ?", (fp,)).fetchone()
    assert row is not None
    assert row["seconds_total"] == pytest.approx(600.0)
    assert row["hits"] == 1

    day_rows = conn.execute(
        "SELECT * FROM unclassified_day WHERE day = ? AND fingerprint = ?", (DAY, fp)
    ).fetchall()
    assert len(day_rows) == 1
    assert day_rows[0]["seconds"] == pytest.approx(600.0)
    assert day_rows[0]["hits"] == 1


def test_unclassified_preserves_existing_llm_tagging(conn, cfg, classifier):
    """이미 llm_category 가 채워진 지문을 재롤업해도 태깅 결과가 지워지면 안 된다."""
    _setup_buckets(conn)
    tz = cfg.tz
    slot = 116
    s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)

    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, e, app="MysteryApp2.exe", title="Something Else")

    rollup_day(conn, cfg, classifier, DAY)

    fp = normalize_fingerprint("MysteryApp2.exe", "Something Else")
    conn.execute(
        "UPDATE unclassified SET llm_category = 'learning', llm_subcategory = 'reading', "
        "llm_confidence = 0.9, llm_at = 123.0 WHERE fingerprint = ?",
        (fp,),
    )
    conn.commit()

    # 재롤업 (같은 날, 같은 이벤트) 해도 LLM 태깅 결과는 보존돼야 한다.
    rollup_day(conn, cfg, classifier, DAY)

    row = conn.execute("SELECT * FROM unclassified WHERE fingerprint = ?", (fp,)).fetchone()
    assert row["llm_category"] == "learning"
    assert row["llm_subcategory"] == "reading"
    assert row["llm_confidence"] == pytest.approx(0.9)
    assert row["llm_at"] == pytest.approx(123.0)
    # 정체성 필드는 여전히 정상적으로 최신화된다.
    assert row["seconds_total"] == pytest.approx(600.0)
    assert row["hits"] == 1


def test_unclassified_sums_across_multiple_days(conn, cfg, classifier):
    """서로 다른 두 날짜를 롤업하면 unclassified.seconds_total 이 두 날의 합이 된다."""
    _setup_buckets(conn)
    tz = cfg.tz
    day1, day2 = DAY, "2026-01-06"
    slot = 117

    s1, e1 = timeutil.slot_bounds(day1, slot, tz, cfg.rollup.slot_minutes)
    _insert_event(conn, "afk_test", s1, e1, status="not-afk")
    _insert_event(conn, "window_test", s1, e1, app="MysteryApp3.exe", title="Thing")
    rollup_day(conn, cfg, classifier, day1)

    s2, e2 = timeutil.slot_bounds(day2, slot, tz, cfg.rollup.slot_minutes)
    _insert_event(conn, "afk_test", s2, e2, status="not-afk")
    _insert_event(conn, "window_test", s2, e2, app="MysteryApp3.exe", title="Thing")
    rollup_day(conn, cfg, classifier, day2)

    fp = normalize_fingerprint("MysteryApp3.exe", "Thing")
    row = conn.execute("SELECT * FROM unclassified WHERE fingerprint = ?", (fp,)).fetchone()
    assert row["seconds_total"] == pytest.approx(1200.0)
    assert row["hits"] == 2

    day_rows = conn.execute(
        "SELECT day, seconds, hits FROM unclassified_day WHERE fingerprint = ? ORDER BY day", (fp,)
    ).fetchall()
    assert [r["day"] for r in day_rows] == [day1, day2]
    assert all(r["seconds"] == pytest.approx(600.0) and r["hits"] == 1 for r in day_rows)


# ── coverage / active_sec 요약 ────────────────────────────────────────────


def test_result_coverage_and_active_sec(conn, cfg, classifier):
    _setup_buckets(conn)
    tz = cfg.tz
    slot = 120
    s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)
    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, e, app="Code.exe", title="main.py")

    result = rollup_day(conn, cfg, classifier, DAY)

    n = cfg.slots_per_day
    # 나머지 슬롯은 전부 off 이므로 coverage 는 (n - (n-1)) / n = 1/n 근처.
    # ★ `sleep` 도 같이 센다 (2026-09-05). 수면은 off/away 에서 **추정한** 것이라
    #   여전히 재어지지 않은 시간이다 — 이름이 바뀌었다고 커버리지가 오르면 측정이
    #   나아진 것처럼 보인다. rollup.py 와 report/stats.py 가 같은 규칙을 쓴다.
    off_count = conn.execute(
        "SELECT COUNT(*) AS c FROM slot WHERE day = ? AND category IN ('off', 'sleep')", (DAY,)
    ).fetchone()["c"]
    assert result.coverage == pytest.approx((n - off_count) / n)
    assert result.active_sec >= 600.0


# ── 짧은 기기 전환 흡수 ──────────────────────────────────────────────────
#
# 폰을 잠깐 만지면 컴퓨터 입력이 끊겨 afk 워처가 away 로 잡고 창 이벤트도 끊긴다.
# 그대로 두면 "코딩 10분 / 자리비움 3분 / 코딩 10분" 세 토막이 되는데, 사람이 보기에
# 그건 그냥 코딩 23분이다. 잠깐 딴짓까지 전부 찍히면 플래너를 읽을 수 없다.

from lifetrainer.rollup.rollup import _dedupe_overlaps as absorb_dedupe, absorb_short_switches


def _win(ts, te, app="Code.exe"):
    return {"ts": ts, "ts_end": te, "app": app, "title": "t"}


def _afk(ts, te, status):
    return {"ts": ts, "ts_end": te, "status": status}


def test_짧은_전환은_앞_활동으로_흡수된다():
    """코딩 10분 → 폰 3분 → 코딩 10분 = 코딩 23분."""
    windows = [_win(0, 600), _win(780, 1380)]
    afks = [_afk(0, 600, "not-afk"), _afk(600, 780, "afk"), _afk(780, 1380, "not-afk")]

    count, seconds = absorb_short_switches(windows, afks, 300)

    assert (count, seconds) == (1, 180)
    assert windows[0]["ts_end"] == 780, "앞 창 이벤트가 공백을 덮도록 늘어나야 한다"
    assert afks[1]["status"] == "not-afk", "안 뒤집으면 클리핑에서 잘려나간다"


def test_긴_전환은_그대로_남는다():
    """5분 이상은 실제로 자리를 비운 것 — 플래너에 남아야 하는 정보다."""
    windows = [_win(0, 600), _win(1080, 1680)]
    afks = [_afk(600, 1080, "afk")]

    assert absorb_short_switches(windows, afks, 300) == (0, 0.0)
    assert windows[0]["ts_end"] == 600
    assert afks[0]["status"] == "afk"


def test_경계값은_흡수하지_않는다():
    windows = [_win(0, 600), _win(900, 1500)]
    assert absorb_short_switches(windows, [_afk(600, 900, "afk")], 300)[0] == 0


def test_뒤에_활동이_없으면_흡수하지_않는다():
    """하루 끝 공백까지 늘리면 '잠들기 전 코딩'이 아침까지 이어진다."""
    windows = [_win(0, 600)]
    assert absorb_short_switches(windows, [_afk(600, 700, "afk")], 300) == (0, 0.0)


def test_연속된_짧은_전환을_모두_흡수한다():
    windows = [_win(0, 300), _win(400, 700), _win(800, 1100)]
    afks = [_afk(300, 400, "afk"), _afk(700, 800, "afk")]

    count, seconds = absorb_short_switches(windows, afks, 300)

    assert (count, seconds) == (2, 200)
    assert [w["ts_end"] for w in windows] == [400, 800, 1100]
    assert all(a["status"] == "not-afk" for a in afks)


def test_임계값이_0이면_아무것도_안_한다():
    windows = [_win(0, 600), _win(780, 1380)]
    assert absorb_short_switches(windows, [_afk(600, 780, "afk")], 0) == (0, 0.0)
    assert windows[0]["ts_end"] == 600


def test_흡수는_afk_구간이_공백_안에_있을_때만_뒤집는다():
    """공백보다 긴 afk 이벤트를 통째로 뒤집으면 실제 자리비움까지 사라진다."""
    windows = [_win(0, 600), _win(780, 1380)]
    afks = [_afk(500, 1000, "afk")]  # 공백(600~780)보다 넓다

    absorb_short_switches(windows, afks, 300)

    assert afks[0]["status"] == "afk", "공백에 완전히 담기지 않으면 건드리지 않는다"


def test_공백과_경계가_어긋난_afk_는_겹친_부분만_잘라낸다():
    """실데이터에서 afk 이벤트와 창 공백은 경계가 딱 맞지 않는다.

    "완전히 포함될 때만 뒤집기" 로 했더니 창만 늘어나고 afk 는 그대로라
    그 구간이 통째로 away 로 분류돼 **away 가 오히려 늘었다.**
    """
    windows = [_win(0, 600), _win(780, 1380)]
    afks = [_afk(590, 800, "afk")]  # 공백(600~780)보다 넓다

    absorb_short_switches(windows, afks, 300)

    statuses = [(a["ts"], a["ts_end"], a["status"]) for a in afks]
    assert statuses == [(590, 600, "afk"), (600, 780, "not-afk"), (780, 800, "afk")]


def test_쪼갠_뒤에도_시간_총량은_보존된다():
    windows = [_win(0, 600), _win(780, 1380)]
    afks = [_afk(590, 800, "afk")]
    before = sum(a["ts_end"] - a["ts"] for a in afks)

    absorb_short_switches(windows, afks, 300)

    assert sum(a["ts_end"] - a["ts"] for a in afks) == before


def _absorbing_classifier(tmp_path) -> Classifier:
    """`MysteryGame.exe` 를 gaming 으로 흡수하는 규칙 파일을 만들어 로드한다.

    dict 를 직접 넣지 않고 실제 YAML 을 거친다 — 규칙 보강은 현실에서 파일 편집으로
    일어나고, 파싱 단계까지 같이 지나야 이 테스트가 그 상황을 재현한다.
    """
    path = tmp_path / "absorb-rules.yaml"
    path.write_text(
        "version: 1\n"
        "default_category: unknown\n"
        "categories:\n"
        '  - {id: gaming, label: 게임, color: "#8f2165"}\n'
        '  - {id: away, label: 자리비움, color: "#475569"}\n'
        '  - {id: unknown, label: 미분류, color: "#64748b"}\n'
        "rules:\n"
        "  - category: gaming\n"
        "    match: {app: '(?i)^mysterygame\\.exe$'}\n",
        encoding="utf-8",
    )
    return Classifier.from_yaml(path)


# ── 규칙 보강 후 미분류 정리 (2026-08-19 버그) ────────────────────────────
#
# 규칙을 추가하면 어떤 지문은 더 이상 미분류가 아니게 된다. 그런데 재계산 대상이
# "이번 롤업에서 미분류로 떨어진 것"뿐이면 사라진 지문의 합계가 옛날 값으로 굳는다.
# 실기기에서 게임 규칙을 넣은 직후 미분류 1~3위가 방금 분류한 게임 세 개였다.


def test_unclassified_total_drops_when_rule_absorbs_it(conn, cfg, classifier, tmp_path):
    """규칙에 흡수된 지문은 목록에서 사라져야 한다 (합계가 굳으면 안 된다)."""
    _setup_buckets(conn)
    tz = cfg.tz
    s, e = timeutil.slot_bounds(DAY, 100, tz, cfg.rollup.slot_minutes)

    # 1) 규칙에 없는 앱으로 한 번 롤업 → 미분류에 쌓인다
    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, e, app="MysteryGame.exe", title="MysteryGame")
    rollup_day(conn, cfg, classifier, DAY, now=1_700_000_000.0)

    fp = normalize_fingerprint("MysteryGame.exe", "MysteryGame")
    row = conn.execute("SELECT * FROM unclassified WHERE fingerprint = ?", (fp,)).fetchone()
    assert row is not None and row["seconds_total"] == pytest.approx(600.0)

    # 2) 그 앱을 잡는 규칙이 생긴 뒤 재롤업
    absorbing = _absorbing_classifier(tmp_path)
    rollup_day(conn, cfg, absorbing, DAY, now=1_700_000_100.0)

    assert conn.execute(
        "SELECT COUNT(*) FROM unclassified WHERE fingerprint = ?", (fp,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM unclassified_day WHERE fingerprint = ?", (fp,)
    ).fetchone()[0] == 0
    # 시간은 사라지지 않는다 — 새 카테고리로 옮겨갔을 뿐이다
    seconds = conn.execute(
        "SELECT COALESCE(SUM(seconds), 0) FROM slot_breakdown WHERE day = ? AND category = 'gaming'",
        (DAY,),
    ).fetchone()[0]
    assert seconds == pytest.approx(600.0)


def test_absorbed_fingerprint_survives_if_llm_tagged(conn, cfg, classifier, tmp_path):
    """태깅에 GPU 를 쓴 지문은 실적이 0이 돼도 남긴다 — 규칙 보강 근거로 봐야 한다."""
    _setup_buckets(conn)
    tz = cfg.tz
    s, e = timeutil.slot_bounds(DAY, 101, tz, cfg.rollup.slot_minutes)
    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, e, app="MysteryGame.exe", title="MysteryGame")
    rollup_day(conn, cfg, classifier, DAY, now=1_700_000_000.0)

    fp = normalize_fingerprint("MysteryGame.exe", "MysteryGame")
    conn.execute(
        "UPDATE unclassified SET llm_category = 'gaming', llm_at = 1 WHERE fingerprint = ?", (fp,)
    )
    conn.commit()

    absorbing = _absorbing_classifier(tmp_path)
    rollup_day(conn, cfg, absorbing, DAY, now=1_700_000_100.0)

    row = conn.execute("SELECT * FROM unclassified WHERE fingerprint = ?", (fp,)).fetchone()
    assert row is not None
    assert row["seconds_total"] == pytest.approx(0.0)
    assert row["llm_category"] == "gaming"


# ── 프라이빗 구간 ────────────────────────────────────────────────────────
#
# 프라이빗은 **네 번째 구조 상태**다 (off/away/unknown 옆). "수집 실패"가 아니라
# "재지 않기로 한 시간"이라 커버리지 분모에서 빠진다.



def _private(conn, start_ts: float, end_ts: float) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO private_span(start_ts, end_ts, kind, source, device, note, created_at, updated_at) "
        "VALUES (?, ?, 'live', 'test', NULL, NULL, ?, ?)",
        (start_ts, end_ts, now, now),
    )


def test_프라이빗_슬롯은_private_이고_breakdown_이_없다(conn, cfg, classifier):
    _setup_buckets(conn)
    tz = cfg.tz
    slot = 60
    s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)
    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, e, app="Code.exe", title="비밀 문서")
    _private(conn, s, e)

    rollup_day(conn, cfg, classifier, DAY)

    row = _slot_row(conn, DAY, slot)
    assert row["category"] == "private"
    assert row["private_sec"] == pytest.approx(600.0)
    assert _breakdown_rows(conn, DAY, slot) == [], "프라이빗 슬롯은 집계에 안 실린다"


def test_프라이빗은_커버리지_분모에서_빠진다(conn, cfg, classifier):
    """★ off 로 세면 프라이빗을 켤수록 "수집이 망가졌다"고 말하게 된다.

    프라이빗은 실패가 아니라 **재지 않기로 한 선택**이다. 분자에서만 빼면
    켤 때마다 커버리지가 떨어져, 사람이 그 숫자를 안 믿게 된다.
    """
    _setup_buckets(conn)
    tz = cfg.tz
    s0, _ = timeutil.slot_bounds(DAY, 0, tz, cfg.rollup.slot_minutes)
    _, e5 = timeutil.slot_bounds(DAY, 5, tz, cfg.rollup.slot_minutes)
    for slot in range(6):
        s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)
        _insert_event(conn, "afk_test", s, e, status="not-afk")
        _insert_event(conn, "window_test", s, e, app="Code.exe", title="main.py")
    _private(conn, s0, e5)                       # 활동이 있던 6슬롯 전부를 프라이빗으로

    result = rollup_day(conn, cfg, classifier, DAY)

    priv = conn.execute(
        "SELECT COUNT(*) AS c FROM slot WHERE day = ? AND category = 'private'", (DAY,)
    ).fetchone()["c"]
    assert priv == 6
    # 측정 대상이 0이면 나눌 것이 없다. NaN 도 예외도 아닌 0.0 이어야 한다.
    n = cfg.slots_per_day
    # `sleep` 도 재어지지 않은 시간이다 (위 주석과 같은 이유).
    off = conn.execute(
        "SELECT COUNT(*) AS c FROM slot WHERE day = ? AND category IN ('off', 'sleep')", (DAY,)
    ).fetchone()["c"]
    assert result.coverage == pytest.approx((n - priv - off) / (n - priv))


def test_롤업과_stats_가_같은_커버리지를_낸다(conn, cfg, classifier):
    """★ 같은 식이 rollup.py 와 report/stats.py 두 곳에 있다.

    한쪽만 고치면 `lt rollup` 출력과 웹 화면이 갈린다 — 이 저장소의 반복된 실패 2번
    (같은 값을 여러 곳에서 각자 계산했다).
    """
    from lifetrainer.report.stats import compute_daily

    _setup_buckets(conn)
    tz = cfg.tz
    for slot in range(6):
        s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)
        _insert_event(conn, "afk_test", s, e, status="not-afk")
        _insert_event(conn, "window_test", s, e, app="Code.exe", title="main.py")
    s0, _ = timeutil.slot_bounds(DAY, 0, tz, cfg.rollup.slot_minutes)
    _, e2 = timeutil.slot_bounds(DAY, 2, tz, cfg.rollup.slot_minutes)
    _private(conn, s0, e2)

    result = rollup_day(conn, cfg, classifier, DAY)
    stats = compute_daily(conn, cfg, DAY)

    assert stats.coverage == pytest.approx(result.coverage)


def test_짧은_프라이빗은_흡수된다_그러나_제목은_안_남는다(conn, cfg, classifier):
    """★ 이건 버그가 아니라 **의도된 동작**이다 (2026-09-01 결정).

    `absorb_short_switches` 는 5분 미만 공백을 앞 활동으로 덮는다. 3분짜리 프라이빗이
    "코딩"으로 칠해지는 것은 그 규칙의 의도와 일치한다 — 잠깐의 공백이 하루를
    조각내면 안 된다.

    새는 것은 "그 3분에 뭔가 하고 있었고 앞 활동으로 셌다"뿐이고, **무엇을 했는지는
    아니다.** 저장 관문이 원천에서 버리기 때문이다. 그 성질을 여기서 고정한다.
    """
    _setup_buckets(conn)
    tz = cfg.tz
    s, e = timeutil.slot_bounds(DAY, 30, tz, cfg.rollup.slot_minutes)
    mid_s, mid_e = s + 180.0, s + 360.0          # 슬롯 한가운데 3분

    _insert_event(conn, "afk_test", s, e, status="not-afk")
    _insert_event(conn, "window_test", s, mid_s, app="Code.exe", title="main.py")
    _insert_event(conn, "window_test", mid_e, e, app="Code.exe", title="main.py")
    # ★ 저장 관문을 뚫고 들어온 것처럼 **일부러** 프라이빗 한가운데에 창을 넣는다.
    #   (관문 자체는 test_private.py 가 본다. 여기서 보는 것은 롤업이 두 번째 방벽인가다)
    _insert_event(conn, "window_test", mid_s, mid_e, app="chrome.exe", title="은행 이체")
    _private(conn, mid_s, mid_e)

    rollup_day(conn, cfg, classifier, DAY)

    row = _slot_row(conn, DAY, 30)
    assert row["category"] == "coding", "3분 프라이빗은 앞 활동으로 흡수된다 (의도)"
    assert row["private_sec"] == pytest.approx(180.0), "그래도 몇 초가 프라이빗이었는지는 남는다"

    apps = {r["app"] for r in _breakdown_rows(conn, DAY, 30)}
    assert "chrome.exe" not in apps, "흡수는 시간을 메울 뿐 내용을 되살리지 않는다"
    unclassified = conn.execute(
        "SELECT COUNT(*) AS c FROM unclassified_day WHERE day = ? AND fingerprint LIKE '%chrome%'", (DAY,)
    ).fetchone()["c"]
    assert unclassified == 0, "미분류 지문으로도 새면 안 된다"


def test_프라이빗이_있어도_두_번_롤업하면_같다(conn, cfg, classifier):
    """멱등성. 구간을 빼는 위치가 트랜잭션 밖으로 새면 두 번째가 달라진다."""
    _setup_buckets(conn)
    tz = cfg.tz
    for slot in (10, 11, 12):
        s, e = timeutil.slot_bounds(DAY, slot, tz, cfg.rollup.slot_minutes)
        _insert_event(conn, "afk_test", s, e, status="not-afk")
        _insert_event(conn, "window_test", s, e, app="Code.exe", title="main.py")
    s10, _ = timeutil.slot_bounds(DAY, 10, tz, cfg.rollup.slot_minutes)
    _, e11 = timeutil.slot_bounds(DAY, 11, tz, cfg.rollup.slot_minutes)
    _private(conn, s10, e11)

    def snapshot():
        slots = conn.execute(
            "SELECT slot, category, active_sec, private_sec, winner_sec FROM slot "
            "WHERE day = ? ORDER BY slot", (DAY,)
        ).fetchall()
        bd = conn.execute(
            "SELECT slot, category, app, seconds FROM slot_breakdown WHERE day = ? "
            "ORDER BY slot, category, app", (DAY,)
        ).fetchall()
        return [tuple(r) for r in slots], [tuple(r) for r in bd]

    rollup_day(conn, cfg, classifier, DAY)
    first = snapshot()
    rollup_day(conn, cfg, classifier, DAY)
    assert snapshot() == first


# ── _dedupe_overlaps 가 같은 상태 뭉치에 하는 일 ─────────────────────────
#
# ★ 아래 테스트들은 **좋은 동작을 지키는 것이 아니다.** 지금 그렇다는 것을 못 박아
#   둔 것이고, 그중 하나는 명시적으로 **고쳐야 할 동작**이다.
#
# ## 왜 만들었나
#
# 2026-09-03, 상류 aw-server v0.13.2 가 같은 시작(0.4ms 안)으로 afk 를 118건 발행한
# 밤에 PC 의 490.5분이 정규화 뒤 6.1분 + 0초짜리 슬리버 117개가 됐다.
# 그것을 보고 **"밤 8시간이 사라진다"고 결론 내고 이슈 우선순위를 올렸다.**
#
# 틀렸다. 파이프라인 끝에서 재 보니 **격자는 한 칸도 안 바뀌었다** — 그 슬롯은 어느
# 쪽이든 `off` 이고, 바뀌는 것은 리포트의 "자리비움" 숫자뿐이었다.
# 원본의 큰 수치와 사람이 겪는 것 사이에 흡수하는 층이 몇 개나 있었다.
#
#   전말: HISTORY/2026-09-03-the-raw-table-was-twelve-times-off-and-the-screen-did-not-move.md
#   이슈: docs/issues/w-0014-dedupe-overlaps-has-a-hole.md  (`w-` = 재 보고 안 고치기로 함)
#
# ## 그래서 이 테스트가 하는 일
#
# **다시 발견하지 않게 한다.** 원본 수치가 커 보여 또 놀라는 일을 막고, 동시에
# 그것이 화면에서는 무해하다는 것도 같이 못 박는다
# (`test_영향은_파이프라인_끝에서_잰다` 가 그 몫이다).
#
# ## 고치려는 사람에게
#
# ★ 2026-09-03 에 **안 고치기로 판정했다**(`w-`). 일주일치를 재 보니 커버리지는 0 변화,
#   활동 최대 3분, 자리비움 최대 11분이었다. 그러니 아래는 "지금 해라" 가 아니라
#   **다시 열게 됐을 때의 방향**이다.
#
# 같은 상태를 자르지 말고 union 하도록 바꾸는 것이 옳은 방향이다
# (한 버킷에서 겹치는 `afk` 둘은 상태 전환이 아니라 같은 상태의 중복 관측이다).
# 그러면 `test_같은_상태_뭉치는_슬리버가_된다` 가 **깨진다. 그게 정상이다** —
# 깨진 테스트를 고치면서 새 기대값과 그 근거를 여기 적으면 된다.
# 나머지 둘은 **그대로 통과해야 한다**:
#   `test_상태가_다르면_자르는_것이_옳다`      상태 전환에서 자르는 성질
#   `test_영향은_파이프라인_끝에서_잰다`        격자가 안 바뀌는 성질


def test_같은_상태_뭉치는_슬리버가_된다(conn, cfg, classifier):
    """★ 이것은 **바람직한 동작이 아니다.** 지금 그렇다는 것을 고정할 뿐이다.

    `_dedupe_overlaps` 는 `(ts, ts_end)` 로 정렬한 뒤 각 이벤트를 **다음 이벤트의
    시작**으로 자른다. 시작이 거의 같으면 앞의 것들이 전부 슬리버가 되고, 살아남는
    것은 가장 늦게 시작한 하나 — 그런데 그것이 가장 짧을 수 있다.
    """
    base = 1_800_000_000.0
    events = [
        {"ts": base + 0.000, "ts_end": base + 3600.0, "status": "afk"},   # 가장 김
        {"ts": base + 0.001, "ts_end": base + 1800.0, "status": "afk"},
        {"ts": base + 0.002, "ts_end": base + 600.0, "status": "afk"},    # 가장 짧고 가장 늦게 시작
    ]
    out = absorb_dedupe(events)

    durations = sorted(round(e["ts_end"] - e["ts"], 3) for e in out)
    assert durations == [0.001, 0.001, 599.998], (
        "가장 긴 이벤트가 살아남지 않는다 — 앞의 둘은 1ms 슬리버가 된다"
    )
    total = sum(e["ts_end"] - e["ts"] for e in out)
    assert total == pytest.approx(600.0), "3600초를 덮던 뭉치가 600초로 줄어든다"


def test_상태가_다르면_자르는_것이_옳다(conn, cfg, classifier):
    """★ 반대로 여기서는 자르는 것이 맞다.

    한 버킷은 하나의 타임라인이고 같은 순간에 두 상태가 참일 수 없다.
    나중 관측을 신뢰해 앞을 자른다 — 이 성질은 고치면 안 된다.
    """
    base = 1_800_000_000.0
    events = [
        {"ts": base, "ts_end": base + 3600.0, "status": "afk"},
        {"ts": base + 600.0, "ts_end": base + 1200.0, "status": "not-afk"},
    ]
    out = sorted(absorb_dedupe(events), key=lambda e: e["ts"])

    assert [(round(e["ts"] - base), round(e["ts_end"] - base), e["status"]) for e in out] == [
        (0, 600, "afk"),
        (600, 1200, "not-afk"),
    ], "상태가 바뀌는 지점에서는 앞 이벤트가 잘려야 한다"


def test_영향은_파이프라인_끝에서_잰다(conn, cfg, classifier):
    """★ `test_같은_상태_뭉치는_슬리버가_된다` 가 보여준 손실이 **화면에서는 무해하다**는
    것을 같이 못 박는다.

    이게 없으면 다음 사람이 그 테스트만 보고 또 "8시간이 사라진다"고 놀란다 —
    그게 2026-09-03 에 실제로 일어난 일이다.

    원본에서 3600초가 600초로 줄어도 **격자는 안 바뀐다.** 그 슬롯은 어느 쪽이든
    `off` 다 — afk 만 있고 활동이 없는 슬롯이기 때문이다.

    "원본이 몇 배냐"가 아니라 **"사람이 보는 것이 뭐가 달라지나"** 를 물어야 한다.
    """
    _setup_buckets(conn)
    tz = cfg.tz
    s, e = timeutil.slot_bounds(DAY, 60, tz, cfg.rollup.slot_minutes)
    # 같은 시작으로 겹겹이 — 상류 중복을 그대로 흉내낸다
    for i in range(5):
        _insert_event(conn, "afk_test", s + i * 0.001, e, status="afk")

    rollup_day(conn, cfg, classifier, DAY)

    row = _slot_row(conn, DAY, 60)
    assert row["category"] == "off", "중복이 몇 겹이든 활동 없는 afk 슬롯은 off 다"
    assert row["active_sec"] == 0.0


# ── 수면 추정 (2026-09-05) ────────────────────────────────────────────
#
# 사람이 요청했다 — "자리비움이 연속 3시간 이상이면 수면으로 추정".
#
# ★ 재 보니 **밤은 `away` 가 아니라 `off`(결측)** 였다. 08-30~09-04 실측에서
#   `away` 최장 연속이 0.2~0.5시간뿐이라, 자리비움만 보면 **한 번도 안 걸린다.**
#   기기를 안 쓰면 워처가 아무것도 안 보내 "자리를 비웠다"가 아니라 "관측이 없다"로
#   떨어지기 때문이다. 그래서 away+off 를 같이 본다.
#
# **이 테스트들이 깨지면** 수면 숫자가 조용히 틀린다 — 화면에는 그럴듯한 값이 뜬다.


def _rollup(conn, cfg, day, *, now=None):
    rollup_day(conn, cfg, Classifier.from_yaml(cfg.rollup.rules_path), day, now=now)


def _seed_quiet_day(conn, cfg, *, quiet_hours: float, now_hours_in: float | None = None) -> str:
    """조용한 구간 하나만 남기고 **나머지 시간은 매시간 활동으로 채운** 하루.

    ★ 이렇게 가두지 않으면 무엇을 재는지 흐려진다. 상한이 23시간이라, 활동 하나만
      넣으면 그 **뒤쪽 21시간짜리 조용한 구간까지 수면**이 되어 "3시간 미만은 아니다"
      를 검사할 수가 없다. 그래서 목표 구간만 비우고 나머지는 매시간 끊는다.

    ★ 창 이벤트만으로는 활동으로 안 쳐진다 — afk 가 not-afk 를 말해 줘야 `active_sec`
      이 찬다. 안 그러면 그 칸도 `off` 가 되어 구간이 안 끊긴다 (여기서 한 번 헤맸다).

    `now_hours_in` 을 주면 하루가 그만큼만 지난 것으로 롤업한다 (미래 슬롯 시험).
    """
    day = "2026-09-04"
    tz = ZoneInfo(cfg.timezone)
    day_start, _ = timeutil.day_bounds(day, tz, boundary_hour=cfg.rollup.day_boundary_hour)
    _setup_buckets(conn)

    # 조용한 구간 **밖은 빈틈없이** 채운다. 매시간 10분씩만 넣었더니 사이의 50분이
    # 같이 조용해져서, 인자로 준 길이와 실제 구간이 안 맞았다 (2.5h 를 줬는데 3.83h).
    quiet_start = day_start + 2.0 * 3600.0    # 앞을 활동으로 막아 구간을 가둔다
    quiet_end = quiet_start + quiet_hours * 3600.0
    day_end = day_start + 24 * 3600.0
    for lo, hi in ((day_start, quiet_start), (quiet_end, day_end)):
        if hi <= lo:
            continue
        _insert_event(conn, "afk_test", lo, hi, status="not-afk")
        _insert_event(conn, "window_test", lo, hi, app="Code.exe", title="작업")
    conn.commit()

    now = day_start + (now_hours_in * 3600.0 if now_hours_in is not None else 24 * 3600.0)
    _rollup(conn, cfg, day, now=now)
    return day


def _sleep_slots(conn, day):
    return [
        r["slot"]
        for r in conn.execute(
            "SELECT slot FROM slot WHERE day = ? AND category = 'sleep' ORDER BY slot", (day,)
        )
    ]


def test_결측이_세시간_이상_이어지면_수면이다(conn, cfg):
    """밤에 실제로 찍히는 것은 `off` 다. 이게 안 걸리면 기능 자체가 안 돈다."""
    from lifetrainer.rollup.rollup import SLEEP_MIN_HOURS

    day = _seed_quiet_day(conn, cfg, quiet_hours=SLEEP_MIN_HOURS + 1)
    assert len(_sleep_slots(conn, day)) >= int(SLEEP_MIN_HOURS * 6)


def test_세시간_미만은_수면이_아니다(conn, cfg):
    """★ 안 울려야 하는 쪽. 잠깐 자리를 비운 것을 잠으로 세면 숫자가 못 쓰게 된다.

    낮잠·중간에 깬 밤은 **사람이 표에서 고친다** — 추정이 욕심을 내면 안 된다.
    """
    from lifetrainer.rollup.rollup import SLEEP_MIN_HOURS

    day = _seed_quiet_day(conn, cfg, quiet_hours=SLEEP_MIN_HOURS - 0.5)
    assert _sleep_slots(conn, day) == []


def test_아직_안_온_시간은_수면이_아니다(conn, cfg):
    """★ 이게 없으면 아침에 "오늘 12시간 잤다" 가 뜬다.

    오늘 격자의 뒤쪽은 전부 `off` 다 — 실측에서 09-05 의 away+off 최장 연속이
    12.5시간으로 나왔는데 대부분이 **미래**였다.
    """
    day = _seed_quiet_day(conn, cfg, quiet_hours=20.0, now_hours_in=3.0)
    # 하루가 1시간밖에 안 지났으므로 3시간짜리 수면이 있을 수 없다.
    assert _sleep_slots(conn, day) == []


def test_사람이_고친_수면이_합계에_들어온다(conn, cfg):
    """추정이 못 잡는 낮잠을 사람이 표에서 표시한다. 그게 숫자에 안 들어오면 안 고친다."""
    from lifetrainer.plan.override import set_override_range
    from lifetrainer.report.stats import compute_daily

    day = _seed_quiet_day(conn, cfg, quiet_hours=0.5)
    before = compute_daily(conn, cfg, day).sleep_sec

    set_override_range(conn, day, 80, 92, "sleep", actor="test")
    conn.commit()
    _rollup(conn, cfg, day)

    after = compute_daily(conn, cfg, day).sleep_sec
    assert after - before == pytest.approx(12 * cfg.rollup.slot_minutes * 60.0)


# ── 수면이 논리적 하루 경계를 넘는다 (2026-09-05 오후) ────────────────
#
# 하루씩 따로 보면 **06:00 에서 잠이 잘린다.** 23시~07시를 자면 앞날은 7시간(잡힘),
# 다음날은 1시간(문턱 미달)이 되어 **아침 몫이 통째로 빠진다.**
# 04시~07시처럼 경계에 걸친 짧은 잠은 양쪽 다 미달이라 아예 사라진다.


def _seed_bracketed(conn, cfg, sleep_from: float, sleep_to: float):
    """`sleep_from`~`sleep_to`(에폭)만 비우고 앞뒤 이틀을 활동으로 채운다."""
    tz = cfg.tz
    d0, _ = timeutil.day_bounds("2026-09-03", tz, boundary_hour=cfg.rollup.day_boundary_hour)
    _setup_buckets(conn)
    for lo, hi in ((d0, sleep_from), (sleep_to, d0 + 3 * 24 * 3600.0)):
        if hi <= lo:
            continue
        _insert_event(conn, "afk_test", lo, hi, status="not-afk")
        _insert_event(conn, "window_test", lo, hi, app="Code.exe", title="작업")
    conn.commit()
    now = d0 + 3 * 24 * 3600.0
    for day in ("2026-09-03", "2026-09-04", "2026-09-05"):
        _rollup(conn, cfg, day, now=now)


def test_경계에_걸친_잠은_양쪽에_다_적힌다(conn, cfg):
    """★ 이 기능의 요점. 23시~07시면 **다음날 아침 1시간도** 수면이어야 한다.

    이게 없으면 경계 앞은 잠이고 경계 뒤는 아닌 비대칭이 남는다 —
    그리고 사람이 보는 "오늘의 수면" 에서 아침 몫이 매일 빠진다.
    """
    tz = cfg.tz
    d0, _ = timeutil.day_bounds("2026-09-03", tz, boundary_hour=cfg.rollup.day_boundary_hour)
    # 논리적 하루는 06:00 시작. 23:00 = +17h, 다음날 07:00 = +25h
    _seed_bracketed(conn, cfg, d0 + 17 * 3600.0, d0 + 25 * 3600.0)

    assert len(_sleep_slots(conn, "2026-09-03")) > 0, "경계 앞이 안 잡혔다"
    morning = _sleep_slots(conn, "2026-09-04")
    assert morning, "★ 경계 뒤(다음날 아침)가 빠졌다 — 하루씩 따로 본 것이다"
    assert morning[0] == 0, "잠은 논리적 하루의 첫 칸부터 이어져야 한다"


def test_경계에_걸친_짧은_잠도_잡힌다(conn, cfg):
    """04시~07시(3시간)는 양쪽 다 미달이라 **따로 보면 통째로 사라진다.**"""
    tz = cfg.tz
    d0, _ = timeutil.day_bounds("2026-09-03", tz, boundary_hour=cfg.rollup.day_boundary_hour)
    _seed_bracketed(conn, cfg, d0 + 22 * 3600.0, d0 + 25 * 3600.0)  # 04:00~07:00

    total = len(_sleep_slots(conn, "2026-09-03")) + len(_sleep_slots(conn, "2026-09-04"))
    assert total >= 18, f"경계에 걸친 3시간이 사라졌다 (잡힌 칸 {total})"


def test_사람이_고친_칸은_추정이_안_덮는다(conn, cfg):
    """★ 보정이 추정을 이긴다. 덮으면 그때부터 아무도 안 고친다."""
    from lifetrainer.plan.override import set_override_range

    tz = cfg.tz
    d0, _ = timeutil.day_bounds("2026-09-03", tz, boundary_hour=cfg.rollup.day_boundary_hour)
    _seed_bracketed(conn, cfg, d0 + 17 * 3600.0, d0 + 25 * 3600.0)

    # 한밤중 한 칸을 "코딩" 이라고 사람이 적는다 (실제로 깨서 일했다)
    set_override_range(conn, "2026-09-03", 110, 111, "coding", actor="test")
    conn.commit()
    _rollup(conn, cfg, "2026-09-03", now=d0 + 3 * 24 * 3600.0)

    row = conn.execute(
        "SELECT category FROM slot WHERE day = '2026-09-03' AND slot = 110"
    ).fetchone()
    assert row["category"] == "coding"
