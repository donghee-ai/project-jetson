"""`lt doctor` 의 **판정**을 항목 단위로 검사한다 (2026-09-07).

## 왜 지금까지 없었나

판정이 `cmd_doctor` 507줄 안에 있었다. 그걸 부르려면 DB·AW·LLM·Slack·폰트·디스크가
전부 있어야 해서, **판정 하나만 시험할 방법이 없었다.**

`lifetrainer/health.py` 로 가르면서 항목이 전부 `check_*(cfg, report)` 가 됐다.
이제 `Report` 하나만 있으면 부를 수 있다.

## 무엇을 지키나

저장소 규칙 §1 은 검사를 붙일 때 여섯 가지를 답하라고 한다. 그중
**"언제 꺼지나"** 와 **"안 울려야 할 때 안 우나"** 는 코드를 읽어서는 확인이 안 된다 —
그래서 여기서 돌려 본다.
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from lifetrainer import db, health
from lifetrainer.config import load_config


@pytest.fixture()
def cfg(tmp_path):
    return dataclasses.replace(load_config(), db_path=tmp_path / "lt.db", data_dir=tmp_path)


@pytest.fixture()
def conn(cfg):
    c = db.open_db(cfg)
    yield c
    c.close()


def _add_source(conn, *, name, enabled=1, last_status=None, fail_count=0, fetched=True):
    conn.execute(
        "INSERT INTO source(kind, name, url, enabled, tags, interval_sec, next_fetch_at, "
        "created_at, last_status, fail_count, last_fetched) "
        "VALUES ('rss', ?, ?, ?, '', 3600, 0, ?, ?, ?, ?)",
        (name, f"https://x.example/{name}", enabled, time.time(),
         last_status, fail_count, time.time() if fetched else None),
    )
    conn.commit()


def _names(report):
    return {name for _, name, _ in report.checks}


def _status(report, name):
    return next(s for s, n, _ in report.checks if n == name)


# ── 수집 소스 ────────────────────────────────────────────────────────────


def test_소스가_다_멀쩡하면_초록이다(cfg, conn):
    _add_source(conn, name="살아있음", last_status=200)
    report = health.Report()
    health.check_data(cfg, report, conn)
    assert _status(report, "수집 소스") == "OK"


def test_연속_실패가_임계를_넘으면_운다(cfg, conn):
    _add_source(conn, name="죽음", last_status=404, fail_count=5)
    report = health.Report()
    health.check_data(cfg, report, conn)
    assert _status(report, "수집 소스") == "WARN"


def test_한두_번_실패에는_안_운다(cfg, conn):
    """★ **안 울려야 하는 쪽.** 남의 서버 502 한 번에 매번 노란불이 켜지면
    사람이 doctor 를 안 본다 (저장소 규칙 §1)."""
    _add_source(conn, name="깜빡", last_status=502, fail_count=2)
    report = health.Report()
    health.check_data(cfg, report, conn)
    assert _status(report, "수집 소스") == "OK"


def test_성공하면_경보가_꺼진다(cfg, conn):
    """★ **언제 꺼지나.** 누적이면 원인을 고쳐도 안 꺼지고, 그러면 새 실패와 구분이 안 된다."""
    _add_source(conn, name="고쳐짐", last_status=404, fail_count=9)
    report = health.Report()
    health.check_data(cfg, report, conn)
    assert _status(report, "수집 소스") == "WARN"

    conn.execute("UPDATE source SET last_status = 200, fail_count = 0")
    conn.commit()
    report = health.Report()
    health.check_data(cfg, report, conn)
    assert _status(report, "수집 소스") == "OK", "고쳤는데 경보가 안 꺼졌다"


def test_한_번도_안_받아_본_소스는_고장이_아니다(cfg, conn):
    """★ **옳지만 아직 증명 못 한 상태.** 실패와 미확인은 다르다."""
    _add_source(conn, name="새것", fetched=False)
    report = health.Report()
    health.check_data(cfg, report, conn)
    assert _status(report, "수집 소스") == "OK"


def test_꺼둔_소스는_안_센다(cfg, conn):
    """일부러 끈 것을 계속 말하면, 그 경보는 끌 방법이 없는 상수가 된다."""
    _add_source(conn, name="꺼둠", enabled=0, last_status=404, fail_count=20)
    report = health.Report()
    health.check_data(cfg, report, conn)
    assert _status(report, "수집 소스") == "OK"


# ── Report 자체 ──────────────────────────────────────────────────────────


def test_report_가_등급을_센다():
    r = health.Report()
    r.ok("가"); r.warn("나", "설명"); r.fail("다", "설명")
    assert len(r.checks) == 3 and len(r.warns) == 1 and len(r.fails) == 1


def test_모든_검사가_같은_모양이다():
    """★ 새 항목을 더할 때 규격이 흔들리지 않게. `run_all` 이 부르는 것과
    모듈에 있는 `check_*` 가 어긋나면 **만들고 안 부르는** 항목이 생긴다."""
    import inspect

    defined = {n for n, o in vars(health).items() if n.startswith("check_") and inspect.isfunction(o)}
    src = inspect.getsource(health.run_all)
    never_called = {n for n in defined if f"{n}(" not in src}
    assert not never_called, f"정의만 되고 run_all 이 안 부르는 검사: {sorted(never_called)}"
