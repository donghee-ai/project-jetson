# Life Trainer — 모듈 계약서 (구현 에이전트용)

> 이 문서는 여러 에이전트가 **동시에** 코딩하기 위한 인터페이스 고정 문서다.
> 여기 적힌 시그니처는 협상 대상이 아니다. 바꿔야 한다면 구현하지 말고 보고할 것.
>
> ★ **이것은 v1 이고 그 위에 두 층이 더 얹혀 있다** —
> [contracts-planner.md](contracts-planner.md)(계획·웹) · [contracts-v2.md](contracts-v2.md)(레퍼런스 흡수).
> **§0 공통 규칙은 계속 유효하지만, 개별 시그니처가 충돌하면 나중 문서가 이긴다.**
>
> 설계 근거: [life-trainer-design.md](../life-trainer-design.md)
> 하드웨어 실측 제약: [../../measure/findings/hardware.md](../../../measure/findings/hardware.md),
> [../../measure/findings/performance.md](../../../measure/findings/performance.md)

---

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
  리랭커는 아직 안 붙인다 ([rag-plan §7](../rag-plan.md)).
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
def day_str(ts: float, tz: tzinfo) -> str: ...            # 'YYYY-MM-DD'
def day_bounds(day: str, tz: tzinfo) -> tuple[float, float]: ...   # [start, end)
def slot_index(ts: float, tz: tzinfo, slot_minutes: int = 10) -> int: ...
def slot_bounds(day: str, slot: int, tz: tzinfo, slot_minutes: int = 10) -> tuple[float, float]: ...
def slots_per_day(slot_minutes: int = 10) -> int: ...
def overlap_sec(a0: float, a1: float, b0: float, b1: float) -> float: ...  # 겹치는 초, 음수 없음
def day_range(start_day: str, end_day: str) -> list[str]: ...     # 양 끝 포함
def parse_duration(s: str) -> float: ...   # '60m', '1h30m', '90', '1.5h' -> 초. 실패 시 ValueError
```

`day_bounds` 는 zoneinfo 를 써서 계산한다 (`datetime.combine(date, time.min, tzinfo=tz)`).
DST 가 있는 타임존에서도 24시간을 가정하지 말 것 — 다음 날 자정을 다시 계산한다.

### `lifetrainer/db.py`

```python
SCHEMA_VERSION: int = 1

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
3. `transaction` 은 `BEGIN IMMEDIATE` 로 시작한다 (WAL 에서 writer 경합 시 즉시 실패하게).
   예외 시 롤백 후 재raise.
4. `backup` 은 sqlite3 온라인 백업 API(`conn.backup`)를 쓴다. 파일 복사 금지.
5. 부모 디렉터리가 없으면 만든다.

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
