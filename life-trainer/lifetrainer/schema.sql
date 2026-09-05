-- Life Trainer — SQLite 스키마 (단일 이벤트 저장소)
--
-- 원칙
--   1. 시각은 전부 REAL (unix epoch, UTC). 로컬 날짜 문자열은 롤업 결과에만 쓴다.
--   2. 집계는 SQL이 한다. LLM은 이미 계산된 숫자를 문장으로만 바꾼다.
--   3. 수집기는 upsert 만 한다. 재폴링이 중복이나 이중 계산을 만들면 안 된다.
--
-- 마이그레이션: 이 파일은 "현재 스키마 전체"다. 변경 시 schema_version 을 올리고
-- lifetrainer/migrations/NNN_*.sql 에 증분을 추가한다.

-- ─────────────────────────────────────────────────────────────
-- 0. 메타 / 상태
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- 수집 커서, 마지막 동기화 시각 등 임의의 키-값 상태
CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- ─────────────────────────────────────────────────────────────
-- 1. 활동 수집 (ActivityWatch 원본)
-- ─────────────────────────────────────────────────────────────

-- 기기 차원.
--
-- 지금은 노트북 하나뿐이지만 폰까지 추적하는 것이 원래 요구사항이다.
-- 차원을 나중에 끼워 넣으면 파생 데이터를 전부 다시 계산해야 하므로 미리 심어둔다.
-- 동시 사용 시 활성 기기를 판정하는 중재 로직은 아직 없다 — 자리만 만들어 둔 것이다.
CREATE TABLE IF NOT EXISTS device (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,       -- 'DESKTOP-EXAMPLE', 'phone-example'
    kind       TEXT NOT NULL DEFAULT 'other'
               CHECK (kind IN ('laptop','desktop','phone','tablet','other')),
    hostname   TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS aw_bucket (
    bucket_id  TEXT PRIMARY KEY,
    host       TEXT NOT NULL,
    client     TEXT,                       -- aw-watcher-window / aw-watcher-afk / aw-watcher-web-*
    -- window | afk | web | android | unlock | unknown
    --
    -- 안드로이드는 `window` 가 아니라 자기 이름으로 받는다. 앱 세션은 데스크톱 창
    -- 포커스와 의미가 다르고(창이 여러 개 뜨지 않고 afk 짝이 없다), 합류는 롤업의
    -- 어댑터 한 곳에서만 한다. 판별 근거는 `collect/aw_sync.bucket_type()` 주석.
    -- CHECK 을 걸지 않는다 — 상류가 새 워처를 내놓을 때 동기화가 통째로 실패하는
    -- 것보다 `unknown` 으로 들어와 무시되는 편이 낫다.
    type       TEXT NOT NULL,
    hostname   TEXT,                       -- 원본 bucket 메타의 hostname
    device_id  INTEGER REFERENCES device(id) ON DELETE SET NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);

-- 원본 이벤트.
--
-- PK 가 (bucket_id, ts) 인 이유: ActivityWatch 의 event id 는 구현/버전에 따라
-- 존재하지 않거나 재사용될 수 있다. 반면 한 버킷 안에서 이벤트는 겹치지 않으므로
-- 시작 시각이 자연 키가 된다. 버킷의 마지막 이벤트는 하트비트로 duration 이
-- 계속 자라므로, 재폴링 시 duration/ts_end 를 갱신하는 upsert 가 필수다.
CREATE TABLE IF NOT EXISTS aw_event (
    bucket_id  TEXT NOT NULL REFERENCES aw_bucket(bucket_id) ON DELETE CASCADE,
    ts         REAL NOT NULL,              -- 시작 (unix epoch, UTC)
    ts_end     REAL NOT NULL,              -- ts + duration
    duration   REAL NOT NULL,              -- 초
    event_id   INTEGER,                    -- 원본 id (참고용, 신뢰하지 않는다)
    app        TEXT,                       -- window 버킷
    title      TEXT,                       -- window 버킷
    url        TEXT,                       -- web 버킷
    status     TEXT,                       -- afk 버킷: afk | not-afk
    data_json  TEXT NOT NULL DEFAULT '{}',
    synced_at  REAL NOT NULL,
    PRIMARY KEY (bucket_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_aw_event_ts      ON aw_event(ts);
CREATE INDEX IF NOT EXISTS idx_aw_event_end     ON aw_event(ts_end);
CREATE INDEX IF NOT EXISTS idx_aw_event_bkt_end ON aw_event(bucket_id, ts_end);

-- ─────────────────────────────────────────────────────────────
-- 2. 수동 보정 (Slack /log)
-- ─────────────────────────────────────────────────────────────
--
-- 수동 입력은 롤업에서 최우선이다. 오프라인 활동(운동·이동·회의)은
-- ActivityWatch 가 볼 수 없으므로 이 테이블이 유일한 근거다.

CREATE TABLE IF NOT EXISTS manual_entry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts    REAL NOT NULL,
    end_ts      REAL NOT NULL,
    category    TEXT NOT NULL,
    subcategory TEXT,
    note        TEXT,
    source      TEXT NOT NULL DEFAULT 'slack',   -- slack | cli
    actor       TEXT,                            -- slack user id 등
    revoked     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_manual_span ON manual_entry(start_ts, end_ts) WHERE revoked = 0;

-- ─────────────────────────────────────────────────────────────
-- 2-B. 프라이빗 구간 — 이 시간은 재지 않기로 한 것
-- ─────────────────────────────────────────────────────────────
--
-- `manual_entry` 와 같은 부류다: **사람이 넣은 구간**이고, 롤업은 읽기만 한다.
-- 그래서 형태를 그대로 베꼈다 (파생물이 아니므로 재롤업이 안 지운다).
--
-- ★ 왜 `sync_state` 키-값이 아닌가: 구간은 **겹칠 수 있고**, 이력이 남아야 하며
--   (구멍의 근거를 설명해야 한다), 이벤트마다 구간 교차 질의를 해야 한다. 셋 다 못 한다.
--
-- ★ `end_ts` 는 **미리 박는다. NULL(열린 구간)을 두지 않는다.**
--   열어 두면 `rollup_day` 가 10분마다 "지금"을 다시 해석해 **멱등성이 깨지고**,
--   젯슨이 재부팅되면 하루가 통째로 빈다. 끄기는 `end_ts` 를 now 로 **당기는** 것이다.
CREATE TABLE IF NOT EXISTS private_span (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    -- live  : 앞으로를 막는다 (토글로 켠 것)
    -- purge : 막고 + 이미 지웠다 (소급 삭제가 만든 것)
    --         ★ purge 행은 revoke 할 수 없다 — 이미 지운 것을 안 지운 척할 수 없다.
    kind       TEXT NOT NULL DEFAULT 'live' CHECK (kind IN ('live', 'purge')),
    source     TEXT NOT NULL DEFAULT 'web',  -- web | tile | pc | cli
    device     TEXT,                         -- 켠 기기 이름 (있으면)
    note       TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_private_span ON private_span(start_ts, end_ts) WHERE revoked = 0;

-- ─────────────────────────────────────────────────────────────
-- 3. 10분 버킷 롤업 (하루 144 슬롯)
-- ─────────────────────────────────────────────────────────────

-- 슬롯 하나 = 그 10분 구간의 최종 판정. winner-takes-all.
CREATE TABLE IF NOT EXISTS slot (
    day         TEXT NOT NULL,             -- 'YYYY-MM-DD' (로컬 tz)
    slot        INTEGER NOT NULL,          -- 0..143
    start_ts    REAL NOT NULL,             -- 슬롯 시작 (unix epoch, UTC)
    category    TEXT NOT NULL,             -- 승자 카테고리 ('off' = 데이터 없음)
    subcategory TEXT,
    top_app     TEXT,
    top_title   TEXT,
    active_sec  REAL NOT NULL DEFAULT 0,   -- not-afk 로 관측된 초
    afk_sec     REAL NOT NULL DEFAULT 0,   -- afk 로 관측된 초
    gap_sec     REAL NOT NULL DEFAULT 0,   -- 관측 자체가 없는 초 (PC off)
    -- 프라이빗으로 재지 않기로 한 초. `gap_sec`(관측 실패)와 **다른 것**이라 따로 센다 —
    -- 커버리지 분모에서 빼야 "하루 종일 프라이빗"이 "수집 0%"로 안 읽힌다.
    private_sec REAL NOT NULL DEFAULT 0,
    winner_sec  REAL NOT NULL DEFAULT 0,   -- 승자 카테고리가 점유한 초
    source      TEXT NOT NULL DEFAULT 'rule',  -- rule | llm | manual | mixed | none
    confidence  REAL NOT NULL DEFAULT 1.0,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (day, slot)
);

CREATE INDEX IF NOT EXISTS idx_slot_day_cat ON slot(day, category);
CREATE INDEX IF NOT EXISTS idx_slot_start   ON slot(start_ts);

-- 슬롯 내부의 전체 분포. winner-takes-all 은 시각화용이고,
-- 정직한 집계(“이번 주 코딩 몇 시간”)는 이 테이블로 한다.
CREATE TABLE IF NOT EXISTS slot_breakdown (
    day       TEXT NOT NULL,
    slot      INTEGER NOT NULL,
    category  TEXT NOT NULL,
    app       TEXT NOT NULL DEFAULT '',
    device_id INTEGER REFERENCES device(id) ON DELETE SET NULL,
    seconds   REAL NOT NULL,
    -- ★ device_id 가 키에 들어간다. 같은 슬롯에서 폰과 노트북이 같은 앱 이름을
    -- 만들면(양쪽 'Chrome' → browsing) 기기가 키에 없을 때 두 번째 행이 충돌한다.
    PRIMARY KEY (day, slot, category, app, device_id)
);

CREATE INDEX IF NOT EXISTS idx_breakdown_day_cat ON slot_breakdown(day, category);
CREATE INDEX IF NOT EXISTS idx_breakdown_day_dev ON slot_breakdown(day, device_id);


-- 문서 임베딩 (RAG). 모델이 바뀌면 통째로 재생성하는 파생물이라 doc 과 분리한다.
-- vec 는 float32 리틀엔디언 raw. 4천 건 × 1024차원 = 16MB 라 numpy 로 전수 계산한다.
CREATE TABLE IF NOT EXISTS doc_embedding (
    doc_id      INTEGER PRIMARY KEY REFERENCES doc(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vec         BLOB NOT NULL,
    source_hash TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_doc_embedding_model ON doc_embedding(model);


-- 미분류 지문의 날짜별 누적. 롤업의 멱등성을 위해 존재한다.
--
-- 왜 테이블이 둘인가: `unclassified` 는 지문의 정체성과 LLM 태깅 결과를 담는다
-- (지문당 1행, 날짜 차원 없음 — 태깅은 날짜별로 하는 게 아니다).
-- 그런데 롤업은 오늘 것을 10분마다 다시 돌린다. 누적 컬럼을 그 테이블에 두고
-- 매번 더하면 같은 시간이 반복 계산되어 부풀어 오른다.
-- 그래서 날짜별 실적은 여기에 두고 "지우고 다시 쓰기"를 가능하게 했고,
-- `unclassified.seconds_total`/`hits` 는 이 테이블의 합계로 재계산한다.
CREATE TABLE IF NOT EXISTS unclassified_day (
    day          TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    seconds      REAL NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_unclassified_day_fp ON unclassified_day(fingerprint);

-- 규칙에 걸리지 않은 (app, title) 지문. Phase 3 에서 LLM 태깅 대상이 된다.
-- 전부 LLM 에 넣지 않고, 시간을 많이 잡아먹은 것부터 처리한다.
-- seconds_total / hits 는 unclassified_day 의 합계로 **재계산**되는 값이다. 직접 더하지 말 것.
CREATE TABLE IF NOT EXISTS unclassified (
    fingerprint    TEXT PRIMARY KEY,       -- normalize(app) || '\x1f' || normalize(title)
    app            TEXT,
    title_sample   TEXT,
    seconds_total  REAL NOT NULL DEFAULT 0,
    hits           INTEGER NOT NULL DEFAULT 0,
    first_seen     REAL NOT NULL,
    last_seen      REAL NOT NULL,
    llm_category   TEXT,
    llm_subcategory TEXT,
    llm_confidence REAL,
    llm_at         REAL
);

CREATE INDEX IF NOT EXISTS idx_unclassified_pending
    ON unclassified(seconds_total DESC) WHERE llm_category IS NULL;

-- ─────────────────────────────────────────────────────────────
-- 3-B. 계획 (플래너) — 사람이 넣는 것
-- ─────────────────────────────────────────────────────────────
--
-- 격자(slot)는 기계가 계측한 "실제"고, 여기는 사람이 선언한 "의도"다.
-- 둘을 겹쳐 보는 것이 플래너의 존재 이유다. 절대 섞어서 저장하지 않는다.

-- 하루 단위 기록. /memo, D-Day, 총 시간 캐시가 여기 붙는다.
-- 종이 플래너의 COMMENT / MEMO / D-DAY / TOTAL TIME 칸에 대응한다.
CREATE TABLE IF NOT EXISTS day (
    day           TEXT PRIMARY KEY,        -- 논리적 하루 'YYYY-MM-DD'
    comment       TEXT,
    memo          TEXT,
    dday_label    TEXT,                    -- '수능', '졸업'
    dday_date     TEXT,
    total_minutes INTEGER NOT NULL DEFAULT 0,   -- 캐시. slot_breakdown 에서 파생
    closed_at     REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

-- 사용자가 정의하는 '과목' 층.
--
-- 고정 카테고리(규칙 47개로 자동 분류)와는 다른 축이다.
-- '코딩'은 기계가 측정하고, '논문 3장 쓰기'는 사람이 정의한다.
-- category 가 있으면 달성률 계산에 그 카테고리의 실측 시간을 쓴다.
--
-- ★ color 는 config/palette.yaml 의 검증된 8슬롯 중 하나여야 한다.
--   임의 hex 를 허용하면 색각 분리가 깨진다 (실제로 겪었다: coding↔research ΔE 12.0).
CREATE TABLE IF NOT EXISTS subject (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    color      TEXT NOT NULL,
    category   TEXT,                       -- NULL 이면 활동 전체로 달성 판정
    ordinal    INTEGER NOT NULL DEFAULT 0,
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS plan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    category    TEXT,                       -- 달성 판정에 쓸 카테고리. NULL 이면 시간만 비교
    start_min   INTEGER NOT NULL,           -- 로컬 자정 기준 분, 0..1439
    end_min     INTEGER NOT NULL,           -- start_min < end_min <= 1440
    kind        TEXT NOT NULL DEFAULT 'recurring',   -- recurring | oneoff
    weekdays    TEXT NOT NULL DEFAULT '1234567',     -- ISO 요일(1=월..7=일). recurring 전용
    day         TEXT,                       -- 'YYYY-MM-DD'. oneoff 전용
    active_from TEXT,                       -- recurring 유효 시작일. NULL 이면 무기한
    active_to   TEXT,                       -- recurring 유효 종료일
    color       TEXT,                       -- 계획 고유색(선택). 없으면 category 색을 쓴다
    sort_order  INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plan_active ON plan(enabled, kind);
CREATE INDEX IF NOT EXISTS idx_plan_day    ON plan(day) WHERE kind = 'oneoff';

-- 반복 계획을 특정 날짜만 건너뛴다 (휴가, 공휴일 등)
CREATE TABLE IF NOT EXISTS plan_skip (
    plan_id INTEGER NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    day     TEXT NOT NULL,
    PRIMARY KEY (plan_id, day)
);

-- 레퍼런스 플래너의 체크박스. 달성률(자동 계산)과 별개로 사람이 직접 찍는 완료 표시다.
CREATE TABLE IF NOT EXISTS plan_check (
    plan_id    INTEGER NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    day        TEXT NOT NULL,
    checked    INTEGER NOT NULL DEFAULT 0,
    checked_at REAL,
    PRIMARY KEY (plan_id, day)
);

-- 사용자가 직접 고친 칸. 자동 분류가 틀렸을 때의 정답이다.
--
-- 롤업은 이 테이블을 **지우지 않는다.** slot/slot_breakdown 은 매번 재생성되지만
-- 여기는 사람이 넣은 입력이라 재생성 대상이 아니다. 롤업의 마지막 단계에서
-- 이 값을 slot 에 덮어씌운다 (source='override').
CREATE TABLE IF NOT EXISTS slot_override (
    day        TEXT NOT NULL,
    slot       INTEGER NOT NULL,
    category   TEXT NOT NULL,
    note       TEXT,
    actor      TEXT,                        -- 'web' | 'cli' | 'slack'
    created_at REAL NOT NULL,
    PRIMARY KEY (day, slot)
);

CREATE INDEX IF NOT EXISTS idx_slot_override_day ON slot_override(day);

-- ─────────────────────────────────────────────────────────────
-- 3-C. 계획 인스턴스 — 템플릿과 그날의 실체를 분리한다
-- ─────────────────────────────────────────────────────────────
--
-- `plan` 은 반복 템플릿("평일 09-12 코딩")이고, 여기는 특정 날짜의 실체다.
-- 분리하지 않으면 "오늘은 진행 중", "오늘은 내일로 연기" 같은 하루 단위 상태를
-- 표현할 자리가 없다. 종이 플래너의 체크 기호 3종(완료 v / 진행 중 / / 연기 >)이
-- 요구하는 구조이기도 하다.
--
-- 이월(deferred)은 다음 날 인스턴스를 새로 만들고 carried_from 으로 잇는다.
-- 그 체인의 깊이가 곧 미루기의 정도다 (v_carry_debt).

CREATE TABLE IF NOT EXISTS plan_instance (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    day          TEXT NOT NULL,            -- 논리적 하루
    plan_id      INTEGER REFERENCES plan(id) ON DELETE SET NULL,
    title        TEXT NOT NULL,
    subject_id   INTEGER REFERENCES subject(id) ON DELETE SET NULL,
    category     TEXT,                     -- 달성 판정 대상. subject 에서 상속 가능
    start_min    INTEGER NOT NULL,         -- 벽시계 분 (0..1440)
    end_min      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'todo'
                 CHECK (status IN ('todo','doing','done','partial','deferred','canceled')),
    priority     TEXT NOT NULL DEFAULT 'normal'
                 CHECK (priority IN ('low','normal','high')),
    ordinal      INTEGER NOT NULL DEFAULT 0,   -- 표시 순서 = /done, /del 의 번호
    planned_min  INTEGER,
    note         TEXT,
    carried_from INTEGER REFERENCES plan_instance(id) ON DELETE SET NULL,
    source       TEXT NOT NULL DEFAULT 'template'
                 CHECK (source IN ('template','manual','carry','chat')),
    archived_at  REAL,                     -- 소프트 삭제. 실적이 사라지면 주간 통계가 깨진다
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pi_day   ON plan_instance(day, ordinal);
CREATE INDEX IF NOT EXISTS idx_pi_carry ON plan_instance(carried_from);
CREATE INDEX IF NOT EXISTS idx_pi_live  ON plan_instance(day) WHERE archived_at IS NULL;

-- 상태 변경 감사 로그. 누가(사람/시스템/LLM) 언제 바꿨는지 남긴다.
CREATE TABLE IF NOT EXISTS plan_instance_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES plan_instance(id) ON DELETE CASCADE,
    at          REAL NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'user'   -- user | system | llm
);

CREATE INDEX IF NOT EXISTS idx_pie_instance ON plan_instance_event(instance_id, at);

-- 미루기 부채: 이월 체인이 2단계 이상인 뿌리 계획
CREATE VIEW IF NOT EXISTS v_carry_debt AS
WITH RECURSIVE chain(id, root, depth) AS (
    SELECT id, id, 0 FROM plan_instance WHERE carried_from IS NULL
    UNION ALL
    SELECT p.id, c.root, c.depth + 1
    FROM plan_instance p JOIN chain c ON p.carried_from = c.id
)
SELECT root, MAX(depth) AS depth
FROM chain
GROUP BY root
HAVING MAX(depth) >= 2;

-- ─────────────────────────────────────────────────────────────
-- 4. 웹 수집 (Phase 2) — 예의 계층 포함
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS source (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,        -- rss | atom | arxiv | github | hn
    name             TEXT NOT NULL,
    url              TEXT NOT NULL UNIQUE,
    enabled          INTEGER NOT NULL DEFAULT 1,
    tags             TEXT NOT NULL DEFAULT '',   -- 콤마 구분
    interval_sec     INTEGER NOT NULL DEFAULT 3600,
    next_fetch_at    REAL NOT NULL DEFAULT 0,
    etag             TEXT,
    last_modified    TEXT,
    content_hash     TEXT,
    last_fetched     REAL,
    last_status      INTEGER,
    fail_count       INTEGER NOT NULL DEFAULT 0,
    unchanged_streak INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_due ON source(next_fetch_at) WHERE enabled = 1;

-- 개별 URL 프론티어. 피드 밖의 임의 URL(본문 가져오기 등)도 여기를 통과한다.
CREATE TABLE IF NOT EXISTS url_state (
    url              TEXT PRIMARY KEY,
    domain           TEXT NOT NULL,
    last_fetched     REAL,
    next_fetch_at    REAL NOT NULL DEFAULT 0,
    etag             TEXT,
    last_modified    TEXT,
    content_hash     TEXT,
    last_status      INTEGER,
    fail_count       INTEGER NOT NULL DEFAULT 0,
    unchanged_streak INTEGER NOT NULL DEFAULT 0,
    interval_sec     INTEGER NOT NULL DEFAULT 21600
);

CREATE INDEX IF NOT EXISTS idx_url_due ON url_state(next_fetch_at);

-- robots.txt 캐시 (도메인당 1행)
CREATE TABLE IF NOT EXISTS robots_cache (
    domain     TEXT PRIMARY KEY,
    body       TEXT,
    fetched_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status     INTEGER
);

CREATE TABLE IF NOT EXISTS doc (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER REFERENCES source(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL DEFAULT 'article',  -- article | paper | repo | note
    url          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    author       TEXT,
    published_at REAL,
    fetched_at   REAL NOT NULL,
    lang         TEXT,
    abstract     TEXT,                    -- 수집 시점 원문 발췌 / 초록
    body_path    TEXT,                    -- 전문 텍스트 파일 경로 (NVMe). 짧은 초록만 받는다
    body_fetch_failed_at REAL,            -- 본문 수집 실패 시각. 있으면 다시 안 두드린다
    content_hash TEXT NOT NULL,           -- SHA-256, 완전 중복
    simhash      INTEGER,                 -- 근사 중복 (뉴스 신디케이션)
    dup_of       INTEGER REFERENCES doc(id) ON DELETE SET NULL,
    score        REAL,                    -- 관심사 스코어 (**시간 없는 기준점수** — 최신성은 읽을 때 곱한다)
    scored_at    REAL,
    digested_at  REAL,                    -- 아침 다이제스트로 나간 시각. NULL = 아직 안 나갔다
    summary      TEXT,                    -- LLM 요약 (Phase 3)
    summary_at   REAL,
    state        TEXT NOT NULL DEFAULT 'new'  -- new | scored | summarized | archived
);

CREATE INDEX IF NOT EXISTS idx_doc_state     ON doc(state, score DESC);
CREATE INDEX IF NOT EXISTS idx_doc_published ON doc(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_doc_hash      ON doc(content_hash);
CREATE INDEX IF NOT EXISTS idx_doc_simhash   ON doc(simhash);

-- BM25 검색용. content= 로 doc 를 외부 콘텐츠로 삼아 중복 저장을 피한다.
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
    title, abstract, summary,
    content='doc', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS doc_ai AFTER INSERT ON doc BEGIN
    INSERT INTO doc_fts(rowid, title, abstract, summary)
    VALUES (new.id, new.title, new.abstract, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS doc_ad AFTER DELETE ON doc BEGIN
    INSERT INTO doc_fts(doc_fts, rowid, title, abstract, summary)
    VALUES ('delete', old.id, old.title, old.abstract, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS doc_au AFTER UPDATE ON doc BEGIN
    INSERT INTO doc_fts(doc_fts, rowid, title, abstract, summary)
    VALUES ('delete', old.id, old.title, old.abstract, old.summary);
    INSERT INTO doc_fts(rowid, title, abstract, summary)
    VALUES (new.id, new.title, new.abstract, new.summary);
END;

CREATE TABLE IF NOT EXISTS doc_tag (
    doc_id INTEGER NOT NULL REFERENCES doc(id) ON DELETE CASCADE,
    tag    TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'rule',   -- rule | llm | manual
    PRIMARY KEY (doc_id, tag)
);

-- 관심사 키워드 프로파일. Phase 2 의 스코어링은 이것만으로 한다 (LLM 없음).
CREATE TABLE IF NOT EXISTS interest (
    term       TEXT PRIMARY KEY,
    weight     REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT 'manual',  -- manual | activity | feedback
    updated_at REAL NOT NULL
);

-- ─────────────────────────────────────────────────────────────
-- 5. GPU 작업 큐 (Phase 3)
-- ─────────────────────────────────────────────────────────────
--
-- GPU 를 쓰는 모든 작업은 이 테이블 하나를 통과한다.
-- 워커는 단 하나만 돈다 — 16GB 공유 메모리에서 동시 로드는 곧 OOM 이다.

CREATE TABLE IF NOT EXISTS job (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,           -- summarize_doc | tag_activity | digest | answer ...
    payload_json TEXT NOT NULL DEFAULT '{}',
    priority     INTEGER NOT NULL DEFAULT 100,   -- 작을수록 먼저
    state        TEXT NOT NULL DEFAULT 'queued', -- queued|running|done|failed|cancelled
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    not_before   REAL NOT NULL DEFAULT 0,
    lease_until  REAL,
    worker       TEXT,
    dedupe_key   TEXT UNIQUE,
    result_json  TEXT,
    error        TEXT,
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_job_ready ON job(state, priority, not_before);

-- LLM 호출 계측. 배치 시간 예산을 세우려면 실측이 있어야 한다.
CREATE TABLE IF NOT EXISTS llm_call (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            INTEGER REFERENCES job(id) ON DELETE SET NULL,
    purpose           TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    latency_ms        INTEGER,
    ok                INTEGER NOT NULL,
    error             TEXT,
    created_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_call_created ON llm_call(created_at);

-- ─────────────────────────────────────────────────────────────
-- 6. 리포트 / 발송
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS report (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,          -- daily | weekly | morning
    day           TEXT NOT NULL,          -- 대상 날짜 'YYYY-MM-DD'
    text          TEXT NOT NULL,          -- 폴백용 평문
    blocks_json   TEXT,                   -- Slack Block Kit
    png_path      TEXT,
    created_at    REAL NOT NULL,
    posted_at     REAL,
    slack_channel TEXT,
    slack_ts      TEXT,
    UNIQUE (kind, day)
);

-- 운영 알림 중복 억제 (같은 실패로 1시간에 한 번만 울리게)
CREATE TABLE IF NOT EXISTS alert_state (
    key        TEXT PRIMARY KEY,
    last_sent  REAL NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1
);
