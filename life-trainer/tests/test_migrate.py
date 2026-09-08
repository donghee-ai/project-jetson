"""lifetrainer.db 의 마이그레이션 러너(SCHEMA_VERSION 2 = 논리적 하루 경계 06:00) 테스트.

전부 임시 SQLite 파일/메모리 DB 에서 돈다. 네트워크 없음. 실제 운영
DB(data/lifetrainer.db)에 대한 마이그레이션+재롤업 검증은 이 테스트 스위트가
아니라 완료 후 수동 절차(README/보고 참고)로 별도 수행한다.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from lifetrainer import db
from lifetrainer import timeutil
from lifetrainer.config import load_config

SEOUL = ZoneInfo("Asia/Seoul")


# ── 픽스처 헬퍼 ───────────────────────────────────────────────────────────



def _pending_count(conn) -> int:
    """이번에 적용됐어야 할 마이그레이션 수 — **세지 말고 물어본다.**

    전에는 `== 4  # 002 + 003 + 004 + 005` 라고 적혀 있었다. 마이그레이션을 하나 더할
    때마다 이 숫자를 다섯 곳에서 고쳐야 했고, 006 을 더할 때 실제로 다섯 곳이 깨졌다.
    적용된 표식을 세면 개수가 저절로 따라온다 — 단언하려는 것도 "숫자가 4다"가 아니라
    "밀린 것이 전부 적용됐다"이다.
    """
    return conn.execute(
        "SELECT count(*) FROM meta WHERE key LIKE 'migration:%'"
    ).fetchone()[0]


def _legacy_conn(tmp_path: Path) -> sqlite3.Connection:
    """"확장 전 스키마"를 흉내낸 연결을 만든다.

    schema.sql 은 이미 device/device_id 를 포함한 최신본이라(읽기 전용, 고칠 수
    없음) 정상적으로 만들면 처음부터 컬럼이 있다. 마이그레이션이 실제로
    ALTER TABLE 을 타는 경로를 검증하려면 방금 만든 DB에서 그 두 컬럼을
    일부러 떼어내 "아직 확장되지 않은 기존 DB" 상태로 되돌려야 한다 —
    실제로 저장소의 data/lifetrainer.db 가 지금 이 상태다.
    """
    conn = db.connect(tmp_path / "legacy.db")
    db.init_db(conn)
    # init_db 는 새 DB 를 "마이그레이션 다 끝난 상태"로 찍는다(baseline). 여기서는
    # **그 이전의 DB** 를 흉내내는 것이므로 표식을 도로 뗀다 — 컬럼을 떼는 것과 같은 이유다.
    conn.execute("DELETE FROM meta WHERE key LIKE 'migration:%'")
    conn.execute("ALTER TABLE aw_bucket DROP COLUMN device_id")
    # slot_breakdown 은 003 이후 device_id 가 **기본키의 일부**라 DROP COLUMN 이 막힌다.
    # 옛 모양(컬럼 없음 · PK 에 기기 없음)으로 테이블을 통째로 다시 만든다.
    conn.executescript(
        """
        DROP TABLE slot_breakdown;
        CREATE TABLE slot_breakdown (
            day      TEXT NOT NULL,
            slot     INTEGER NOT NULL,
            category TEXT NOT NULL,
            app      TEXT NOT NULL DEFAULT '',
            seconds  REAL NOT NULL,
            PRIMARY KEY (day, slot, category, app)
        );
        """
    )
    conn.execute("DELETE FROM meta WHERE key LIKE 'migration:%'")
    return conn


def _insert_bucket(conn: sqlite3.Connection, bucket_id: str, host: str, btype: str = "window") -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO aw_bucket(bucket_id, host, client, type, hostname, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bucket_id, host, btype, btype, host, now, now),
    )


def _insert_override(
    conn: sqlite3.Connection,
    day: str,
    slot: int,
    category: str,
    *,
    note: str | None = None,
    actor: str = "web",
    created_at: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO slot_override(day, slot, category, note, actor, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (day, slot, category, note, actor, created_at if created_at is not None else time.time()),
    )


# ── ALTER TABLE 멱등성 (PRAGMA table_info 확인) ──────────────────────────


def test_migrate_adds_missing_device_id_columns(tmp_path):
    conn = _legacy_conn(tmp_path)
    assert not db._column_exists(conn, "aw_bucket", "device_id")
    assert not db._column_exists(conn, "slot_breakdown", "device_id")

    applied = db.migrate(conn)

    assert applied == _pending_count(conn)
    assert db._column_exists(conn, "aw_bucket", "device_id")
    assert db._column_exists(conn, "slot_breakdown", "device_id")


def test_migrate_does_not_error_when_columns_already_present(tmp_path):
    # 최신 schema.sql 로 막 만든 DB 는 처음부터 device_id 가 있다. ALTER TABLE 을
    # 조건 없이 실행하면 "duplicate column" 에러가 나야 정상인데, PRAGMA 로
    # 먼저 확인하기 때문에 에러 없이 끝나야 한다.
    conn = db.connect(tmp_path / "fresh.db")
    db.init_db(conn)
    assert db._column_exists(conn, "aw_bucket", "device_id")

    # init_db 가 baseline 을 찍으므로 그냥 두면 migrate 가 아무것도 안 돈다.
    # 여기서 보려는 건 "컬럼이 이미 있을 때 ALTER 가 터지지 않는가" 이므로 강제로 돌린다.
    conn.execute("DELETE FROM meta WHERE key LIKE 'migration:%'")
    applied = db.migrate(conn)  # 에러 없이 끝나야 한다.
    assert applied == _pending_count(conn)


# ── device 차원 생성 + aw_bucket 연결 ────────────────────────────────────


def test_migrate_creates_device_rows_and_links_aw_bucket(tmp_path):
    conn = _legacy_conn(tmp_path)
    _insert_bucket(conn, "aw-watcher-window_host-a", "host-a")
    _insert_bucket(conn, "aw-watcher-afk_host-a", "host-a", btype="afk")
    _insert_bucket(conn, "aw-watcher-window_host-b", "host-b")

    db.migrate(conn)

    devices = {r["name"]: r for r in conn.execute("SELECT * FROM device").fetchall()}
    assert set(devices) == {"host-a", "host-b"}
    assert devices["host-a"]["kind"] == "laptop"
    assert devices["host-a"]["active"] == 1

    buckets = conn.execute("SELECT bucket_id, host, device_id FROM aw_bucket").fetchall()
    assert len(buckets) == 3
    for b in buckets:
        assert b["device_id"] is not None
        assert b["device_id"] == devices[b["host"]]["id"]


def test_migrate_device_backfill_is_idempotent_across_hosts(tmp_path):
    # 같은 호스트를 쓰는 버킷이 여러 개여도 device 행은 하나만 만든다.
    conn = _legacy_conn(tmp_path)
    _insert_bucket(conn, "aw-watcher-window_host-a", "host-a")
    _insert_bucket(conn, "aw-watcher-afk_host-a", "host-a", btype="afk")
    _insert_bucket(conn, "aw-watcher-web-chrome_host-a", "host-a", btype="web")

    db.migrate(conn)

    count = conn.execute("SELECT COUNT(*) AS c FROM device WHERE name = 'host-a'").fetchone()["c"]
    assert count == 1


# ── 파생 테이블(slot/slot_breakdown/unclassified_day) 비우기 ────────────


def test_migrate_clears_derived_tables(tmp_path):
    conn = _legacy_conn(tmp_path)
    now = time.time()
    conn.execute(
        "INSERT INTO slot(day, slot, start_ts, category, active_sec, afk_sec, gap_sec, winner_sec, updated_at) "
        "VALUES ('2026-08-16', 0, 0, 'coding', 0, 0, 0, 0, ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO slot_breakdown(day, slot, category, app, seconds) VALUES ('2026-08-16', 0, 'coding', 'x', 10)"
    )
    conn.execute("INSERT INTO unclassified_day(day, fingerprint, seconds, hits) VALUES ('2026-08-16', 'fp', 10, 1)")

    db.migrate(conn)

    assert conn.execute("SELECT COUNT(*) AS c FROM slot").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM slot_breakdown").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM unclassified_day").fetchone()["c"] == 0


def test_migrate_does_not_touch_plan_check(tmp_path):
    # plan_check -> plan_instance 이관은 V2 몫이다. 이 마이그레이션은 손대지 않는다.
    conn = _legacy_conn(tmp_path)
    now = time.time()
    conn.execute(
        "INSERT INTO plan(id, title, start_min, end_min, created_at, updated_at) "
        "VALUES (1, 'test', 0, 60, ?, ?)",
        (now, now),
    )
    conn.execute("INSERT INTO plan_check(plan_id, day, checked, checked_at) VALUES (1, '2026-08-16', 1, ?)", (now,))

    db.migrate(conn)

    row = conn.execute("SELECT checked FROM plan_check WHERE plan_id = 1 AND day = '2026-08-16'").fetchone()
    assert row["checked"] == 1


# ── slot_override 좌표 왕복 검증 (계약서 §2 핵심) ─────────────────────────


def test_migrate_slot_override_roundtrip_same_wallclock_moment(tmp_path):
    conn = _legacy_conn(tmp_path)
    day = "2026-08-16"
    slot = 40  # 옛(자정 기준) 좌표로 06:40~06:50
    _insert_override(conn, day, slot, "exercise", note="헬스장", actor="web")

    # 옛 좌표가 가리키던 실제 시각(슬롯 중간점).
    old_start, _ = timeutil.day_bounds(day, SEOUL, boundary_hour=0)
    expected_mid = old_start + slot * 10 * 60 + 5 * 60

    db.migrate(conn)

    rows = conn.execute("SELECT day, slot, category, note, actor FROM slot_override").fetchall()
    assert len(rows) == 1
    row = rows[0]
    # 사람이 입력한 내용은 좌표만 바뀌고 그대로 보존된다.
    assert row["category"] == "exercise"
    assert row["note"] == "헬스장"
    assert row["actor"] == "web"

    # 새(06:00 기준) 좌표가 감싸는 구간 안에 옛 좌표가 가리키던 실제 시각이 그대로 들어있다.
    new_s, new_e = timeutil.slot_bounds(row["day"], row["slot"], SEOUL, 10, boundary_hour=6)
    assert new_s <= expected_mid < new_e

    # timeutil 이 그 실제 시각에서 직접 계산한 (day, slot) 과도 정확히 일치해야 한다.
    assert row["day"] == timeutil.day_str(expected_mid, SEOUL, boundary_hour=6)
    assert row["slot"] == timeutil.slot_index(expected_mid, SEOUL, slot_minutes=10, boundary_hour=6)


def test_migrate_slot_override_crossing_midnight_moves_to_previous_logical_day(tmp_path):
    # 옛 좌표로 자정 직후(슬롯 2 = 00:20~00:30)에 넣은 오버라이드는
    # 06:00 경계 기준으로는 "전날 저녁~새벽"에 속해야 한다.
    conn = _legacy_conn(tmp_path)
    day = "2026-08-16"
    slot = 2
    _insert_override(conn, day, slot, "sleep")

    db.migrate(conn)

    rows = conn.execute("SELECT day, slot, category FROM slot_override").fetchall()
    assert len(rows) == 1
    assert rows[0]["day"] == "2026-08-15"
    assert rows[0]["category"] == "sleep"


def test_migrate_slot_override_multiple_rows_all_preserved(tmp_path):
    conn = _legacy_conn(tmp_path)
    _insert_override(conn, "2026-08-16", 0, "coding")
    _insert_override(conn, "2026-08-16", 80, "exercise")
    _insert_override(conn, "2026-08-17", 143, "reading")

    db.migrate(conn)

    rows = conn.execute("SELECT * FROM slot_override").fetchall()
    assert len(rows) == 3  # 아무것도 잃지 않았다 — 사람이 넣은 입력이라 버리면 안 된다.
    assert {r["category"] for r in rows} == {"coding", "exercise", "reading"}


def test_migrate_no_slot_override_rows_is_a_noop(tmp_path):
    conn = _legacy_conn(tmp_path)
    applied = db.migrate(conn)
    assert applied == _pending_count(conn)
    assert conn.execute("SELECT COUNT(*) AS c FROM slot_override").fetchone()["c"] == 0


# ── 멱등성: migrate(conn) 을 두 번 불러도 안전한지 ────────────────────────


def test_migrate_second_call_returns_zero(tmp_path):
    conn = _legacy_conn(tmp_path)
    assert db.migrate(conn) == _pending_count(conn)
    assert db.migrate(conn) == 0


def test_migrate_idempotent_data_unchanged_on_second_call(tmp_path):
    conn = _legacy_conn(tmp_path)
    _insert_bucket(conn, "aw-watcher-window_host-a", "host-a")
    _insert_override(conn, "2026-08-16", 40, "exercise", note="헬스장")

    db.migrate(conn)
    after_first = {
        "override": [tuple(r) for r in conn.execute("SELECT day, slot, category, note FROM slot_override")],
        "device": [tuple(r) for r in conn.execute("SELECT name, kind FROM device ORDER BY name")],
        "bucket": [tuple(r) for r in conn.execute("SELECT bucket_id, device_id FROM aw_bucket ORDER BY bucket_id")],
    }

    db.migrate(conn)  # 두 번째 호출 — 이미 적용됐으니 아무것도 다시 하면 안 된다.
    after_second = {
        "override": [tuple(r) for r in conn.execute("SELECT day, slot, category, note FROM slot_override")],
        "device": [tuple(r) for r in conn.execute("SELECT name, kind FROM device ORDER BY name")],
        "bucket": [tuple(r) for r in conn.execute("SELECT bucket_id, device_id FROM aw_bucket ORDER BY bucket_id")],
    }

    assert after_first == after_second


def test_migrate_returns_zero_on_freshly_initialized_db_after_first_call():
    # open_db 를 두 번 부르는 것과 같은 상황(서비스가 재시작될 때마다 일어남) —
    # 매번 슬롯을 지웠다 다시 계산하게 만들면 안 된다.
    #
    # ★ 새 DB 는 schema.sql 로 이미 최신이므로 **한 번도** 안 돌아야 한다(baseline).
    #   전에는 첫 호출에서 002·003 이 돌았는데, 그 둘이 파생 테이블을 비우기 때문에
    #   "방금 만든 DB 에 슬롯을 넣고 읽으면 비어 있다"가 됐다.
    conn = db.connect(":memory:")
    db.init_db(conn)
    assert _pending_count(conn) == len(db._MIGRATIONS), "새 DB 에 표식이 안 찍혔다"
    assert db.migrate(conn) == 0
    db.init_db(conn)  # open_db()가 매번 하는 일 — 멱등해야 하고 마이그레이션 상태를 안 건드려야 한다.
    assert db.migrate(conn) == 0


def test_새_db_에_넣은_슬롯은_다시_열어도_남아_있다(tmp_path):
    """★ 2026-09-01 회귀. `open_db` 가 `migrate()` 를 부르기 시작하면서 깨졌던 것.

    새로 만든 DB 는 마이그레이션 표식이 없었고, 그래서 다음 `open_db` 가 그것을
    "오래된 DB"로 보고 002·003 을 돌려 `slot` 을 **통째로 비웠다.** 웹 격자가 빈
    채로 렌더됐다 — 오류 없이, 조용히.
    """
    cfg = dataclasses.replace(load_config(), db_path=tmp_path / "fresh.db")
    conn = db.open_db(cfg)
    conn.execute(
        "INSERT INTO slot(day, slot, start_ts, category, active_sec, afk_sec, gap_sec, winner_sec, updated_at) "
        "VALUES ('2026-09-01', 0, 0, 'coding', 600, 0, 0, 600, 0)"
    )
    conn.commit()
    conn.close()

    conn = db.open_db(cfg)  # 서비스 재시작 · 다음 요청
    assert conn.execute("SELECT count(*) AS c FROM slot").fetchone()["c"] == 1


def test_schema_sql_은_마이그레이션이_더할_것을_이미_갖고_있다(tmp_path):
    """baseline 이 안전하려면 **schema.sql 이 마이그레이션 뒤의 모양**이어야 한다.

    이 검사가 없으면: 007 을 만들면서 `schema.sql` 을 안 고쳐도 아무도 안 운다.
    기존 DB 는 마이그레이션이 컬럼을 더해 주고, 새 DB 는 baseline 때문에 그걸 건너뛰어
    **컬럼 없는 채로 최신이라고 표시된다.** 어긋남이 새 기기에서만 터진다.

    언제 안 우나: schema.sql 이 마이그레이션과 같은 모양이면 마이그레이션은 구조를
    바꿀 게 없다 — 표식만 떼고 다시 돌려도 `PRAGMA table_info` 가 그대로다.
    """
    cfg = dataclasses.replace(load_config(), db_path=tmp_path / "shape.db")
    conn = db.open_db(cfg)
    shape = _schema_shape(conn)

    conn.execute("DELETE FROM meta WHERE key LIKE 'migration:%'")
    db.migrate(conn, cfg)

    assert _schema_shape(conn) == shape, "schema.sql 이 마이그레이션과 어긋났다"


def _schema_shape(conn) -> dict[str, list[tuple]]:
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {
        t: [(r["name"], r["type"], r["notnull"], r["pk"]) for r in conn.execute(f"PRAGMA table_info({t})")]
        for t in tables
    }


# ── 007: 잘린 판 합치기 (issues/0027) ────────────────────────────────────


def _seed_event(conn, bucket, ts, dur, eid):
    conn.execute(
        "INSERT INTO aw_event(bucket_id, ts, ts_end, duration, event_id, data_json, synced_at) "
        "VALUES (?, ?, ?, ?, ?, '{}', 0)",
        (bucket, ts, ts + dur, dur, eid),
    )


def _seed_bucket(conn, bucket="b1"):
    conn.execute(
        "INSERT OR IGNORE INTO aw_bucket(bucket_id, host, client, type, hostname, first_seen, last_seen) "
        "VALUES (?, 'h', 'c', 'afkstatus', 'h', 0, 0)",
        (bucket,),
    )


def _events(conn, bucket="b1"):
    return [
        (round(r["ts"], 1), round(r["ts_end"], 1), r["event_id"])
        for r in conn.execute(
            "SELECT ts, ts_end, event_id FROM aw_event WHERE bucket_id=? ORDER BY ts", (bucket,)
        )
    ]


def _migrated(tmp_path, name, seed):
    """새 DB 를 만들고 007 표식만 떼어 그 마이그레이션만 다시 돌린다."""
    cfg = dataclasses.replace(load_config(), db_path=tmp_path / name)
    conn = db.open_db(cfg)
    _seed_bucket(conn)
    seed(conn)
    conn.commit()
    conn.execute("DELETE FROM meta WHERE key = 'migration:007_collapse_clipped_events'")
    assert db.migrate(conn, cfg) == 1
    return conn


def test_007_잘린_판을_한_행으로_합친다(tmp_path):
    """실측 그대로의 모양: 같은 id, 끝이 같고 시작만 밀린 조각들."""
    def seed(c):
        _seed_event(c, "b1", 1000.0, 900.0, 7)   # 원본
        _seed_event(c, "b1", 1300.0, 600.0, 7)   # 창이 300초 밀린 판
        _seed_event(c, "b1", 1600.0, 300.0, 7)   # 600초 밀린 판

    conn = _migrated(tmp_path, "clip.db", seed)
    assert _events(conn) == [(1000.0, 1900.0, 7)]
    conn.close()


def test_007_재사용된_id_는_안_건드린다(tmp_path):
    """★ 안전장치. `aw-watcher-android` 에서 id 가 5,152회 뒤로 점프한다."""
    def seed(c):
        _seed_event(c, "b1", 1000.0, 0.0, 7)     # 0초짜리
        _seed_event(c, "b1", 5000.0, 900.0, 7)   # 한참 뒤, 같은 id

    conn = _migrated(tmp_path, "reuse.db", seed)
    assert _events(conn) == [(1000.0, 1000.0, 7), (5000.0, 5900.0, 7)]
    conn.close()


def test_007_두_번_돌아도_같고_파생_테이블을_안_비운다(tmp_path):
    def seed(c):
        _seed_event(c, "b1", 1000.0, 900.0, 7)
        _seed_event(c, "b1", 1600.0, 300.0, 7)
        c.execute(
            "INSERT INTO slot(day, slot, start_ts, category, active_sec, afk_sec, gap_sec, "
            "winner_sec, updated_at) VALUES ('2026-09-01', 0, 0, 'coding', 600, 0, 0, 600, 0)"
        )

    conn = _migrated(tmp_path, "idem.db", seed)
    first = _events(conn)
    conn.execute("DELETE FROM meta WHERE key = 'migration:007_collapse_clipped_events'")
    db.migrate(conn)
    assert _events(conn) == first
    assert conn.execute("SELECT count(*) AS c FROM slot").fetchone()["c"] == 1, (
        "007 은 원천만 합친다 — 롤업은 어차피 슬롯마다 잘라 흡수하고 있었다"
    )
    conn.close()


def test_init_db_does_not_move_the_recorded_version_backwards(tmp_path):
    """★ 옛 코드를 들고 떠 있는 프로세스가 버전을 되돌리면 안 된다.

    `init_db` 는 `open_db` 마다 불린다. 전에는 매번 **그 프로세스의 상수**로
    `meta.schema_version` 을 갈아엎었다. 2026-09-04 에 008·009 를 적용해 9 가 됐는데,
    `SCHEMA_VERSION=7` 을 메모리에 들고 며칠째 떠 있던 web·worker·slack 이 다음
    요청에서 7 로 되돌려 놨다.

    증상은 `lt doctor` 의 *"마이그레이션 확인 필요"* 인데, **마이그레이션을 다시 돌려도
    안 꺼진다** — 원인이 다른 프로세스라서다. 안 꺼지는 신호는 신호가 아니다
    (CLAUDE.md §1 의 1번).
    """
    from lifetrainer import db

    conn = db.connect(tmp_path / "lt.db")
    db.init_db(conn)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()

    db.init_db(conn)  # 옛 상수를 든 프로세스가 다시 여는 것과 같은 모양

    assert db.schema_version(conn) == 99, "init_db 가 기록된 버전을 되돌렸다"
    conn.close()


def test_migrate_stamps_the_version_even_when_nothing_new_applies(tmp_path):
    """★ 기록만 뒤처진 DB 를 고칠 수 있어야 한다.

    도장을 `if applied:` 안에 두면, 마이그레이션은 다 적용됐는데 `meta` 만 뒤처진 상태를
    **아무도 못 고친다** — `migrate()` 를 몇 번 불러도 `applied == 0` 이라 그냥 지나간다.
    2026-09-04 에 그 상태가 됐고, `lt doctor` 가 *"마이그레이션 확인 필요"* 를 띄웠는데
    **띄운 대로 해도 안 꺼졌다.** 원인을 고쳐도 안 꺼지는 신호가 그것이다.
    """
    from lifetrainer import db

    conn = db.connect(tmp_path / "lt.db")
    db.init_db(conn)
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()

    assert db.migrate(conn) == 0, "이 DB 에 새로 적용할 것은 없어야 한다"
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    conn.close()


# ── 상수와 목록이 같이 움직이는가 (2026-09-07) ────────────────────────────


def test_schema_version_와_마이그레이션_목록이_어긋나지_않는다():
    """★ 2026-09-07 에 실제로 밟았다.

    `SCHEMA_VERSION` 만 10 → 11 로 올리고 `_MIGRATIONS` 에 등록을 안 했다.
    `lt init-db` 는 **"스키마 적용 완료 · schema 11"** 을 찍었고, 그 DB 에는
    11 이 만들어야 할 열이 없었다 — *스탬프는 최신인데 스키마는 옛것*이다.
    이 상태가 특히 나쁜 이유는 **다음 실행이 아무것도 안 한다**는 것이다.
    이미 11 이라고 적혀 있으니 마이그레이션이 대기하지 않는다.

    두 값은 정의상 묶여 있다 — 마이그레이션은 002 부터 붙고, 그 앞의 1 이 기준선이다.
    """
    from lifetrainer.db import _MIGRATIONS, SCHEMA_VERSION

    assert SCHEMA_VERSION == 1 + len(_MIGRATIONS), (
        f"SCHEMA_VERSION={SCHEMA_VERSION} 인데 등록된 마이그레이션은 {len(_MIGRATIONS)}개다 — "
        "상수만 올렸거나, 목록에만 넣었다"
    )


def test_마이그레이션_번호가_겹치지_않는다():
    """★ 실제로 겹친 적이 있다 — 옛 DB 에 `010_aw_event_soft_delete` 와
    `010_purged_event` 가 나란히 적혀 있다. 지금은 알파벳 순서 덕에 우연히 맞지만,
    같은 번호 둘의 순서를 **이름의 우연**에 맡기고 있는 것이다.
    """
    from lifetrainer.db import _MIGRATIONS

    numbers = [name.split("_", 1)[0] for name, _ in _MIGRATIONS]
    dupes = {n for n in numbers if numbers.count(n) > 1}
    assert not dupes, f"번호가 겹치는 마이그레이션: {sorted(dupes)}"


def test_등록된_마이그레이션의_sql_파일이_실재한다():
    """목록에 있는데 파일이 없으면 **그 DB 를 올릴 수 없다.** 이름 오타가 여기서 걸린다."""
    from pathlib import Path

    from lifetrainer.db import _MIGRATIONS, _MIGRATIONS_DIR

    # 002 는 파이썬으로 좌표를 변환하므로 .sql 파일이 없다 — 나머지만 본다.
    missing = [
        name for name, _ in _MIGRATIONS
        if name != "002_day_boundary" and not Path(_MIGRATIONS_DIR / f"{name}.sql").exists()
    ]
    assert not missing, f"등록됐는데 .sql 이 없다: {missing}"
