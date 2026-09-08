# Life Trainer — 플래너 계약서 (2차 구현)

> [contracts.md](../contracts.md) 의 §0 공통 규칙은 그대로 유효하다. 이 문서는 그 위에 얹는다.
> 그 위에 다시 [contracts-v2.md](contracts-v2.md) 가 얹힌다 — **충돌하면 나중 것이 이긴다.**
> 근거: [refs/reference/planner_example.png](../design/refs/planner_example.png) (24행 × 6칸 = 144, 06:00 시작)

---

## 0. 무엇을 만드는가

종이 플래너를 그대로 옮긴다. 단, **격자는 기계가 채운다.**

```
┌─ 계획 (사람이 선언) ─────┬─ TIMETABLE (기계가 계측 + 사람이 보정) ─┐
│ ☑ 코딩   09:00-12:00 92% │  06 ▫▫▫▫▫▫                            │
│ ☐ 운동   18:00-19:00  0% │  07 ▪▪▫▫▫▫                            │
│ ☑ 독서   21:00-22:00 78% │  …  (24행 × 6칸, 한 칸 10분)            │
└──────────────────────────┴────────────────────────────────────────┘
```

**설계 원칙**: `slot` = 기계가 계측한 **실제**. `plan` = 사람이 선언한 **의도**.
둘을 겹쳐 보는 것이 플래너의 존재 이유다. **절대 같은 테이블에 섞어 저장하지 않는다.**

---

## 1. 파일 소유권

| 담당 | 소유 파일 |
|---|---|
| **P1 계획 계층** | `lifetrainer/plan/__init__.py`, `plan/models.py`, `plan/achieve.py`, `plan/override.py`, `lifetrainer/config.py`(추가만), `lifetrainer/rollup/rollup.py`(오버라이드 적용부만), `tests/test_plan.py`, `tests/test_override.py` |
| **P2 웹** | `lifetrainer/web/__init__.py`, `web/app.py`, `web/templates/*`, `web/static/*`, `tests/test_web.py` |
| **P3 플래너 PNG** | `lifetrainer/report/planner.py`, `lifetrainer/report/palette.py`, `tests/test_planner_png.py` |
| **P4 배선** | `lifetrainer/cli.py`(추가만), `lifetrainer/slackio/notify.py`(링크 추가만), `systemd/lifetrainer-web.service`, `scripts/enable-tailscale-serve.sh`, `tests/test_cli_plan.py` |

`schema.sql`, `config/palette.yaml`, 이 문서는 **읽기 전용**이다.

---

## 2. 색 — `config/palette.yaml` 이 유일한 원본

**색 하드코딩 절대 금지.** 웹 CSS도 matplotlib도 이 파일을 읽어 쓴다.

`lifetrainer/report/palette.py` (P3 소유)가 로더를 제공하고 나머지가 쓴다:

```python
@dataclass(frozen=True)
class Palette:
    theme: str                       # 'light' | 'dark'
    surface: str
    ink: dict[str, str]              # primary/secondary/muted/gridline/baseline
    categories: dict[str, str]       # category_id -> hex
    labels: dict[str, str]           # category_id -> 한글 라벨
    structural: dict[str, str]       # away/off/unknown -> hex
    plan: dict[str, object]          # plan_overlay 값들
    order: list[str]                 # 고정 슬롯 순서

def load_palette(path: str | Path, theme: str = "light") -> Palette: ...
def css_variables(pal: Palette) -> str: ...   # 웹이 쓸 :root { --k: v } 문자열
```

### 반드시 지킬 2차 인코딩

팔레트는 `adjacent` 검증만 통과했고 `all-pairs` 는 실패한다
(research↔ops, research↔entertainment). **색만으로 정체를 전달하면 안 된다:**

1. 범례는 항상 띄운다
2. **3칸(30분) 이상 연속 블록에는 카테고리 이름을 직접 적는다**
3. 웹에서는 칸을 tap/hover 하면 카테고리명이 보인다

이건 취향이 아니라 접근성 요구사항이다. 빼면 색각 이상 사용자에게 두 카테고리가 같은 색이다.

---

## 3. P1 — 계획 계층

### 설정 추가 (`config.py`, 기존 필드는 건드리지 말 것)

```python
@dataclass(frozen=True)
class ReportConfig:
    ...기존 그대로...
    grid_start_hour: int      # 기본 6 — 플래너 격자 시작 시각

@dataclass(frozen=True)
class WebConfig:
    host: str                 # 기본 "127.0.0.1"
    port: int                 # 기본 8770
    base_url: str             # Slack 링크에 쓸 외부 주소. 기본 "" (미설정)
    read_only: bool           # 기본 False
```

`Config` 에 `web: WebConfig` 추가. 환경변수는 기존 규칙대로 `LT_WEB_PORT` 등.
`config/lifetrainer.example.toml` 은 **읽기 전용이니 건드리지 말고**, 기본값을 코드에 넣어라.

### `lifetrainer/plan/models.py`

```python
@dataclass(frozen=True)
class Plan:
    id: int; title: str; category: str | None
    start_min: int; end_min: int
    kind: str; weekdays: str; day: str | None
    active_from: str | None; active_to: str | None
    color: str | None; sort_order: int; enabled: bool

@dataclass
class PlanInstance:
    plan: Plan
    day: str
    start_slot: int          # 0..143
    end_slot: int            # exclusive
    checked: bool
    planned_sec: float
    actual_sec: float        # 그 구간에서 plan.category 로 계측된 초
    achievement: float       # 0.0~1.0
    dominant_actual: str | None   # 그 구간에서 실제로 가장 많았던 카테고리

def create_plan(conn, *, title: str, start_min: int, end_min: int,
                category: str | None = None, kind: str = "recurring",
                weekdays: str = "1234567", day: str | None = None,
                active_from: str | None = None, active_to: str | None = None,
                color: str | None = None, sort_order: int = 0) -> int: ...
def update_plan(conn, plan_id: int, **fields) -> None: ...
def delete_plan(conn, plan_id: int) -> None: ...
def get_plan(conn, plan_id: int) -> Plan | None: ...
def list_plans(conn, *, enabled_only: bool = True) -> list[Plan]: ...
def skip_plan(conn, plan_id: int, day: str) -> None: ...
def unskip_plan(conn, plan_id: int, day: str) -> None: ...
def set_check(conn, plan_id: int, day: str, checked: bool) -> None: ...
def parse_time_range(s: str) -> tuple[int, int]: ...   # '09:00-12:00' -> (540, 720)
def parse_weekdays(s: str) -> str: ...                 # 아래 참조
```

`parse_weekdays` 가 받아야 하는 형태 (전부 ISO 문자열 `'12345'` 로 정규화):
`'평일'`/`'weekday'` → `'12345'`, `'주말'`/`'weekend'` → `'67'`,
`'매일'`/`'daily'` → `'1234567'`, `'mon,wed,fri'` → `'135'`,
`'월수금'` → `'135'`, `'1-5'` → `'12345'`. 실패는 `ValueError`.

`end_min` 은 `1440` 까지 허용(자정). **자정을 넘기는 계획은 지원하지 않는다** —
넘기려 하면 `ValueError`. (플래너는 하루 단위다.)

### `lifetrainer/plan/achieve.py`

```python
def plans_for_day(conn, cfg, day: str) -> list[PlanInstance]: ...
def day_achievement(conn, cfg, day: str) -> tuple[float, int, int]: ...
    # (전체 달성률 0~1, 달성한 계획 수, 전체 계획 수)
```

`plans_for_day` 절차:

1. `kind='oneoff' AND day=?` 인 계획 + `kind='recurring'` 중
   요일이 맞고(`weekdays` 에 해당 ISO 요일 포함), `active_from/active_to` 범위 안이고,
   `plan_skip` 에 없는 것을 모은다.
2. 각 계획의 `[start_min, end_min)` 을 슬롯 범위로 바꾼다
   (`start_slot = start_min // slot_minutes`).
3. `actual_sec` = 그 슬롯 범위의 `slot_breakdown` 에서 `category = plan.category` 인 초의 합.
   `plan.category` 가 `None` 이면 **`off`/`away` 를 뺀 모든 활동 초의 합**.
4. `achievement = min(1.0, actual_sec / planned_sec)`. `planned_sec` 이 0이면 0.
5. `sort_order`, 그다음 `start_min` 순으로 정렬.

**전부 SQL 집계로.** 파이썬에서 슬롯을 순회해 더하지 마라 (설계 원칙 2번).

### `lifetrainer/plan/override.py`

```python
def set_override(conn, day: str, slot: int, category: str, *,
                 note: str | None = None, actor: str = "web") -> None: ...
def set_override_range(conn, day: str, start_slot: int, end_slot: int,
                       category: str, *, actor: str = "web") -> int: ...
def clear_override(conn, day: str, slot: int) -> None: ...
def clear_override_range(conn, day: str, start_slot: int, end_slot: int) -> int: ...
def list_overrides(conn, day: str) -> dict[int, dict]: ...
def apply_overrides(conn, day: str) -> int: ...   # slot 테이블에 반영, 건드린 행 수 반환
```

### `rollup.py` 수정 — 딱 한 군데만

`rollup_day` 의 **맨 마지막**에 `apply_overrides(conn, day)` 를 호출한다.

- `slot.category` 를 덮어쓰고 `source='override'`, `confidence=1.0` 으로 표시한다.
- **`slot_override` 테이블은 절대 지우지 않는다.** 사람이 넣은 입력이다.
- `slot_breakdown` 은 건드리지 않는다 (실측 원본은 보존).
  → 그래서 오버라이드는 격자 표시만 바꾸고 집계 숫자는 안 바꾼다. 이게 맞다.
- 기존 롤업 로직·테스트는 그대로 통과해야 한다.

### 테스트 (`tests/test_plan.py`, `tests/test_override.py`)

- `parse_time_range` / `parse_weekdays` 의 위 모든 형태
- 자정 넘기는 계획이 `ValueError`
- 반복 계획이 요일에 맞는 날에만 뜨고, `plan_skip` 한 날엔 안 뜨는지
- `active_from/active_to` 범위 밖이면 안 뜨는지
- oneoff 가 그 날짜에만 뜨는지
- `achievement` 계산: 계획 3시간 중 실제 카테고리가 1.5시간이면 0.5
- `category=None` 계획은 off/away 를 뺀 전체 활동으로 계산되는지
- 오버라이드가 `slot.category` 를 바꾸고 `source='override'` 가 되는지
- **롤업을 다시 돌려도 오버라이드가 살아남는지** (이게 핵심이다)
- 오버라이드가 `slot_breakdown` 을 바꾸지 않는지

---

## 4. P2 — 웹 플래너

Flask. **폰에서 쓰는 것이 주 용도다** — 모바일 우선으로 만들어라.

### 라우트

```
GET    /                      → 오늘로 리다이렉트
GET    /d/<day>               → 플래너 페이지 (HTML)
GET    /api/day/<day>         → {slots, plans, stats, palette}
POST   /api/plan              → 계획 생성
PATCH  /api/plan/<id>         → 수정
DELETE /api/plan/<id>         → 삭제
POST   /api/plan/<id>/check   → {day, checked} 체크 토글
POST   /api/plan/<id>/skip    → {day} 그날만 건너뛰기
POST   /api/slot              → {day, start_slot, end_slot, category} 수동 보정
DELETE /api/slot              → {day, start_slot, end_slot} 보정 해제
GET    /healthz               → {"ok": true}
```

### 화면

- **상단**: 날짜(앞/뒤 이동), 총 활동 시간, 달성률, 커버리지
- **좌측(모바일에선 상단)**: 계획 목록 — 체크박스, 제목, 시간, 달성률 막대
  - "+ 계획 추가" → 제목/카테고리/시작·종료/반복요일
- **우측(모바일에선 하단)**: 24행 × 6칸 격자
  - 행 라벨은 `cfg.report.grid_start_hour`(기본 6)부터 24시간
  - 칸 색 = 실제 카테고리. 계획 구간은 테두리/옅은 wash 로 겹쳐 표시
  - **드래그로 범위 선택 → 카테고리 지정** (수동 보정). 모바일은 탭-앤-탭
  - 3칸 이상 연속 블록에는 카테고리 이름을 직접 표시

### 제약

- **CDN 금지.** 외부 요청 없이 동작해야 한다 (오프라인·프라이버시).
  CSS/JS 전부 인라인 또는 `static/` 로컬 파일. 프레임워크 쓰지 말고 바닐라 JS.
- **다크/라이트 둘 다.** `prefers-color-scheme` + 수동 토글. 색은 `palette.yaml` 에서.
- 요청마다 DB 커넥션을 새로 연다 (sqlite3 스레드 제약).
- `cfg.web.read_only` 면 쓰기 라우트를 전부 405 로 막는다.
- **인증은 Tailscale 이 담당한다.** 앱에 로그인을 만들지 마라. 대신
  `cfg.web.host` 기본값을 `127.0.0.1` 로 두어 실수로 공개되지 않게 한다.
- 파괴적 동작(계획 삭제)은 되돌릴 수 있게 하거나 확인을 받아라 (apple-design §16 Agency).

### 테스트 (`tests/test_web.py`)

Flask `test_client()` 사용, 실제 서버 기동 금지.
라우트별 200/JSON 형태, 계획 CRUD 왕복, 오버라이드 왕복, `read_only` 일 때 405,
없는 날짜도 죽지 않고 빈 격자를 주는지.

---

## 5. P3 — 플래너 PNG

```python
def render_planner_day(conn, cfg, day: str, *, out_path: Path | None = None,
                       theme: str = "light") -> Path: ...
```

레퍼런스 플래너의 레이아웃을 따른다. 기존 `timeline.py` 는 **건드리지 마라**
(주간 뷰는 가로 띠가 여전히 맞다). 이건 하루용 새 렌더러다.

- 좌측: 계획 목록(체크박스 · 제목 · 시간 · 달성률), 하단에 미분류 상위 몇 개
- 우측: 24행 × 6칸 격자. 행 라벨은 `grid_start_hour` 부터. 정시마다 진한 가로선
- 상단: 날짜/요일, 총 활동, 달성률
- 계획 구간은 격자 위에 **테두리**로 겹친다 (채우지 않는다 — 실제와 구분돼야 한다)
- 3칸 이상 블록에 카테고리 이름 직접 표기 (§2 의 2차 인코딩 요구사항)
- 색은 전부 `palette.py` 경유. **하드코딩 금지**
- `matplotlib.use("Agg")`, 한글 폰트 폴백은 기존 `timeline.py` 방식을 따른다
- 세로로 긴 형태(레퍼런스가 그렇다). 모바일 Slack 에서 읽히게 폭 900~1100px

---

## 6. P4 — 배선

### CLI 추가 (기존 명령은 건드리지 말 것)

```
lt plan add "코딩" 09:00-12:00 --category coding --days 평일
lt plan add "병원" 14:00-15:00 --day 2026-08-20        # 일회성
lt plan list [--day D]
lt plan rm <id>
lt plan check <id> [--day D] [--off]
lt plan skip <id> --day D
lt slot set <day> <start_slot>-<end_slot> <category>   # 수동 보정
lt slot clear <day> <start_slot>-<end_slot>
lt planner --day D [--out P] [--theme light|dark]      # 플래너 PNG
lt web [--host H] [--port P]                           # 웹 서버
```

### Slack

`notify.py` 의 리포트 게시에 **"플래너 열기" 링크**를 붙인다.
`cfg.web.base_url` 이 비어 있으면 링크를 넣지 않는다 (깨진 링크 금지).
Block Kit `actions` 블록의 버튼 또는 context 의 링크로.

### systemd + Tailscale

- `systemd/lifetrainer-web.service` — 상시 구동, `MemoryMax=384M`, `Restart=always`
- `scripts/enable-tailscale-serve.sh` — `tailscale serve` 로 HTTPS 노출.
  **저장소 루트의 `openclaw-setup/enable-tailscale-serve.sh` 를 먼저 읽고 그 방식을 따라라**
  (이 기기에서 이미 검증된 방식이 있다). `sudo tailscale set --operator=$USER` 가
  선행 조건이면 스크립트가 검사하고 안내만 해라 — sudo 를 직접 실행하지 마라.
  스크립트는 마지막에 **`cfg.web.base_url` 에 넣을 주소를 출력**해야 한다.
