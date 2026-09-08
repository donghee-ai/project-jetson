"""lifetrainer.cli 의 plan/slot/planner/web 서브커맨드 테스트 (담당 P4 — 배선).

`lt` 를 subprocess 로 띄우지 않고 `cli.main(argv)` 함수 호출로 검증한다
(계약서: subprocess 금지, 네트워크 금지). `lt planner`/`lt web` 은 다른 담당
(P3/P2)이 아직 만들지 않았을 수도 있는 모듈을 런타임 import 하므로, 실제
존재 여부와 무관하게 `sys.modules` 를 `None` 으로 심어 강제로 ImportError 를
재현한다 — 그래야 이 테스트가 다른 담당의 작업 완료 시점과 무관하게 안전하고,
`lt web` 이 실제로 서버를 띄워 테스트가 멈추는 사고도 피한다.
"""

from __future__ import annotations

import pytest

from lifetrainer import cli, db
from lifetrainer.config import load_config
from lifetrainer.plan.override import list_overrides

MON = "2026-01-05"  # ISO 요일 1 (월) — tests/test_plan.py 와 동일한 기준일
TUE = "2026-01-06"  # 2 (화)


@pytest.fixture()
def config_path(tmp_path) -> str:
    """db_path/data_dir/png_dir 만 tmp_path 로 돌린 최소 toml 설정 파일을 만든다.

    tests/test_cli.py 의 픽스처와 같은 패턴: 실제 openclaw 토큰이 새어 들어오지
    않게 openclaw_config 를 존재하지 않는 경로로 돌린다.
    """
    data_dir = tmp_path / "data"
    text = f"""
[general]
data_dir = "{data_dir.as_posix()}"
db_path  = "{(data_dir / "lifetrainer.db").as_posix()}"

[report]
png_dir = "{(data_dir / "png").as_posix()}"

[slack]
bot_token       = ""
openclaw_config = "{(tmp_path / "no-such-openclaw.json").as_posix()}"
"""
    path = tmp_path / "lifetrainer.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture()
def cfg(config_path):
    return load_config(config_path)


def _run(argv: list[str]) -> int:
    return cli.main(argv)


# ── lt plan add / list 왕복 ─────────────────────────────────────────────


def test_plan_add_list_roundtrip(config_path, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    capsys.readouterr()

    code = _run(
        ["--config", config_path, "plan", "add", "코딩", "09:00-12:00", "--category", "coding", "--days", "평일"]
    )
    assert code == 0
    assert "계획 생성됨" in capsys.readouterr().out

    code = _run(["--config", config_path, "plan", "list", "--day", MON])
    assert code == 0
    out = capsys.readouterr().out
    assert "코딩" in out
    assert "09:00-12:00" in out
    assert "달성률" in out
    assert "0%" in out  # 실측 데이터가 없으니 0%


def test_plan_add_oneoff_shows_only_on_its_day(config_path, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    capsys.readouterr()

    code = _run(["--config", config_path, "plan", "add", "병원", "14:00-15:00", "--day", "2026-08-20"])
    assert code == 0

    code = _run(["--config", config_path, "plan", "list", "--day", "2026-08-20"])
    assert code == 0
    assert "병원" in capsys.readouterr().out

    code = _run(["--config", config_path, "plan", "list", "--day", MON])
    assert code == 0
    assert "등록된 계획이 없습니다" in capsys.readouterr().out


def test_plan_add_rejects_day_and_days_together(config_path):
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(
        ["--config", config_path, "plan", "add", "이상함", "09:00-10:00", "--day", "2026-08-20", "--days", "평일"]
    )
    assert code == 2


def test_plan_add_snaps_time_to_slot_boundary(config_path, cfg, capsys):
    """09:03-09:47 처럼 10분 경계에서 어긋난 시각은 슬롯 경계로 스냅되어 저장돼야 한다."""
    assert _run(["--config", config_path, "init-db"]) == 0
    capsys.readouterr()

    code = _run(["--config", config_path, "plan", "add", "애매한시간", "09:03-09:47", "--days", "매일"])
    assert code == 0
    out = capsys.readouterr().out
    assert "스냅" in out

    conn = db.connect(cfg.db_path, readonly=True)
    try:
        row = conn.execute("SELECT start_min, end_min FROM plan WHERE title = '애매한시간'").fetchone()
    finally:
        conn.close()
    assert row["start_min"] == 540  # 09:00
    assert row["end_min"] == 590  # 09:50


# ── 잘못된 시간 형식 -> 종료 코드 2 ────────────────────────────────────────


def test_plan_add_bad_time_format_exits_2(config_path):
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "plan", "add", "잘못됨", "이상한시간", "--days", "평일"])
    assert code == 2


def test_plan_add_crossing_midnight_exits_2(config_path):
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "plan", "add", "야근", "22:00-02:00", "--days", "평일"])
    assert code == 2


# ── lt plan rm ────────────────────────────────────────────────────────────


def test_plan_rm_removes_plan(config_path, cfg, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    capsys.readouterr()
    _run(["--config", config_path, "plan", "add", "지울것", "09:00-10:00", "--days", "매일"])

    conn = db.connect(cfg.db_path, readonly=True)
    try:
        plan_id = conn.execute("SELECT id FROM plan WHERE title = '지울것'").fetchone()["id"]
    finally:
        conn.close()

    capsys.readouterr()
    code = _run(["--config", config_path, "plan", "rm", str(plan_id)])
    assert code == 0
    assert "계획 삭제됨" in capsys.readouterr().out

    code = _run(["--config", config_path, "plan", "list", "--day", MON])
    assert code == 0
    assert "등록된 계획이 없습니다" in capsys.readouterr().out


def test_plan_rm_unknown_id_exits_2(config_path):
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "plan", "rm", "9999"])
    assert code == 2


# ── lt plan check 토글 ──────────────────────────────────────────────────


def test_plan_check_toggle(config_path, cfg, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    capsys.readouterr()
    _run(["--config", config_path, "plan", "add", "운동", "07:00-08:00", "--days", "매일"])

    conn = db.connect(cfg.db_path, readonly=True)
    try:
        plan_id = conn.execute("SELECT id FROM plan WHERE title = '운동'").fetchone()["id"]
    finally:
        conn.close()

    code = _run(["--config", config_path, "plan", "check", str(plan_id), "--day", MON])
    assert code == 0

    capsys.readouterr()
    _run(["--config", config_path, "plan", "list", "--day", MON])
    assert "☑" in capsys.readouterr().out

    code = _run(["--config", config_path, "plan", "check", str(plan_id), "--day", MON, "--off"])
    assert code == 0

    capsys.readouterr()
    _run(["--config", config_path, "plan", "list", "--day", MON])
    assert "☐" in capsys.readouterr().out


# ── lt plan skip ──────────────────────────────────────────────────────────


def test_plan_skip_removes_it_for_that_day_only(config_path, cfg, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    capsys.readouterr()
    _run(["--config", config_path, "plan", "add", "코딩", "09:00-10:00", "--days", "평일"])

    conn = db.connect(cfg.db_path, readonly=True)
    try:
        plan_id = conn.execute("SELECT id FROM plan WHERE title = '코딩'").fetchone()["id"]
    finally:
        conn.close()

    code = _run(["--config", config_path, "plan", "skip", str(plan_id), "--day", MON])
    assert code == 0

    capsys.readouterr()
    _run(["--config", config_path, "plan", "list", "--day", MON])
    assert "등록된 계획이 없습니다" in capsys.readouterr().out

    capsys.readouterr()
    _run(["--config", config_path, "plan", "list", "--day", TUE])
    assert "코딩" in capsys.readouterr().out


# ── lt slot set / clear ────────────────────────────────────────────────────


def test_slot_set_creates_override_and_rerollups(config_path, cfg, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    capsys.readouterr()

    code = _run(["--config", config_path, "slot", "set", MON, "54-60", "coding"])
    assert code == 0
    out = capsys.readouterr().out
    assert "재롤업 완료" in out

    conn = db.connect(cfg.db_path, readonly=True)
    try:
        row = conn.execute("SELECT category, source FROM slot WHERE day = ? AND slot = ?", (MON, 54)).fetchone()
        assert row["category"] == "coding"
        assert row["source"] == "override"
        total = conn.execute("SELECT COUNT(*) FROM slot WHERE day = ?", (MON,)).fetchone()[0]
        assert total == 144  # 재롤업이 실제로 하루 전체를 채웠는지
    finally:
        conn.close()


def test_slot_clear_removes_override_and_rerollups(config_path, cfg, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    assert _run(["--config", config_path, "slot", "set", MON, "54-60", "coding"]) == 0
    capsys.readouterr()

    code = _run(["--config", config_path, "slot", "clear", MON, "54-60"])
    assert code == 0
    assert "재롤업 완료" in capsys.readouterr().out

    conn = db.connect(cfg.db_path, readonly=True)
    try:
        assert list_overrides(conn, MON) == {}
        row = conn.execute("SELECT category, source FROM slot WHERE day = ? AND slot = ?", (MON, 54)).fetchone()
        assert row["source"] != "override"  # 오버라이드가 해제되어 원래 분류로 돌아감
    finally:
        conn.close()


def test_slot_set_bad_range_exits_2(config_path):
    assert _run(["--config", config_path, "init-db"]) == 0
    assert _run(["--config", config_path, "slot", "set", MON, "not-a-range", "coding"]) == 2
    assert _run(["--config", config_path, "slot", "set", MON, "60-54", "coding"]) == 2  # 거꾸로
    assert _run(["--config", config_path, "slot", "set", MON, "0-9999", "coding"]) == 2  # 하루 범위 초과


# ── lt planner / lt web — 아직 없는 모듈에도 죽지 않는지 ───────────────────


def test_planner_missing_module_is_friendly_not_a_crash(config_path, monkeypatch, capsys):
    """P3 의 report.planner 가 아직 없어도(또는 강제로 없다고 가정해도) ImportError 로 죽지 않아야 한다."""
    assert _run(["--config", config_path, "init-db"]) == 0
    monkeypatch.setitem(__import__("sys").modules, "lifetrainer.report.planner", None)

    code = _run(["--config", config_path, "planner", "--day", MON])
    assert code == 1
    assert "준비되지 않았습니다" in capsys.readouterr().err


def test_web_missing_module_is_friendly_not_a_crash(config_path, monkeypatch, capsys):
    """P2 의 web.app 이 아직 없어도(또는 강제로 없다고 가정해도) ImportError 로 죽지 않고,
    (있더라도) 실제 서버를 띄워 테스트가 멈추는 일이 없어야 한다."""
    monkeypatch.setitem(__import__("sys").modules, "lifetrainer.web.app", None)

    code = _run(["--config", config_path, "web"])
    assert code == 1
    assert "준비되지 않았습니다" in capsys.readouterr().err
