-- 009: 본문 수집이 실패한 문서에 시각을 남긴다 (`doc.body_fetch_failed_at`)
--
-- ★ db.py 와 이 파일이 어긋나면 **db.py 가 맞다** (002 머리말과 같은 규칙).
--   실제 실행은 `_apply_009_doc_body_fetch_failed` 가 한다.
--
-- 왜 필요한가: `collect/bodies.py` 가 짧은 초록을 가진 문서의 전문을 받는다. 실패한
-- 문서에 아무 표시도 안 남기면 **다음 배치가 같은 URL 을 또 두드린다** — 남의 서버에
-- 대고 도는 루프가 된다. `body_path IS NULL` 만으로는 "아직 안 받아 봤다" 와
-- "받아 봤는데 안 됐다" 가 구분되지 않는다.
--
-- ★ 인덱스를 만들지 않는다 (008 이 남긴 함정과 같은 이유 — `open_db` 가 schema.sql 을
--   migrate 보다 먼저 붓는다). 후보 질의는 어차피 짧은 초록으로 좁혀진다.

ALTER TABLE doc ADD COLUMN body_fetch_failed_at REAL;
