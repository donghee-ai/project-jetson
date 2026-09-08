"""lifetrainer.collect.synthetic 테스트. 네트워크 없음 — HTTP 를 아예 거치지 않는 모듈이다.

날짜 선택에 주의: `generate()` 는 `now` 이후의 이벤트를 만들지 않는다(버그 수정 #2).
그래서 재현성·비겹침 같은 "결과가 항상 같아야 하는" 테스트는 실행 시각과 무관하게
안정적이도록, 실제 오늘(시스템 시각 기준)보다 확실히 과거인 날짜를 앵커로 쓴다.
`now` 관련 동작 자체를 검증하는 테스트만 실제 오늘 날짜를 쓴다.
"""

from __future__ import annotations

import pytest

from lifetrainer import db
from lifetrainer.collect.synthetic import generate
from lifetrainer.config import load_config
from lifetrainer.timeutil import day_bounds, day_str, now_ts

# 과거로 고정된 앵커 날짜 — 이 세션이 언제 실행되든 "오늘"보다 항상 과거다.
# (테스트 스위트가 먼 미래에 실행되더라도 실패하지 않도록, 실행 시점의 실제 오늘 날짜보다
#  최소 며칠 전인지 fixture 에서 확인한다.)
PAST_END = "2020-01-12"  # 일요일


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    yield c
    c.close()


def _cfg():
    return load_config()


def _rows(conn, host="synth-test"):
    return conn.execute(
        "SELECT bucket_id, ts, ts_end, duration, app, title, url, status "
        "FROM aw_event WHERE bucket_id LIKE ? ORDER BY bucket_id, ts",
        (f"%_{host}",),
    ).fetchall()


def test_past_end_anchor_is_actually_in_the_past():
    """PAST_END 가 실제로 과거인지 자체 점검 — 아니면 아래 테스트들의 전제가 깨진다."""
    cfg = _cfg()
    assert PAST_END < day_str(now_ts(), cfg.tz)


# ── 기본 동작 ────────────────────────────────────────────────────────────


def test_generate_returns_event_count_matching_db(conn):
    cfg = _cfg()
    n = generate(conn, cfg, days=5, end_day=PAST_END, seed=1, host="synth-test")
    assert n > 0
    total = conn.execute(
        "SELECT COUNT(*) FROM aw_event WHERE bucket_id LIKE '%_synth-test'"
    ).fetchone()[0]
    assert total == n


def test_generate_creates_three_bucket_types(conn):
    cfg = _cfg()
    generate(conn, cfg, days=3, end_day=PAST_END, seed=1, host="synth-test")
    rows = conn.execute(
        "SELECT bucket_id, type FROM aw_bucket WHERE bucket_id LIKE '%_synth-test'"
    ).fetchall()
    types = {r["type"] for r in rows}
    assert types == {"window", "afk", "web"}
    ids = {r["bucket_id"] for r in rows}
    assert ids == {
        "aw-watcher-window_synth-test",
        "aw-watcher-afk_synth-test",
        "aw-watcher-web-chrome_synth-test",
    }


# ── 재현성 ───────────────────────────────────────────────────────────────


def test_same_seed_is_reproducible(tmp_path):
    cfg = _cfg()
    conn1 = db.connect(tmp_path / "a.db")
    db.init_db(conn1)
    conn2 = db.connect(tmp_path / "b.db")
    db.init_db(conn2)
    try:
        n1 = generate(conn1, cfg, days=6, end_day=PAST_END, seed=99, host="synth-repro")
        n2 = generate(conn2, cfg, days=6, end_day=PAST_END, seed=99, host="synth-repro")
        assert n1 == n2
        rows1 = _rows(conn1, host="synth-repro")
        rows2 = _rows(conn2, host="synth-repro")
        assert [tuple(r) for r in rows1] == [tuple(r) for r in rows2]
    finally:
        conn1.close()
        conn2.close()


def test_different_seed_changes_result(tmp_path):
    cfg = _cfg()
    conn1 = db.connect(tmp_path / "a.db")
    db.init_db(conn1)
    conn2 = db.connect(tmp_path / "b.db")
    db.init_db(conn2)
    try:
        generate(conn1, cfg, days=4, end_day=PAST_END, seed=1, host="synth-diff")
        generate(conn2, cfg, days=4, end_day=PAST_END, seed=2, host="synth-diff")
        rows1 = [tuple(r) for r in _rows(conn1, host="synth-diff")]
        rows2 = [tuple(r) for r in _rows(conn2, host="synth-diff")]
        assert rows1 != rows2
    finally:
        conn1.close()
        conn2.close()


# ── 버킷 내부 비겹침 + ts 정렬 ─────────────────────────────────────────────


def test_events_within_bucket_do_not_overlap_and_are_sorted(conn):
    cfg = _cfg()
    generate(conn, cfg, days=10, end_day=PAST_END, seed=42, host="synth-test")
    rows = _rows(conn)
    by_bucket: dict[str, list] = {}
    for r in rows:
        by_bucket.setdefault(r["bucket_id"], []).append(r)

    assert len(by_bucket) == 3
    for bucket_id, evs in by_bucket.items():
        assert len(evs) > 0
        for i in range(len(evs) - 1):
            cur_end = evs[i]["ts_end"]
            nxt_start = evs[i + 1]["ts"]
            assert evs[i]["ts"] <= evs[i + 1]["ts"], f"{bucket_id} 의 ts 가 오름차순이 아님"
            assert cur_end <= nxt_start + 1e-6, (
                f"{bucket_id} 이벤트가 겹침: {evs[i]['ts']}~{cur_end} vs {nxt_start}"
            )


# ── 현실적인 데이터 (앱/도메인/구조) ─────────────────────────────────────


def test_realistic_apps_and_domains_present(conn):
    cfg = _cfg()
    generate(conn, cfg, days=14, end_day=PAST_END, seed=7, host="synth-test")

    apps = {
        r["app"]
        for r in conn.execute(
            "SELECT DISTINCT app FROM aw_event WHERE bucket_id='aw-watcher-window_synth-test'"
        ).fetchall()
    }
    expected_apps = {"Code.exe", "WindowsTerminal.exe", "Slack.exe", "Zoom.exe", "chrome.exe"}
    assert expected_apps & apps, f"기대한 앱이 하나도 없음: {apps}"

    urls = [
        r["url"]
        for r in conn.execute(
            "SELECT url FROM aw_event WHERE bucket_id='aw-watcher-web-chrome_synth-test'"
        ).fetchall()
    ]
    assert urls, "web 버킷에 이벤트가 없음"
    known_domains = ("github.com", "arxiv.org", "stackoverflow.com", "youtube.com", "reddit.com", "news.ycombinator.com")
    assert any(any(d in u for d in known_domains) for u in urls)

    statuses = {
        r["status"]
        for r in conn.execute(
            "SELECT DISTINCT status FROM aw_event WHERE bucket_id='aw-watcher-afk_synth-test'"
        ).fetchall()
    }
    assert statuses == {"afk", "not-afk"}


def test_afk_segments_are_not_entirely_empty(conn):
    """afk 구간이 window 이벤트와 일관되게 맞물려야 한다 — afk 이벤트 자체가 텅 비면 안 된다."""
    cfg = _cfg()
    generate(conn, cfg, days=5, end_day=PAST_END, seed=3, host="synth-test")
    afk_count = conn.execute(
        "SELECT COUNT(*) FROM aw_event WHERE bucket_id='aw-watcher-afk_synth-test' AND status='afk'"
    ).fetchone()[0]
    not_afk_count = conn.execute(
        "SELECT COUNT(*) FROM aw_event WHERE bucket_id='aw-watcher-afk_synth-test' AND status='not-afk'"
    ).fetchone()[0]
    assert afk_count > 0
    assert not_afk_count > 0


def test_day_has_sleep_gap_before_first_event(conn):
    """기상 전에는 이벤트 자체가 없는 gap(수면)이어야 한다."""
    cfg = _cfg()
    generate(conn, cfg, days=1, end_day=PAST_END, seed=5, host="synth-test")
    day_start, _day_end = day_bounds(PAST_END, cfg.tz)
    first_ts = conn.execute(
        "SELECT MIN(ts) FROM aw_event WHERE bucket_id LIKE '%_synth-test'"
    ).fetchone()[0]
    assert first_ts is not None
    assert first_ts - day_start >= 3600.0  # 최소 한 시간 이상은 수면 gap


# ── ★ 회귀 테스트 (버그 1: 하루가 오후에 끝나던 문제) ────────────────────


def _day_active_sec(conn, day: str, tz, host: str = "synth-test") -> float:
    ds, de = day_bounds(day, tz)
    return conn.execute(
        "SELECT COALESCE(SUM(duration), 0) FROM aw_event "
        "WHERE bucket_id = ? AND status = 'not-afk' AND ts >= ? AND ts < ?",
        (f"aw-watcher-afk_{host}", ds, de),
    ).fetchone()[0]


def _day_last_event_ts_end(conn, day: str, tz, host: str = "synth-test") -> float | None:
    ds, de = day_bounds(day, tz)
    return conn.execute(
        "SELECT MAX(ts_end) FROM aw_event WHERE bucket_id LIKE ? AND ts >= ? AND ts < ?",
        (f"%_{host}", ds, de),
    ).fetchone()[0]


def _past_days_list(end_day: str, days: int) -> list[str]:
    """end_day 를 포함해 과거로 `days` 일치 날짜 리스트(오래된 순)를 만든다."""
    import datetime as _dt

    end_date = _dt.date.fromisoformat(end_day)
    return [(end_date - _dt.timedelta(days=days - 1 - i)).isoformat() for i in range(days)]


def test_last_activity_ends_after_21_local_time(conn):
    """마지막 활동이 오후 1~2시쯤 끝나던 버그의 회귀 테스트. 매일 밤 21시는 넘겨야 한다."""
    cfg = _cfg()
    days = 8
    generate(conn, cfg, days=days, end_day=PAST_END, seed=17, host="synth-test")
    for day in _past_days_list(PAST_END, days):
        last_ts_end = _day_last_event_ts_end(conn, day, cfg.tz)
        assert last_ts_end is not None, f"{day} 에 이벤트가 없음"
        day_start, _ = day_bounds(day, cfg.tz)
        threshold = day_start + 21 * 3600.0  # 로컬 21:00
        assert last_ts_end >= threshold, (
            f"{day} 의 마지막 활동이 21시 전에 끝남 — 하루가 너무 일찍 끝나는 버그 재발"
        )


def test_daily_active_hours_within_target_range(conn):
    """활동 시간 목표(하루 5~9시간)에 들어오는지 — 이전 버그는 활동 3~4시간짜리 하루를 만들었다."""
    cfg = _cfg()
    days = 10
    generate(conn, cfg, days=days, end_day=PAST_END, seed=23, host="synth-test")
    for day in _past_days_list(PAST_END, days):
        active_hours = _day_active_sec(conn, day, cfg.tz) / 3600.0
        assert 5.0 <= active_hours <= 9.0, f"{day} 활동시간 {active_hours:.2f}h — 목표 5~9시간 범위 밖"


def test_weekday_vs_weekend_category_distribution_differs(conn):
    """평일/주말 스케줄 구성이 실제로 달라야 한다 (앱 사용 비중을 카테고리 대용으로 확인)."""
    cfg = _cfg()
    import datetime as _dt

    days = 21  # 3주치 — 평일/주말 표본이 충분해야 우연히 같아지지 않는다
    generate(conn, cfg, days=days, end_day=PAST_END, seed=29, host="synth-test")
    days_list = _past_days_list(PAST_END, days)

    coding_apps = ("Code.exe", "WindowsTerminal.exe")
    weekday_coding = weekday_total = weekend_coding = weekend_total = 0.0

    for day in days_list:
        ds, de = day_bounds(day, cfg.tz)
        is_weekend = _dt.date.fromisoformat(day).weekday() >= 5
        rows = conn.execute(
            "SELECT app, duration FROM aw_event "
            "WHERE bucket_id='aw-watcher-window_synth-test' AND ts >= ? AND ts < ?",
            (ds, de),
        ).fetchall()
        total = sum(r["duration"] for r in rows)
        coding = sum(r["duration"] for r in rows if r["app"] in coding_apps)
        if is_weekend:
            weekend_total += total
            weekend_coding += coding
        else:
            weekday_total += total
            weekday_coding += coding

    assert weekday_total > 0 and weekend_total > 0
    weekday_ratio = weekday_coding / weekday_total
    weekend_ratio = weekend_coding / weekend_total
    assert weekday_ratio > weekend_ratio + 0.05, (
        f"평일/주말 코딩 비중이 거의 같음 (평일={weekday_ratio:.2f}, 주말={weekend_ratio:.2f})"
    )


# ── ★ 회귀 테스트 (버그 2: 미래 이벤트 생성) ─────────────────────────────


def test_no_events_after_now(conn):
    """`now` 이후의 이벤트는 절대 만들면 안 된다 — 오늘 날짜를 기본값으로 생성해서 확인한다."""
    cfg = _cfg()
    generate(conn, cfg, days=3, end_day=None, seed=11, host="synth-nowcheck")  # end_day 기본값 = 오늘
    checked_now = now_ts()  # generate() 호출이 끝난 뒤 읽은 시각 — 상한의 상한
    max_ts = conn.execute(
        "SELECT MAX(ts) FROM aw_event WHERE bucket_id LIKE '%_synth-nowcheck'"
    ).fetchone()[0]
    if max_ts is not None:  # 새벽에 실행되면 오늘치가 아예 없을 수도 있다 — 그 자체가 정상 동작
        assert max_ts <= checked_now


def test_no_events_after_now_even_with_future_end_day(conn):
    """`end_day` 를 실수로 미래로 넘겨도 미래 이벤트가 생기면 안 된다."""
    cfg = _cfg()
    far_future = "2099-01-01"
    n = generate(conn, cfg, days=2, end_day=far_future, seed=11, host="synth-future")
    assert n == 0  # 완전히 미래인 날짜 -> 이벤트가 하나도 없어야 정상
    count = conn.execute(
        "SELECT COUNT(*) FROM aw_event WHERE bucket_id LIKE '%_synth-future'"
    ).fetchone()[0]
    assert count == 0


# ── ★ 회귀 테스트 (버그 3: 하루가 10분마다 카테고리를 갈아치우는 "스트로브") ────────
#
# 실제 사람의 하루는 몇 시간짜리 덩어리 몇 개다. rollup 을 직접 돌려 슬롯의 "승자
# 카테고리" 배열을 얻은 뒤, (1) 하루 안에서 카테고리가 연속으로 유지되는 블록이
# 12개를 넘지 않는지, (2) 그중 가장 긴 블록(off/away 제외)이 최소 45분 이상인지 본다.
# 두 성질 다 이 모듈만으로는 검증할 수 없다 — rollup(B 담당)까지 실제로 돌려야
# "타임라인이 사람의 하루처럼 보이는지"를 확인할 수 있기 때문에, 여기서만 예외적으로
# lifetrainer.rollup 을 읽기 전용으로 가져와 쓴다 (rollup.py/classify.py 는 건드리지 않는다).


def _rollup_category_sequences(conn, cfg, days: list[str]) -> dict[str, list[str]]:
    """주어진 날짜들을 실제로 rollup 한 뒤 (day -> 144개 슬롯의 승자 카테고리 배열) 을 만든다."""
    from lifetrainer.rollup.classify import Classifier
    from lifetrainer.rollup.rollup import rollup_day

    classifier = Classifier.from_yaml(cfg.rollup.rules_path)
    result: dict[str, list[str]] = {}
    for day in days:
        rollup_day(conn, cfg, classifier, day)
        rows = conn.execute(
            "SELECT category FROM slot WHERE day = ? ORDER BY slot", (day,)
        ).fetchall()
        result[day] = [r["category"] for r in rows]
    return result


def _count_blocks(categories: list[str]) -> int:
    """연속된 같은 카테고리를 하나의 블록으로 셌을 때 총 블록 개수."""
    if not categories:
        return 0
    blocks = 1
    for i in range(1, len(categories)):
        if categories[i] != categories[i - 1]:
            blocks += 1
    return blocks


def _longest_focus_run_slots(categories: list[str], off_cat: str, away_cat: str) -> int:
    """off/away 를 제외하고, 같은 카테고리가 가장 길게 연속되는 슬롯 수."""
    longest = 0
    run = 0
    prev = None
    for c in categories:
        if c in (off_cat, away_cat):
            run = 0
            prev = c
            continue
        run = run + 1 if c == prev else 1
        longest = max(longest, run)
        prev = c
    return longest


def test_daily_category_blocks_within_budget(conn):
    """하루의 연속된 카테고리 블록 개수가 12개 이하여야 한다 (스트로브 버그 회귀 테스트)."""
    cfg = _cfg()
    days = 10
    generate(conn, cfg, days=days, end_day=PAST_END, seed=13, host="synth-blocks")
    day_list = _past_days_list(PAST_END, days)
    sequences = _rollup_category_sequences(conn, cfg, day_list)
    for day, cats in sequences.items():
        blocks = _count_blocks(cats)
        assert blocks <= 12, f"{day} 의 카테고리 블록이 {blocks}개 — 스트로브 버그 재발 의심"


def test_longest_focus_block_at_least_45_minutes(conn):
    """하루의 최장 연속 동일 카테고리 구간(off/away 제외)이 최소 45분 이상이어야 한다."""
    cfg = _cfg()
    days = 10
    generate(conn, cfg, days=days, end_day=PAST_END, seed=13, host="synth-focus")
    day_list = _past_days_list(PAST_END, days)
    sequences = _rollup_category_sequences(conn, cfg, day_list)
    slot_minutes = cfg.rollup.slot_minutes
    for day, cats in sequences.items():
        longest_slots = _longest_focus_run_slots(cats, cfg.rollup.no_data_category, cfg.rollup.afk_category)
        longest_minutes = longest_slots * slot_minutes
        assert longest_minutes >= 45, f"{day} 의 최장 집중 블록이 {longest_minutes}분 — 45분 미만"
