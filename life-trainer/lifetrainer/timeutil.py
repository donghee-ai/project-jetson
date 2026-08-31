"""시각 유틸리티.

원칙(계약서 §0): 내부는 전부 float unix epoch(UTC 초)로 다룬다.
ISO 문자열/로컬 날짜 문자열은 입출력 경계(ActivityWatch API, 롤업 결과, 사람에게 보여줄 때)에서만 쓴다.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone, tzinfo

# 'YYYY-MM-DDTHH:MM:SS' (구분자는 'T' 또는 공백) + 선택적 소수초 + 선택적 tz(Z 또는 ±HH:MM/±HHMM)
_ISO_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?P<frac>\.\d+)?"
    r"(?P<tz>Z|z|[+-]\d{2}:?\d{2})?$"
)


def now_ts() -> float:
    """현재 시각 (unix epoch, UTC 초)."""
    return time.time()


def to_ts(dt: datetime) -> float:
    """datetime -> epoch. naive datetime 은 UTC 로 간주한다."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def from_ts(ts: float, tz: tzinfo | None = None) -> datetime:
    """epoch -> datetime. tz 를 주지 않으면 UTC aware datetime 을 반환한다."""
    return datetime.fromtimestamp(ts, tz=tz or timezone.utc)


def parse_iso(s: str) -> float:
    """RFC3339/ISO8601 문자열을 epoch 로 변환한다.

    ActivityWatch 가 실제로 주는 형태를 전부 커버한다:
    'Z' 서픽스, '+09:00' 같은 명시적 오프셋, 마이크로초 유무·자릿수 편차.
    `datetime.fromisoformat` 은 3.10 에서 'Z' 를 못 읽고 소수초 자릿수도 까다로우므로
    직접 정규화한 뒤 넘긴다. tz 정보가 아예 없으면 UTC 로 간주한다.
    """
    text = s.strip()
    m = _ISO_RE.match(text)
    if not m:
        # 흔치 않은 형식은 표준 파서에 마지막으로 맡겨본다.
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    base = m.group("base").replace(" ", "T")
    frac = m.group("frac")
    tz_part = m.group("tz")

    if frac:
        digits = frac[1:]
        digits = (digits + "000000")[:6]  # 6자리로 맞춤 (부족하면 0으로 채움, 넘치면 자름)
        frac_norm = "." + digits
    else:
        frac_norm = ""

    if tz_part in (None, ""):
        tz_norm = "+00:00"  # tz 정보 없는 naive 문자열은 UTC 로 간주
    elif tz_part in ("Z", "z"):
        tz_norm = "+00:00"
    elif ":" in tz_part:
        tz_norm = tz_part
    else:
        tz_norm = tz_part[:3] + ":" + tz_part[3:]  # '+0900' -> '+09:00'

    iso = base + frac_norm + tz_norm
    dt = datetime.fromisoformat(iso)
    return dt.timestamp()


def iso_utc(ts: float) -> str:
    """epoch -> '2026-08-16T01:02:03.000000+00:00' 형태의 UTC ISO 문자열."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def day_str(ts: float, tz: tzinfo, *, boundary_hour: int = 6) -> str:
    """epoch -> 로컬 타임존 기준 'YYYY-MM-DD' (논리적 하루 경계 = boundary_hour 시).

    로컬 시각이 boundary_hour 이전이면 아직 "전날"이 끝나지 않은 것으로 본다
    (새벽 1시에 한 작업이 다음 날로 잘리지 않게). `boundary_hour=0` 이면 예전과
    같은 자정 기준 동작이 된다.
    """
    dt = datetime.fromtimestamp(ts, tz=tz)
    local_date = dt.date()
    if dt.hour < boundary_hour:
        local_date -= timedelta(days=1)
    return local_date.isoformat()


def day_bounds(day: str, tz: tzinfo, *, boundary_hour: int = 6) -> tuple[float, float]:
    """'YYYY-MM-DD' 날짜의 [시작, 끝) 을 로컬 타임존 기준 epoch 로 반환한다.

    경계는 그 날짜의 boundary_hour 시부터 다음 날 boundary_hour 시까지다
    (기본 06:00~익일 06:00). DST 가 있는 타임존에서도 하루가 24시간이라고
    가정하지 않는다 — 끝은 '시작 + 24시간'이 아니라 '다음 날 boundary_hour 시'를
    다시 계산해서 구한다.
    """
    y, mo, d = (int(part) for part in day.split("-"))
    start_date = date(y, mo, d)
    end_date = start_date + timedelta(days=1)
    start = datetime(start_date.year, start_date.month, start_date.day, boundary_hour, 0, 0, tzinfo=tz)
    end = datetime(end_date.year, end_date.month, end_date.day, boundary_hour, 0, 0, tzinfo=tz)
    return start.timestamp(), end.timestamp()


def wallclock_min_to_slot(minute: int, slot_minutes: int = 10, *, boundary_hour: int = 6) -> int:
    """자정 기준 벽시계 분(0..1439) -> 논리적 하루 경계 기준 슬롯 번호.

    `plan.start_min`/`end_min` 은 항상 자정 기준 벽시계 분으로 저장한다(사람은
    시계로 생각하므로, 경계값이 바뀌어도 계획 데이터를 마이그레이션할 필요가 없다).
    이 함수가 그 벽시계 분을 슬롯 격자 좌표로 바꾸는 유일한 변환점이다.
    """
    return ((minute - boundary_hour * 60) % 1440) // slot_minutes


def slot_index(ts: float, tz: tzinfo, slot_minutes: int = 10, *, boundary_hour: int = 6) -> int:
    """epoch 를 해당 논리적 하루 안에서의 슬롯 번호(0-base)로 변환한다."""
    dt = datetime.fromtimestamp(ts, tz=tz)
    minutes_since_midnight = dt.hour * 60 + dt.minute
    return wallclock_min_to_slot(minutes_since_midnight, slot_minutes, boundary_hour=boundary_hour)


def slot_bounds(
    day: str, slot: int, tz: tzinfo, slot_minutes: int = 10, *, boundary_hour: int = 6
) -> tuple[float, float]:
    """(day, slot) -> 그 슬롯의 [시작, 끝) epoch. 마지막 슬롯은 다음 날 경계 시각을 넘지 않는다."""
    day_start, day_end = day_bounds(day, tz, boundary_hour=boundary_hour)
    start = day_start + slot * slot_minutes * 60
    end = min(start + slot_minutes * 60, day_end)
    return start, end


def slots_per_day(slot_minutes: int = 10) -> int:
    """하루를 몇 개의 슬롯으로 나누는지."""
    return 1440 // slot_minutes


def overlap_sec(a0: float, a1: float, b0: float, b1: float) -> float:
    """두 구간 [a0,a1), [b0,b1) 이 겹치는 초. 음수를 반환하지 않는다."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def day_range(start_day: str, end_day: str) -> list[str]:
    """start_day 부터 end_day 까지 (양 끝 포함) 'YYYY-MM-DD' 리스트. 역순 지정도 허용."""
    d0 = date.fromisoformat(start_day)
    d1 = date.fromisoformat(end_day)
    step = timedelta(days=1) if d1 >= d0 else timedelta(days=-1)
    days: list[str] = []
    cur = d0
    while True:
        days.append(cur.isoformat())
        if cur == d1:
            break
        cur += step
    return days


_DURATION_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")
_DURATION_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(h|m|s)")

_DURATION_UNIT_SEC = {"h": 3600.0, "m": 60.0, "s": 1.0}


def parse_duration(s: str) -> float:
    """'60m', '1h30m', '90', '1.5h' 같은 기간 문자열을 초로 변환한다.

    단위 없는 순수 숫자는 **분**으로 해석한다 (Slack `/log` 사용자 입력 관례).
    형식이 어긋나면 ValueError.
    """
    text = s.strip().lower()
    if not text:
        raise ValueError("빈 기간 문자열입니다")

    if _DURATION_NUMBER_RE.match(text):
        return float(text) * 60.0

    total = 0.0
    pos = 0
    matched = False
    for m in _DURATION_TOKEN_RE.finditer(text):
        if m.start() != pos:
            raise ValueError(f"기간 문자열을 해석할 수 없습니다: {s!r}")
        total += float(m.group(1)) * _DURATION_UNIT_SEC[m.group(2)]
        pos = m.end()
        matched = True

    if not matched or pos != len(text):
        raise ValueError(f"기간 문자열을 해석할 수 없습니다: {s!r}")

    return total
