-- 003: slot_breakdown 의 기본키에 device_id 를 넣는다.
--
-- 왜: 002 가 device_id 컬럼을 더했지만 기본키는 (day, slot, category, app) 그대로였다.
-- 폰이 들어오기 전에는 기기가 하나라 문제가 없었는데, 같은 10분 슬롯에서 폰과 노트북이
-- **같은 카테고리·같은 앱 이름**을 만들면(예: 양쪽 'Chrome' → browsing) 두 번째 INSERT 가
-- 기본키 충돌로 죽는다. 그래서 롤업이 device_id 를 아예 안 쓰고 NULL 로 두고 있었다.
--
-- SQLite 는 기본키를 ALTER 로 못 바꾼다. 테이블을 다시 만든다.
-- slot_breakdown 은 aw_event 에서 무손실로 재생성되는 **파생 데이터**라 옮기지 않고 비운다
-- (002 가 같은 이유로 같은 선택을 했다). 롤업이 다시 채운다.

DROP TABLE IF EXISTS slot_breakdown;

CREATE TABLE slot_breakdown (
    day       TEXT NOT NULL,
    slot      INTEGER NOT NULL,
    category  TEXT NOT NULL,
    app       TEXT NOT NULL DEFAULT '',
    device_id INTEGER REFERENCES device(id) ON DELETE SET NULL,
    seconds   REAL NOT NULL,
    -- device_id 가 NULL 인 행(수동 입력 등)도 유일해야 하므로 COALESCE 로 접는다.
    -- SQLite 의 PRIMARY KEY 는 NULL 을 서로 다른 값으로 보기 때문이다.
    PRIMARY KEY (day, slot, category, app, device_id)
);

CREATE INDEX IF NOT EXISTS idx_breakdown_day_cat ON slot_breakdown(day, category);
CREATE INDEX IF NOT EXISTS idx_breakdown_day_dev ON slot_breakdown(day, device_id);
