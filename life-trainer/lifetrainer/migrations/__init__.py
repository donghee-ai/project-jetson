"""스키마 증분 마이그레이션 모음.

각 마이그레이션은 `NNN_설명.sql` 파일로 무엇이 바뀌는지 사람이 읽을 수 있게 기록한다.
실제 적용 로직(조건부 `ALTER TABLE`, zoneinfo 기반 좌표 변환 등 순수 SQL로 표현할 수
없는 부분 포함)은 `lifetrainer/db.py` 의 `migrate(conn)` 러너가 담당한다 — 이 파일들은
`schema.sql` 헤더의 "증분" 관례를 따르는 문서 겸 스펙이다.
"""

from __future__ import annotations
