# Life Trainer — 계약서 v2 (레퍼런스 흡수)

> [contracts.md](../contracts.md) §0 공통 규칙은 그대로 유효하다. 이 문서는 그 위에 얹는다.
> 배경·근거: [implementation-plan.md](../implementation-plan.md) 및 [REFERENCE/README.md](../../../refs/reference/README.md)
>
> **당시 기준선: 테스트 442개.** (2026-08-17 시점) **현재 기준선은 여기 적지 않는다** —
> [../CLAUDE.md](../../CLAUDE.md) 한 곳에만 있다. 어느 단계에서도 아래로 내려가면 안 된다.
>
> ★ **이 문서는 시대순 누적의 마지막 층이다.** `contracts.md`(v1) → `contracts-planner.md`(2차)
> → 이 문서 순으로 얹혀 있고, 충돌하면 **나중 것이 이긴다.**

---

## 0. 이번 변경의 성격

`REFERENCE/` 의 자료 3개에서 개념을 흡수한다. **우리 구조를 갈아엎지 않는다** —
자동 계측이 실제로 도는 것은 우리뿐이고, 그게 이 프로젝트의 핵심 자산이다.

스키마는 **이미 확장해 두었다** (읽기 전용):
`device`, `day`, `subject`, `plan_instance`, `plan_instance_event`, `v_carry_debt`,
그리고 `aw_bucket.device_id` / `slot_breakdown.device_id`.

---

## 1. 파일 소유권

| 담당 | 소유 파일 |
|---|---|
| **V1 경계** | `lifetrainer/timeutil.py`, `lifetrainer/config.py`, `lifetrainer/db.py`, `lifetrainer/migrations/*`, `lifetrainer/rollup/rollup.py`, `lifetrainer/report/timeline.py`, `tests/test_timeutil.py`, `tests/test_migrate.py` |
| **V2 계획** | `lifetrainer/plan/models.py`, `plan/achieve.py`, `plan/subjects.py`, `tests/test_plan.py`, `tests/test_subjects.py` |
| **V3 인증** | `lifetrainer/web/auth.py`, `lifetrainer/web/app.py`, `tests/test_web_auth.py` — **`config.py` 는 건드리지 마라** (V1 이 필드를 미리 넣어둔다) |
| **V4 슬래시** | `lifetrainer/slackio/app.py`, `slackio/commands.py`, `slackio/blocks.py`, `tests/test_slack_commands.py` |
| **V5 UI** | `lifetrainer/web/templates/*`, `web/static/*`, `tests/test_web_ui.py` |
| **A 에이전트** (08-24 신설) | `lifetrainer/agent/*`, `scripts/eval_agent.py`, `scripts/install-agent.sh`, `tests/test_agent_*.py` — **`slackio/app.py` 는 V4 것이다.** 위임 배선(`_try_agent`·`_Ticker`)과 `day` 인자만 손댔고, 그 파일을 더 고칠 일이 생기면 V4 와 상의한다 |

`schema.sql`, `config/palette.yaml`, `docs/*`, `REFERENCE/*` 는 **읽기 전용**.

---

## 2. V1 — 논리적 하루 경계 06:00 ★ 먼저, 파괴적

**`slot 0 = 06:00~06:10`, `slot 143 = 익일 05:50~06:00`.**

지금은 자정 기준이라 새벽 1시 작업이 다음 날로 잘린다. 플래너 격자는 06시부터
그리는데 데이터 경계는 자정이라 **둘이 어긋나 있다.**

### `lifetrainer/timeutil.py`

기존 함수에 `boundary_hour: int = 6` 키워드를 추가한다. **기본값을 줘서 기존 호출부가
안 깨지게** 하되, 내부 계산은 전부 경계 기준으로 바꾼다.

```python
def day_str(ts: float, tz: tzinfo, *, boundary_hour: int = 6) -> str: ...
def day_bounds(day: str, tz: tzinfo, *, boundary_hour: int = 6) -> tuple[float, float]: ...
def slot_index(ts: float, tz: tzinfo, slot_minutes: int = 10, *, boundary_hour: int = 6) -> int: ...
def slot_bounds(day: str, slot: int, tz: tzinfo, slot_minutes: int = 10,
                *, boundary_hour: int = 6) -> tuple[float, float]: ...
def wallclock_min_to_slot(minute: int, slot_minutes: int = 10,
                          *, boundary_hour: int = 6) -> int: ...   # 신규
```

- `day_str`: 로컬 시각이 `boundary_hour` 이전이면 **전날** 날짜를 돌려준다
- `day_bounds`: `[해당일 06:00, 익일 06:00)`. **하루가 24시간이라고 가정하지 마라** — zoneinfo 로 계산
- `wallclock_min_to_slot(540)` (=09:00, boundary 6) → `18`. `wallclock_min_to_slot(60)` (=01:00) → `114`

`plan.start_min`/`end_min` 은 **벽시계 분 그대로 유지**한다. 사람은 시계로 생각하고,
경계값이 바뀌어도 마이그레이션이 필요 없다.

### `lifetrainer/config.py`

- `RollupConfig.day_boundary_hour: int = 6` 추가
- `ReportConfig.grid_start_hour` **제거** — 같은 값을 두 곳에 두면 반드시 갈라진다.
  참조하던 `report/planner.py`, `web/app.py` 는 `cfg.rollup.day_boundary_hour` 를 쓰게 바꾼다
  (그 두 파일은 네 소유가 아니므로 **최소한의 치환만** 하고 보고하라)

### 마이그레이션 — `lifetrainer/migrations/002_day_boundary.sql` + 러너

`db.py` 에 마이그레이션 러너를 만든다. `SCHEMA_VERSION = 2`.

```python
def migrate(conn) -> int: ...   # 적용한 마이그레이션 수 반환. 멱등
```

002 가 하는 일:
1. `aw_bucket.device_id`, `slot_breakdown.device_id` 컬럼 추가 (`ALTER TABLE ... ADD COLUMN`,
   이미 있으면 건너뜀 — `PRAGMA table_info` 로 확인)
2. 기존 호스트마다 `device` 행을 만들고 `aw_bucket.device_id` 를 채운다 (`kind='laptop'` 기본)
3. **`slot` / `slot_breakdown` / `unclassified_day` 를 전부 비운다** — 파생 데이터이고
   `aw_event`(epoch)에서 무손실 재생성된다
4. **`slot_override` 는 좌표 변환**한다. 사람이 넣은 입력이라 버리면 안 된다:
   기존 `(day, slot)` → 자정 기준으로 epoch 복원 → 새 경계 기준 `(day, slot)` 로 다시 기록.
   변환 중 충돌하면 나중 것이 이긴다.
5. `plan_check` 의 `checked=1` 을 `plan_instance` 로 이관하는 것은 **V2 담당이 한다** — 여기서 하지 마라

`lt migrate` CLI 는 V1 이 만들지 말고 **보고만** 하라 (`cli.py` 는 다른 담당 소유).

### `report/timeline.py`

주간 가로 띠의 x축이 00~24 고정이다. **06~06 으로** 바꾸고 눈금 라벨을 회전
(06,09,12,15,18,21,00,03,06). `render_day`/`render_week` 시그니처는 유지.

### 테스트 (`tests/test_timeutil.py` 보강, `tests/test_migrate.py` 신규)

- `day_str`: 05:59 → 전날, 06:00 → 당일, 23:59 → 당일
- `slot_index`: 06:00 → 0, 06:09 → 0, 06:10 → 1, 05:50(익일) → 143
- `wallclock_min_to_slot`: 09:00 → 18, 01:00 → 114, 06:00 → 0
- `day_bounds` 가 정확히 24시간이고 다음 날 06:00 에서 끝나는지
- **마이그레이션**: `slot_override` 가 변환 후에도 같은 실제 시각을 가리키는지(왕복 검증),
  두 번 실행해도 안전한지(멱등), `device` 행이 생기고 `aw_bucket` 이 연결되는지

---

## 3. V2 — 계획 인스턴스 · 상태 · 이월

`plan` 은 **반복 템플릿**, `plan_instance` 는 **그날의 실체**로 역할을 나눈다.

### `lifetrainer/plan/models.py` (기존 `Plan` CRUD 는 유지)

```python
@dataclass
class PlanInstanceRow:
    id: int; day: str; plan_id: int | None; title: str
    subject_id: int | None; category: str | None
    start_min: int; end_min: int
    status: str; priority: str; ordinal: int
    planned_min: int | None; note: str | None
    carried_from: int | None; source: str; archived: bool

def materialize_day(conn, cfg, day: str) -> int: ...
def add_instance(conn, cfg, day: str, *, title: str, start_min: int, end_min: int,
                 subject_id: int | None = None, category: str | None = None,
                 priority: str = "normal", source: str = "manual",
                 note: str | None = None) -> int: ...
def set_status(conn, cfg, instance_id: int, status: str, *, actor: str = "user") -> None: ...
def archive_instance(conn, instance_id: int) -> None: ...       # 소프트 삭제
def unarchive_instance(conn, instance_id: int) -> None: ...     # 실행 취소용
def list_instances(conn, day: str, *, include_archived: bool = False,
                   include_done: bool = True) -> list[PlanInstanceRow]: ...
def renumber(conn, day: str) -> None: ...                       # ordinal 1..N 재부여
def carry_debt(conn) -> list[tuple[int, int]]: ...              # v_carry_debt
```

**핵심 규칙**:
- `materialize_day` 는 **멱등**하다. 같은 날 여러 번 불러도 인스턴스가 늘지 않는다
  (`plan_id` + `day` 로 중복 판정). `plan_skip` 을 존중한다.
- `set_status(..., 'deferred')` 는 **다음 날 인스턴스를 자동 생성**하고
  새 인스턴스의 `carried_from` 에 원본 id 를 넣는다. `source='carry'`.
- 모든 상태 변경은 `plan_instance_event` 에 기록한다 (`from_status`, `to_status`, `actor`).
- 삭제는 `archived_at` 을 채우는 **소프트 삭제**. 실적이 사라지면 주간 통계가 깨진다.
- `ordinal` 은 `/done 3` 의 "3". 목록 표시 순서와 일치해야 한다.

기존 `set_check(conn, plan_id, day, checked)` 는 **얇은 래퍼로 남겨라** —
해당 날짜의 인스턴스를 찾아 `set_status(... 'done'|'todo')` 로 위임. 호출부를 깨지 마라.

### `lifetrainer/plan/achieve.py`

`plans_for_day` 가 `plan` 이 아니라 `plan_instance` 를 읽도록 전환한다.
진입 시 `materialize_day` 를 먼저 호출. **달성률 계산은 지금처럼 SQL 집계로** —
파이썬에서 슬롯을 순회하지 마라.

`PlanInstance` 에 `status`, `carried_depth`, `instance_id` 필드를 추가한다.
`subject_id` 가 있고 `category` 가 비어 있으면 **subject 의 category 를 상속**한다.

### `lifetrainer/plan/subjects.py` (신규)

```python
def create_subject(conn, cfg, *, name: str, color: str,
                   category: str | None = None, ordinal: int = 0) -> int: ...
def update_subject(conn, cfg, subject_id: int, **fields) -> None: ...
def delete_subject(conn, subject_id: int) -> None: ...          # archived=1
def list_subjects(conn, *, include_archived: bool = False) -> list[dict]: ...
def palette_choices(cfg) -> list[dict]: ...   # [{id, hex, label}] — 검증된 8슬롯
```

**`color` 는 `config/palette.yaml` 의 8슬롯 hex 중 하나여야 한다.**
아니면 `ValueError`. 자유 hex 를 받지 마라 — 색각 분리가 깨진다.
`report/palette.py` 의 `load_palette` 를 재사용하라. 색을 하드코딩하지 마라.

---

## 4. V3 — 원타임 링크 + 세션 + 외부 마스킹

### `lifetrainer/web/auth.py` (신규)

```python
def make_link_token(cfg, user_id: str, *, now: float | None = None) -> str: ...
def verify_link_token(cfg, token: str, *, now: float | None = None) -> str: ...  # user_id, 실패 시 예외
def make_session_cookie(cfg, user_id: str) -> str: ...
def verify_session_cookie(cfg, raw: str) -> str | None: ...
def signed_planner_url(cfg, user_id: str, day: str) -> str | None: ...  # base_url 없으면 None
class AuthError(Exception): ...
```

- HMAC-SHA256, `cfg.web.session_secret`. 비교는 `hmac.compare_digest`
- 링크 토큰 TTL `cfg.web.link_ttl_sec` (기본 600초)
- **URL 토큰은 1회용**: `/auth/enter` 에서 검증 후 쿠키로 교환하고, 사용한 토큰의
  jti 를 `sync_state` 에 기록해 재사용을 막는다
- 쿠키는 `HttpOnly`, `SameSite=Lax`, `cfg.web.external` 이면 `Secure`

### `config.py` 의 `WebConfig` 확장 — **V1 이 미리 넣어둔다. V3 은 쓰기만 한다.**

`config.py` 를 두 담당이 동시에 고치면 충돌한다. 그래서 V1 이 경계값과 함께
아래 필드까지 한 번에 추가한다:

`session_secret: str = ""`, `link_ttl_sec: int = 600`,
`session_ttl_sec: int = 2592000`, `external: bool = False`

`session_secret` 이 비었을 때 생성해 `data/websecret` 에 **0600** 으로 저장하는 로직은
**V3 이 `web/auth.py` 안에서** 처리한다 (`ensure_session_secret(cfg) -> str`).
설정 로더는 빈 문자열을 그대로 두면 된다.

### `web/app.py`

- `GET /auth/enter?u=&t=` — 토큰 검증 → 쿠키 설정 → `/d/<오늘>` 리다이렉트
- `before_request` 에서 세션 검증. `/healthz`, `/auth/enter`, 정적 파일은 면제
- `cfg.web.external` 이 아니면 **인증을 요구하지 않는다** (로컬/Tailscale 전용일 때 편의)

### ★ 외부 모드 프라이버시 마스킹

설계서 §10 위험표: *"창 제목 내 민감정보 → 카테고리만 저장"*.
`cfg.web.external = True` 면 **응답에서 다음을 제거**한다:

- `slot.top_title`, `slot.top_app`
- `slot_breakdown` 의 `app` 값
- `unclassified` 의 원문 제목

카테고리 id·라벨·시간만 내보낸다. **선택이 아니라 기본 동작이다.**

---

## 5. V4 — 슬래시 명령 전체 세트 (주 경로)

**왜 슬래시가 주 경로인가**: 실측으로 에이전트 경유 25~40초 vs 슬래시 밀리초.
게다가 소형 모델은 툴을 안 부르고 "했습니다"라고 답하는 실패 모드가 있다
(`operate/notes/agent-gateway.md §4-6`). 명시적 명령은 그게 불가능하다.

### `lifetrainer/slackio/commands.py` (신규 — 파싱만, Slack 비의존)

```python
@dataclass
class ParsedTask:
    title: str; subject: str | None; minutes: int | None
    priority: str; start_min: int | None; end_min: int | None

def parse_plan_text(text: str) -> list[ParsedTask]: ...
def parse_index_list(text: str) -> list[int]: ...      # '3,5' / '3 5' / '3' -> [3,5]
```

`parse_plan_text` 가 처리해야 하는 것:
- `저녁 약속가기` → 1건
- `1. 첫째\n2. 둘째` → **번호 목록이면 여러 건 일괄**
- `프로젝트 보고서 #프로젝트 @90m !high` → `subject='운영체제'`, `minutes=90`, `priority='high'`,
  토큰은 title 에서 제거
- `@60m` `@1h30m` 은 `timeutil.parse_duration` 재사용
- 시각 범위 `09:00-12:00` 이 있으면 `start_min`/`end_min`. `parse_time_range` 재사용

**Slack 을 import 하지 마라.** 순수 파서여야 테스트가 쉽다.

### `lifetrainer/slackio/app.py` 명령

| 명령 | 동작 |
|---|---|
| `/plan <텍스트>` | `parse_plan_text` → `add_instance`. 결과를 ephemeral 로 확인 |
| `/del` | **체크박스 목록** → 선택 → 삭제 버튼. 삭제 후 **15초 실행취소** 버튼 |
| `/del 3,5` | 번호 즉시 소프트 삭제 |
| `/view [어제\|YYYY-MM-DD]` | 플래너 PNG 업로드 + 카드 |
| `/done N` `/doing N` `/defer N` | `set_status`. 성공 시 짧은 확인 |
| `/memo <텍스트>` | `day.memo` 갱신 |
| `/week` | 주간 카드 + PNG |
| `/lt …` `/log …` | **기존 동작 유지** |

**`/del` 설계**: 완료(`done`) 항목은 **기본 숨김**. 삭제는 소프트 삭제.
실행취소는 `unarchive_instance`.

**공통**: `ack()` 를 3초 안에, 무거운 일은 `threading.Thread`.
핸들러마다 DB 커넥션을 새로 연다. 기존 `tests/test_notify.py`·`test_blocks.py` 가 계속 통과해야 한다.

---

## 6. V5 — 웹 UI (harugyeol 흡수)

**스택을 바꾸지 마라.** Next.js/React 를 도입하지 않는다 — 가용 메모리 4.5GB 에
(08-23 ctx 축소 전 2.8GB) Node 런타임을 더 올릴 이유가 없다. 메모리가 늘어난 것이
스택을 바꿀 근거는 아니다. 바닐라 JS + Flask 템플릿 그대로,
**디자인만 이식**한다.

`REFERENCE/harugyeol/app/globals.css` 와 `app/page.tsx` 를 읽고 가져올 것:

| 요소 | 클래스 참고 |
|---|---|
| NOW 라인 (현재 시각 표시) | `.now-line` |
| 완료율 링 (conic-gradient) | `.day-score`, `--score` |
| "지금 집중 중" 카드 | `.now-card`, `.live-dot` |
| 종이 질감 토큰 | `--paper #fffdf8`, `--cream #f4f0e8`, `--line #dad8d2` |
| eyebrow + paper-card | `.eyebrow`, `.paper-card` |
| 한 줄 기록 입력 | `.quick-memo` |
| 주간 뷰 | `.stats-grid`, `.week-chart` |
| 모바일 바텀시트 | `.adjust-sheet`, `.sheet-handle` |
| `prefers-reduced-motion` | 있음 |

**가져오지 마라**: 그들의 강조색(파스텔, CVD 미검증), 가짜 문구("Qwen의 아침 브리핑" 등),
`DEMO MODE` 배지, React 상태 모델.

### ★ 표면색을 바꾸면 팔레트를 다시 검증해야 한다

크림 표면 `#fffdf8` 은 현재 검증 표면 `#fcfcfb` 과 다르다. 검증기는 **대비를 표면 기준**으로
계산한다.

```bash
node /tmp/claude-1000/bundled-skills/*/dataviz/scripts/validate_palette.js \
  "#2a78d6,#eb6834,#1baf7a,#eda100,#e87ba4,#008300,#4a3aa7,#e34948" \
  --mode light --surface "#fffdf8"
```

**통과하면 채택, 실패하면 표면을 포기하고 `#fcfcfb` 유지.** 결과를 보고하라.
색은 취향보다 검증이 우선이다 — 이미 눈대중으로 골랐다가 3항목 FAIL 을 겪었다.

### 신규 라우트
`GET /w/<end_day>` (주간 화면), `GET /api/week/<end_day>`.
**기존 라우트 계약은 유지**한다 — `tests/test_web.py` 31개가 그대로 통과해야 한다.

---

## 7. 완료 조건 (모든 담당 공통)

1. 네 테스트 파일 통과 + **전체 스위트가 442개 이상**에서 실패 0
2. 계약 시그니처 정확히 일치
3. 남의 파일 미수정 (`git status` 로 확인)
4. 한국어 주석, 타입 힌트, `from __future__ import annotations`
5. **Slack 으로 아무것도 발송하지 마라**
6. 마지막에 간결히 보고: 만든 것, 계약과 달라진 점, 발견한 문제
