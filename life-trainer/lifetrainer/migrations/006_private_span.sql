-- 006: 프라이빗 구간 — 이 시간은 재지 않기로 한 것
--
-- ★ db.py 와 이 파일이 어긋나면 **db.py 가 맞다** (002 머리말과 같은 규칙).
--   이 파일은 사람이 읽는 스펙이고, 실제 실행은 `_apply_006_private_span` 이 한다.
--
-- 002·003 과 달리 **파생 테이블을 비우지 않는다.** 새 테이블과 기본값 0 인 새 컬럼뿐이라
-- 기존 롤업 결과가 그대로 맞다. 재계산이 필요 없다.

CREATE TABLE IF NOT EXISTS private_span (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'live' CHECK (kind IN ('live', 'purge')),
    source     TEXT NOT NULL DEFAULT 'web',
    device     TEXT,
    note       TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_private_span ON private_span(start_ts, end_ts) WHERE revoked = 0;

-- 그 슬롯에서 프라이빗이 차지한 초. `off`(데이터 없음)와 구분해야 커버리지를
-- 정직하게 낼 수 있다 — 프라이빗은 수집 실패가 아니라 재지 않기로 한 시간이다.
ALTER TABLE slot ADD COLUMN private_sec REAL NOT NULL DEFAULT 0;
