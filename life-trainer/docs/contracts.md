# Life Trainer — 모듈 계약서 (통합본)

> 이 문서는 **여러 에이전트가 동시에 코딩하기 위한 인터페이스 고정 문서**였다.
> 그 국면은 끝났고 지금은 **코드가 정본**이다. 이 문서는 *왜 이 시그니처인가* 를 남긴다.
>
> **2026-08-28: 세 문서를 하나로 합쳤다.** 예전에는 `contracts.md`(v1) →
> `contracts-planner.md`(2차) → `contracts-v2.md`(3차) 가 시대순으로 쌓여 있었고
> *"충돌하면 나중 것이 이긴다"* 였다 — **현재 계약을 알려면 셋을 읽고 직접 무효화
> 판정을 해야 했다.** 원본은 [`archive/`](archive/) 에 있다.

## ★ 합치면서 코드와 대조했다 (2026-08-28)

세 문서의 시그니처 주장 **189건**을 AST 로 실제 코드와 맞춰봤다.

| 결과 | 건수 |
|---|---|
| 코드에 그대로 있음 | **187** |
| 검증기 artifact | 2 — 아래 |

**틀린 계약은 하나도 없었다.** 걸린 둘은 검증기가 헤더를 잘못 물려받은 것이다:

- `migrate` — `### 마이그레이션 …` 헤더 아래에 있어 앞선 `config.py` 헤더를 물려받았다.
  본문은 *"`db.py` 에 마이그레이션 러너를 만든다"* 로 **정확히 적혀 있다** (실제 `db.py:349`)
- `rollup.py` — 헤더가 경로 없이 `### \`rollup.py\`` 라 파일을 못 찾았다.
  실제 대상은 `lifetrainer/rollup/rollup.py` 다

**충돌은 `timeutil.py` 의 함수 4개뿐이었다.** 나머지는 서로 다른 모듈을 다뤄서
겹치지 않는다 — 셋을 다 읽어야 했던 이유가 실제로는 이 4개였다.

| 무엇 | v1 | 최종 (= 코드) |
|---|---|---|
| `day_str` · `day_bounds` · `slot_index` · `slot_bounds` | 인자 없음 | **`*, boundary_hour: int = 6`** (V1 이 덮어씀) |
| `wallclock_min_to_slot` | 없음 | V1 이 추가 |

값이 드리프트한 상수 1건도 고쳤다 — `SCHEMA_VERSION` 은 계약서가 `1` 이라고 적어
뒀는데 실제는 **5** 다.

> 재검증: 이 문서의 시그니처 블록을 다시 코드와 맞추려면 `docs/archive/verify-contracts.py`.

---

# Part I — 기반 (v1)

## 0. 공통 규칙

| 항목 | 규칙 |
|---|---|
| Python | 3.10 (`from __future__ import annotations` 필수) |
| 가상환경 | `life-trainer/.venv` — `.venv/bin/python`, `.venv/bin/pytest` |
| 프로젝트 루트 | `life-trainer/` (`pyproject.toml` 이 있는 디렉터리) |
| 시각 표현 | **내부는 전부 `float` unix epoch (UTC 초)**. ISO 문자열은 입출력 경계에서만 |
| 날짜 문자열 | `'YYYY-MM-DD'` — 항상 **로컬 타임존**(설정값, 기본 Asia/Seoul) 기준 |
| 로깅 | `logging.getLogger(__name__)`. `print()` 금지 (CLI 출력 제외) |
| 타입 힌트 | 모든 public 함수에 필수 |
| 전역 상태 | 금지. `conn`, `cfg` 는 항상 인자로 받는다 |
| 주석/독스트링 | 한국어. 저장소 나머지 문서와 톤을 맞춘다 |
| 테스트 | `tests/test_<영역>.py`. **네트워크 호출 금지** — 전부 페이크/픽스처 |
| DB | 스키마는 `lifetrainer/schema.sql` 이 유일한 원본. 임의로 테이블을 추가하지 말 것 |

### 실패 처리 원칙

수집기·배치는 **개별 항목 실패로 전체가 죽으면 안 된다.**
항목 단위 예외는 잡아서 로그 + 카운터에 반영하고 계속 진행한다.
연결 실패처럼 전체가 무의미해지는 경우만 예외를 올린다.

### 하드웨어에서 온 제약 (반드시 지킬 것)

- **메모리 13.4GB 가 천장이다.** llama-server(8B @ **20480ctx**)가 6.7GB 를 쓴다
  (08-23 ctx 축소 전에는 40960ctx · 10.4GB 였다).
  파이썬 프로세스는 상시 300MB 를 넘기지 말 것.
  ~~임베딩·리랭커 상주 금지~~ → **임베딩은 상주로 바뀌었다** (`llama-embed`, CPU, 1.8GB).
  리랭커는 아직 안 붙인다 ([rag-plan §7](rag-plan.md)).
- **LLM 입력은 3~5K 토큰으로 끊는다.** 깊이 3K에서 10.9 tok/s, 32K에서 3.96 tok/s로 급락한다.
- **한국어는 같은 내용에 토큰이 55% 더 든다.** 프롬프트를 짧게 쓴다.
- **JSON 출력 시 thinking 은 반드시 끈다.** 사고 텍스트가 섞여 파싱이 깨진다.
- **JSON 스키마에 `pattern` 키워드 금지.** llama.cpp 의 GBNF 변환기가 정규식을 못 다뤄
  요청 전체를 400 으로 거부한다 (실제로 겪은 사고 — `operate/notes/agent-gateway.md §4-5`).

---

## 1. 파일 소유권 — 남의 파일을 건드리지 말 것

| 담당 | 소유 파일 |
|---|---|
| **F0 기반** | `lifetrainer/config.py`, `lifetrainer/timeutil.py`, `lifetrainer/db.py`, `tests/test_config.py`, `tests/test_timeutil.py`, `tests/test_db.py` |
| **A 수집** | `lifetrainer/collect/aw_client.py`, `collect/aw_sync.py`, `collect/synthetic.py`, `tests/test_aw_client.py`, `tests/test_aw_sync.py`, `tests/test_synthetic.py` |
| **B 분류·롤업** | `lifetrainer/rollup/classify.py`, `rollup/rollup.py`, `config/rules.yaml`, `tests/test_classify.py`, `tests/test_rollup.py` |
| **C 리포트** | `lifetrainer/report/timeline.py`, `report/stats.py`, `tests/test_stats.py`, `tests/test_timeline.py` |
| **D Slack** | `lifetrainer/slackio/notify.py`, `slackio/blocks.py`, `slackio/app.py`, `config/slack-app-manifest.json`, `tests/test_blocks.py`, `tests/test_notify.py` |
| **E 웹수집** | `lifetrainer/collect/http.py`, `collect/feeds.py`, `collect/arxiv.py`, `collect/dedupe.py`, `collect/score.py`, `config/sources.yaml`, `tests/test_http.py`, `tests/test_feeds.py`, `tests/test_dedupe.py` |
| **G LLM** | `lifetrainer/llm/client.py`, `llm/queue.py`, `llm/worker.py`, `llm/schemas.py`, `tests/test_llm_client.py`, `tests/test_queue.py` |
| **H 배선** | `lifetrainer/report/daily.py`, `lifetrainer/cli.py`, `systemd/*`, `scripts/*`, `life-trainer/README.md`, `tests/test_daily.py`, `tests/test_cli.py` |

> `report/daily.py` 는 stats(C) 와 blocks(D) 를 **엮는** 계층이라 둘이 다 끝난 뒤 H 가 만든다.
> C 는 숫자(`stats.py`)와 그림(`timeline.py`)까지만 책임진다.

공용 파일(`schema.sql`, `pyproject.toml`, `config/lifetrainer.example.toml`, 이 문서)은 **읽기 전용**이다.

---

## 2. F0 — 기반 계층 (다른 모든 것이 여기에 의존)

### `lifetrainer/config.py`

```python
@dataclass(frozen=True)
class AWConfig:
    base_url: str
    api_key: str               # aw-server v0.14.0b1+ 의 [auth] api_key. 없으면 빈 문자열
    timeout_sec: float
    poll_interval_sec: int
    overlap_sec: int
    backfill_days: int
    hosts: tuple[str, ...]

@dataclass(frozen=True)
class RollupConfig:
    slot_minutes: int          # 기본 10 → 하루 144 슬롯
    afk_category: str          # 'away'
    no_data_category: str      # 'off'
    min_active_ratio: float
    rules_path: Path

@dataclass(frozen=True)
class ReportConfig:
    png_dir: Path
    font_family: str
    daily_at: str              # 'HH:MM'
    weekly_at: str
    morning_at: str

@dataclass(frozen=True)
class SlackConfig:
    mode: str                  # 'notify' | 'bolt'
    bot_token: str             # 비어 있으면 openclaw_config 에서 로드된 값이 채워진다
    app_token: str
    default_channel: str
    openclaw_config: Path

@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    timeout_sec: float
    max_input_tokens: int
    enable_thinking: bool

@dataclass(frozen=True)
class CollectConfig:
    user_agent: str
    per_domain_min_interval_sec: float
    per_domain_concurrency: int
    arxiv_min_interval_sec: float
    max_fail_before_disable: int
    sources_path: Path
    respect_robots: bool

@dataclass(frozen=True)
class Config:
    root: Path                 # 프로젝트 루트 (절대 경로)
    timezone: str
    data_dir: Path             # 절대 경로로 정규화됨
    db_path: Path              # 절대 경로
    log_level: str
    aw: AWConfig
    rollup: RollupConfig
    report: ReportConfig
    slack: SlackConfig
    llm: LLMConfig
    collect: CollectConfig

    @property
    def tz(self) -> ZoneInfo: ...
    @property
    def slots_per_day(self) -> int: ...     # 1440 // slot_minutes

def load_config(path: str | Path | None = None) -> Config: ...
def setup_logging(cfg: Config) -> None: ...
```

요구사항:

1. 로드 순서: 내장 기본값 → `config/lifetrainer.toml`(있으면) → 환경변수.
   `path` 인자 > `$LT_CONFIG` > `<root>/config/lifetrainer.toml`.
2. 환경변수는 `LT_<SECTION>_<KEY>` 대문자. 예 `LT_ACTIVITYWATCH_BASE_URL`,
   `LT_GENERAL_DB_PATH`, `LT_SLACK_BOT_TOKEN`. 값은 대상 필드 타입으로 캐스팅.
3. TOML 파싱: `tomllib`(3.11+) 을 먼저 시도하고 없으면 `tomli` 를 쓴다.
   **`tomli` 는 이미 `.venv` 에 설치되어 있다.** 둘 다 없으면 설치 안내가 담긴
   `RuntimeError`. `pyproject.toml` 은 건드리지 말 것 (공용 파일).

   ```python
   try:
       import tomllib
   except ModuleNotFoundError:
       import tomli as tomllib
   ```
4. 상대 경로(`data_dir`, `db_path`, `png_dir`, `rules_path`, `sources_path`)는
   `root` 기준으로 절대화한다. `root` 는 `config.py` 파일 위치에서 위로 올라가며
   `pyproject.toml` 을 찾아 결정한다.
5. **Slack 토큰 폴백**: `slack.bot_token` 이 비어 있고 `openclaw_config` 파일이 존재하면
   그 JSON 의 `channels.slack.botToken` 을 읽어 채운다. `app_token` 은 `channels.slack.appToken`.
   파일이 없거나 키가 없으면 조용히 빈 문자열로 둔다 (예외 금지).
6. `setup_logging` 은 stderr 핸들러 + `%(asctime)s %(levelname)s %(name)s: %(message)s`.
   중복 호출해도 핸들러가 쌓이지 않아야 한다.

### `lifetrainer/timeutil.py`

```python
def now_ts() -> float: ...
def to_ts(dt: datetime) -> float: ...                     # naive 는 UTC 로 간주
def from_ts(ts: float, tz: tzinfo | None = None) -> datetime: ...
def parse_iso(s: str) -> float: ...                       # 'Z', '+09:00', 마이크로초 모두 처리
def iso_utc(ts: float) -> str: ...                        # '2026-08-16T01:02:03.000000+00:00'
# ★ 아래 넷은 V1(06:00 하루 경계)이 덮어썼다. 코드가 이 모양이다 (2026-08-28 검증).
def day_str(ts: float, tz: tzinfo, *, boundary_hour: int = 6) -> str: ...
def day_bounds(day: str, tz: tzinfo, *, boundary_hour: int = 6) -> tuple[float, float]: ...
def slot_index(ts: float, tz: tzinfo, slot_minutes: int = 10, *, boundary_hour: int = 6) -> int: ...
def slot_bounds(day: str, slot: int, tz: tzinfo, slot_minutes: int = 10, *, boundary_hour: int = 6) -> tuple[float, float]: ...
def wallclock_min_to_slot(minute: int, slot_minutes: int = 10, *, boundary_hour: int = 6) -> int: ...
def slots_per_day(slot_minutes: int = 10) -> int: ...
def overlap_sec(a0: float, a1: float, b0: float, b1: float) -> float: ...  # 겹치는 초, 음수 없음
def day_range(start_day: str, end_day: str) -> list[str]: ...     # 양 끝 포함
def parse_duration(s: str) -> float: ...   # '60m', '1h30m', '90', '1.5h' -> 초. 실패 시 ValueError
```

`day_bounds` 는 zoneinfo 를 써서 계산한다 (`datetime.combine(date, time.min, tzinfo=tz)`).
DST 가 있는 타임존에서도 24시간을 가정하지 말 것 — 다음 날 자정을 다시 계산한다.

### `lifetrainer/db.py`

```python
SCHEMA_VERSION: int = 5      # ★ 정정: 계약서는 1 로 적혀 있었다 (2026-08-28 검증)

def connect(db_path: str | Path, *, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection: ...
def init_db(conn: sqlite3.Connection) -> None: ...
def schema_version(conn: sqlite3.Connection) -> int: ...
@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]: ...
def get_state(conn, key: str, default: str | None = None) -> str | None: ...
def set_state(conn, key: str, value: str) -> None: ...
def get_state_float(conn, key: str, default: float | None = None) -> float | None: ...
def set_state_float(conn, key: str, value: float) -> None: ...
def backup(conn: sqlite3.Connection, dest: str | Path) -> Path: ...
def open_db(cfg) -> sqlite3.Connection: ...   # connect + init_db (멱등)
```

요구사항:

1. `connect` 가 거는 PRAGMA: `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`,
   `busy_timeout=30000`, `temp_store=MEMORY`. `row_factory = sqlite3.Row`.
   `readonly=True` 면 `file:...?mode=ro` URI 로 연다.
2. `init_db` 는 `schema.sql` 을 `executescript` 로 적용하고 `meta.schema_version` 을 기록한다.
   **멱등**해야 한다 (여러 번 호출해도 안전).
   ★ **빈 파일이었으면 마이그레이션 표식을 찍고 끝낸다**(baseline, 2026-09-01).
   `schema.sql` 은 언제나 *모든 마이그레이션이 끝난 뒤의 모양*이라 새 DB 는 이미
   최신이고, 002·003 은 파생 테이블을 **비우므로** 돌리면 방금 넣은 데이터가 사라진다.
3. `open_db` 가 `migrate` 를 **부른다.** 그전에는 `migrate()` 에 프로덕션 호출자가
   하나도 없어서 라이브 DB 가 한 달간 옛 모양이었다
   ([HISTORY](../HISTORY/2026-09-01-the-migration-runner-nobody-called.md)).
   마이그레이션 이름 목록은 `_MIGRATIONS` **한 곳**이다.
4. `transaction` 은 `BEGIN IMMEDIATE` 로 시작한다 (WAL 에서 writer 경합 시 즉시 실패하게).
   예외 시 롤백 후 재raise.
5. `backup` 은 sqlite3 온라인 백업 API(`conn.backup`)를 쓴다. 파일 복사 금지.
6. 부모 디렉터리가 없으면 만든다.

---

## 3. A — 활동 수집

### `lifetrainer/collect/aw_client.py`

```python
@dataclass(frozen=True)
class AWEvent:
    bucket_id: str
    ts: float                  # epoch UTC
    duration: float            # 초
    data: dict
    event_id: int | None = None
    @property
    def ts_end(self) -> float: ...

class AWClient:
    def __init__(self, base_url: str, *, api_key: str = "", timeout: float = 10.0,
                 user_agent: str = "LifeTrainer/0.1",
                 session: "requests.Session | None" = None) -> None: ...
    def ping(self) -> bool: ...                  # 예외를 삼키고 bool 반환
    def info(self) -> dict: ...
    def buckets(self) -> dict[str, dict]: ...
    def events(self, bucket_id: str, *, start: float | None = None,
               end: float | None = None, limit: int = -1) -> list[AWEvent]: ...
```

**`aw-client` 패키지를 쓰지 않는다.** `requests` 로 직접 친다
(이유는 `docs/research/activitywatch.md §6` — 0.5.15 는 Bearer 인증을 보낼 코드 자체가 없다).

- 엔드포인트·스키마는 `docs/research/activitywatch.md` **§2 를 그대로** 따른다.
  `GET /api/0/buckets/`, `GET /api/0/buckets/<id>/events?start=&end=&limit=`.
- `start`/`end` 는 **RFC3339 문자열**로 보낸다 (`iso_utc`). epoch 를 그대로 주면 400.
- `api_key` 가 비어 있지 않으면 모든 요청에 `Authorization: Bearer <key>` 를 붙인다.
  단 `info()`/`ping()` 은 인증 면제 엔드포인트라 없어도 통한다.
- 타임스탬프는 `parse_iso` 로 epoch 변환. 반환 리스트는 **ts 오름차순** 정렬.
- HTTP 오류는 `AWError(Exception)` 으로 감싼다. `session` 인자는 테스트 주입용이다.

### `lifetrainer/collect/aw_sync.py`

```python
@dataclass
class SyncResult:
    buckets_seen: int
    events_upserted: int
    window_start: float
    window_end: float
    errors: list[str]

def bucket_type(bucket_id: str, meta: dict) -> str: ...   # 'window'|'afk'|'web'|'unknown'
def upsert_bucket(conn, bucket_id: str, meta: dict) -> None: ...
def upsert_events(conn, events: Iterable[AWEvent]) -> int: ...
def sync(conn, client: AWClient, cfg: Config, *, now: float | None = None) -> SyncResult: ...
```

동기화 전략 (**이중 계산 방지가 핵심**):

1. 버킷마다 커서 키 `aw_cursor:<bucket_id>` (epoch, `sync_state`).
   커서에 저장하는 값은 **마지막으로 본 이벤트의 `ts`(시작 시각)** 다. `ts_end` 가 아니다.
2. 조회 구간 = `[max(cursor - overlap_sec, now - backfill_days*86400), now]`.
   커서가 없으면 `now - backfill_days*86400` 부터.
3. `aw_event` upsert 키는 `(bucket_id, ts)`. `duration`/`ts_end`/`data_json`/`synced_at` 를 갱신.
   → 버킷 마지막 이벤트의 duration 이 자라도 정확히 반영되고 중복 행이 생기지 않는다.
4. 커서는 **`now` 도 `ts_end` 도 아닌, 실제로 본 최대 `ts`** 로 갱신한다.

> **왜 이래야 하는가** (`docs/research/activitywatch.md §3`):
> 버킷의 마지막 이벤트는 진행 중인 하트비트다. 병합될 때 `id` 와 `timestamp` 는 고정이고
> `duration` 만 자란다. 끝 시각 기준으로 페이징하면 진행 중이던 활동이 다음 폴링에서
> **별개의 새 이벤트처럼** 잡혀 하나의 활동이 조각나고 이중 계산된다.
> 시작 시각 기준 + 겹침 재조회 + `(bucket_id, ts)` upsert 가 이 문제를 완전히 없앤다.
5. `data` 에서 `app`/`title`/`url`/`status` 를 꺼내 전용 컬럼에도 넣는다 (원본은 `data_json` 유지).
6. 버킷 하나가 실패해도 나머지는 계속 진행하고 `errors` 에 문자열로 모은다.

### `lifetrainer/collect/synthetic.py`

```python
def generate(conn, cfg: Config, *, days: int = 7, end_day: str | None = None,
             seed: int = 42, host: str = "synth-pc") -> int: ...
```

ActivityWatch 없이 파이프라인 전체를 돌리기 위한 **현실적인** 가짜 데이터.
`aw_bucket` / `aw_event` 에 직접 쓴다 (HTTP 경유 안 함). 반환값은 생성한 이벤트 수.

만족해야 할 성질:

- window / afk / web 세 버킷을 만든다 (`aw-watcher-window_<host>`, `aw-watcher-afk_<host>`, `aw-watcher-web-chrome_<host>`).
- 하루 안에 수면 구간(이벤트 없음), afk 구간, 집중 코딩 블록, 브라우징, 회의를 섞는다.
- 같은 시드로는 같은 결과 (재현 가능).
- 이벤트는 버킷 안에서 겹치지 않고 ts 오름차순.
- 앱/제목은 실제로 흔한 것 (`Code.exe` / `chrome.exe` / `WindowsTerminal.exe` / `Slack.exe` 등)
  으로 만들어 규칙 분류기가 실제로 매칭되게 한다.

---

## 4. B — 분류 · 롤업

### `config/rules.yaml`

```yaml
version: 1
default_category: unknown
categories:
  - {id: coding,        label: 코딩,    color: "#3b82f6"}
  - {id: research,      label: 리서치,  color: "#8b5cf6"}
  - {id: writing,       label: 문서,    color: "#06b6d4"}
  - {id: communication, label: 소통,    color: "#f59e0b"}
  - {id: ops,           label: 운영,    color: "#10b981"}
  - {id: learning,      label: 학습,    color: "#ec4899"}
  - {id: browsing,      label: 웹,      color: "#94a3b8"}
  - {id: entertainment, label: 여가,    color: "#ef4444"}
  - {id: away,          label: 자리비움, color: "#475569"}
  - {id: off,           label: 꺼짐,    color: "#1e293b"}
  - {id: unknown,       label: 미분류,  color: "#64748b"}
rules:
  - {category: coding, subcategory: editor, match: {app: "(?i)^(code|code\\.exe|cursor|nvim|vim|pycharm.*|idea.*)$"}}
  - {category: research, match: {url: "(?i)(arxiv\\.org|semanticscholar|paperswithcode)"}}
  # ...
```

- 규칙은 **리스트 순서대로 평가, 첫 매칭 승리.**
- `match` 안의 여러 필드는 **AND**. 필드는 `app`, `title`, `url` 셋뿐.
- 정규식은 파이썬 `re` 문법. 대소문자 무시가 필요하면 `(?i)` 를 직접 붙인다.
- `away` / `off` / `unknown` 은 예약 카테고리다. 규칙에서 직접 지정하지 않는다.
- 실제 규칙은 Windows/Linux 양쪽에서 흔한 앱을 40개 이상 커버할 것.

### `lifetrainer/rollup/classify.py`

```python
@dataclass(frozen=True)
class Category:
    id: str
    label: str
    color: str

@dataclass(frozen=True)
class Rule:
    index: int
    category: str
    subcategory: str | None
    app: re.Pattern | None
    title: re.Pattern | None
    url: re.Pattern | None
    def matches(self, app: str | None, title: str | None, url: str | None) -> bool: ...

@dataclass(frozen=True)
class Classification:
    category: str
    subcategory: str | None
    rule_index: int | None       # None 이면 기본값으로 떨어진 것
    source: str                  # 'rule' | 'default'

class Classifier:
    categories: dict[str, Category]
    default_category: str
    @classmethod
    def from_yaml(cls, path: str | Path) -> "Classifier": ...
    def classify(self, app: str | None = None, title: str | None = None,
                 url: str | None = None) -> Classification: ...
    def color(self, category_id: str) -> str: ...
    def label(self, category_id: str) -> str: ...
    def order(self) -> list[str]: ...      # 시각화용 안정 순서

def normalize_fingerprint(app: str | None, title: str | None) -> str: ...
```

`normalize_fingerprint`: 소문자화, 공백 정규화, 제목에서 숫자·경로·GUID 같은 변동부를
치환해 같은 종류의 창이 하나의 지문으로 모이게 한다 (예: `— 3 of 12` → `— N of N`).
`\x1f` 로 app 과 title 을 잇는다.

### `lifetrainer/rollup/rollup.py`

```python
@dataclass
class RollupResult:
    day: str
    slots_written: int
    breakdown_rows: int
    unclassified_seen: int
    active_sec: float
    coverage: float            # (144 - off슬롯) / 144

def rollup_day(conn, cfg: Config, classifier: Classifier, day: str,
               *, now: float | None = None) -> RollupResult: ...
def rollup_range(conn, cfg, classifier, start_day: str, end_day: str) -> list[RollupResult]: ...
```

슬롯 하나(기본 600초)를 계산하는 절차 — **이 순서를 지킬 것**:

1. `[s, e)` 구간과 겹치는 afk 버킷 이벤트에서
   `active_sec`(status=`not-afk`), `afk_sec`(status=`afk`) 를 겹친 초만큼 누적.
   `gap_sec = max(0, (e-s) - active_sec - afk_sec)` — PC 가 꺼져 있던 시간.
2. window 이벤트를 겹친 초만큼 순회. 같은 시각의 web 이벤트가 있으면 `url` 을 붙인다
   (브라우저 앱일 때만). `classifier.classify(app, title, url)` 로 카테고리 결정.
3. **활동 초는 not-afk 구간으로 클리핑한다.** afk 중에 켜져 있던 창은 활동이 아니다.
   클리핑되어 떨어져 나간 시간은 `away` 카테고리로 간다.
4. `(category, app)` 별 초를 `slot_breakdown` 에 upsert.
5. 수동 입력(`manual_entry`, `revoked=0`)이 겹치면 그 카테고리에 겹친 초를 더하고
   **가중치 2배**를 준다 (사람이 직접 말한 것이 우선).
6. 승자 = 초가 가장 큰 카테고리. 동점이면 `classifier.order()` 순.
7. `active_sec / (e-s) < min_active_ratio` 이고 수동 입력도 없으면
   카테고리 = `off`, `source='none'`.
8. 기본값으로 떨어진 (app, title) 은 `unclassified` 에 지문 단위로 누적.
9. `slot` / `slot_breakdown` 은 해당 날짜 것을 **지우고 다시 쓴다** (멱등).

전부 하나의 트랜잭션 안에서. `rollup_day` 를 두 번 돌려도 결과가 같아야 한다.

---

## 5. C — 리포트

### `lifetrainer/report/stats.py`

```python
@dataclass
class CategorySec:
    category: str
    seconds: float
    share: float               # 0~1, active 총합 대비

@dataclass
class DailyStats:
    day: str
    total_span_sec: float          # 관측된 첫 활동 ~ 마지막 활동
    active_sec: float
    afk_sec: float
    off_sec: float
    coverage: float
    by_category: list[CategorySec] # 내림차순, away/off 제외
    top_apps: list[tuple[str, float]]
    first_activity_ts: float | None
    last_activity_ts: float | None
    longest_focus: tuple[str, int, int] | None   # (category, 시작슬롯, 슬롯길이)
    slot_categories: list[str]     # 길이 144, 시각화용

@dataclass
class WeeklyStats:
    end_day: str
    days: list[str]
    this_week: list[CategorySec]
    last_week: list[CategorySec]
    delta: dict[str, float]        # category -> 초 증감
    daily_active: list[tuple[str, float]]
    best_day: tuple[str, float] | None

def compute_daily(conn, cfg: Config, day: str) -> DailyStats: ...
def compute_weekly(conn, cfg: Config, end_day: str) -> WeeklyStats: ...
def format_hm(seconds: float) -> str: ...          # 4230 -> '1시간 10분'
def render_daily_text(stats: DailyStats, classifier: Classifier) -> str: ...
def render_weekly_text(stats: WeeklyStats, classifier: Classifier) -> str: ...
```

**모든 숫자는 SQL 집계에서 나온다.** 파이썬에서 이벤트를 다시 순회해 합치지 말 것.
`slot_breakdown` 이 집계의 원천이다. LLM 은 이 단계에 없다.

### `lifetrainer/report/timeline.py`

```python
def render_day(conn, cfg: Config, classifier: Classifier, day: str,
               *, out_path: Path | None = None) -> Path: ...
def render_week(conn, cfg: Config, classifier: Classifier, end_day: str,
                *, out_path: Path | None = None) -> Path: ...
```

- matplotlib **Agg** 백엔드 강제 (`matplotlib.use("Agg")`) — 헤드리스 서버다.
- 한글 폰트: `cfg.report.font_family` 를 우선 시도, 실패 시
  `NanumGothic` → `Noto Sans CJK KR` → `DejaVu Sans` 순 폴백. 폰트 경고를 로그로 남긴다.
- 하루 = 144칸 가로 띠 1줄. 3시간 간격 눈금 + 카테고리 범례.
- 주간 = 7줄(월~일 또는 end_day 기준 7일). 지난주 비교가 눈으로 되게.
- 모바일 Slack 에서 읽히는 크기 (가로 1200px 내외, `dpi=150`).
- 출력 경로 기본값: `cfg.report.png_dir / f"{day}-timeline.png"`. 디렉터리는 만든다.
- 파일을 실제로 만들고 `Path` 를 반환. 크기가 0 이면 안 된다.

### `lifetrainer/report/daily.py` — **담당은 H** (C 아님)

```python
@dataclass
class BuiltReport:
    kind: str
    day: str
    text: str
    blocks: list[dict]
    png_path: Path | None
    report_id: int

def build_daily(conn, cfg, classifier, day: str) -> BuiltReport: ...
def build_weekly(conn, cfg, classifier, end_day: str) -> BuiltReport: ...
```

`report` 테이블에 `(kind, day)` 로 upsert 하고 `report_id` 를 채워 반환한다.
Block Kit 구성은 `slackio.blocks` 의 빌더를 호출해서 만든다 (D 담당과의 접점).

---

## 6. D — Slack

### `lifetrainer/slackio/blocks.py`

```python
def header(text: str) -> dict: ...
def section(text: str) -> dict: ...
def fields_section(pairs: list[tuple[str, str]]) -> dict: ...   # 최대 10개, 2열
def context(texts: list[str]) -> dict: ...
def divider() -> dict: ...
def daily_report_blocks(stats: "DailyStats", classifier: "Classifier") -> list[dict]: ...
def weekly_report_blocks(stats: "WeeklyStats", classifier: "Classifier") -> list[dict]: ...
def error_blocks(title: str, detail: str) -> list[dict]: ...
```

- `stats`/`classifier` 는 순환 import 를 피하려고 `TYPE_CHECKING` 으로만 import 한다.
- 블록 개수 50 개, 텍스트 3000자 제한을 넘기지 않도록 자른다.
- 텍스트는 `mrkdwn`. 이모지 남발 금지 — 카테고리당 1개까지.

### `lifetrainer/slackio/notify.py`

```python
class SlackError(Exception): ...

class SlackNotifier:
    def __init__(self, cfg: Config) -> None: ...
    @property
    def enabled(self) -> bool: ...                 # 토큰이 있으면 True
    def resolve_channel(self, channel: str | None = None) -> str: ...
        # 'U…' 면 conversations.open 으로 DM 채널 ID 로 바꾼다. 결과를 캐시.
    def post(self, text: str, *, blocks: list[dict] | None = None,
             channel: str | None = None, thread_ts: str | None = None) -> str: ...
    def upload_png(self, path: str | Path, *, title: str,
                   initial_comment: str | None = None,
                   channel: str | None = None,
                   thread_ts: str | None = None) -> dict: ...
    def post_report(self, conn, report: "BuiltReport", *, channel: str | None = None) -> str: ...
        # PNG 업로드(files_upload_v2) → 반환된 file id 를 image 블록의 slack_file 에 끼워
        # chat.postMessage 한 번으로 카드 완성 → report 테이블의 posted_at/slack_ts 갱신
    def alert(self, conn, key: str, title: str, detail: str, *, cooldown_sec: float = 3600) -> bool: ...
        # alert_state 로 중복 억제. 실제로 보냈으면 True
```

- 토큰이 없으면 `enabled=False` 이고 모든 발송은 **경고 로그 후 조용히 no-op**.
  (설치 직후 토큰 없이 CLI 를 돌려도 파이프라인이 죽으면 안 된다.)
- 레이트리밋(429)은 `Retry-After` 를 보고 최대 3회 재시도.
- 파일 업로드는 **`files_upload_v2` 만** 쓴다. v1 `files.upload` 는 2025-11-12 완전 차단됐다.
- **이미지 블록은 `slack_file: {"id": "F…"}`** 로 업로드한 파일을 직접 참조한다.
  공개 URL 이 필요 없다. `image_url` 과 동시에 지정하면 안 된다.
  (`docs/measure/findings/slack.md §5·§6`)
- 봇이 대상 채널의 멤버가 아니면 업로드 공유가 실패한다 (`not_in_channel`).
  실패 시 명확한 메시지로 로그를 남길 것.

### `lifetrainer/slackio/app.py`

Socket Mode 앱. `cfg.slack.mode == 'bolt'` 이고 두 토큰이 다 있을 때만 동작.

```python
def build_app(cfg: Config) -> "App": ...
def run(cfg: Config) -> None: ...        # SocketModeHandler(...).start()
```

명령:

| 명령 | 동작 |
|---|---|
| `/lt ping` | `pong` + 버전·DB 경로 |
| `/lt today` | 오늘 롤업 요약 + 타임라인 PNG |
| `/lt yesterday` | 어제 리포트 |
| `/lt week` | 주간 비교 |
| `/lt status` | 마지막 동기화 시각, 이벤트 수, 큐 상태 |
| `/log <카테고리> <기간> [메모]` | `manual_entry` 삽입. 예 `/log 운동 60m 헬스장` |

- **3초 안에 `ack()`** 하고 무거운 작업은 그 뒤에 한다.
- 핸들러 안에서 DB 는 매번 새로 연다 (스레드 안전).
- `/log` 의 기간 파싱은 `timeutil.parse_duration`. 기본 종료 시각은 "지금", 시작은 "지금 - 기간".
  `at HH:MM` 접미사가 있으면 그 시각 기준으로.

### `config/slack-app-manifest.json`

Life Trainer 전용 앱 매니페스트. 조사 문서의 현행 스키마를 따르고,
스코프는 필요한 최소(`chat:write`, `commands`, `files:write`, `im:history`, `im:write`, `users:read`)만.

---

## 7. E — 웹 수집 (Phase 2)

### `lifetrainer/collect/http.py`

```python
class PoliteSession:
    """도메인별 토큰 버킷 + 조건부 GET + robots.txt 를 강제하는 HTTP 계층.

    수집기는 requests 를 직접 쓰지 않는다. 전부 이 클래스를 통과한다."""
    def __init__(self, cfg: Config, conn) -> None: ...
    def get(self, url: str, *, headers: dict | None = None,
            timeout: float | None = None, use_state: bool = True) -> "FetchResult": ...
    def allowed(self, url: str) -> bool: ...      # robots.txt (캐시: robots_cache)

@dataclass
class FetchResult:
    url: str
    status: int                 # 304 면 not_modified
    body: bytes | None
    text: str | None
    headers: dict
    from_cache: bool            # 304 로 본문 없이 끝난 경우 True
    elapsed_ms: int
    error: str | None = None
```

규칙 (설계서 §6):

1. 도메인당 최소 간격 `per_domain_min_interval_sec`(기본 2초), 동시 연결 1.
   arXiv 도메인은 `arxiv_min_interval_sec`(3초).
2. `url_state` 의 `etag`/`last_modified` 를 `If-None-Match`/`If-Modified-Since` 로 보낸다.
   304 면 본문 없이 종료하고 `unchanged_streak += 1`.
3. 적응형 주기: 3회 연속 무변경이면 `interval_sec *= 2`(상한 7일),
   변경 감지되면 `interval_sec //= 2`(하한 10분).
4. 실패는 지수 백오프 (`fail_count` 기반, 상한 24시간). `max_fail_before_disable` 초과 시
   `next_fetch_at` 을 아주 멀리 밀어 사실상 비활성화.
5. User-Agent 는 `cfg.collect.user_agent` 고정. `respect_robots=True` 면 차단 시 요청하지 않는다.
6. **테스트에서 실제 네트워크를 타면 안 된다** — `requests.Session` 을 주입 가능하게 만들 것.

### `lifetrainer/collect/dedupe.py`

```python
def sha256_hex(data: bytes | str) -> str: ...
def simhash64(text: str) -> int: ...                  # 64비트 SimHash
def hamming(a: int, b: int) -> int: ...
def is_near_duplicate(conn, simhash: int, *, threshold: int = 3,
                      within_days: int = 14) -> int | None: ...   # 중복이면 doc.id
```

### `lifetrainer/collect/feeds.py`

```python
@dataclass
class FeedResult:
    source_id: int
    name: str
    status: int
    new_docs: int
    dup_docs: int
    error: str | None = None

def load_sources(conn, cfg: Config) -> int: ...       # config/sources.yaml -> source 테이블 upsert
def due_sources(conn, *, now: float | None = None, limit: int = 100) -> list[sqlite3.Row]: ...
def fetch_source(conn, cfg, session: PoliteSession, row: sqlite3.Row) -> FeedResult: ...
def run_once(conn, cfg, *, now: float | None = None) -> list[FeedResult]: ...
```

- RSS/Atom 파싱은 `feedparser`.
- 문서는 `doc` 에 `url` UNIQUE 로 넣는다. 이미 있으면 갱신하지 않고 dup 로 센다.
- `content_hash` = 정규화된 `title + abstract` 의 sha256. `simhash` 도 같이 계산.
- 근사 중복이면 `dup_of` 를 채워 넣되 행은 남긴다 (뉴스 신디케이션 추적용).

### `lifetrainer/collect/arxiv.py`

```python
def search(cfg, session: PoliteSession, query: str, *, max_results: int = 50,
           start: int = 0) -> list[dict]: ...
def collect(conn, cfg, session, queries: list[str]) -> int: ...
```

arXiv API(`http://export.arxiv.org/api/query`) 사용. 요청 간 3초 이상. 페이지당 최대 100.

### `lifetrainer/collect/score.py`

```python
def load_interests(conn, cfg) -> dict[str, float]: ...
def score_doc(title: str, abstract: str | None, interests: dict[str, float]) -> float: ...
def score_pending(conn, cfg, *, limit: int = 500) -> int: ...
def top_docs(conn, *, day: str | None = None, limit: int = 3,
             min_score: float = 0.0) -> list[sqlite3.Row]: ...
```

Phase 2 의 스코어링은 **키워드 랭킹만**. LLM 없음. 관심사 항목은 `config/sources.yaml` 의
`interests:` 섹션에서 `interest` 테이블로 로드한다.

### `config/sources.yaml`

```yaml
version: 1
sources:
  - {kind: rss,   name: "…", url: "…", tags: "ai,llm", interval_sec: 3600}
  - {kind: arxiv, name: "arXiv cs.CL", url: "cat:cs.CL", tags: "paper", interval_sec: 21600}
interests:
  - {term: "on-device", weight: 3.0}
  - {term: "jetson",    weight: 3.0}
```

실제로 쓸만한 소스 12~20개를 채울 것 (LLM/온디바이스/로보틱스/한국 기술 뉴스 등).
**HTML 스크레이핑 대상은 넣지 않는다.** RSS·API 가 있는 곳만.

---

## 8. G — LLM · GPU 큐 (Phase 3 골격)

### `lifetrainer/llm/client.py`

```python
class LLMError(Exception): ...
class LLMUnavailable(LLMError): ...

@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    raw: dict

class LLMClient:
    def __init__(self, cfg: Config, *, conn=None) -> None: ...
    def health(self) -> bool: ...
    def model_id(self) -> str | None: ...          # /v1/models 의 실제 id
    def chat(self, messages: list[dict], *, max_tokens: int = 512,
             temperature: float = 0.7, json_schema: dict | None = None,
             purpose: str = "", stop: list[str] | None = None) -> LLMResponse: ...
    def complete_json(self, system: str, user: str, schema: dict, *,
                      purpose: str, max_tokens: int = 512) -> dict: ...
```

**반드시 지킬 것**:

1. `enable_thinking` 은 요청 본문의 `chat_template_kwargs` 로 넘긴다
   (`{"chat_template_kwargs": {"enable_thinking": false}}`). JSON 요청에서는 무조건 false.
2. `json_schema` 는 `response_format={"type":"json_schema","json_schema":{"name":…,"schema":…,"strict":true}}` 로.
3. **스키마 검증기**: 스키마 트리에 `pattern` 키가 있으면 `ValueError` 를 즉시 올린다.
   llama.cpp 가 요청 전체를 400 으로 거부하는 실제 사고 사례가 있다.
4. `conn` 이 주어지면 모든 호출을 `llm_call` 에 계측 기록 (실패도 기록).
5. 서버가 죽어 있으면 `LLMUnavailable`. 호출부가 이걸 잡고 우아하게 건너뛸 수 있어야 한다.
6. 응답 텍스트에서 `<think>…</think>` 가 섞여 나오면 제거한 뒤 파싱한다 (방어).

### `lifetrainer/llm/schemas.py`

Pydantic 모델 + `model_json_schema()` 결과에서 `pattern` 을 제거하는 헬퍼.
최소한 다음 스키마를 제공:

```python
class ActivityTag(BaseModel):     # unclassified 태깅
    category: str
    subcategory: str | None
    confidence: float
class DocSummary(BaseModel):      # 문서 3~4줄 요약
    summary: str
    tags: list[str]
    relevance: float
def json_schema_of(model: type[BaseModel], name: str) -> dict: ...   # pattern 제거 포함
```

### `lifetrainer/llm/queue.py`

```python
@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int
    priority: int

def enqueue(conn, kind: str, payload: dict, *, priority: int = 100,
            dedupe_key: str | None = None, not_before: float = 0.0,
            max_attempts: int = 3) -> int | None: ...    # 중복이면 None
def claim(conn, *, worker: str, kinds: list[str] | None = None,
          lease_sec: float = 1800, now: float | None = None) -> Job | None: ...
def complete(conn, job_id: int, result: dict | None = None) -> None: ...
def fail(conn, job_id: int, error: str, *, retry_in: float | None = None) -> None: ...
def reap_expired(conn, *, now: float | None = None) -> int: ...
def stats(conn) -> dict[str, int]: ...
def purge_done(conn, *, older_than_days: int = 14) -> int: ...
```

`claim` 은 `BEGIN IMMEDIATE` 안에서 하나만 집어 `state='running'`, `lease_until=now+lease_sec` 로
바꾼다. 재시도는 지수 백오프(`not_before = now + 60 * 2**attempts`).

### `lifetrainer/llm/worker.py`

```python
HANDLERS: dict[str, Callable[[sqlite3.Connection, Config, LLMClient, Job], dict]]

def run_once(conn, cfg, *, worker: str | None = None) -> int: ...   # 처리한 잡 수
def run_forever(conn, cfg, *, poll_sec: float = 5.0) -> None: ...
def acquire_gpu_lock(cfg) -> "IO | None": ...   # data/gpu.lock, fcntl.flock(LOCK_EX|LOCK_NB)
```

**GPU 잡은 동시에 하나만 돈다.** 프로세스 락(`fcntl.flock`)으로 강제한다 —
두 번째 워커는 즉시 종료한다. 16GB 공유 메모리에서 동시 로드는 곧 OOM 이다.

Phase 3 골격에서 구현할 핸들러는 두 개면 충분하다:
`tag_activity`(unclassified 상위 N개 태깅), `summarize_doc`(doc 요약).

---

## 9. H — CLI · 서비스

### `lifetrainer/cli.py`

`argparse` 서브커맨드. 모든 명령은 `--config`, `-v/--verbose` 를 받는다.

```
lt doctor                      환경 점검 (DB, AW 연결, LLM 헬스, Slack 토큰, 폰트)
lt init-db                     스키마 생성
lt sync                        ActivityWatch 동기화
lt synth --days 7              합성 데이터 생성
lt rollup [--day D | --yesterday | --range A B]
lt stats [--day D]             콘솔 요약
lt timeline --day D [--out P]
lt report daily [--day D] [--post]
lt report weekly [--end D] [--post]
lt log <카테고리> <기간> [메모]  수동 입력 (Slack 없이도)
lt collect                     피드 수집 1회
lt score                       미채점 문서 스코어링
lt digest [--post]             아침 다이제스트 (키워드 랭킹)
lt worker [--once]             GPU 잡 워커
lt queue stats
lt backup [--out P]
lt slack serve                 Socket Mode (bolt 모드)
lt slack test                  테스트 메시지 발송
```

종료 코드: 성공 0, 사용자 오류 2, 런타임 실패 1.

### `systemd/` (사용자 유닛, `~/.config/systemd/user/`)

| 유닛 | 내용 |
|---|---|
| `lifetrainer-sync.service` + `.timer` | 10분 주기 `lt sync && lt rollup --today` |
| `lifetrainer-daily.service` + `.timer` | 매일 23:30 `lt report daily --post` |
| `lifetrainer-weekly.service` + `.timer` | 일요일 22:00 `lt report weekly --post` |
| `lifetrainer-collect.service` + `.timer` | 1시간 주기 `lt collect && lt score` |
| `lifetrainer-digest.service` + `.timer` | 매일 07:30 `lt digest --post` |
| `lifetrainer-worker.service` | 상시 워커 (`Restart=always`), 야간에만 도는 잡은 큐가 제어 |
| `lifetrainer-slack.service` | `lt slack serve` (bolt 모드에서만 enable) |

- 전부 `Type=oneshot` (워커·slack 제외), `WorkingDirectory=`, `.venv/bin/lt` 절대 경로.
- **선행 조건**: `loginctl enable-linger aisw` 가 안 되어 있으면 재부팅 후 안 뜬다
  (실제로 겪은 함정 — `operate/notes/agent-gateway.md §4-7`).
- 타이머는 `Persistent=true` 로 놓쳐도 따라잡게. `RandomizedDelaySec` 로 동시 기동 분산.
- `MemoryMax=` 를 걸어 파이썬 쪽이 폭주해도 llama-server 를 밀어내지 않게 한다.

### `scripts/`

| 스크립트 | 용도 |
|---|---|
| `setup-activitywatch-windows.ps1` | Windows PC 에 ActivityWatch 설치 + Tailscale 바인딩 + 방화벽 규칙 |
| `install-units.sh` | systemd 사용자 유닛 설치·기동 |
| `uninstall-units.sh` | 되돌리기 |

---

## 10. 완료 기준

각 담당은 아래를 만족해야 "끝"이다.

1. `.venv/bin/python -m pytest tests/test_<내영역>.py -q` 통과.
2. `.venv/bin/python -c "import lifetrainer.<내모듈>"` 이 부작용 없이 성공.
3. 남의 파일을 수정하지 않았다.
4. 네트워크·GPU 없이 테스트가 돈다.
5. 계약서의 시그니처가 정확히 일치한다 (이름·인자·반환형).

---

# Part II — 계획 계층 · 웹 플래너 (2차)

> 원본: [`archive/contracts-planner.md`](archive/contracts-planner.md).
> **§0 무엇을 만드는가 · §1 파일 소유권은 옮기지 않았다** — 그때의 작업 배분이라
> 지금 계약이 아니다. 필요하면 원본을 본다.

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

---

# Part III — 레퍼런스 흡수 (3차)

> 원본: [`archive/contracts-v2.md`](archive/contracts-v2.md).
> 여기의 **V1(하루 경계 06:00)** 이 Part I 의 `timeutil` 시그니처를 덮어썼다 —
> Part I 쪽에 그 사실을 표시해 뒀다.

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
