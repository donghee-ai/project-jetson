"""lifetrainer.timeutil 테스트. 전부 순수 함수라 네트워크/DB 없이 돈다."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from lifetrainer import timeutil

SEOUL = ZoneInfo("Asia/Seoul")


# ── parse_iso ────────────────────────────────────────────────────────────


def test_parse_iso_z_suffix():
    ts = timeutil.parse_iso("2026-08-16T01:02:03Z")
    expected = datetime(2026, 8, 16, 1, 2, 3, tzinfo=timezone.utc).timestamp()
    assert ts == pytest.approx(expected)


def test_parse_iso_z_suffix_with_microseconds():
    ts = timeutil.parse_iso("2026-08-16T01:02:03.123456Z")
    expected = datetime(2026, 8, 16, 1, 2, 3, 123456, tzinfo=timezone.utc).timestamp()
    assert ts == pytest.approx(expected)


def test_parse_iso_explicit_offset():
    ts = timeutil.parse_iso("2026-08-16T10:02:03+09:00")
    # Asia/Seoul 10:02:03 == UTC 01:02:03
    expected = datetime(2026, 8, 16, 1, 2, 3, tzinfo=timezone.utc).timestamp()
    assert ts == pytest.approx(expected)


def test_parse_iso_offset_without_microseconds():
    ts = timeutil.parse_iso("2026-08-16T01:02:03+00:00")
    expected = datetime(2026, 8, 16, 1, 2, 3, tzinfo=timezone.utc).timestamp()
    assert ts == pytest.approx(expected)


def test_parse_iso_fewer_fraction_digits():
    # ActivityWatch/기타 소스가 3자리 밀리초만 줄 수도 있다.
    ts = timeutil.parse_iso("2026-08-16T01:02:03.5Z")
    expected = datetime(2026, 8, 16, 1, 2, 3, 500000, tzinfo=timezone.utc).timestamp()
    assert ts == pytest.approx(expected)


def test_parse_iso_roundtrip_with_iso_utc():
    original = 1755305000.123456
    s = timeutil.iso_utc(original)
    back = timeutil.parse_iso(s)
    assert back == pytest.approx(original, abs=1e-6)


def test_iso_utc_format():
    ts = datetime(2026, 8, 16, 1, 2, 3, tzinfo=timezone.utc).timestamp()
    s = timeutil.iso_utc(ts)
    assert s == "2026-08-16T01:02:03.000000+00:00"


# ── to_ts / from_ts ──────────────────────────────────────────────────────


def test_to_ts_naive_is_utc():
    # noqa 사유: tzinfo 없는 datetime 을 만드는 것이 **이 테스트의 대상**이다.
    naive = datetime(2026, 8, 16, 1, 2, 3)  # noqa: DTZ001
    aware = datetime(2026, 8, 16, 1, 2, 3, tzinfo=timezone.utc)
    assert timeutil.to_ts(naive) == pytest.approx(aware.timestamp())


def test_from_ts_default_utc():
    ts = 1755305000.0
    dt = timeutil.from_ts(ts)
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_from_ts_with_tz():
    ts = 1755305000.0
    dt = timeutil.from_ts(ts, SEOUL)
    assert dt.tzinfo is not None


# ── day_str / day_bounds — 논리적 하루 경계 06:00(기본값) ───────────────────
#
# V1 의 파괴적 변경: `boundary_hour` 의 기본값이 6이라 인자를 안 주는 기존
# 호출부도 전부 "06:00 이 하루의 시작" 이라는 새 규칙을 따르게 된다.
# 옛 자정 기준 동작이 필요하면 `boundary_hour=0` 을 명시하면 그대로 재현된다
# (아래 *_legacy_midnight 테스트가 그 하위호환을 검증한다).


@pytest.mark.parametrize(
    "hhmm,expected_day",
    [
        ("05:59", "2026-08-15"),  # 경계 직전 -> 아직 전날
        ("06:00", "2026-08-16"),  # 경계 정각 -> 당일 시작
        ("23:59", "2026-08-16"),  # 당일
    ],
)
def test_day_str_boundary_hour_default(hhmm, expected_day):
    h, m = (int(x) for x in hhmm.split(":"))
    dt = datetime(2026, 8, 16, h, m, tzinfo=SEOUL)
    assert timeutil.day_str(dt.timestamp(), SEOUL) == expected_day


def test_day_str_legacy_midnight_boundary():
    # boundary_hour=0 이면 옛 자정 기준 동작과 동일하다.
    ts = datetime(2026, 8, 16, 0, 30, tzinfo=SEOUL).timestamp()
    assert timeutil.day_str(ts, SEOUL, boundary_hour=0) == "2026-08-16"


def test_day_bounds_seoul_span():
    start, end = timeutil.day_bounds("2026-08-16", SEOUL)
    assert end - start == 86400.0  # Asia/Seoul 은 DST 가 없다
    assert timeutil.day_str(start, SEOUL) == "2026-08-16"
    assert timeutil.day_str(end - 1, SEOUL) == "2026-08-16"
    assert timeutil.day_str(end, SEOUL) == "2026-08-17"  # end 는 배타적 경계


def test_day_bounds_starts_and_ends_at_0600():
    start, end = timeutil.day_bounds("2026-08-16", SEOUL)
    expected_start = datetime(2026, 8, 16, 6, 0, 0, tzinfo=SEOUL).timestamp()
    expected_end = datetime(2026, 8, 17, 6, 0, 0, tzinfo=SEOUL).timestamp()
    assert start == pytest.approx(expected_start)
    assert end == pytest.approx(expected_end)


def test_day_bounds_does_not_assume_24h_next_midnight():
    # 끝은 항상 '다음 날 경계 시각'을 다시 계산한 값과 같아야 한다.
    start, end = timeutil.day_bounds("2026-08-16", SEOUL)
    next_start, _ = timeutil.day_bounds("2026-08-17", SEOUL)
    assert end == next_start


def test_day_bounds_legacy_midnight_boundary():
    # boundary_hour=0 이면 [자정, 다음날 자정) 을 그대로 재현한다.
    start, end = timeutil.day_bounds("2026-08-16", SEOUL, boundary_hour=0)
    expected_start = datetime(2026, 8, 16, 0, 0, 0, tzinfo=SEOUL).timestamp()
    expected_end = datetime(2026, 8, 17, 0, 0, 0, tzinfo=SEOUL).timestamp()
    assert start == pytest.approx(expected_start)
    assert end == pytest.approx(expected_end)


# ── wallclock_min_to_slot ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "minute,expected_slot",
    [
        (540, 18),  # 09:00, boundary 6
        (60, 114),  # 01:00
        (360, 0),  # 06:00 -> 슬롯 0
    ],
)
def test_wallclock_min_to_slot_default_boundary(minute, expected_slot):
    assert timeutil.wallclock_min_to_slot(minute) == expected_slot


def test_wallclock_min_to_slot_legacy_midnight_boundary():
    # boundary_hour=0 이면 자정 기준 그대로 (분을 10 으로 나눈 값).
    assert timeutil.wallclock_min_to_slot(540, boundary_hour=0) == 54
    assert timeutil.wallclock_min_to_slot(0, boundary_hour=0) == 0


# ── slot_index / slot_bounds / slots_per_day ────────────────────────────


@pytest.mark.parametrize(
    "hhmm,expected_slot",
    [
        ("06:00", 0),
        ("06:09", 0),
        ("06:10", 1),
    ],
)
def test_slot_index_boundary_hour_default(hhmm, expected_slot):
    h, m = (int(x) for x in hhmm.split(":"))
    dt = datetime(2026, 8, 16, h, m, tzinfo=SEOUL)
    assert timeutil.slot_index(dt.timestamp(), SEOUL, slot_minutes=10) == expected_slot


def test_slot_index_last_slot_is_next_day_0550():
    # 05:50(익일) -> 143. 06:00 경계 하루의 마지막 10분 슬롯.
    dt = datetime(2026, 8, 17, 5, 50, tzinfo=SEOUL)
    assert timeutil.slot_index(dt.timestamp(), SEOUL, slot_minutes=10) == 143


def test_slot_index_legacy_midnight_boundary():
    # boundary_hour=0 이면 옛 자정 기준 슬롯 번호와 동일하다.
    dt = datetime(2026, 8, 16, 10, 5, tzinfo=SEOUL)
    assert timeutil.slot_index(dt.timestamp(), SEOUL, slot_minutes=10, boundary_hour=0) == 60


def test_slot_bounds_within_day():
    day_start, day_end = timeutil.day_bounds("2026-08-16", SEOUL)
    s0, s1 = timeutil.slot_bounds("2026-08-16", 0, SEOUL, slot_minutes=10)
    assert s0 == day_start
    assert s1 - s0 == 600.0

    last0, last1 = timeutil.slot_bounds("2026-08-16", 143, SEOUL, slot_minutes=10)
    assert last1 == day_end


def test_slots_per_day():
    assert timeutil.slots_per_day(10) == 144
    assert timeutil.slots_per_day(30) == 48
    assert timeutil.slots_per_day() == 144


# ── overlap_sec ──────────────────────────────────────────────────────────


def test_overlap_sec_basic():
    assert timeutil.overlap_sec(0, 10, 5, 15) == 5
    assert timeutil.overlap_sec(0, 10, 10, 20) == 0
    assert timeutil.overlap_sec(0, 10, 20, 30) == 0  # 안 겹침


def test_overlap_sec_never_negative():
    assert timeutil.overlap_sec(0, 5, 100, 200) == 0
    assert timeutil.overlap_sec(100, 200, 0, 5) == 0


def test_overlap_sec_containment():
    assert timeutil.overlap_sec(0, 100, 20, 30) == 10


# ── day_range ────────────────────────────────────────────────────────────


def test_day_range_inclusive():
    days = timeutil.day_range("2026-08-14", "2026-08-16")
    assert days == ["2026-08-14", "2026-08-15", "2026-08-16"]


def test_day_range_single_day():
    assert timeutil.day_range("2026-08-16", "2026-08-16") == ["2026-08-16"]


# ── parse_duration ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected_sec",
    [
        ("1h30m", 5400.0),
        ("60m", 3600.0),
        ("90", 5400.0),  # 단위 없는 숫자는 분
        ("1.5h", 5400.0),
        ("30s", 30.0),
        ("2h", 7200.0),
        ("0.5h30m", 3600.0),  # 0.5h(1800s) + 30m(1800s)
    ],
)
def test_parse_duration_valid(text, expected_sec):
    assert timeutil.parse_duration(text) == pytest.approx(expected_sec)


@pytest.mark.parametrize("text", ["", "abc", "1x", "h30m", "1h 30", "  "])
def test_parse_duration_invalid_raises(text):
    with pytest.raises(ValueError):
        timeutil.parse_duration(text)


def test_슬롯과_벽시계_분이_왕복한다():
    """★ 격자 슬롯은 **하루 경계(06:00) 기준**이고 `plan.start_min` 은 **자정 기준**이다.

    `slot * slot_minutes` 로 보내면 6시간(36슬롯) 어긋난다 — 실제로 그렇게 만들었다가
    계획이 엉뚱한 시간에 찍혔다 (2026-09-05, `/api/plan-slot`). 변환점은 여기 하나다.
    """
    for slot in (0, 1, 24, 60, 107, 108, 143):
        minute = timeutil.slot_to_wallclock_min(slot)
        assert timeutil.wallclock_min_to_slot(minute) == slot, (slot, minute)

    assert timeutil.slot_to_wallclock_min(0) == 6 * 60, "slot 0 은 06:00 이다"
    assert timeutil.slot_to_wallclock_min(108) == 0, "slot 108 은 자정이다"
