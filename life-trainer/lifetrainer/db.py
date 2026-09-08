"""SQLite 연결·스키마·트랜잭션 유틸리티.

스키마 원본은 `lifetrainer/schema.sql` 하나뿐이다. 이 모듈은 그것을 적용하고,
WAL/트랜잭션 관례를 강제하고, 온라인 백업을 제공한다.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from lifetrainer import timeutil

logger = logging.getLogger(__name__)

SCHEMA_VERSION: int = 11
# ★ 이 상수와 아래 `_MIGRATIONS` 목록은 **같이 움직여야 한다.** 2026-09-07 에 상수만
#   11 로 올리고 목록에 안 넣었더니, `init-db` 가 "schema 11" 을 찍으면서 열은 안 만들었다 —
#   *스탬프는 최신인데 스키마는 옛것*이라는 최악의 상태다. 이 저장소가 이미 겪은 부류라
#   (`HISTORY/2026-09-04-the-version-stamp-two-processes-fought-over.md`)
#   `tests/test_migrate.py` 가 `SCHEMA_VERSION == 1 + len(_MIGRATIONS)` 를 지킨다.

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# 마이그레이션 002(논리적 하루 경계 06:00)가 쓰는 고정 전제.
#
# migrate(conn) 은 계약서 시그니처상 cfg 를 받지 않는다. 이 프로젝트는 로컬
# 전용 단일 사용자 도구라 config.py 의 general.timezone/rollup.slot_minutes
# 기본값과 동일한 값을 여기 상수로 둔다 — 다른 타임존/슬롯 폭을 쓰는 배포는
# 이 마이그레이션의 대상이 아니다.
_MIGRATION_TZ = ZoneInfo("Asia/Seoul")
_MIGRATION_SLOT_MINUTES = 10
_MIGRATION_OLD_BOUNDARY_HOUR = 0  # 옛 자정 기준
_MIGRATION_NEW_BOUNDARY_HOUR = 6  # 새 06:00 기준


def connect(db_path: str | Path, *, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    """SQLite 연결을 만들고 계약서가 요구하는 PRAGMA 를 전부 건다.

    isolation_level=None (오토커밋) 으로 열어서, `transaction()` 의 명시적
    BEGIN IMMEDIATE 가 sqlite3 모듈이 몰래 여는 트랜잭션과 충돌하지 않게 한다.
    """
    path = Path(db_path)

    if readonly:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)

    conn.row_factory = sqlite3.Row

    if readonly:
        # 이미 WAL 인 DB 를 읽기 전용으로 열 때는 같은 값으로의 재설정이라 쓰기가 필요 없다.
        # 아직 WAL 로 전환된 적 없는 DB 라면 쓰기가 필요해 실패할 수 있으니 조용히 넘어간다.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
    else:
        conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """schema.sql 을 적용한다. IF NOT EXISTS 로 이루어져 있어 여러 번 호출해도 안전하다.

    ★ **빈 파일이었으면 마이그레이션을 "적용됨"으로 찍고 끝낸다**(baseline).
      `schema.sql` 은 언제나 *모든 마이그레이션이 끝난 뒤의 모양*이므로, 빈 파일에
      부은 DB 는 이미 최신이고 마이그레이션을 **돌리면 안 된다.** 002·003 은 하루
      경계가 바뀌었다는 뜻으로 `slot`·`slot_breakdown` 을 **비우기** 때문에, 돌리면
      방금 넣은 데이터가 사라진다.

      판정은 `open_db` 가 아니라 여기서 한다 — `init_db` 만 부르고 데이터를 넣는
      호출부(테스트 픽스처가 그렇다)가 있어서, 그쪽이 표식 없는 DB 를 만들면 나중에
      `open_db` 가 그걸 "오래된 DB"로 오해한다. 실제로 그렇게 웹 격자가 통째로 비었다.
    """
    fresh = conn.execute(
        "SELECT count(*) AS c FROM sqlite_master WHERE type = 'table'"
    ).fetchone()["c"] == 0
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        conn.executescript(schema_sql)
    except sqlite3.OperationalError as exc:
        if "fts5" in str(exc).lower():
            raise RuntimeError(
                "이 환경의 SQLite 는 FTS5 확장이 빌드되어 있지 않습니다. "
                "doc_fts 가상 테이블을 만들 수 없어 스키마 적용이 실패했습니다. "
                "FTS5 가 포함된 sqlite3(또는 pysqlite3-binary)로 교체하세요."
            ) from exc
        raise

    now = time.time()
    # ★ **이미 적힌 값을 덮지 않는다** (`DO NOTHING`). 전에는 매번 이 프로세스의 상수로
    #   갈아엎었는데, `init_db` 는 `open_db` 마다 불리므로 **옛 코드를 들고 며칠째 떠 있는
    #   프로세스가 버전을 뒤로 되돌린다.** 2026-09-04 에 실제로 그랬다 — 008·009 를
    #   적용해 meta 가 9 가 됐는데, SCHEMA_VERSION=7 을 메모리에 들고 있던 web·worker·
    #   slack 이 다음 요청에서 7 로 되돌려 놨다. doctor 는 "마이그레이션 확인 필요" 를
    #   계속 띄웠고, **그건 원인을 고쳐도 안 꺼지는 신호다** (CLAUDE.md §1 의 1번).
    #
    #   버전을 **올리는** 것은 `migrate()` 와 `baseline_migrations()` 의 일이다.
    #   여기서는 값이 아예 없을 때만(=아주 옛 DB) 처음 심는다.
    conn.execute(
        "INSERT INTO meta(key, value, updated_at) VALUES ('schema_version', ?, ?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION), now),
    )
    if fresh:
        baseline_migrations(conn, now=now)


def schema_version(conn: sqlite3.Connection) -> int:
    """현재 DB 에 기록된 schema_version. meta 에 없으면 0."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row["value"]) if row is not None else 0


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """`BEGIN IMMEDIATE` 트랜잭션. 예외 발생 시 롤백 후 재raise.

    WAL 모드에서 writer 경합을 락 대기 없이 즉시 실패시키기 위해 IMMEDIATE 를 쓴다.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """sync_state 에서 문자열 값을 읽는다 (수집 커서 등 임의의 키-값 상태)."""
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """sync_state 에 문자열 값을 upsert 한다."""
    conn.execute(
        "INSERT INTO sync_state(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, time.time()),
    )


def get_state_float(conn: sqlite3.Connection, key: str, default: float | None = None) -> float | None:
    """sync_state 에서 float 값을 읽는다 (예: 동기화 커서 epoch)."""
    raw = get_state(conn, key)
    return float(raw) if raw is not None else default


def set_state_float(conn: sqlite3.Connection, key: str, value: float) -> None:
    """sync_state 에 float 값을 upsert 한다."""
    set_state(conn, key, repr(float(value)))


def upsert_device(
    conn: sqlite3.Connection,
    name: str,
    *,
    kind: str = "other",
    hostname: str | None = None,
    now: float | None = None,
) -> int:
    """`device` 를 이름으로 upsert 하고 id 를 반환한다.

    **`kind` 는 이미 있는 행에 덮어쓰지 않는다.** 기기 종류는 사람이 의미를 부여한
    분류값이라(`lt` 로 고칠 수 있다) 동기화가 매번 되돌리면 안 된다. 최초 삽입에만
    쓰인다. `hostname` 은 관측값이므로 새로 오면 갱신한다.
    """
    ts = now if now is not None else timeutil.now_ts()
    conn.execute(
        """
        INSERT INTO device(name, kind, hostname, active, created_at)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(name) DO UPDATE SET
            hostname = COALESCE(excluded.hostname, device.hostname)
        """,
        (name, kind, hostname, ts),
    )
    row = conn.execute("SELECT id FROM device WHERE name = ?", (name,)).fetchone()
    return int(row["id"] if isinstance(row, sqlite3.Row) else row[0])


def backup(conn: sqlite3.Connection, dest: str | Path) -> Path:
    """sqlite3 온라인 백업 API(`conn.backup`)로 dest 에 백업한다. 파일 복사를 쓰지 않는다."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_conn = sqlite3.connect(str(dest_path))
    try:
        conn.backup(dest_conn)
    finally:
        dest_conn.close()
    return dest_path


def open_db(cfg) -> sqlite3.Connection:
    """cfg.db_path 로 connect 하고 스키마 + **마이그레이션**을 적용한다 (멱등). 일반적인 진입점.

    ★ 2026-09-01: 여기서 `migrate()` 를 부르기 시작했다.

    그전까지 `migrate()` 에는 **프로덕션 호출자가 하나도 없었다.** `lt init-db` 조차
    `init_db()` 만 부르고 "schema v5 적용 완료" 를 찍었다 — `SCHEMA_VERSION` 상수를
    출력할 뿐 실제로 적용한 것이 아니었다. 그래서 005(communication→sns)가 이 기기에
    **한 번도 적용되지 않은 채** 남아 있었고, 006 이 추가한 `slot.private_sec` 컬럼이
    없어서 10분마다 도는 롤업이 통째로 실패했다.

    `init_db` 의 `CREATE TABLE IF NOT EXISTS` 는 **새 DB 에만** 충분하다. 이미 있는
    테이블에 컬럼을 더하는 것은 `migrate()` 만 할 수 있는데, 그걸 사람이 기억해서
    부르게 두면 안 부른다. 이 저장소가 아는 부류다 — *만들었다 ≠ 그게 불린다.*

    비용은 마커 조회 몇 번뿐이다(`_migration_applied` 가 `meta` 를 본다). 무거운
    마이그레이션도 마커로 한 번만 돈다.
    """
    conn = connect(cfg.db_path)
    init_db(conn)      # 빈 파일이면 여기서 baseline 까지 한다
    migrate(conn, cfg)  # 기존 DB 면 밀린 것만 돈다
    return conn


# ── 마이그레이션 러너 ────────────────────────────────────────────────────
#
# schema.sql 의 `CREATE TABLE IF NOT EXISTS` 는 완전히 새로운 DB 에는 충분하지만,
# 이미 존재하는 테이블에 컬럼을 추가하거나(ALTER TABLE) 기존 데이터의 좌표를
# 바꾸는 작업(예: 슬롯 좌표계 변경)은 처리하지 못한다. 그 증분을 여기서 다룬다.
# `lt migrate` 같은 CLI 배선은 이 모듈 담당이 아니다 — 다른 담당이 붙인다.


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """PRAGMA table_info 로 컬럼 존재 여부를 확인한다 (ALTER TABLE 을 멱등하게 만들기 위함).

    SQLite 의 ALTER TABLE ... ADD COLUMN 에는 "IF NOT EXISTS" 가 없어, 이미
    있는 컬럼에 다시 추가를 시도하면 OperationalError 가 난다.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _migration_applied(conn: sqlite3.Connection, name: str) -> bool:
    """meta 테이블에 'migration:<name>' 키가 있으면 이미 적용된 것으로 본다."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"migration:{name}",)).fetchone()
    return row is not None


def _mark_migration_applied(conn: sqlite3.Connection, name: str, now: float) -> None:
    conn.execute(
        "INSERT INTO meta(key, value, updated_at) VALUES (?, 'applied', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (f"migration:{name}", now),
    )


def _apply_002_day_boundary(
    conn: sqlite3.Connection, *, now: float, cfg: object | None = None
) -> None:
    """SCHEMA_VERSION 2: 논리적 하루 경계를 자정(0시)에서 06:00 으로 옮긴다.

    `migrations/002_day_boundary.sql` 에 사람이 읽을 수 있는 형태로 같은 내용을
    적어 두었다. ALTER TABLE 의 존재 확인과 slot_override 좌표 변환은 zoneinfo
    계산과 조건 분기가 필요해 순수 SQL 스크립트로 표현할 수 없으므로, 여기서
    그 내용을 그대로 파이썬으로 실행한다. 전부 하나의 트랜잭션 안에서 처리해
    중간에 실패해도 부분 적용 상태로 남지 않는다.
    """
    # cfg 가 있으면 설정된 타임존·슬롯 크기를 쓴다. 없을 때만 상수로 떨어진다.
    tz = getattr(cfg, "tz", None) or _MIGRATION_TZ
    slot_minutes = getattr(getattr(cfg, "rollup", None), "slot_minutes", _MIGRATION_SLOT_MINUTES)

    with transaction(conn) as tx:
        # 1) aw_bucket / slot_breakdown 에 device_id 컬럼 추가 — 이미 있으면 건너뛴다.
        #    (schema.sql 로 새로 만든 DB 는 CREATE TABLE 단계에서 이미 갖고 있어
        #    여기서는 no-op. 옛 스키마로 만들어진 기존 DB 만 실제로 컬럼이 늘어난다.)
        if not _column_exists(tx, "aw_bucket", "device_id"):
            tx.execute("ALTER TABLE aw_bucket ADD COLUMN device_id INTEGER REFERENCES device(id)")
        if not _column_exists(tx, "slot_breakdown", "device_id"):
            tx.execute("ALTER TABLE slot_breakdown ADD COLUMN device_id INTEGER REFERENCES device(id)")

        # 2) 기존 aw_bucket.host 마다 device 행을 만들고 aw_bucket.device_id 를 채운다.
        hosts = [row["host"] for row in tx.execute("SELECT DISTINCT host FROM aw_bucket").fetchall()]
        for host in hosts:
            tx.execute(
                "INSERT INTO device(name, kind, hostname, active, created_at) "
                "VALUES (?, 'laptop', ?, 1, ?) ON CONFLICT(name) DO NOTHING",
                (host, host, now),
            )
        tx.execute(
            "UPDATE aw_bucket SET device_id = "
            "(SELECT id FROM device WHERE device.name = aw_bucket.host) WHERE device_id IS NULL"
        )

        # 3) 파생 데이터(slot / slot_breakdown / unclassified_day) 비우기.
        #    aw_event(원본, epoch)에서 06:00 경계로 무손실 재생성되는 값들이다.
        #    plan_check 의 checked=1 을 plan_instance 로 이관하는 것은 V2 담당 몫이라
        #    여기서는 손대지 않는다.
        tx.execute("DELETE FROM slot")
        tx.execute("DELETE FROM slot_breakdown")
        tx.execute("DELETE FROM unclassified_day")

        # 4) slot_override 좌표 변환 — 사람이 넣은 입력이라 버리지 않고 좌표만 바꾼다.
        #    기존 (day, slot) 을 "자정 기준" 실제 시각(epoch)으로 복원한 뒤,
        #    그 시각을 "06:00 기준" 새 (day, slot) 으로 다시 계산한다.
        #
        #    한 번에 전부 읽어 새 좌표를 계산해 둔 다음, 지우고 다시 쓰는 두
        #    단계로 나눈다 — 옛 주소 공간과 새 주소 공간이 같은 (day, slot)
        #    형식을 공유하므로, 한 행씩 지우면서 바로 다시 쓰면 아직 처리하지
        #    않은 다른 행의 "옛" 좌표를 실수로 덮어쓸 수 있다.
        old_rows = tx.execute(
            "SELECT day, slot, category, note, actor, created_at FROM slot_override "
            "ORDER BY created_at ASC, day ASC, slot ASC"
        ).fetchall()
        new_rows: list[tuple] = []
        for row in old_rows:
            old_day_start, _ = timeutil.day_bounds(row["day"], tz, boundary_hour=_MIGRATION_OLD_BOUNDARY_HOUR)
            # 슬롯 중간 지점을 쓴다 — 슬롯 경계에 걸치는 부동소수 오차를 피한다.
            mid_ts = old_day_start + row["slot"] * slot_minutes * 60 + slot_minutes * 30
            new_day = timeutil.day_str(mid_ts, tz, boundary_hour=_MIGRATION_NEW_BOUNDARY_HOUR)
            new_slot = timeutil.slot_index(
                mid_ts, tz, slot_minutes=slot_minutes, boundary_hour=_MIGRATION_NEW_BOUNDARY_HOUR
            )
            new_rows.append((new_day, new_slot, row["category"], row["note"], row["actor"], row["created_at"]))

        if old_rows:
            tx.execute("DELETE FROM slot_override")
            # 같은 (day, slot) 으로 여러 옛 좌표가 모이는 경우(수학적으로는 일어나지
            # 않는 완전 전단사 변환이지만 방어적으로) created_at 오름차순으로 넣어
            # 나중 것이 이기게 한다.
            tx.executemany(
                "INSERT INTO slot_override(day, slot, category, note, actor, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(day, slot) DO UPDATE SET category = excluded.category, note = excluded.note, "
                "actor = excluded.actor, created_at = excluded.created_at",
                new_rows,
            )


def _apply_003_breakdown_device(conn: sqlite3.Connection, *, now: float) -> None:
    """SCHEMA_VERSION 3: `slot_breakdown` 의 기본키에 `device_id` 를 넣는다.

    002 가 컬럼만 더하고 **기본키는 그대로 뒀다.** 기기가 노트북 하나일 때는 드러나지
    않았지만, 폰이 들어오면 같은 10분 슬롯에서 두 기기가 같은 카테고리·같은 앱 이름을
    만들 수 있다(양쪽 'Chrome' → browsing). 그때 두 번째 INSERT 가 기본키 충돌로 죽는다.
    그래서 롤업이 `device_id` 를 아예 안 채우고 NULL 로 두고 있었다 — 컬럼은 있는데
    100% 비어 있던 이유다.

    SQLite 는 기본키를 ALTER 로 못 바꾸므로 테이블을 다시 만든다. `slot_breakdown` 은
    `aw_event` 에서 무손실로 재생성되는 파생 데이터라 옮기지 않고 비운다 —
    002 가 같은 이유로 같은 선택을 했다. 다음 롤업이 채운다.

    사람이 넣은 `slot_override` 는 건드리지 않는다.
    """
    sql = (_MIGRATIONS_DIR / "003_breakdown_device.sql").read_text(encoding="utf-8")
    with transaction(conn) as tx:
        tx.executescript(sql)
        # slot 도 함께 비운다 — breakdown 이 비었는데 slot 만 남으면 표와 합계가 어긋난다.
        tx.execute("DELETE FROM slot")


def _apply_004_doc_embedding(conn: sqlite3.Connection, *, now: float) -> None:
    """SCHEMA_VERSION 4: 문서 임베딩 테이블 (RAG).

    새로 만들기만 한다 — 기존 데이터를 건드리지 않으므로 되돌리기도 `DROP` 한 번이다.
    벡터는 파생물이라 백필은 `lt embed --backfill` 이 따로 한다.
    """
    sql = (_MIGRATIONS_DIR / "004_doc_embedding.sql").read_text(encoding="utf-8")
    with transaction(conn) as tx:
        tx.executescript(sql)


def _apply_005_sns_category(conn: sqlite3.Connection, *, now: float) -> None:
    """SCHEMA_VERSION 5: 카테고리 `communication` 을 `sns` 로 바꾼다.

    사용자 요청이다 — 메신저·SNS·커뮤니티가 한 범주인데 라벨이 '소통'이라
    무엇을 본 시간인지 읽히지 않았다. 개인별 앱 이름·사용량은 공개본에서 제거했다.

    ★ **파생 테이블만 바꾸는 게 아니다.** `slot_override`·`manual_entry`·`plan` 은
    사람이 넣은 원본이라 롤업이 다시 쓰지 않는다 — 여기서 안 바꾸면 그 행들만
    영영 `communication` 으로 남아 어느 화면에서도 색이 안 붙는다.
    """
    sql = (_MIGRATIONS_DIR / "005_sns_category.sql").read_text(encoding="utf-8")
    with transaction(conn) as tx:
        tx.executescript(sql)


def _apply_006_private_span(conn: sqlite3.Connection, *, now: float) -> None:
    """SCHEMA_VERSION 6: 프라이빗 구간 — 이 시간은 재지 않기로 한 것.

    ★ 002·003 과 달리 **파생 테이블을 비우지 않는다.** 새 테이블과 기본값 0 인 새 컬럼뿐이라
    기존 롤업 결과가 그대로 맞다. 재계산이 필요 없으니 비우면 손해만 본다.

    `ALTER TABLE` 은 두 번 돌면 실패하므로 `_column_exists` 로 감싼다 — `migrate()` 는
    여러 번 불려도 안전해야 한다.
    """
    with transaction(conn) as tx:
        tx.executescript(
            (_MIGRATIONS_DIR / "006_private_span.sql")
            .read_text(encoding="utf-8")
            .split("ALTER TABLE")[0]
        )
        if not _column_exists(tx, "slot", "private_sec"):
            tx.execute("ALTER TABLE slot ADD COLUMN private_sec REAL NOT NULL DEFAULT 0")


def _apply_007_collapse_clipped_events(conn: sqlite3.Connection, *, now: float) -> None:
    """잘린 판들을 한 행으로 합친다 (issues/0027).

    aw-server 는 이벤트를 질의 창에 맞춰 잘라서 준다. 우리 PK 가 `(bucket_id, ts)` 라
    잘린 판마다 새 행이 생겼다 — PC afk 버킷 총합이 합집합보다 크게 부풀었다.
    앞으로 안 생기게 하는 것은 `aw_sync._absorb_clipped_view` 가 하고,
    **이미 쌓인 것을 걷어내는 것이 이 마이그레이션이다.**

    ★ `event_id` 가 같아도 **겹치지 않으면 안 건드린다.** id 는 재사용된다.
    """
    rows = conn.execute(
        "SELECT bucket_id, event_id FROM aw_event WHERE event_id IS NOT NULL "
        "GROUP BY bucket_id, event_id HAVING count(*) > 1"
    ).fetchall()

    merged = 0
    with transaction(conn) as tx:
        for r in rows:
            bid, eid = r["bucket_id"], r["event_id"]
            evs = tx.execute(
                "SELECT ts, ts_end FROM aw_event WHERE bucket_id = ? AND event_id = ? ORDER BY ts",
                (bid, eid),
            ).fetchall()
            # 겹치거나 맞닿는 것끼리만 묶는다 (한 덩어리로 안 이어지면 건드리지 않는다)
            lo, hi = float(evs[0]["ts"]), float(evs[0]["ts_end"])
            contiguous = True
            for e in evs[1:]:
                if float(e["ts"]) > hi:
                    contiguous = False
                    break
                hi = max(hi, float(e["ts_end"]))
            if not contiguous:
                continue
            # 가장 넓은 행 하나만 남기고 나머지를 지운 뒤 그 행을 합집합으로 넓힌다
            tx.execute(
                "DELETE FROM aw_event WHERE bucket_id = ? AND event_id = ? AND ts != ?",
                (bid, eid, evs[0]["ts"]),
            )
            tx.execute(
                "UPDATE aw_event SET ts_end = ?, duration = ? WHERE bucket_id = ? AND ts = ?",
                (hi, hi - lo, bid, evs[0]["ts"]),
            )
            merged += 1
    logger.info("007: 잘린 판 %d 그룹을 합쳤다", merged)


def _apply_008_doc_digested_at(conn: sqlite3.Connection, *, now: float) -> None:
    """SCHEMA_VERSION 8: 다이제스트로 나간 문서에 표식 (`doc.digested_at`).

    아침 다이제스트가 8일 연속 같은 문서를 보냈다. 최신성 보정이 채점 시점에 굳은 것이
    한 축이고(`collect/score.py`), **보낸 것을 거를 방법이 없던 것**이 다른 한 축이다.

    ★ 006 과 같은 부류다 — 새 컬럼 하나뿐이라 **파생 테이블을 비우지 않는다.**
      `ALTER TABLE` 은 두 번 돌면 실패하므로 `_column_exists` 로 감싼다.

    ★ **인덱스를 안 만든다.** 처음엔 `idx_doc_digested` 를 두고 schema.sql 에도
      같이 적었는데, `open_db` 는 `init_db`(schema.sql) 를 **migrate 보다 먼저** 부른다.
      옛 DB 에는 아직 컬럼이 없으니 그 인덱스 한 줄에서 schema.sql 이 통째로 깨졌다
      (`lt backup` 이 즉시 실패했다). 006 은 컬럼만 더해서 이 함정을 안 밟았다.
      **마이그레이션이 더한 컬럼을 참조하는 인덱스를 schema.sql 에 두면 안 된다.**
      그리고 이 인덱스는 값도 없었다 — 다이제스트 질의는 계산식으로 정렬하느라
      어차피 훑고, `digested_at` 은 거의 전부 NULL 이다.
    """
    with transaction(conn) as tx:
        if not _column_exists(tx, "doc", "digested_at"):
            tx.execute("ALTER TABLE doc ADD COLUMN digested_at REAL")


def _apply_009_doc_body_fetch_failed(conn: sqlite3.Connection, *, now: float) -> None:
    """SCHEMA_VERSION 9: 본문 수집 실패 표식 (`doc.body_fetch_failed_at`).

    `body_path IS NULL` 만으로는 **"아직 안 받아 봤다"** 와 **"받아 봤는데 안 됐다"** 가
    구분되지 않는다. 구분이 없으면 다음 배치가 같은 URL 을 또 두드린다.

    ★ 008 과 같이 **인덱스를 만들지 않는다** — `open_db` 가 schema.sql 을 migrate 보다
      먼저 붓기 때문이다 (008 머리말).
    """
    with transaction(conn) as tx:
        if not _column_exists(tx, "doc", "body_fetch_failed_at"):
            tx.execute("ALTER TABLE doc ADD COLUMN body_fetch_failed_at REAL")


def _apply_010_purged_event(conn: sqlite3.Connection, *, now: float) -> None:
    """SCHEMA_VERSION 10: 지운 이벤트가 옮겨 가는 휴지통 (`purged_event`).

    `purge` 가 행을 진짜 지워서 **되돌릴 방법이 없었다.** `private_span.revoked` 는
    컬럼만 있고 1 로 만드는 코드가 없었다 — 약속만 있고 길이 없는 상태였다.

    ★ `aw_event.deleted_at` 플래그로 만들려다 **테이블을 나눴다.** 읽는 쪽 12곳이
      전부 조건을 달아야 하는데 하나만 빠뜨리면 지운 것이 그 화면에만 나타나고,
      게다가 PK 가 `(bucket_id, ts)` 라 걸친 이벤트의 왼쪽 조각이 원본과 충돌했다.
      경위는 `migrations/010_purged_event.sql` 머리말.

    새 테이블 하나뿐이라 **파생 테이블을 비우지 않는다** (006 과 같은 부류).
    """
    with transaction(conn) as tx:
        tx.executescript((_MIGRATIONS_DIR / "010_purged_event.sql").read_text(encoding="utf-8"))


def _apply_011_plan_instance_skipped_at(conn: sqlite3.Connection, *, now: float) -> None:
    """SCHEMA_VERSION 11: `plan_instance.skipped_at` — "오늘만 건너뛰기" 가 실제로 듣게.

    열 하나만 더한다. 파생 테이블을 비우지 않는다 (006·010 과 같은 부류).
    경위는 `migrations/011_plan_instance_skipped_at.sql` 머리말과 `docs/issues/0026`.
    """
    # ★ 008·009 와 같은 이유로 **열 존재를 먼저 본다.** `open_db` 는 schema.sql 을
    #   migrate 보다 먼저 붓는다 — 갓 만든 DB 에는 이미 열이 있고, 그대로 ALTER 하면
    #   `duplicate column name` 으로 죽는다 (테스트 6개가 이걸 잡았다).
    with transaction(conn) as tx:
        if not _column_exists(tx, "plan_instance", "skipped_at"):
            tx.execute("ALTER TABLE plan_instance ADD COLUMN skipped_at REAL")


_MIGRATIONS: tuple[tuple[str, object], ...] = (
    # ★ 이름 목록은 **여기 한 곳**이다. `migrate()` 와 `baseline_migrations()` 가 같은
    #   목록을 봐야 새 DB 에 표식을 빠뜨리지 않는다. 전에는 `migrate()` 안의 if 블록이
    #   유일한 목록이라 새 DB 를 표식 없이 만들었고, 그게 아래 baseline 이 고치는 사고다.
    #   (같은 부류: `operate/systemd/desired-state.txt` — 목록을 스크립트에 박지 않는다)
    ("002_day_boundary", lambda conn, now, cfg: _apply_002_day_boundary(conn, now=now, cfg=cfg)),
    ("003_breakdown_device", lambda conn, now, cfg: _apply_003_breakdown_device(conn, now=now)),
    ("004_doc_embedding", lambda conn, now, cfg: _apply_004_doc_embedding(conn, now=now)),
    ("005_sns_category", lambda conn, now, cfg: _apply_005_sns_category(conn, now=now)),
    ("006_private_span", lambda conn, now, cfg: _apply_006_private_span(conn, now=now)),
    ("007_collapse_clipped_events", lambda conn, now, cfg: _apply_007_collapse_clipped_events(conn, now=now)),
    ("008_doc_digested_at", lambda conn, now, cfg: _apply_008_doc_digested_at(conn, now=now)),
    ("009_doc_body_fetch_failed", lambda conn, now, cfg: _apply_009_doc_body_fetch_failed(conn, now=now)),
    ("010_purged_event", lambda conn, now, cfg: _apply_010_purged_event(conn, now=now)),
    ("011_plan_instance_skipped_at", lambda conn, now, cfg: _apply_011_plan_instance_skipped_at(conn, now=now)),
)


def baseline_migrations(conn: sqlite3.Connection, *, now: float | None = None) -> int:
    """**갓 만든** DB 에 마이그레이션을 "이미 적용됨"으로 표시한다. 반환값은 표식 수.

    `schema.sql` 은 항상 **모든 마이그레이션이 끝난 뒤의 모양**이다. 그러니 빈 파일에
    schema.sql 을 부으면 그 DB 는 이미 최신이고, 마이그레이션을 *돌리면 안 된다*.

    ★ 2026-09-01 사고: `open_db` 가 `migrate()` 를 부르기 시작하자 **새로 만든 DB 마다
      002·003 이 돌았다.** 그 둘은 하루 경계가 바뀌어 재계산이 필요하다는 뜻으로
      `slot`·`slot_breakdown` 을 **비운다.** 그래서 방금 만든 DB 에 슬롯을 넣고 읽으면
      격자가 통째로 비었다(웹 테스트 5개가 이걸 잡았다).

    Alembic `stamp head` · Rails `db:schema:load` 뒤의 `schema_migrations` 삽입과 같은 것이다.
    """
    now = time.time() if now is None else now
    for name, _ in _MIGRATIONS:
        _mark_migration_applied(conn, name, now)
    conn.execute(
        "INSERT INTO meta(key, value, updated_at) VALUES ('schema_version', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (str(SCHEMA_VERSION), now),
    )
    conn.commit()
    return len(_MIGRATIONS)


def migrate(conn: sqlite3.Connection, cfg: object | None = None) -> int:
    """대기 중인 마이그레이션을 순서대로 적용한다. 반환값은 이번에 적용한 개수.

    이미 적용된 마이그레이션은 meta 테이블의 'migration:<이름>' 키로 판정해
    건너뛴다 — 여러 번 호출해도 안전하다(멱등). `open_db(cfg)` 로 연결을 연
    뒤에 호출하는 것을 전제로 한다(스키마 자체는 이미 적용돼 있어야 한다).

    `cfg` 를 주면 그 타임존과 슬롯 크기로 좌표를 변환한다. 안 주면 `_MIGRATION_TZ`
    (Asia/Seoul) 로 떨어진다 — 설정 타임존이 다른데 cfg 없이 부르면 slot_override
    좌표가 **조용히 어긋난다.** 호출부는 되도록 cfg 를 넘길 것.
    """
    applied = 0
    now = time.time()

    for name, apply in _MIGRATIONS:
        if _migration_applied(conn, name):
            continue
        apply(conn, now, cfg)
        _mark_migration_applied(conn, name, now)
        applied += 1

    # ★ **`if applied:` 안에 두면 안 된다.** 그러면 마이그레이션이 이미 다 적용된 DB 의
    #   기록만 뒤처진 상태를 아무도 못 고친다 — 2026-09-04 에 정확히 그랬다.
    #   옛 프로세스가 meta 를 7 로 되돌려 놨는데, 008·009 는 이미 `applied` 라
    #   `migrate()` 를 몇 번을 불러도 `applied == 0` 이라 도장을 안 찍었다.
    #   `lt doctor` 는 **고칠 방법이 없는 WARN** 을 계속 띄웠다 (CLAUDE.md §1 의 1번).
    #
    # ★ **올리기만 한다.** 옛 코드가 새 DB 를 열었을 때 버전을 낮추면 안 된다 —
    #   그건 방금 고친 사고와 같은 방향이다.
    if SCHEMA_VERSION > schema_version(conn):
        conn.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES ('schema_version', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (str(SCHEMA_VERSION), now),
        )
    if applied or SCHEMA_VERSION > schema_version(conn):
        conn.commit()

    return applied
