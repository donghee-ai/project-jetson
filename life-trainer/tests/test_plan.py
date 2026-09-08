"""lifetrainer.plan.models / lifetrainer.plan.achieve 테스트.

`slot`/`slot_breakdown` 은 롤업의 출력물이라 여기서는 직접 INSERT 해서 픽스처를
만든다 (rollup.py 에 의존하지 않는다). 네트워크 호출 없음.

★ 하루 경계가 06:00 이라 벽시계 분 -> 슬롯 변환은 `timeutil.wallclock_min_to_slot`
을 거쳐야 한다(직접 `// slot_minutes` 로 나누면 경계를 무시한 슬롯이 나온다).
그래서 아래 테스트들은 슬롯 번호를 하드코딩하지 않고 `_slot(cfg, minute)` 헬퍼로
계산한다 — 경계값이 나중에 또 바뀌어도 테스트가 스스로 따라간다.
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from lifetrainer import db, timeutil
from lifetrainer.config import load_config
from lifetrainer.plan.achieve import day_achievement, plans_for_day
from lifetrainer.plan.models import (
    archive_instance,
    add_instance,
    carry_debt,
    create_plan,
    delete_plan,
    get_plan,
    list_instances,
    list_plans,
    materialize_day,
    parse_time_range,
    parse_weekdays,
    renumber,
    set_check,
    set_status,
    skip_plan,
    sync_instances_from_plan,
    unarchive_instance,
    unskip_plan,
    update_plan,
)

MON = "2026-01-05"  # ISO 요일 1 (월)
TUE = "2026-01-06"  # 2 (화)
WED = "2026-01-07"  # 3 (수)
THU = "2026-01-08"  # 4 (목)
FRI = "2026-01-09"  # 5 (금)
SAT = "2026-01-10"  # 6 (토)
SUN = "2026-01-11"  # 7 (일)


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    return dataclasses.replace(base, db_path=tmp_path / "lt.db")


@pytest.fixture()
def conn(cfg):
    c = db.connect(cfg.db_path)
    db.init_db(c)
    yield c
    c.close()


def _slot(cfg, minute: int) -> int:
    """벽시계 분(자정 기준) -> 하루 경계(day_boundary_hour) 기준 슬롯 번호."""
    return timeutil.wallclock_min_to_slot(
        minute, cfg.rollup.slot_minutes, boundary_hour=cfg.rollup.day_boundary_hour
    )


def _insert_breakdown(conn, day: str, slot: int, category: str, seconds: float, app: str = "") -> None:
    conn.execute(
        "INSERT INTO slot_breakdown(day, slot, category, app, seconds) VALUES (?, ?, ?, ?, ?)",
        (day, slot, category, app, seconds),
    )
    conn.commit()


# ── parse_time_range ─────────────────────────────────────────────────────


def test_parse_time_range_basic():
    assert parse_time_range("09:00-12:00") == (540, 720)


def test_parse_time_range_allows_midnight_end():
    assert parse_time_range("23:00-24:00") == (1380, 1440)


def test_parse_time_range_rejects_crossing_midnight():
    with pytest.raises(ValueError):
        parse_time_range("22:00-02:00")


def test_parse_time_range_rejects_equal_start_end():
    with pytest.raises(ValueError):
        parse_time_range("09:00-09:00")


def test_parse_time_range_rejects_bad_format():
    with pytest.raises(ValueError):
        parse_time_range("아무거나")


# ── parse_weekdays ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("평일", "12345"),
        ("weekday", "12345"),
        ("주말", "67"),
        ("weekend", "67"),
        ("매일", "1234567"),
        ("daily", "1234567"),
        ("월수금", "135"),
        ("mon,wed,fri", "135"),
        ("1-5", "12345"),
        ("1234567", "1234567"),  # 이미 정규화된 값도 그대로(멱등)
    ],
)
def test_parse_weekdays_forms(raw, expected):
    assert parse_weekdays(raw) == expected


def test_parse_weekdays_invalid_raises():
    with pytest.raises(ValueError):
        parse_weekdays("이상한요일")


def test_parse_weekdays_out_of_range_digit_raises():
    with pytest.raises(ValueError):
        parse_weekdays("8")


# ── create_plan 유효성 ────────────────────────────────────────────────────


def test_create_plan_rejects_midnight_crossing(conn):
    with pytest.raises(ValueError):
        create_plan(conn, title="야근", start_min=1380, end_min=1441)


def test_create_plan_rejects_start_after_end(conn):
    with pytest.raises(ValueError):
        create_plan(conn, title="이상함", start_min=700, end_min=600)


def test_create_plan_oneoff_requires_day(conn):
    with pytest.raises(ValueError):
        create_plan(conn, title="병원", start_min=840, end_min=900, kind="oneoff")


# ── CRUD 왕복 ─────────────────────────────────────────────────────────────


def test_create_get_update_delete_plan(conn):
    plan_id = create_plan(
        conn, title="코딩", start_min=540, end_min=720, category="coding", weekdays="평일"
    )
    plan = get_plan(conn, plan_id)
    assert plan is not None
    assert plan.title == "코딩"
    assert plan.weekdays == "12345"  # '평일' 이 정규화되어 저장됨
    assert plan.enabled is True

    update_plan(conn, plan_id, title="집중 코딩", sort_order=5)
    updated = get_plan(conn, plan_id)
    assert updated.title == "집중 코딩"
    assert updated.sort_order == 5
    assert updated.start_min == 540  # 안 건드린 필드는 그대로

    delete_plan(conn, plan_id)
    assert get_plan(conn, plan_id) is None


def test_update_plan_revalidates_time_range(conn):
    plan_id = create_plan(conn, title="코딩", start_min=540, end_min=720)
    with pytest.raises(ValueError):
        update_plan(conn, plan_id, start_min=800)  # 800 >= 기존 end_min(720)


def test_list_plans_enabled_only(conn):
    p1 = create_plan(conn, title="A", start_min=0, end_min=60)
    p2 = create_plan(conn, title="B", start_min=60, end_min=120)
    update_plan(conn, p2, enabled=False)

    enabled = list_plans(conn)
    assert [p.id for p in enabled] == [p1]

    every = list_plans(conn, enabled_only=False)
    assert {p.id for p in every} == {p1, p2}


# ── 반복 계획: 요일 필터 ────────────────────────────────────────────────


def test_recurring_plan_shows_only_on_matching_weekday(conn, cfg):
    create_plan(conn, title="코딩", start_min=540, end_min=720, weekdays="월수금")

    mon_instances = plans_for_day(conn, cfg, MON)
    tue_instances = plans_for_day(conn, cfg, TUE)

    assert len(mon_instances) == 1
    assert mon_instances[0].plan.title == "코딩"
    assert tue_instances == []


def test_recurring_plan_skip_removes_it_for_that_day(conn, cfg):
    plan_id = create_plan(conn, title="코딩", start_min=540, end_min=720, weekdays="평일")

    skip_plan(conn, plan_id, MON)
    assert plans_for_day(conn, cfg, MON) == []
    assert len(plans_for_day(conn, cfg, TUE)) == 1  # 다른 날엔 영향 없음

    unskip_plan(conn, plan_id, MON)
    assert len(plans_for_day(conn, cfg, MON)) == 1


def test_recurring_plan_active_range(conn, cfg):
    create_plan(
        conn,
        title="다이어트",
        start_min=0,
        end_min=60,
        weekdays="매일",
        active_from="2026-01-10",
        active_to="2026-01-12",
    )

    assert plans_for_day(conn, cfg, MON) == []  # active_from 이전
    assert len(plans_for_day(conn, cfg, SAT)) == 1  # active_from == SAT
    assert plans_for_day(conn, cfg, "2026-01-13") == []  # active_to 이후


# ── oneoff ────────────────────────────────────────────────────────────────


def test_oneoff_plan_shows_only_on_its_day(conn, cfg):
    create_plan(conn, title="병원", start_min=840, end_min=900, kind="oneoff", day=FRI)

    assert len(plans_for_day(conn, cfg, FRI)) == 1
    assert plans_for_day(conn, cfg, MON) == []
    assert plans_for_day(conn, cfg, SAT) == []


# ── 달성률 계산 ───────────────────────────────────────────────────────────


def test_achievement_half_of_three_hours(conn, cfg):
    plan_id = create_plan(
        conn, title="코딩", start_min=540, end_min=720, category="coding", weekdays="매일"
    )  # 09:00-12:00, 3시간 = 18슬롯
    s54, s72 = _slot(cfg, 540), _slot(cfg, 720)  # day_boundary_hour=6 기준 슬롯 (자정 기준 아님)

    # 1.5시간(9슬롯)만 coding, 나머지 9슬롯은 다른 카테고리.
    for slot in range(s54, s54 + 9):
        _insert_breakdown(conn, MON, slot, "coding", 600.0)
    for slot in range(s54 + 9, s72):
        _insert_breakdown(conn, MON, slot, "browsing", 600.0)

    [instance] = plans_for_day(conn, cfg, MON)
    assert instance.plan.id == plan_id
    assert instance.start_slot == s54
    assert instance.end_slot == s72
    assert instance.planned_sec == pytest.approx(10800.0)
    assert instance.actual_sec == pytest.approx(5400.0)
    assert instance.achievement == pytest.approx(0.5)


def test_achievement_caps_at_one_when_overachieved(conn, cfg):
    create_plan(conn, title="독서", start_min=0, end_min=60, category="learning", weekdays="매일")
    s0 = _slot(cfg, 0)
    for slot in range(s0, s0 + 6):
        _insert_breakdown(conn, MON, slot, "learning", 900.0)  # 6*900=5400 > planned(3600)

    [instance] = plans_for_day(conn, cfg, MON)
    assert instance.achievement == pytest.approx(1.0)


def test_achievement_category_none_excludes_off_and_away(conn, cfg):
    create_plan(conn, title="아무거나", start_min=100, end_min=120, category=None, weekdays="매일")
    s10, s11 = _slot(cfg, 100), _slot(cfg, 110)
    _insert_breakdown(conn, MON, s10, "coding", 400.0, app="Code.exe")
    _insert_breakdown(conn, MON, s10, "away", 200.0)
    _insert_breakdown(conn, MON, s11, "browsing", 300.0, app="chrome.exe")
    _insert_breakdown(conn, MON, s11, "off", 300.0)

    [instance] = plans_for_day(conn, cfg, MON)
    assert instance.actual_sec == pytest.approx(700.0)  # coding 400 + browsing 300 (away/off 제외)
    assert instance.dominant_actual == "coding"  # 400 이 가장 큼


def test_checked_flag_reflected_in_plan_instance(conn, cfg):
    plan_id = create_plan(conn, title="운동", start_min=0, end_min=60, weekdays="매일")

    [before] = plans_for_day(conn, cfg, MON)
    assert before.checked is False

    set_check(conn, plan_id, MON, True)
    [after] = plans_for_day(conn, cfg, MON)
    assert after.checked is True

    set_check(conn, plan_id, MON, False)
    [again] = plans_for_day(conn, cfg, MON)
    assert again.checked is False


def test_plans_for_day_sorted_by_sort_order_then_start_min(conn, cfg):
    create_plan(conn, title="늦은거", start_min=600, end_min=660, weekdays="매일", sort_order=1)
    create_plan(conn, title="이른거", start_min=0, end_min=60, weekdays="매일", sort_order=0)
    create_plan(conn, title="같은순위 늦음", start_min=700, end_min=760, weekdays="매일", sort_order=1)

    instances = plans_for_day(conn, cfg, MON)
    titles = [i.plan.title for i in instances]
    assert titles == ["이른거", "늦은거", "같은순위 늦음"]


# ── day_achievement ────────────────────────────────────────────────────────


def test_day_achievement_aggregate(conn, cfg):
    create_plan(conn, title="A", start_min=0, end_min=60, category="coding", weekdays="매일")  # 3600s
    create_plan(conn, title="B", start_min=60, end_min=120, category="learning", weekdays="매일")  # 3600s

    # A: 완전 달성(6슬롯 * 600 = 3600). B: 절반 달성(3슬롯 * 600 = 1800).
    s0, s6 = _slot(cfg, 0), _slot(cfg, 60)
    for slot in range(s0, s6):
        _insert_breakdown(conn, MON, slot, "coding", 600.0)
    for slot in range(s6, s6 + 3):
        _insert_breakdown(conn, MON, slot, "learning", 600.0)

    overall, achieved, total = day_achievement(conn, cfg, MON)
    assert total == 2
    assert achieved == 1  # A만 100% 달성
    assert overall == pytest.approx((3600.0 + 1800.0) / (3600.0 + 3600.0))


def test_day_achievement_no_plans_is_zero(conn, cfg):
    overall, achieved, total = day_achievement(conn, cfg, MON)
    assert (overall, achieved, total) == (0.0, 0, 0)


# ── subject category 상속 (achieve.py) ──────────────────────────────────────


def test_achievement_inherits_subject_category(conn, cfg):
    """subject_id 는 있고 인스턴스 자체 category 가 비어 있으면 subject.category 를 쓴다."""
    from lifetrainer.plan import subjects

    coding_hex = subjects.palette_choices(cfg)[0]["hex"]  # slot 1 = coding
    subject_id = subjects.create_subject(conn, cfg, name="시험공부", color=coding_hex, category="coding")

    instance_id = add_instance(
        conn, cfg, MON, title="공부", start_min=540, end_min=720, subject_id=subject_id, category=None
    )
    s54, s72 = _slot(cfg, 540), _slot(cfg, 720)
    for slot in range(s54, s54 + 9):  # 1.5시간만 coding
        _insert_breakdown(conn, MON, slot, "coding", 600.0)

    [instance] = plans_for_day(conn, cfg, MON)
    assert instance.instance_id == instance_id
    assert instance.achievement == pytest.approx(0.5)  # 3시간 계획 중 1.5시간 실측


# ── materialize_day 멱등성 ───────────────────────────────────────────────────


def test_materialize_day_is_idempotent(conn, cfg):
    create_plan(conn, title="코딩", start_min=540, end_min=720, weekdays="매일")

    created_1 = materialize_day(conn, cfg, MON)
    created_2 = materialize_day(conn, cfg, MON)
    created_3 = materialize_day(conn, cfg, MON)

    assert created_1 == 1
    assert created_2 == 0
    assert created_3 == 0

    rows = conn.execute("SELECT COUNT(*) AS n FROM plan_instance WHERE day = ?", (MON,)).fetchone()
    assert rows["n"] == 1  # 세 번 불러도 인스턴스가 늘지 않는다


def test_materialize_day_respects_plan_skip(conn, cfg):
    plan_id = create_plan(conn, title="요가", start_min=420, end_min=480, weekdays="매일")
    skip_plan(conn, plan_id, MON)

    created = materialize_day(conn, cfg, MON)
    assert created == 0
    assert list_instances(conn, MON) == []


def test_materialize_day_respects_active_range(conn, cfg):
    create_plan(
        conn, title="다이어트", start_min=0, end_min=60, weekdays="매일",
        active_from="2026-01-10", active_to="2026-01-12",
    )
    assert materialize_day(conn, cfg, MON) == 0  # active_from 이전
    assert materialize_day(conn, cfg, SAT) == 1  # active_from == SAT(01-10)


def test_materialize_day_survives_soft_delete(conn, cfg):
    """지운(archived) 인스턴스는 다시 materialize 해도 되살아나지 않는다 — 10분 롤업 증식 방지."""
    create_plan(conn, title="코딩", start_min=540, end_min=720, weekdays="매일")
    materialize_day(conn, cfg, MON)
    [inst] = list_instances(conn, MON)
    archive_instance(conn, inst.id)

    materialize_day(conn, cfg, MON)  # 다시 전개해도
    assert list_instances(conn, MON) == []  # 삭제 상태가 유지된다
    assert len(list_instances(conn, MON, include_archived=True)) == 1


# ── add_instance / 상태 전환 / 이벤트 로그 ───────────────────────────────────


def test_add_instance_manual(conn, cfg):
    instance_id = add_instance(
        conn, cfg, MON, title="병원", start_min=840, end_min=900, priority="high", source="manual",
        note="정기검진",
    )
    [inst] = list_instances(conn, MON)
    assert inst.id == instance_id
    assert inst.title == "병원"
    assert inst.plan_id is None
    assert inst.status == "todo"
    assert inst.priority == "high"
    assert inst.source == "manual"
    assert inst.note == "정기검진"
    assert inst.archived is False


def test_add_instance_rejects_bad_time_range(conn, cfg):
    with pytest.raises(ValueError):
        add_instance(conn, cfg, MON, title="이상함", start_min=700, end_min=600)


def test_set_status_records_event_with_from_and_to(conn, cfg):
    instance_id = add_instance(conn, cfg, MON, title="운동", start_min=420, end_min=480)

    set_status(conn, cfg, instance_id, "doing", actor="user")
    [inst] = list_instances(conn, MON)
    assert inst.status == "doing"

    events = conn.execute(
        "SELECT from_status, to_status, actor FROM plan_instance_event "
        "WHERE instance_id = ? ORDER BY id", (instance_id,),
    ).fetchall()
    assert len(events) == 1
    assert events[0]["from_status"] == "todo"
    assert events[0]["to_status"] == "doing"
    assert events[0]["actor"] == "user"

    set_status(conn, cfg, instance_id, "done", actor="system")
    events = conn.execute(
        "SELECT from_status, to_status, actor FROM plan_instance_event "
        "WHERE instance_id = ? ORDER BY id", (instance_id,),
    ).fetchall()
    assert len(events) == 2
    assert events[1]["from_status"] == "doing"
    assert events[1]["to_status"] == "done"
    assert events[1]["actor"] == "system"


def test_set_status_unknown_status_raises(conn, cfg):
    instance_id = add_instance(conn, cfg, MON, title="x", start_min=0, end_min=60)
    with pytest.raises(ValueError):
        set_status(conn, cfg, instance_id, "이상한상태")


def test_set_status_unknown_instance_raises(conn, cfg):
    with pytest.raises(ValueError):
        set_status(conn, cfg, 999999, "done")


# ── 이월(carry) ──────────────────────────────────────────────────────────────


def test_deferred_creates_next_day_instance_with_carry_link(conn, cfg):
    instance_id = add_instance(conn, cfg, MON, title="보고서", start_min=540, end_min=600)

    set_status(conn, cfg, instance_id, "deferred", actor="user")

    [original] = list_instances(conn, MON)
    assert original.status == "deferred"

    [carried] = list_instances(conn, TUE)
    assert carried.carried_from == instance_id
    assert carried.source == "carry"
    assert carried.status == "todo"
    assert carried.title == "보고서"


def test_deferred_twice_on_same_instance_does_not_duplicate_carry(conn, cfg):
    instance_id = add_instance(conn, cfg, MON, title="보고서", start_min=540, end_min=600)

    set_status(conn, cfg, instance_id, "deferred", actor="user")
    set_status(conn, cfg, instance_id, "deferred", actor="user")  # 재시도해도

    assert len(list_instances(conn, TUE)) == 1  # 이월 인스턴스가 하나만 있어야 한다


def test_carry_debt_not_triggered_by_single_defer(conn, cfg):
    instance_id = add_instance(conn, cfg, MON, title="한번만미룸", start_min=540, end_min=600)
    set_status(conn, cfg, instance_id, "deferred", actor="user")

    debt = carry_debt(conn)
    assert instance_id not in [root for root, _depth in debt]


def test_carry_debt_triggered_by_three_consecutive_defers(conn, cfg):
    root_id = add_instance(conn, cfg, MON, title="계속미룸", start_min=540, end_min=600)

    set_status(conn, cfg, root_id, "deferred", actor="user")
    [day2] = list_instances(conn, TUE)
    assert day2.carried_from == root_id

    set_status(conn, cfg, day2.id, "deferred", actor="user")
    [day3] = list_instances(conn, WED)
    assert day3.carried_from == day2.id

    debt = carry_debt(conn)
    debt_map = dict(debt)
    assert root_id in debt_map
    assert debt_map[root_id] >= 2


# ── 소프트 삭제 / 실행취소 ────────────────────────────────────────────────────


def test_archive_and_unarchive_instance(conn, cfg):
    instance_id = add_instance(conn, cfg, MON, title="지울것", start_min=0, end_min=60)

    archive_instance(conn, instance_id)
    assert list_instances(conn, MON) == []
    archived_list = list_instances(conn, MON, include_archived=True)
    assert len(archived_list) == 1
    assert archived_list[0].archived is True

    unarchive_instance(conn, instance_id)
    restored = list_instances(conn, MON)
    assert len(restored) == 1
    assert restored[0].archived is False


def test_list_instances_include_done_filter(conn, cfg):
    done_id = add_instance(conn, cfg, MON, title="끝난것", start_min=0, end_min=60)
    add_instance(conn, cfg, MON, title="안끝난것", start_min=60, end_min=120)
    set_status(conn, cfg, done_id, "done", actor="user")

    all_instances = list_instances(conn, MON, include_done=True)
    assert len(all_instances) == 2

    not_done = list_instances(conn, MON, include_done=False)
    assert len(not_done) == 1
    assert not_done[0].title == "안끝난것"


# ── renumber / ordinal ────────────────────────────────────────────────────────


def test_renumber_assigns_ordinal_by_start_time(conn, cfg):
    add_instance(conn, cfg, MON, title="늦음", start_min=600, end_min=660)
    add_instance(conn, cfg, MON, title="이름", start_min=0, end_min=60)
    add_instance(conn, cfg, MON, title="중간", start_min=300, end_min=360)

    renumber(conn, MON)
    ordered = list_instances(conn, MON)
    assert [i.title for i in ordered] == ["이름", "중간", "늦음"]
    assert [i.ordinal for i in ordered] == [1, 2, 3]


# ── 계획 삭제와 인스턴스 (2026-08-23) ────────────────────────────────────
# ★ 실제 사고: 계획을 지웠는데 TASKS 에 그대로 남아 있었다.
#   `plan_instance.plan_id` 가 ON DELETE SET NULL 이라 인스턴스가 살아남았고,
#   표시 기준이 `archived_at IS NULL` 이라 화면에서 안 사라진 것이다.


def _instance_for(conn, plan_id, day, title="계획"):
    """계획에 묶인 인스턴스를 직접 넣는다 (평소엔 롤업이 만든다)."""
    now = time.time()
    conn.execute(
        "INSERT INTO plan_instance(day, plan_id, title, start_min, end_min, status, "
        "priority, ordinal, source, created_at, updated_at) "
        "VALUES (?, ?, ?, 600, 720, 'todo', 'normal', 0, 'template', ?, ?)",
        (day, plan_id, title, now, now),
    )
    conn.commit()


def test_계획을_지우면_오늘_이후_인스턴스가_보관된다(conn, cfg):
    from lifetrainer.plan.models import create_plan, delete_plan

    pid = create_plan(conn, title="지울 계획", start_min=600, end_min=720, category="coding")
    _instance_for(conn, pid, "2026-08-20")
    _instance_for(conn, pid, "2026-08-23")

    assert delete_plan(conn, pid, from_day="2026-08-23") == 1

    rows = dict(conn.execute("SELECT day, archived_at IS NOT NULL FROM plan_instance").fetchall())
    assert rows["2026-08-23"], "오늘 인스턴스가 화면에 계속 남는다"
    assert not rows["2026-08-20"], "과거까지 지우면 주간 통계가 소급해 바뀐다"


def test_from_day_를_안_주면_인스턴스를_안_건드린다(conn, cfg):
    """기존 호출부(다른 경로)의 동작을 바꾸지 않는다."""
    from lifetrainer.plan.models import create_plan, delete_plan

    pid = create_plan(conn, title="계획", start_min=600, end_min=720)
    _instance_for(conn, pid, "2026-08-23")
    assert delete_plan(conn, pid) == 0


# ── 계획을 고치면 인스턴스도 따라간다 (2026-08-23 실측 사고) ────────────────
# ★ `materialize_day` 는 (plan_id, day) 기준 멱등이라 한 번 만들어진 인스턴스는
#   다시 만들지 않는다(사용자가 지운 것이 되살아나면 안 되므로). 그 결과 계획을
#   고쳐도 **오늘 인스턴스가 옛 값을 그대로 들고 있었다.** 화면은 plan_instance 를
#   읽으므로 사용자에게는 "수정이 저장이 안 된다" 로 보였다.


def _inst(conn, day):
    return {
        r["title"]: (r["start_min"], r["end_min"], r["status"], r["source"])
        for r in conn.execute(
            "SELECT title, start_min, end_min, status, source FROM plan_instance WHERE day = ?",
            (day,),
        )
    }


def test_계획을_고치면_오늘_인스턴스가_따라간다(conn, cfg):
    pid = create_plan(conn, title="점심", start_min=720, end_min=780, kind="oneoff", day=WED)
    materialize_day(conn, cfg, WED)
    assert _inst(conn, WED)["점심"][0] == 720

    update_plan(conn, pid, start_min=730)
    sync_instances_from_plan(conn, cfg, pid, from_day=WED)
    assert _inst(conn, WED)["점심"][0] == 730


def test_과거_인스턴스는_그대로_둔다(conn, cfg):
    """지나간 날의 기록까지 바뀌면 주간 통계가 소급해서 달라진다."""
    pid = create_plan(conn, title="운동", start_min=1080, end_min=1140, weekdays="1234567")
    for d in (MON, TUE, WED):
        materialize_day(conn, cfg, d)

    update_plan(conn, pid, start_min=1200, end_min=1260)
    sync_instances_from_plan(conn, cfg, pid, from_day=WED)

    assert _inst(conn, MON)["운동"][0] == 1080  # 과거는 그대로
    assert _inst(conn, TUE)["운동"][0] == 1080
    assert _inst(conn, WED)["운동"][0] == 1200  # 오늘부터 반영


def test_사람이_만든_인스턴스는_덮지_않는다(conn, cfg):
    """손으로 옮겨 둔 것을 템플릿으로 덮으면 그 편집이 조용히 사라진다."""
    pid = create_plan(conn, title="독서", start_min=600, end_min=660, kind="oneoff", day=WED)
    materialize_day(conn, cfg, WED)
    add_instance(conn, cfg, WED, title="독서", start_min=900, end_min=960)  # source=manual

    update_plan(conn, pid, start_min=610)
    sync_instances_from_plan(conn, cfg, pid, from_day=WED)

    rows = [
        (r["start_min"], r["source"])
        for r in conn.execute(
            "SELECT start_min, source FROM plan_instance WHERE day = ? AND title = ' 독서'".replace(
                "' 독서'", "'독서'"
            ),
            (WED,),
        )
    ]
    assert (610, "template") in rows  # 템플릿 것은 따라갔고
    assert (900, "manual") in rows  # 손으로 만든 것은 그대로다


def test_완료_표시는_유지된다(conn, cfg):
    """시간을 옮겼다고 완료가 풀리면 안 된다."""
    pid = create_plan(conn, title="점심", start_min=720, end_min=780, kind="oneoff", day=WED)
    materialize_day(conn, cfg, WED)
    iid = conn.execute("SELECT id FROM plan_instance WHERE day = ?", (WED,)).fetchone()["id"]
    set_status(conn, cfg, iid, "done")

    update_plan(conn, pid, start_min=730)
    sync_instances_from_plan(conn, cfg, pid, from_day=WED)

    got = _inst(conn, WED)["점심"]
    assert got[0] == 730 and got[2] == "done"


def test_제목과_카테고리도_따라간다(conn, cfg):
    pid = create_plan(conn, title="옛 제목", start_min=600, end_min=660, kind="oneoff", day=WED)
    materialize_day(conn, cfg, WED)

    update_plan(conn, pid, title="새 제목", category="코딩")
    sync_instances_from_plan(conn, cfg, pid, from_day=WED)

    assert "새 제목" in _inst(conn, WED)
    row = conn.execute("SELECT category FROM plan_instance WHERE day = ?", (WED,)).fetchone()
    assert row["category"] == "코딩"


def test_없는_계획이면_조용히_0(conn, cfg):
    assert sync_instances_from_plan(conn, cfg, 9999, from_day=WED) == 0


# ── 건너뛰기 되돌리기 ────────────────────────────────────────────────────


def test_건너뛴_계획을_되돌릴_수_있다(tmp_path):
    """★ 2026-09-01 까지 이 길이 없었다.

    `unskip_plan` 은 만들어져 있었는데 CLI·웹 어디서도 안 불렀다. 그리고 건너뛴
    계획은 목록에서 빠지므로 **화면에서 되돌릴 방법이 아예 없었다** — 잘못 누르면
    DB 를 직접 손대야 했다.
    """
    import dataclasses

    from lifetrainer import db
    from lifetrainer.config import load_config
    from lifetrainer.plan.achieve import plans_for_day
    from lifetrainer.plan.models import create_plan, skip_plan, unskip_plan

    cfg = dataclasses.replace(load_config(), db_path=tmp_path / "lt.db")
    conn = db.open_db(cfg)
    day = "2026-09-01"
    plan_id = create_plan(
        conn, title="아침 산책", category="away", start_min=420, end_min=450, weekdays="1234567"
    )

    def titles():
        return [p.plan.title for p in plans_for_day(conn, cfg, day)]

    assert "아침 산책" in titles()

    # ★ 여기서는 **전개 전**에 건너뛴다. 전개 뒤에 누르면 화면이 안 바뀌는데,
    #   그게 `docs/issues/0026` 이다 — 이 테스트가 그 경계를 넘지 않도록 새 DB 를 쓴다.
    conn2 = db.open_db(dataclasses.replace(cfg, db_path=tmp_path / "lt2.db"))
    pid2 = create_plan(
        conn2, title="아침 산책", category="away", start_min=420, end_min=450, weekdays="1234567"
    )
    skip_plan(conn2, pid2, day)
    assert [p.plan.title for p in plans_for_day(conn2, cfg, day)] == [], (
        "전개 전에 건너뛰면 그날 아예 안 뜬다"
    )

    unskip_plan(conn2, pid2, day)
    assert [p.plan.title for p in plans_for_day(conn2, cfg, day)] == ["아침 산책"], (
        "되돌리면 다시 나와야 한다 — 2026-09-01 까지 이 길이 없었다"
    )

    unskip_plan(conn2, pid2, day)   # 두 번 해도 안전해야 한다
    assert [p.plan.title for p in plans_for_day(conn2, cfg, day)] == ["아침 산책"]
    conn2.close()
    conn.close()


# ── 달성률이 사람의 보정을 본다 (2026-09-07) ──────────────────────────
#
# `slot_breakdown` 은 실측 원본이라 보정이 안 들어간다. 그대로 세면 **"그때 코딩했다"고
# 표에서 고쳐도 코딩 계획의 달성률이 안 오른다** — 화면에는 반영되는데 달성률만 딴소리다.
# 같은 결함을 오늘 카테고리 합계에서 먼저 잡았고, 여기가 마지막 자리였다.


def test_보정한_칸이_계획_달성률에_들어간다(conn, cfg):
    from lifetrainer.plan.achieve import _actual_sec
    from lifetrainer.plan.override import set_override_range

    day = "2026-09-04"
    before = _actual_sec(conn, day, 40, 46, "coding", cfg)

    set_override_range(conn, day, 40, 46, "coding", actor="test")
    conn.commit()
    after = _actual_sec(conn, day, 40, 46, "coding", cfg)

    assert after - before == pytest.approx(6 * cfg.rollup.slot_minutes * 60.0), (before, after)


def test_다른_카테고리로_보정하면_안_들어간다(conn, cfg):
    """★ 안 세어야 하는 쪽. 보정했다고 아무 계획이나 달성되면 숫자가 못 쓰게 된다."""
    from lifetrainer.plan.achieve import _actual_sec
    from lifetrainer.plan.override import set_override_range

    day = "2026-09-04"
    set_override_range(conn, day, 40, 46, "gaming", actor="test")
    conn.commit()

    assert _actual_sec(conn, day, 40, 46, "coding", cfg) == pytest.approx(0.0)


# ── "오늘만 건너뛰기" 가 오늘을 건너뛰는가 (2026-09-07 · issues/0026) ──────
#
# ★ 이 절이 없어서 버그가 일주일 살아남았다. 테스트는 `skip_plan` 을 **함수 단위로**
#   검증하고 있었다 — 전개 → 건너뛰기 → 다시 조회라는 **사람이 실제로 하는 순서**를
#   밟는 것이 하나도 없었다. 그래서 아래 테스트들은 전부 그 순서를 지킨다.


def _walk_plan(conn):
    from lifetrainer.plan.models import create_plan

    return create_plan(conn, title="산책", category="away", start_min=420, end_min=450, weekdays="1234567")


def test_이미_전개된_날을_건너뛰면_화면에서_사라진다(conn, cfg):
    """★ 이게 0026 이다. 사람이 보고 있는 날은 **언제나** 이미 전개된 날이다 —
    화면을 열었다는 것이 곧 전개다. 전개 전에만 듣는 장치는 사람에게 안 듣는다."""
    from lifetrainer.plan.models import list_instances, materialize_day, skip_plan

    day = "2026-09-01"
    plan_id = _walk_plan(conn)
    materialize_day(conn, cfg, day)
    assert [i.title for i in list_instances(conn, day)] == ["산책"]

    skip_plan(conn, plan_id, day)

    assert list_instances(conn, day) == [], "건너뛰었는데 계획이 그대로 있다 (버튼이 거짓말한다)"


def test_건너뛰기를_풀면_그대로_돌아온다(conn, cfg):
    from lifetrainer.plan.models import list_instances, materialize_day, skip_plan, unskip_plan

    day = "2026-09-01"
    plan_id = _walk_plan(conn)
    materialize_day(conn, cfg, day)
    skip_plan(conn, plan_id, day)

    unskip_plan(conn, plan_id, day)

    assert [i.title for i in list_instances(conn, day)] == ["산책"]


def test_건너뛰기_해제가_사람이_지운_것까지_되살리지는_않는다(conn, cfg):
    """★ 반대쪽 경계. `archived_at` 하나로 판단하면 **삭제 취소로 번진다** —
    그래서 `skipped_at` 이 따로 있다 (마이그레이션 011).
    """
    from lifetrainer.plan.models import (
        archive_instance, list_instances, materialize_day, skip_plan, unskip_plan,
    )

    day = "2026-09-01"
    plan_id = _walk_plan(conn)
    materialize_day(conn, cfg, day)
    inst = list_instances(conn, day)[0]

    archive_instance(conn, inst.id)      # 사람이 지웠다
    skip_plan(conn, plan_id, day)        # 그 뒤에 건너뛰기까지 눌렀다
    unskip_plan(conn, plan_id, day)      # 건너뛰기만 풀었다

    assert list_instances(conn, day) == [], "지운 계획이 건너뛰기 해제로 되살아났다"


def test_건너뛴_날은_다시_전개되지_않는다(conn, cfg):
    """`plan_skip` 의 원래 역할(미래를 막는 것)이 살아 있는지 — 고치면서 깨기 쉬운 쪽이다."""
    from lifetrainer.plan.models import list_instances, materialize_day, skip_plan

    day = "2026-09-01"
    plan_id = _walk_plan(conn)
    materialize_day(conn, cfg, day)
    skip_plan(conn, plan_id, day)

    materialize_day(conn, cfg, day)  # 10분마다 도는 롤업이 이걸 부른다

    assert list_instances(conn, day) == [], "건너뛴 계획이 다음 롤업에 되살아났다"


def test_건너뛰기가_다른_날에는_영향이_없다(conn, cfg):
    from lifetrainer.plan.models import list_instances, materialize_day, skip_plan

    plan_id = _walk_plan(conn)
    for day in ("2026-09-01", "2026-09-02"):
        materialize_day(conn, cfg, day)
    skip_plan(conn, plan_id, "2026-09-01")

    assert list_instances(conn, "2026-09-01") == []
    assert [i.title for i in list_instances(conn, "2026-09-02")] == ["산책"]
