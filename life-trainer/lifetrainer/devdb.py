"""운영 DB 의 **개발 사본** — 실험이 실데이터를 건드리지 않게.

## 왜 있나

이 시스템의 표본은 사람의 실생활이라 **운영 DB 가 곧 유일한 데이터셋**이다.
따로 만든 합성 데이터로는 재현되지 않는 버그가 대부분이라 (`collect/synthetic.py` 가
있지만 그건 골격 검증용이다), *"개발용 DB 를 따로 쓴다"* 가 곧 *"실데이터로는
개발을 안 한다"* 가 되면 안 된다.

그래서 분리하는 방식이 **복사**다. 실데이터 그대로, 다만 쓰는 것은 사본이다.

## 무엇을 막나 — 추상적인 위험이 아니다

2026-09-07 실사 중에 실제로 밟았다. 되돌리기 버튼을 검증하려고 **운영 DB 에**
삭제 4건을 만들었다. 되돌릴 수 있는 기능이라 전부 복원됐지만
(`purged_event` 0건 확인), **되돌릴 수 없는 것을 시험했으면 그대로 손실이었다.**
`docs/issues/h-0008` 이 같은 것을 이미 적어 두고 있었다 — 한 건이 아니라 구조다.

## 왜 `cp` 가 아니라 백업 API 인가

DB 는 WAL 모드로 돈다. 파일만 복사하면 **최근 커밋이 `-wal` 에 남아 있어 안 따라온다** —
"동작 중인 WAL DB 를 복사한 것" 과 구분이 안 되는 상태가 되고, 그건
`docs/runbook-backup-restore.md` 를 쓰게 만든 바로 그 결함이다.
`sqlite3.Connection.backup()` 은 잠금을 잡고 일관된 사본을 만든다.

## 어떻게 쓰나

    lt dev-db          # 사본을 만든다/새로 뜬다
    lt --dev rollup --today     # 사본에 대고 돈다
    lt rollup --today           # 운영에 대고 돈다 (평소대로)

★ **환경변수가 아니라 플래그인 이유**: `export` 는 셸을 옮기면 사라지고
  안 옮기면 남는다 — 둘 다 조용하다. 플래그는 그 명령 한 줄에만 걸리고,
  무엇에 대고 돌렸는지가 **셸 히스토리에 남는다.**
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

logger = logging.getLogger(__name__)

# 사본이 사는 곳. 운영 `data/` 안의 하위 폴더다 — 백업 스크립트가 `data/backup/` 을
# 이미 건너뛰므로 같은 규칙을 쓴다.
DEV_DIRNAME = "dev"
DEV_DB_NAME = "lifetrainer.db"

# `meta` 에 찍는 표식. **운영 DB 에는 아무것도 안 쓴다** — 사본에만 찍고,
# 표식이 없으면 운영으로 본다. 운영을 건드리지 않는 것이 이 모듈의 요점이라
# 그 표식을 남기려고 운영에 쓰는 것은 자기모순이다.
ROLE_KEY = "db_role"
ROLE_DEV = "dev"


def dev_paths(cfg) -> tuple[Path, Path]:
    """(사본 DB 경로, 사본 data_dir). 만들지는 않는다."""
    data_dir = Path(cfg.data_dir) / DEV_DIRNAME
    return data_dir / DEV_DB_NAME, data_dir


def as_dev(cfg):
    """설정을 **사본을 보도록** 바꾼 새 Config.

    `data_dir` 도 같이 옮긴다. DB 만 옮기면 본문 파일(`data/bodies/`)과
    에이전트 작업 폴더(`data/agent/`)는 여전히 운영 쪽에 쓴다 — 절반만 분리된 상태가
    가장 나쁘다 (이 저장소가 nvm/시스템 Node 로 이미 겪은 부류다).
    """
    db_path, data_dir = dev_paths(cfg)
    return replace(cfg, db_path=db_path, data_dir=data_dir)


def role_of(conn: sqlite3.Connection) -> str:
    """이 연결이 보고 있는 DB 가 사본인가 운영인가. 표식이 없으면 운영이다."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (ROLE_KEY,)).fetchone()
    except sqlite3.Error:
        return "prod"
    if row is None:
        return "prod"
    value = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    return ROLE_DEV if value == ROLE_DEV else "prod"


def make_copy(cfg, *, force: bool = False) -> tuple[Path, int]:
    """운영 DB 를 사본으로 뜬다. `(사본 경로, 바이트)` 를 돌려준다.

    이미 있으면 `force` 없이는 덮지 않는다 — 사본에서 하던 작업이 있을 수 있고,
    "새로 뜬다" 는 **지운다**는 뜻이기 때문이다.
    """
    src = Path(cfg.db_path)
    if not src.exists():
        raise FileNotFoundError(f"운영 DB 가 없다: {src}")

    dst, data_dir = dev_paths(cfg)
    if dst.exists() and not force:
        raise FileExistsError(f"사본이 이미 있다: {dst} (새로 뜨려면 --refresh)")

    data_dir.mkdir(parents=True, exist_ok=True)
    # ★ 새로 뜰 때는 옛 사본의 WAL 부산물까지 지운다. 남겨 두면 새 사본 옆에
    #   옛 `-wal` 이 붙어 **섞인 상태**가 된다.
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst) + suffix)
        if p.exists():
            p.unlink()

    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)  # 잠금을 잡고 일관된 사본을 만든다 (WAL 안전)
            now = time.time()
            for key, value in (
                (ROLE_KEY, ROLE_DEV),
                ("db_copied_at", str(int(now))),
                ("db_copied_from", str(src)),
            ):
                target.execute(
                    "INSERT INTO meta(key, value, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (key, value, now),
                )
            target.commit()
        finally:
            target.close()
    finally:
        source.close()

    return dst, dst.stat().st_size
