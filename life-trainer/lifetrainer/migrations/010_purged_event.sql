-- 010: 지운 이벤트가 옮겨 가는 **휴지통** (`purged_event`)
--
-- ★ db.py 와 이 파일이 어긋나면 **db.py 가 맞다** (002 머리말과 같은 규칙).
--
-- 왜: `lt private purge` 가 행을 진짜 `DELETE` 했다. 그런데 `privacy.load_spans` 는
-- *"`revoked` 는 제외한다 — 잘못 켠 것을 취소할 수 있어야 한다"* 고 적어 두고 있었고,
-- **`revoked = 1` 을 만드는 코드는 저장소에 하나도 없었다.** 되돌린다는 약속만 있고
-- 길이 없었으며, 있었어도 지워진 행은 안 돌아왔다.
--
-- ★ `aw_event.deleted_at` 플래그로 만들려다 **테이블을 나눴다.** 이유가 둘이다:
--
--   ① 읽는 쪽이 12곳이다. 전부 `deleted_at IS NULL` 을 달아야 하는데 **하나만
--      빠뜨리면 지운 것이 그 화면에만 나타난다.** 그리고 그 실수는 조용하다 —
--      다음에 붙는 질의가 또 밟는다. 테이블이 다르면 그 실수 자체가 불가능하다
--   ② `aw_event` 의 PK 가 `(bucket_id, ts)` 다. 구간에 걸친 이벤트를 잘라 남기면
--      **왼쪽 조각이 원본과 같은 ts** 라 UNIQUE 로 죽는다 (실제로 죽었다)
--
-- 지우기는 이제 두 단계다:
--
--   ① 휴지통으로 — `purge()`. 화면·집계·LLM 어디에도 안 나온다. `undo()` 로 되돌아온다
--   ② 완전 삭제  — `lt private forget`. **사람이 부른다**
--
-- ★ **자동으로 ②를 안 한다.** 보존기간을 정하는 것은 사람의 결정이고, 정하지 않은 채
--   타이머를 붙이면 "며칠 뒤 사라지는 줄 알았는데 안 사라진" 또는 그 반대가 된다.
--   대신 `lt private status` 가 휴지통 건수를 늘 말한다 — 안 보이면 잊힌다.

CREATE TABLE IF NOT EXISTS purged_event (
    bucket_id     TEXT NOT NULL,
    ts            REAL NOT NULL,
    ts_end        REAL NOT NULL,
    duration      REAL NOT NULL,
    event_id      INTEGER,
    app           TEXT,
    title         TEXT,
    url           TEXT,
    status        TEXT,
    data_json     TEXT NOT NULL DEFAULT '{}',
    synced_at     REAL NOT NULL,
    purge_span_id INTEGER NOT NULL,
    purged_at     REAL NOT NULL,
    PRIMARY KEY (bucket_id, ts)
);
