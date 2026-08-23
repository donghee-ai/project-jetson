-- 004: 문서 임베딩 저장소.
--
-- 왜 별도 테이블인가: `doc` 는 수집·요약이 계속 갱신하는 행이고, 임베딩은 **모델이
-- 바뀌면 통째로 다시 만들어야 하는 파생물**이다. 같은 테이블에 두면 모델 교체 때
-- doc 을 건드려야 한다. 분리해 두면 이 테이블만 비우면 된다.
--
-- 왜 BLOB 인가: 문서 4천 건 × 1024차원 × float32 = 16MB 다. 이 규모에서 벡터 인덱스
-- (sqlite-vec 등 새 의존성)는 과하고, numpy 로 전수 코사인이 밀리초 단위다.
-- 인덱스가 필요해지는 규모가 오면 그때 얹는다 — 지금 얹으면 검증할 기준이 없다.
--
-- source_hash 를 두는 이유: 요약이 나중에 채워져 원문이 바뀐다. 해시가 다른 것만
-- 다시 임베딩하면 4천 건을 매번 다시 도는 낭비를 피한다.
--
-- model/dim 을 함께 적는 이유: 모델을 바꾸면 차원이 달라져 옛 벡터와 섞이면
-- 조용히 틀린 유사도가 나온다. 읽을 때 현재 모델과 다른 행은 건너뛴다.

CREATE TABLE IF NOT EXISTS doc_embedding (
    doc_id     INTEGER PRIMARY KEY REFERENCES doc(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,          -- float32 리틀엔디언, L2 정규화된 상태로 저장
    source_hash TEXT NOT NULL,         -- 임베딩에 넣은 원문의 해시. 바뀐 문서만 다시 만든다
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_doc_embedding_model ON doc_embedding(model);
