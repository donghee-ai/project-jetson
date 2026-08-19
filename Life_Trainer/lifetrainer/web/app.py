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
from lifetrainer.report.stats import compute_daily, format_hm
from lifetrainer.web import auth

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

    return payload


def _build_day_payload(
    conn: sqlite3.Connection, cfg: Any, day: str, palette_path: Path, theme: str = "light"
) -> dict[str, Any]:
    """`/api/day/<day>` 와 `/d/<day>` 가 공유하는 페이로드. 데이터가 없는 날짜도 안전하다.

    외부 모드 마스킹은 이 함수가 하지 않는다 — 호출부(`day_page`/`api_day`)가
    `cfg.web.external` 을 보고 `_mask_external` 을 적용한다(마스킹 여부가 이
    함수 하나에 숨어 있지 않고 라우트에서 명시적으로 보이게 하기 위함).
    """
    stats = compute_daily(conn, cfg, day)
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
        "palette": dataclasses.asdict(pal),
    }


def _label_for(pal: Palette, category: str) -> str:
    return pal.labels.get(category) or _STRUCTURAL_LABELS.get(category) or category


def _build_grid_rows(
    cfg: Any, slots: list[dict[str, Any]], pal: Palette, instances: list[PlanInstance]
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
                }
            )
        rows.append({"hour": hour, "cells": cells})
    return rows


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


def create_app(cfg: Any) -> Flask:
    """Flask 앱 팩토리. 테스트는 이 함수로 `test_client()` 를 만든다."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    palette_path = cfg.root / "config" / "palette.yaml"

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
        if not cfg.web.external:
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

            return jsonify(
                {
                    "ok": True,
                    "device": result.device,
                    "buckets": result.buckets,
                    "events": result.events,
                    "skipped": result.skipped,
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
            auth.SESSION_COOKIE_NAME, auth.make_session_cookie(cfg, user_id), **auth.session_cookie_kwargs(cfg)
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
            if cfg.web.external:
                _mask_external(data)
            pal_light = load_palette(palette_path, "light")
            instances = plans_for_day(conn, cfg, day)
            grid_rows = _build_grid_rows(cfg, data["slots"], pal_light, instances)
        finally:
            conn.close()

        light_css = css_variables(pal_light)
        dark_css = _inner_decls(css_variables(load_palette(palette_path, "dark")))
        today = timeutil.day_str(timeutil.now_ts(), cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)

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
            if cfg.web.external:
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
            delete_plan(conn, plan_id)
        finally:
            conn.close()
        return jsonify({"ok": True, "id": plan_id, "deleted": True})

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
