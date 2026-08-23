"""Life Trainer 웹 플래너 — Flask 앱 (계약서 §4).

종이 플래너를 그대로 옮긴다: 왼쪽/위쪽은 사람이 선언한 "계획", 오른쪽/아래쪽은
기계가 계측한 "실제"(24행×6칸 격자)다. 둘을 겹쳐 보는 것이 이 화면의 존재 이유다.

요청마다 `db.open_db(cfg)` 로 새 커넥션을 열고 끝나면 닫는다 (sqlite3 스레드 제약).
색은 전부 `lifetrainer.report.palette` 를 거친다 — 이 파일에는 색 하드코딩이 없다.
`cfg.web.read_only` 면 쓰기 라우트를 405 로 막는다.

인증(`cfg.web.external`)은 **기본적으로 꺼져 있다** — 로컬/Tailscale 전용일 때
로그인 벽을 세우면 불편하기만 하다. 외부에 열 때만 `/auth/enter` 의 원타임 서명
링크로 세션 쿠키를 발급하고, 이후 요청은 그 쿠키로 검증한다(`web/auth.py`).
그리고 외부에 열리는 순간 창 제목·앱 이름 같은 민감정보는 응답에서 지운다
(`_mask_external` — 설계서 §10 위험표).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

from lifetrainer import db, timeutil
from lifetrainer.collect import ingest
from lifetrainer.plan.achieve import day_achievement, plans_for_day
from lifetrainer.plan.models import (
    Plan,
    PlanInstance,
    create_plan,
    delete_plan,
    get_plan,
    parse_time_range,
    set_check,
    skip_plan,
    update_plan,
)
from lifetrainer.plan.override import clear_override_range, list_overrides, set_override_range
from lifetrainer.report.palette import Palette, css_variables, load_palette
from lifetrainer.rollup.classify import Classifier
from lifetrainer.rollup.rollup import rollup_day
from lifetrainer.report.stats import compute_daily, device_breakdown, format_hm
from lifetrainer.web import auth

logger = logging.getLogger(__name__)

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 인증 없이 통과하는 경로. `/auth/enter` 는 로그인 자체를 하는 경로라 당연히
# 면제고, `/healthz` 는 헬스체크(모니터링이 쿠키를 들고 다니지 않는다),
# 정적 파일은 민감정보가 없는 CSS/JS 다.
_AUTH_EXEMPT_PATHS = ("/healthz", "/auth/enter")
# `/ingest/` 는 세션 쿠키가 아니라 **본문 서명**(auth.verify_ingest)으로 인증한다.
# 폰에는 브라우저 세션이 없다. 면제는 "인증 없음"이 아니라 "다른 인증"이다.
_AUTH_EXEMPT_PREFIXES = ("/static/", "/ingest/")

# palette.yaml 에는 구조 상태(off/away/unknown)의 한글 라벨이 있지만
# report/palette.py 의 Palette.structural 은 색상만 뽑아온다(라벨은 활동 카테고리 전용).
# 이 셋은 스키마/설정에 고정된 어휘(색 선택이 아니다)라 여기서 상수로 둔다.
_STRUCTURAL_LABELS = {"away": "자리비움", "off": "빈칸", "unknown": "미분류"}

# CSS 변수 선언(`--key: value;`) 하나를 뽑아내는 정규식. css_variables() 의
# 정확한 래핑 문법(`:root { ... }`)에 의존하지 않기 위해 선언 자체만 추출한다.
_DECL_RE = re.compile(r"--[\w-]+\s*:\s*[^;]+;")


def _valid_day(s: Any) -> bool:
    """`s` 가 'YYYY-MM-DD' 형식의 실제 존재하는 날짜인지."""
    if not isinstance(s, str) or not _DAY_RE.match(s):
        return False
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return True


def _shift_day(day: str, delta: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=delta)).isoformat()


def _snap10(minute: int) -> int:
    """분을 10분 경계로 반올림한다 (계획 시각이 슬롯과 어긋나면 달성률이 부정확해진다)."""
    snapped = round(minute / 10) * 10
    return max(0, min(1440, snapped))


def _inner_decls(css_block: str) -> str:
    """`css_variables()` 출력에서 `--key: value;` 선언들만 뽑아 한 줄씩 이어붙인다."""
    return "\n".join(_DECL_RE.findall(css_block))


def _weekdays_kr(iso_weekdays: str) -> str:
    """ISO 요일 문자열 -> 사람이 읽기 좋은 한글 표현.

    '1234567' -> '매일', '12345' -> '평일', '67' -> '주말'. 그 외에는 '월수금'처럼
    요일을 나열한다 (`parse_weekdays` 가 항상 오름차순으로 정규화해 두므로 이 세
    패턴은 문자열 비교만으로 안전하게 잡힌다).
    """
    if iso_weekdays == "1234567":
        return "매일"
    if iso_weekdays == "12345":
        return "평일"
    if iso_weekdays == "67":
        return "주말"
    names = {"1": "월", "2": "화", "3": "수", "4": "목", "5": "금", "6": "토", "7": "일"}
    return "".join(names.get(ch, "") for ch in iso_weekdays)


def _hhmm(total_min: int) -> str:
    total_min = max(0, min(1440, int(total_min)))
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def _plan_to_dict(plan: Plan) -> dict[str, Any]:
    return dataclasses.asdict(plan)


def _instance_to_dict(pi: PlanInstance) -> dict[str, Any]:
    """PlanInstance -> JSON 직렬화 가능한 평평한 dict (계획 필드 + 그날의 달성 결과)."""
    d = dataclasses.asdict(pi.plan)
    d.update(
        day=pi.day,
        start_slot=pi.start_slot,
        end_slot=pi.end_slot,
        checked=pi.checked,
        planned_sec=pi.planned_sec,
        actual_sec=pi.actual_sec,
        achievement=pi.achievement,
        dominant_actual=pi.dominant_actual,
        time_range=f"{_hhmm(pi.plan.start_min)}-{_hhmm(pi.plan.end_min)}",
        weekdays_kr=_weekdays_kr(pi.plan.weekdays) if pi.plan.kind == "recurring" else "",
    )
    return d


_UNCLASSIFIED_LIMIT = 10  # 미분류 요약에 담을 지문 개수 (report/planner.py 의 PNG 카드와 맞춤)


def _fetch_slot_titles(conn: sqlite3.Connection, day: str) -> dict[int, tuple[str | None, str | None]]:
    """`slot` 테이블의 top_app/top_title 을 슬롯 번호로 인덱싱한다.

    로컬(비외부) 모드에서 격자 칸에 마우스를 올렸을 때 "정확히 뭘 하고 있었는지"
    보여주기 위한 값이다. 외부 모드에서는 `_mask_external` 이 이 값을 전부 지운다
    (설계서 §10: 창 제목 내 민감정보는 카테고리만 남기고 지워야 한다).
    """
    rows = conn.execute("SELECT slot, top_app, top_title FROM slot WHERE day = ?", (day,)).fetchall()
    return {int(r["slot"]): (r["top_app"], r["top_title"]) for r in rows}


def _unclassified_summary(
    conn: sqlite3.Connection, day: str, limit: int = _UNCLASSIFIED_LIMIT
) -> list[dict[str, Any]]:
    """그날 규칙에 안 걸려 미분류로 떨어진 지문 상위 N개(점유 시간 순).

    app/title 은 그 자체가 식별정보라 외부 모드에서는 `_mask_external` 이 지운다.
    """
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(u.app, ''), '') AS app,
               COALESCE(NULLIF(u.title_sample, ''), '') AS title,
               SUM(ud.seconds) AS sec
        FROM unclassified_day ud
        LEFT JOIN unclassified u ON u.fingerprint = ud.fingerprint
        WHERE ud.day = ?
        GROUP BY ud.fingerprint
        ORDER BY sec DESC
        LIMIT ?
        """,
        (day, limit),
    ).fetchall()
    return [{"app": r["app"], "title": r["title"], "seconds": float(r["sec"])} for r in rows]


def _mask_external(payload: dict[str, Any]) -> dict[str, Any]:
    """`cfg.web.external=True` 일 때 창 제목·앱 이름을 응답에서 지운다.

    설계서 §10 위험표: "창 제목 내 민감정보 → 카테고리만 저장". 서명 링크·평문
    HTTP 로 외부에 열리는 순간 이 값들은 브라우저 히스토리·프록시 로그로 샐 수
    있다. 카테고리 id·라벨·시간만 내보낸다 — 선택이 아니라 기본 동작이다.

    `payload`(`_build_day_payload` 의 반환값)를 in-place 로 고치고 그대로
    돌려준다. 지우는 필드(계약서 §4 그대로):
      - `slot.top_title`, `slot.top_app`
      - `slot_breakdown` 에서 나온 `stats.top_apps` (app 값 그 자체가 목적인 필드)
      - `unclassified` 항목의 원문 제목. app 도 함께 지운다 — "앱 이름이 노출되면
        안 된다"는 원칙을 title 여부로 예외 없이 지키기 위한 방어적 확장이다.
    """
    for slot in payload.get("slots", ()):
        slot.pop("top_app", None)
        slot.pop("top_title", None)

    stats = payload.get("stats")
    if isinstance(stats, dict):
        stats.pop("top_apps", None)

    for item in payload.get("unclassified", ()):
        item.pop("app", None)
        item.pop("title", None)

    # 기기 이름은 사람이 붙인 것이라("phone-example") 바깥에서는 종류만 남긴다.
    for dev in payload.get("devices", ()):
        dev["name"] = dev.get("label") or dev.get("kind") or "기기"

    return payload


def _should_mask(cfg: Any) -> bool:
    """창 제목·앱 이름을 지울지. 인터넷에서 온 요청에만, 그리고 설정이 켜져 있을 때만."""
    if cfg.web.external:
        return True
    return _request_is_external(cfg) and cfg.web.external_mask


def _request_is_external(cfg: Any) -> bool:
    """이 요청이 **인터넷(터널)** 에서 온 것인가.

    tailnet 직결(100.x IP:8770)과 구분한다. 예전에는 `cfg.web.external` 하나가
    인증·Secure 쿠키·마스킹 세 가지를 한꺼번에 켰는데, 그러면 tailnet 에서도 창
    제목이 사라지고, 반대로 tailnet 이 http 라서 Secure 쿠키를 못 걸었다.
    셋은 서로 다른 질문이라 따로 답한다.
    """
    host = (request.host or "").split(":")[0].lower()
    return bool(cfg.web.external_host) and host == cfg.web.external_host


def _request_is_https() -> bool:
    """터널은 앞단이 HTTPS 다. cloudflared 가 X-Forwarded-Proto 를 붙여 준다."""
    return request.headers.get("X-Forwarded-Proto", request.scheme).lower() == "https"


def _build_day_payload(
    conn: sqlite3.Connection, cfg: Any, day: str, palette_path: Path, theme: str = "light"
) -> dict[str, Any]:
    """`/api/day/<day>` 와 `/d/<day>` 가 공유하는 페이로드. 데이터가 없는 날짜도 안전하다.

    외부 모드 마스킹은 이 함수가 하지 않는다 — 호출부(`day_page`/`api_day`)가
    `cfg.web.external` 을 보고 `_mask_external` 을 적용한다(마스킹 여부가 이
    함수 하나에 숨어 있지 않고 라우트에서 명시적으로 보이게 하기 위함).
    """
    stats = compute_daily(conn, cfg, day)
    devices = device_breakdown(conn, cfg, day)
    overall, achieved_n, total_n = day_achievement(conn, cfg, day)
    instances = plans_for_day(conn, cfg, day)
    overrides = list_overrides(conn, day)
    titles = _fetch_slot_titles(conn, day)

    # 표시용 "실제" 카테고리 = 자동 분류 결과 위에 아직 롤업이 반영하지 않았을 수 있는
    # 최신 수동 보정을 겹쳐 보여준다 (slot_override 는 다음 롤업 때 slot.category 에
    # 반영되지만, 웹에서는 사용자가 보정한 즉시 격자에 보여야 한다).
    slots: list[dict[str, Any]] = []
    for i, cat in enumerate(stats.slot_categories):
        ov = overrides.get(i)
        eff_cat = ov["category"] if ov else cat
        top_app, top_title = titles.get(i, (None, None))
        slots.append(
            {
                "slot": i,
                "category": eff_cat,
                "raw_category": cat,
                "overridden": ov is not None,
                "top_app": top_app,
                "top_title": top_title,
            }
        )

    pal = load_palette(palette_path, theme)
    total_device_sec = sum(d.seconds for d in devices)

    return {
        "day": day,
        "grid_start_hour": cfg.rollup.day_boundary_hour,
        "slot_minutes": cfg.rollup.slot_minutes,
        "slots_per_day": cfg.slots_per_day,
        "slots": slots,
        "plans": [_instance_to_dict(pi) for pi in instances],
        "unclassified": _unclassified_summary(conn, day),
        "stats": {
            "active_sec": stats.active_sec,
            "active_hm": format_hm(stats.active_sec),
            "afk_sec": stats.afk_sec,
            "off_sec": stats.off_sec,
            "coverage": stats.coverage,
            "total_span_sec": stats.total_span_sec,
            "achievement": overall,
            "achieved_count": achieved_n,
            "total_count": total_n,
            "top_apps": [{"app": app, "seconds": sec} for app, sec in stats.top_apps],
        },
        # 기기별 활동. 폰이 들어오기 전에는 항상 한 줄이었다.
        "devices": [
            {
                "name": d.name,
                "kind": d.kind,
                "label": _DEVICE_LABELS.get(d.kind, d.kind),
                "seconds": d.seconds,
                "hm": format_hm(d.seconds),
                "share": (d.seconds / total_device_sec) if total_device_sec else 0.0,
            }
            for d in devices
        ],
        "palette": dataclasses.asdict(pal),
    }


_DEVICE_LABELS = {"laptop": "노트북", "phone": "폰", "tablet": "태블릿", "manual": "수동 입력"}


def _label_for(pal: Palette, category: str) -> str:
    return pal.labels.get(category) or _STRUCTURAL_LABELS.get(category) or category


def _fetch_slot_devices(conn: sqlite3.Connection, day: str) -> dict[int, str]:
    """슬롯마다 **그 칸의 색을 만든 기기**의 종류. 없으면 빠진다.

    칸 색은 승자 카테고리다. 그래서 기기도 "그 카테고리에 가장 많이 기여한 기기"로
    고른다 — 슬롯 전체에서 최다인 기기를 쓰면 색과 톤이 서로 다른 것을 가리킬 수 있다.
    """
    rows = conn.execute(
        """
        SELECT b.slot, d.kind AS kind, SUM(b.seconds) AS secs
        FROM slot_breakdown b
        JOIN slot sl ON sl.day = b.day AND sl.slot = b.slot AND sl.category = b.category
        LEFT JOIN device d ON d.id = b.device_id
        WHERE b.day = ? AND d.kind IS NOT NULL
        GROUP BY b.slot, d.kind
        """,
        (day,),
    ).fetchall()
    best: dict[int, tuple[str, float]] = {}
    for r in rows:
        cur = best.get(r["slot"])
        if cur is None or r["secs"] > cur[1]:
            best[r["slot"]] = (r["kind"], float(r["secs"]))
    return {slot: kind for slot, (kind, _) in best.items()}


def _build_grid_rows(
    cfg: Any,
    slots: list[dict[str, Any]],
    pal: Palette,
    instances: list[PlanInstance],
    slot_devices: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """24행(시간) × N열(슬롯) 격자를 렌더링용 구조로 만든다.

    행 라벨은 `cfg.rollup.day_boundary_hour` 부터 24시간 회전한다. 2차 인코딩 요구사항
    (계약서 §2): 한 행 안에서 같은 카테고리가 3칸 이상 연속되면 그 블록 가운데 칸에
    카테고리 이름을 직접 적는다. (행을 넘어가는 블록은 각 행에서 독립적으로 판정한다 —
    보통 긴 활동은 행 전체(6칸)를 채우므로 대부분의 경우 자연히 라벨이 붙는다.)
    계획 구간(plan)은 칸을 채우지 않고 테두리/옅은 wash 로만 겹친다 — `has_plan` 플래그를
    CSS 가 그렇게 그린다 (실제와 의도를 구분해야 하므로).
    """
    cols = max(1, cfg.slots_per_day // 24)
    grid_start_hour = cfg.rollup.day_boundary_hour
    plan_slots: set[int] = set()
    for pi in instances:
        plan_slots.update(range(pi.start_slot, pi.end_slot))

    rows: list[dict[str, Any]] = []
    for r in range(24):
        hour = (grid_start_hour + r) % 24
        # 슬롯 인덱스는 **행 번호**로 찾는다. 벽시계 hour 로 찾으면 안 된다 —
        # 하루 경계가 06:00 이 된 뒤로 slot 0 이 곧 06:00 이라, hour 를 쓰면
        # 6시간(36슬롯)만큼 이중으로 회전한다. hour 는 라벨 표시용일 뿐이다.
        base = r * cols
        row_cats = [slots[base + c]["category"] for c in range(cols) if base + c < len(slots)]

        # 연속 동일 카테고리 구간(run) 탐지 -> 길이 3 이상이면 가운데 칸에 라벨.
        label_at = [False] * len(row_cats)
        i = 0
        while i < len(row_cats):
            j = i
            while j < len(row_cats) and row_cats[j] == row_cats[i]:
                j += 1
            # 구조 상태(off/away/unknown)는 활동이 아니다 — 라벨을 붙이지 않는다
            # (종이 플래너의 빈 칸은 그냥 비어 있다). 활동 카테고리에만 적용한다.
            if j - i >= 3 and row_cats[i] in pal.categories:
                label_at[i + (j - i) // 2] = True
            i = j

        cells = []
        for c in range(len(row_cats)):
            slot_idx = base + c
            s = slots[slot_idx]
            cat = s["category"]
            is_structural = cat in pal.structural
            css_var = f"--structural-{cat}" if is_structural else f"--cat-{cat}"
            cells.append(
                {
                    "slot": slot_idx,
                    "category": cat,
                    "label_text": _label_for(pal, cat) if label_at[c] else "",
                    "tooltip": _label_for(pal, cat),
                    "css_var": css_var,
                    "overridden": s["overridden"],
                    "has_plan": slot_idx in plan_slots,
                    # 구조 상태(빈칸·자리비움)는 활동이 아니라 기기를 붙이지 않는다.
                    "device_kind": None if is_structural else (slot_devices or {}).get(slot_idx),
                }
            )
        rows.append({"hour": hour, "cells": cells})
    return rows


def _build_activity_runs(
    slots: list[dict[str, Any]], pal: Palette, grid_start_hour: int, slot_minutes: int
) -> list[dict[str, Any]]:
    """Dayflow처럼 읽을 수 있는 실제 활동 피드를 만든다.

    격자는 10분 단위 정밀도를 담당하고, 이 피드는 연속된 같은 카테고리를 한 문장으로
    압축해 회고를 담당한다. 기록 없음(off)과 자리비움(away)은 활동 이야기가 아니므로
    제외한다. 상세 문구는
    같은 런 안의 top_title을 우선하고 없으면 top_app을 쓰며, 외부 모드에서는 호출 전에
    `_mask_external`이 두 필드를 제거하므로 자동으로 빠진다.
    """
    runs: list[dict[str, Any]] = []
    index = 0
    while index < len(slots):
        category = str(slots[index].get("category") or "off")
        end = index + 1
        while end < len(slots) and slots[end].get("category") == category:
            end += 1

        if category not in ("off", "away"):
            start_clock_min = (grid_start_hour * 60 + index * slot_minutes) % 1440
            end_clock_min = (grid_start_hour * 60 + end * slot_minutes) % 1440
            detail = ""
            for slot in slots[index:end]:
                detail = str(slot.get("top_title") or slot.get("top_app") or "").strip()
                if detail:
                    break
            runs.append(
                {
                    "category": category,
                    "label": _label_for(pal, category),
                    "start": _hhmm(start_clock_min),
                    "end": _hhmm(end_clock_min),
                    # 격자 칸(`cell.slot`)과 이어 주려고 슬롯 번호를 함께 싣는다.
                    # 시각 문자열만 주면 클라이언트가 시각→슬롯 변환을 또 구현해야 하고,
                    # 그러면 하루 경계(06:00) 규칙이 두 벌이 된다(반복 실패 2번).
                    "slot_start": index,
                    "slot_end": end - 1,
                    "duration_min": (end - index) * slot_minutes,
                    "detail": detail,
                    "css_var": (
                        f"--structural-{category}" if category in pal.structural else f"--cat-{category}"
                    ),
                }
            )
        index = end
    return runs


_PLAN_UPDATE_KEYS = (
    "title",
    "category",
    "kind",
    "weekdays",
    "day",
    "active_from",
    "active_to",
    "color",
    "sort_order",
    "enabled",
)


def _parse_plan_body(body: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    """요청 바디를 `create_plan`/`update_plan` 키워드 인자로 바꾼다.

    'start'/'end' (HH:MM) 축약형과 'start_min'/'end_min' 직접 지정을 모두 받는다.
    계획 시각은 10분 경계로 스냅한다 (계약서: 09:05 같은 입력이 슬롯과 어긋나면
    달성률이 부정확해진다).
    """
    fields: dict[str, Any] = {}

    if "title" in body:
        title = str(body["title"]).strip()
        if not title:
            raise ValueError("title 은 비어 있을 수 없습니다")
        fields["title"] = title
    elif not partial:
        raise ValueError("title 은 필수입니다")

    has_hhmm = body.get("start") and body.get("end")
    has_min = "start_min" in body and "end_min" in body
    if has_hhmm:
        start_min, end_min = parse_time_range(f"{body['start']}-{body['end']}")
        fields["start_min"] = _snap10(start_min)
        fields["end_min"] = _snap10(end_min)
    elif has_min:
        fields["start_min"] = _snap10(int(body["start_min"]))
        fields["end_min"] = _snap10(int(body["end_min"]))
    elif not partial:
        raise ValueError("start/end (HH:MM) 또는 start_min/end_min 이 필요합니다")

    if "category" in body:
        cat = body["category"]
        fields["category"] = (str(cat).strip() or None) if cat is not None else None

    if "weekdays" in body and body["weekdays"]:
        fields["weekdays"] = str(body["weekdays"])

    if "day" in body:
        day_val = body["day"] or None
        if day_val is not None and not _valid_day(day_val):
            raise ValueError(f"잘못된 날짜 형식입니다: {day_val!r}")
        fields["day"] = day_val

    if "kind" in body:
        fields["kind"] = str(body["kind"])
    elif not partial and fields.get("day"):
        # 폼에서 특정 날짜를 지정했는데 kind 를 안 줬으면 일회성으로 간주한다.
        fields["kind"] = "oneoff"

    for key in ("active_from", "active_to"):
        if key in body:
            val = body[key] or None
            if val is not None and not _valid_day(val):
                raise ValueError(f"잘못된 날짜 형식입니다: {val!r}")
            fields[key] = val

    if "color" in body:
        fields["color"] = body["color"] or None

    if "sort_order" in body:
        fields["sort_order"] = int(body["sort_order"])

    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])

    return fields


def _json_script(data: Any) -> str:
    """`<script type="application/json">` 안에 안전하게 박아 넣을 문자열.

    `</script>` 로 태그가 끊기지 않도록 슬래시 앞에 이스케이프를 넣는다.
    """
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


class _PrefixMiddleware:
    """앱을 `prefix` 아래에도 응답하게 한다 (예: `/planner/d/2026-08-22`).

    ## 왜 접두사가 필요한가

    터널·Cloudflare Access 를 **경로 하나로** 막기 위해서다. 접두사가 없으면
    `/d/` `/w/` `/api/` `/static/` `/auth/` 를 하나씩 열거해야 하고, 나중에 라우트를
    추가하면서 그 목록 갱신을 잊으면 **조용히 인터넷에 노출된다.** 경계는 한 줄이어야 한다.

    ## 왜 접두사 없는 경로도 계속 받나

    tailnet 직결은 예전 주소(`http://100.64.0.2:8770/d/...`)를 그대로 쓴다.
    인터넷 쪽 경계는 터널 ingress 와 Access 가 지키므로, 앱이 둘 다 받아도 노출이
    늘지 않는다. 북마크와 Slack 링크를 깨뜨리지 않는 쪽을 택했다.
    """

    def __init__(self, wsgi_app, prefix: str) -> None:
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if self.prefix and (path == self.prefix or path.startswith(self.prefix + "/")):
            environ["SCRIPT_NAME"] = self.prefix + environ.get("SCRIPT_NAME", "")
            environ["PATH_INFO"] = path[len(self.prefix) :] or "/"
        return self.wsgi_app(environ, start_response)


def _asset_version(app: Flask) -> str:
    """정적 파일의 최신 수정 시각. `?v=` 로 붙여 브라우저 캐시를 깬다.

    ★ 왜 필요한가: `planner.js` 를 고쳐도 브라우저가 옛 파일을 들고 있으면 화면은
    멀쩡한데 **버튼만 안 먹는다.** 서버 쪽 `no-cache` 헤더로는 부족했다 — 이미
    캐시에 앉은 사본은 그대로 쓰인다. 실제로 `/planner` 접두사 수정을 배포한 뒤에도
    같은 404 가 이어졌다.

    mtime 이라 파일을 고칠 때만 값이 바뀐다 — 재시작만으로는 안 바뀐다.
    """
    root = Path(app.static_folder or ".")
    try:
        return str(int(max(f.stat().st_mtime for f in root.glob("*") if f.is_file())))
    except (ValueError, OSError):
        return "0"


def create_app(cfg: Any) -> Flask:
    """Flask 앱 팩토리. 테스트는 이 함수로 `test_client()` 를 만든다."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    palette_path = cfg.root / "config" / "palette.yaml"
    asset_v = _asset_version(app)

    if cfg.web.url_prefix:
        app.wsgi_app = _PrefixMiddleware(app.wsgi_app, cfg.web.url_prefix)

    @app.errorhandler(HTTPException)
    def _json_errors(exc: HTTPException) -> Response:
        """API 라우트 위주 앱이라 에러도 JSON 으로 통일한다."""
        resp = jsonify({"ok": False, "error": exc.name, "message": exc.description})
        resp.status_code = exc.code or 500
        return resp

    @app.before_request
    def _enforce_auth() -> None:
        """`cfg.web.external` 이면 세션 쿠키를 요구한다.

        기본(`external=False`, 로컬/Tailscale 전용)은 인증 없이 통과한다 —
        요구사항 그대로: 인증은 "선택해서 켜는" 것이 아니라 "외부에 열 때만
        자동으로 켜지는" 것이다.
        """
        # ★ 외부 호스트로 들어온 요청은 `cfg.web.external` 과 무관하게 **항상** 세션을
        #   요구한다. 전에는 이 플래그 하나에 걸려 있어서, 터널만 열고 플래그를 깜빡하면
        #   플래너가 통째로 공개되는 구조였다.
        if not (cfg.web.external or _request_is_external(cfg)):
            return
        path = request.path
        if path in _AUTH_EXEMPT_PATHS or path.startswith(_AUTH_EXEMPT_PREFIXES):
            return
        raw = request.cookies.get(auth.SESSION_COOKIE_NAME, "")
        if auth.verify_session_cookie(cfg, raw) is None:
            abort(401, description="로그인이 필요합니다 (슬랙에서 서명 링크로 다시 열어 주세요)")

    @app.before_request
    def _enforce_read_only() -> None:
        if cfg.web.read_only and request.method in ("POST", "PATCH", "DELETE"):
            abort(405, description="읽기 전용 모드입니다 (cfg.web.read_only=True)")

    # ── 수신 (폰 → 젯슨) ────────────────────────────────────────────────

    def _rollup_after_ingest(conn, cfg, result) -> set[str]:
        """받은 이벤트가 걸친 날짜를 **그 자리에서** 다시 롤업한다.

        ## 왜 여기서 하는가

        전에는 `apply_payload` 만 하고 끝냈다. 표에 반영되는 것은 10분마다 도는
        `lifetrainer-sync.timer` 에 의존했는데, 그 타이머는 **오늘만** 롤업한다.
        폰은 Doze 때문에 늦게 도착해서 어제 후반부를 실어 오는 일이 흔하고,
        그 구간은 **아무도 다시 롤업하지 않아 영영 표에 안 나타났다.**

        ## 실패해도 200 을 돌려준다

        이벤트는 이미 커밋됐다. 롤업은 언제든 다시 돌릴 수 있는 파생 계산이라,
        여기서 실패했다고 폰에게 재전송을 시키면 같은 데이터가 또 올 뿐이다.
        대신 로그에 남기고 다음 sync 타이머가 오늘치를 다시 맞춘다.
        """
        days = ingest.days_touched(cfg, result)
        if not days:
            return set()
        try:
            classifier = Classifier.from_yaml(cfg.rollup.rules_path)
            for day in sorted(days):
                rollup_day(conn, cfg, classifier, day)
        except Exception as exc:  # noqa: BLE001 - 수신 자체는 이미 성공했다
            logger.warning("수신 후 롤업 실패 (days=%s): %s", sorted(days), exc)
            return set()
        return days



    @app.post("/ingest/aw")
    def ingest_aw() -> Response:
        """폰이 밀어 넣는 ActivityWatch export 를 받는다.

        **이 앱에서 공개 인터넷(Cloudflare Tunnel)으로 열리는 유일한 경로다.**
        그래서 검사 순서가 곧 방어선이다: 켜져 있는가 → 크기 → 서명 → 형식.
        서명 전에 본문을 파싱하지 않는다.
        """
        if not cfg.ingest.enabled:
            abort(404, description="수신이 꺼져 있습니다 (cfg.ingest.enabled=False)")

        # Content-Length 를 먼저 보고, 값이 없거나 거짓말일 수 있으므로 실제로 읽은
        # 바이트로 다시 확인한다.
        limit = cfg.ingest.max_body_bytes
        if (request.content_length or 0) > limit:
            abort(413, description=f"본문이 너무 큽니다 (상한 {limit} 바이트)")
        body = request.get_data(cache=False)
        if len(body) > limit:
            abort(413, description=f"본문이 너무 큽니다 (상한 {limit} 바이트)")

        conn = db.open_db(cfg)
        try:
            try:
                device = auth.verify_ingest(conn, cfg, request.headers.get("Authorization"), body)
            except auth.AuthError as exc:
                # 실패 사유를 그대로 돌려준다. 폰 쪽 설정 오류(시계·비밀키)를
                # 로그 없이 진단할 수 있어야 한다 — 어차피 서명을 못 만든 상대는
                # 이 문장으로 얻을 것이 없다.
                abort(401, description=str(exc))

            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                abort(400, description=f"JSON 을 해석할 수 없습니다: {exc}")

            try:
                with db.transaction(conn):
                    result = ingest.apply_payload(conn, payload, device=device)
                    auth.prune_ingest_nonces(conn, cfg)
            except ingest.IngestError as exc:
                abort(400, description=str(exc))

            rolled = _rollup_after_ingest(conn, cfg, result)

            return jsonify(
                {
                    "ok": True,
                    "device": result.device,
                    "buckets": result.buckets,
                    "events": result.events,
                    "skipped": result.skipped,
                    "rolled": sorted(rolled),
                }
            )
        finally:
            conn.close()

    # ── 인증 ────────────────────────────────────────────────────────────

    @app.get("/auth/enter")
    def auth_enter() -> Response:
        """서명 링크 → 세션 쿠키 교환. 링크 토큰은 검증 즉시 1회용으로 소비된다."""
        token = request.args.get("t", "")
        u_param = request.args.get("u")
        day_param = request.args.get("d")

        conn = db.open_db(cfg)
        try:
            try:
                user_id = auth.consume_link_token(conn, cfg, token)
            except auth.AuthError as exc:
                abort(403, description=str(exc))
        finally:
            conn.close()

        # u= 는 토큰 자체에도 이미 서명되어 담겨 있어 검증에 필수는 아니지만,
        # 있으면 방어적으로 한 번 더 대조한다.
        if u_param and u_param != user_id:
            abort(403, description="링크의 사용자 정보가 일치하지 않습니다")

        target_day = (
            day_param
            if _valid_day(day_param)
            else timeutil.day_str(timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)
        )
        resp = redirect(url_for("day_page", day=target_day))
        resp.set_cookie(
            auth.SESSION_COOKIE_NAME,
            auth.make_session_cookie(cfg, user_id),
            # HTTPS 로 들어온 요청이면 Secure. 전역 external 모드는 예전 계약대로 항상 Secure.
            **auth.session_cookie_kwargs(cfg, secure=_request_is_https() or cfg.web.external),
        )
        return resp

    # ── 페이지 ──────────────────────────────────────────────────────────

    @app.get("/")
    def index() -> Response:
        today = timeutil.day_str(timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)
        return redirect(url_for("day_page", day=today))

    @app.get("/d/<day>")
    def day_page(day: str) -> Response:
        if not _valid_day(day):
            abort(400, description=f"잘못된 날짜 형식입니다: {day!r} (YYYY-MM-DD)")

        conn = db.open_db(cfg)
        try:
            data = _build_day_payload(conn, cfg, day, palette_path)
            if _should_mask(cfg):
                _mask_external(data)
            pal_light = load_palette(palette_path, "light")
            instances = plans_for_day(conn, cfg, day)
            slot_devices = _fetch_slot_devices(conn, day)
            grid_rows = _build_grid_rows(cfg, data["slots"], pal_light, instances, slot_devices)
        finally:
            conn.close()

        light_css = css_variables(pal_light)
        dark_css = _inner_decls(css_variables(load_palette(palette_path, "dark")))
        today = timeutil.day_str(timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)
        present_categories = {slot["category"] for slot in data["slots"]}
        used_categories = [cat for cat in pal_light.order if cat in present_categories]
        used_structural = [cat for cat in ("away", "unknown", "off") if cat in present_categories]
        cols = grid_rows[0]["cells"] if grid_rows else []
        minute_labels = [cfg.rollup.slot_minutes * (idx + 1) for idx in range(len(cols))]
        activity_runs = _build_activity_runs(
            data["slots"], pal_light, cfg.rollup.day_boundary_hour, cfg.rollup.slot_minutes
        )

        return render_template(
            "planner.html",
            day=day,
            today=today,
            prev_day=_shift_day(day, -1),
            next_day=_shift_day(day, 1),
            data=data,
            data_json=_json_script(data),
            grid_rows=grid_rows,
            palette=pal_light,
            used_categories=used_categories,
            used_structural=used_structural,
            minute_labels=minute_labels,
            has_phone_cells=any(k == "phone" for k in slot_devices.values()),
            # ★ JS 가 API 를 부를 때 붙일 접두사. 절대 경로(`/api/...`)로 부르면
            #   터널(`lt.example.com/planner/...`)에서 접두사 밖으로 나가 404 가 난다 —
            #   실제로 계획 수정·삭제·체크가 전부 조용히 실패했다.
            url_prefix=request.script_root or "",
            asset_v=asset_v,
            activity_runs=activity_runs,
            light_css_vars=light_css,
            dark_css_vars=dark_css,
            read_only=cfg.web.read_only,
            hhmm=_hhmm,
        )

    @app.get("/w/<end_day>")
    def week_page(end_day: str) -> Response:
        """주간 화면. 데이터는 week.js 가 /api/day 를 7번 불러 클라이언트에서 집계한다.

        전용 /api/week 를 두지 않은 이유: 집계 로직이 이미 /api/day 안에 있고,
        7일치를 서버에서 다시 합치면 같은 계산이 두 벌이 된다. 하루 7회 호출은
        로컬 SQLite 라 비용이 무시할 수준이다.
        """
        if not _valid_day(end_day):
            abort(400, description=f"잘못된 날짜 형식입니다: {end_day!r} (YYYY-MM-DD)")

        pal_light = load_palette(palette_path, "light")
        light_css = css_variables(pal_light)
        dark_css = _inner_decls(css_variables(load_palette(palette_path, "dark")))
        today = timeutil.day_str(
            timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour
        )

        return render_template(
            "week.html",
            url_prefix=request.script_root or "",
            asset_v=asset_v,
            end_day=end_day,
            today=today,
            prev_end_day=_shift_day(end_day, -7),
            next_end_day=_shift_day(end_day, 7),
            palette=pal_light,
            light_css_vars=light_css,
            dark_css_vars=dark_css,
            read_only=cfg.web.read_only,
        )

    # ── 조회 API ────────────────────────────────────────────────────────

    @app.get("/api/day/<day>")
    def api_day(day: str) -> Response:
        if not _valid_day(day):
            abort(400, description=f"잘못된 날짜 형식입니다: {day!r} (YYYY-MM-DD)")
        theme = request.args.get("theme", "light")
        if theme not in ("light", "dark"):
            abort(400, description=f"알 수 없는 테마입니다: {theme!r}")
        conn = db.open_db(cfg)
        try:
            data = _build_day_payload(conn, cfg, day, palette_path, theme)
            if _should_mask(cfg):
                _mask_external(data)
        finally:
            conn.close()
        return jsonify(data)

    @app.get("/healthz")
    def healthz() -> Response:
        return jsonify({"ok": True})

    # ── 계획 CRUD ───────────────────────────────────────────────────────

    @app.post("/api/plan")
    def api_plan_create() -> tuple[Response, int]:
        body = request.get_json(silent=True) or {}
        try:
            fields = _parse_plan_body(body, partial=False)
        except ValueError as exc:
            abort(400, description=str(exc))

        conn = db.open_db(cfg)
        try:
            try:
                plan_id = create_plan(conn, **fields)
            except ValueError as exc:
                abort(400, description=str(exc))
            plan = get_plan(conn, plan_id)
        finally:
            conn.close()
        return jsonify({"ok": True, "plan": _plan_to_dict(plan)}), 201

    @app.patch("/api/plan/<int:plan_id>")
    def api_plan_update(plan_id: int) -> Response:
        conn = db.open_db(cfg)
        try:
            if get_plan(conn, plan_id) is None:
                abort(404, description=f"계획을 찾을 수 없습니다: id={plan_id}")

            body = request.get_json(silent=True) or {}
            try:
                fields = _parse_plan_body(body, partial=True)
            except ValueError as exc:
                abort(400, description=str(exc))
            if not fields:
                abort(400, description="수정할 필드가 없습니다")

            try:
                update_plan(conn, plan_id, **fields)
            except ValueError as exc:
                abort(400, description=str(exc))
            plan = get_plan(conn, plan_id)
        finally:
            conn.close()
        return jsonify({"ok": True, "plan": _plan_to_dict(plan)})

    @app.delete("/api/plan/<int:plan_id>")
    def api_plan_delete(plan_id: int) -> Response:
        conn = db.open_db(cfg)
        try:
            if get_plan(conn, plan_id) is None:
                abort(404, description=f"계획을 찾을 수 없습니다: id={plan_id}")
            # 오늘부터의 인스턴스를 함께 보관 처리한다 — 안 하면 지운 계획이
            # 오늘 목록에 그대로 남는다. 과거는 남긴다(주간 통계 보존).
            today = timeutil.day_str(
                timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour
            )
            archived = delete_plan(conn, plan_id, from_day=today)
        finally:
            conn.close()
        return jsonify({"ok": True, "id": plan_id, "deleted": True, "archived": archived})

    @app.post("/api/plan/<int:plan_id>/check")
    def api_plan_check(plan_id: int) -> Response:
        body = request.get_json(silent=True) or {}
        day = body.get("day")
        if not _valid_day(day):
            abort(400, description="day 가 필요합니다 (YYYY-MM-DD)")
        checked = bool(body.get("checked", True))

        conn = db.open_db(cfg)
        try:
            if get_plan(conn, plan_id) is None:
                abort(404, description=f"계획을 찾을 수 없습니다: id={plan_id}")
            set_check(conn, plan_id, day, checked)
        finally:
            conn.close()
        return jsonify({"ok": True, "id": plan_id, "day": day, "checked": checked})

    @app.post("/api/plan/<int:plan_id>/skip")
    def api_plan_skip(plan_id: int) -> Response:
        body = request.get_json(silent=True) or {}
        day = body.get("day")
        if not _valid_day(day):
            abort(400, description="day 가 필요합니다 (YYYY-MM-DD)")

        conn = db.open_db(cfg)
        try:
            if get_plan(conn, plan_id) is None:
                abort(404, description=f"계획을 찾을 수 없습니다: id={plan_id}")
            skip_plan(conn, plan_id, day)
        finally:
            conn.close()
        return jsonify({"ok": True, "id": plan_id, "day": day, "skipped": True})

    # ── 수동 보정(슬롯 오버라이드) ──────────────────────────────────────

    def _parse_slot_range(body: dict[str, Any]) -> tuple[str, int, int]:
        day = body.get("day")
        if not _valid_day(day):
            abort(400, description="day 가 필요합니다 (YYYY-MM-DD)")
        if "start_slot" not in body or "end_slot" not in body:
            abort(400, description="start_slot, end_slot 이 필요합니다")
        try:
            start_slot = int(body["start_slot"])
            end_slot = int(body["end_slot"])
        except (TypeError, ValueError):
            abort(400, description="start_slot/end_slot 은 정수여야 합니다")
        if not (0 <= start_slot < end_slot <= cfg.slots_per_day):
            abort(400, description=f"잘못된 슬롯 범위입니다: {start_slot}..{end_slot}")
        return day, start_slot, end_slot

    @app.post("/api/slot")
    def api_slot_set() -> Response:
        body = request.get_json(silent=True) or {}
        day, start_slot, end_slot = _parse_slot_range(body)
        category = body.get("category")
        if not category:
            abort(400, description="category 가 필요합니다")

        conn = db.open_db(cfg)
        try:
            touched = set_override_range(conn, day, start_slot, end_slot, category, actor="web")
        finally:
            conn.close()
        return jsonify(
            {
                "ok": True,
                "day": day,
                "start_slot": start_slot,
                "end_slot": end_slot,
                "category": category,
                "touched": touched,
            }
        )

    @app.delete("/api/slot")
    def api_slot_clear() -> Response:
        body = request.get_json(silent=True) or {}
        day, start_slot, end_slot = _parse_slot_range(body)

        conn = db.open_db(cfg)
        try:
            touched = clear_override_range(conn, day, start_slot, end_slot)
        finally:
            conn.close()
        return jsonify(
            {"ok": True, "day": day, "start_slot": start_slot, "end_slot": end_slot, "touched": touched}
        )

    return app
