-- 005: 카테고리 communication -> sns (2026-08-25)
--
-- 라벨 '소통'이 공개용 익명 표본의 사용을 설명하지 못했다:
-- 공개용 익명 표본에서 메신저·커뮤니티 앱이 같은 분류에 반복됐다.
-- 같은 노랑 슬롯에 이름만 SNS 로 바꾸고,
-- entertainment 에 있던 SNS 앱 계열을 이쪽으로 옮긴다(rules.yaml).
--
-- 파생(slot·slot_breakdown)은 재롤업으로도 복구되지만, 사람이 넣은 원본
-- (slot_override·manual_entry·plan·subject)은 롤업이 다시 쓰지 않으므로
-- 여기서 바꿔야 한다.

UPDATE slot           SET category = 'sns' WHERE category = 'communication';
UPDATE slot_breakdown SET category = 'sns' WHERE category = 'communication';
UPDATE slot_override  SET category = 'sns' WHERE category = 'communication';
UPDATE manual_entry   SET category = 'sns' WHERE category = 'communication';
UPDATE plan           SET category = 'sns' WHERE category = 'communication';
UPDATE subject        SET category = 'sns' WHERE category = 'communication';
