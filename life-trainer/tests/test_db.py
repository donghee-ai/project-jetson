"""lifetrainer.db 테스트. 전부 tmp_path 의 임시 SQLite 파일로 돈다 (네트워크 없음)."""

from __future__ import annotations

import sqlite3
import time

import pytest

from lifetrainer import db


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    yield c
    c.close()


# ── connect / PRAGMA ─────────────────────────────────────────────────────


def test_connect_creates_parent_dir(tmp_path):
    nested = tmp_path / "sub" / "dir" / "lt.db"
    c = db.connect(nested)
    try:
        assert nested.parent.exists()
    finally:
        c.close()


def test_connect_row_factory_is_row(conn):
    row = conn.execute("SELECT 1 AS one").fetchone()
    assert isinstance(row, sqlite3.Row)
    assert row["one"] == 1


def test_connect_enables_wal_mode(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_connect_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL == 2
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy_timeout == 30000


def test_connect_readonly_opens_existing_db(tmp_path):
    path = tmp_path / "lt.db"
    writer = db.connect(path)
    db.init_db(writer)
    writer.close()

    reader = db.connect(path, readonly=True)
    try:
        row = reader.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert row["value"] == str(db.SCHEMA_VERSION)
    finally:
        reader.close()


# ── init_db ──────────────────────────────────────────────────────────────


def test_init_db_idempotent(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    try:
        db.init_db(c)
        db.init_db(c)  # 두 번째 호출도 안전해야 함
        assert db.schema_version(c) == db.SCHEMA_VERSION
        # 핵심 테이블이 여전히 하나씩만 있는지 (중복 생성 없음)
        tables = {
            row["name"]
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"aw_event", "aw_bucket", "slot", "slot_breakdown", "doc", "job"} <= tables
    finally:
        c.close()


def test_schema_version_zero_without_init(tmp_path):
    c = sqlite3.connect(str(tmp_path / "raw.db"))
    c.row_factory = sqlite3.Row
    try:
        c.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        assert db.schema_version(c) == 0
    finally:
        c.close()


# ── transaction ──────────────────────────────────────────────────────────


def test_transaction_commits_on_success(conn):
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO manual_entry(start_ts, end_ts, category, source, created_at) "
            "VALUES (0, 60, 'exercise', 'cli', 0)"
        )
    count = conn.execute("SELECT COUNT(*) FROM manual_entry").fetchone()[0]
    assert count == 1


def test_transaction_rolls_back_on_exception(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO manual_entry(start_ts, end_ts, category, source, created_at) "
                "VALUES (0, 60, 'exercise', 'cli', 0)"
            )
            raise RuntimeError("boom")
    count = conn.execute("SELECT COUNT(*) FROM manual_entry").fetchone()[0]
    assert count == 0


# ── sync_state get/set ───────────────────────────────────────────────────


def test_get_set_state_string(conn):
    assert db.get_state(conn, "missing_key") is None
    assert db.get_state(conn, "missing_key", "fallback") == "fallback"
    db.set_state(conn, "aw_cursor:bucket-a", "hello")
    assert db.get_state(conn, "aw_cursor:bucket-a") == "hello"
    db.set_state(conn, "aw_cursor:bucket-a", "world")  # upsert, 중복행 아님
    assert db.get_state(conn, "aw_cursor:bucket-a") == "world"
    n = conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0]
    assert n == 1


def test_get_set_state_float(conn):
    assert db.get_state_float(conn, "cursor_ts") is None
    assert db.get_state_float(conn, "cursor_ts", 1.5) == 1.5
    db.set_state_float(conn, "cursor_ts", 1755305000.123)
    assert db.get_state_float(conn, "cursor_ts") == pytest.approx(1755305000.123)


# ── aw_event upsert (bucket_id, ts) ──────────────────────────────────────


def _insert_bucket(conn, bucket_id="aw-watcher-window_test"):
    now = time.time()
    conn.execute(
        "INSERT INTO aw_bucket(bucket_id, host, client, type, hostname, first_seen, last_seen) "
        "VALUES (?, 'test-pc', 'aw-watcher-window', 'window', 'test-pc', ?, ?)",
        (bucket_id, now, now),
    )


def test_aw_event_upsert_on_bucket_ts_conflict(conn):
    _insert_bucket(conn)
    conn.execute(
        "INSERT INTO aw_event(bucket_id, ts, ts_end, duration, app, title, data_json, synced_at) "
        "VALUES ('aw-watcher-window_test', 100.0, 110.0, 10.0, 'Code.exe', 'a.py', '{}', ?)",
        (time.time(),),
    )
    # 같은 (bucket_id, ts) 로 재폴링 -> duration/ts_end 갱신, 새 행 아님 (하트비트 성장 시나리오)
    conn.execute(
        "INSERT INTO aw_event(bucket_id, ts, ts_end, duration, app, title, data_json, synced_at) "
        "VALUES ('aw-watcher-window_test', 100.0, 130.0, 30.0, 'Code.exe', 'a.py', '{}', ?) "
        "ON CONFLICT(bucket_id, ts) DO UPDATE SET "
        "ts_end=excluded.ts_end, duration=excluded.duration, synced_at=excluded.synced_at",
        (time.time(),),
    )
    rows = conn.execute(
        "SELECT ts_end, duration FROM aw_event WHERE bucket_id='aw-watcher-window_test' AND ts=100.0"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ts_end"] == 130.0
    assert rows[0]["duration"] == 30.0


# ── doc / doc_fts (FTS5 트리거) ───────────────────────────────────────────


def test_doc_insert_is_searchable_via_fts_trigger(conn):
    conn.execute(
        "INSERT INTO doc(kind, url, title, fetched_at, abstract, content_hash, state) "
        "VALUES ('paper', 'https://arxiv.org/abs/1234', "
        "'Attention Mechanisms for On-Device Inference', ?, "
        "'a survey of jetson-friendly transformer tricks', 'deadbeef', 'new')",
        (time.time(),),
    )
    rows = conn.execute(
        "SELECT d.title FROM doc_fts f JOIN doc d ON d.id = f.rowid "
        "WHERE doc_fts MATCH 'attention'"
    ).fetchall()
    assert len(rows) == 1
    assert "Attention" in rows[0]["title"]

    # 근거 없는 검색어는 안 걸려야 함
    rows2 = conn.execute("SELECT rowid FROM doc_fts WHERE doc_fts MATCH 'quantum'").fetchall()
    assert rows2 == []


def test_doc_delete_removes_from_fts(conn):
    conn.execute(
        "INSERT INTO doc(kind, url, title, fetched_at, content_hash, state) "
        "VALUES ('article', 'https://example.com/x', 'Robotics Roundup', ?, 'cafebabe', 'new')",
        (time.time(),),
    )
    doc_id = conn.execute("SELECT id FROM doc WHERE url='https://example.com/x'").fetchone()["id"]
    conn.execute("DELETE FROM doc WHERE id=?", (doc_id,))
    rows = conn.execute("SELECT rowid FROM doc_fts WHERE doc_fts MATCH 'robotics'").fetchall()
    assert rows == []


# ── backup ───────────────────────────────────────────────────────────────


def test_backup_creates_file_with_data(conn, tmp_path):
    conn.execute(
        "INSERT INTO manual_entry(start_ts, end_ts, category, source, created_at) "
        "VALUES (0, 60, 'exercise', 'cli', 0)"
    )
    dest = tmp_path / "backups" / "lt-backup.db"
    result = db.backup(conn, dest)
    assert result == dest
    assert dest.exists()
    assert dest.stat().st_size > 0

    check = sqlite3.connect(str(dest))
    try:
        count = check.execute("SELECT COUNT(*) FROM manual_entry").fetchone()[0]
        assert count == 1
    finally:
        check.close()


# ── open_db ──────────────────────────────────────────────────────────────


class _FakeCfg:
    def __init__(self, db_path):
        self.db_path = db_path


def test_open_db_connects_and_inits(tmp_path):
    cfg = _FakeCfg(tmp_path / "openme.db")
    c = db.open_db(cfg)
    try:
        assert db.schema_version(c) == db.SCHEMA_VERSION
    finally:
        c.close()
