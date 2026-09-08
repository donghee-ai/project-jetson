"""lifetrainer.cli 테스트.

네트워크 절대 금지. AW/LLM 헬스체크처럼 실제 소켓을 여는 경로는 monkeypatch 로
막는다 (`AWClient.ping`, `LLMClient.health`). `lt collect` 는 실제 config/sources.yaml
(수십 개의 실제 URL)을 그대로 로드하므로 `feeds.due_sources` 를 패치해 아무 것도
가져오지 않게 한다. `lt worker --once` 는 큐가 비어 있으면 애초에 LLM 을 호출하지
않으므로 패치 없이도 안전하다.

각 테스트는 `--config`로 임시 toml(하나의 실제 프로젝트 루트를 가리키되 db_path/
data_dir/png_dir 만 tmp_path 로 돌린 것)을 가리켜 실제 저장소의 데이터를 건드리지
않는다. `rules_path`/`sources_path` 는 기본값을 그대로 써서 실제 `config/rules.yaml`
을 로드한다 (그래야 CLI 배선이 실제로 동작하는지 검증된다).
"""

from __future__ import annotations

import sqlite3

import pytest

from lifetrainer import cli, db, timeutil
from lifetrainer.config import load_config


@pytest.fixture()
def config_path(tmp_path) -> str:
    """db_path/data_dir/png_dir 만 tmp_path 로 돌린 최소 toml 설정 파일을 만든다.

    `[slack].openclaw_config` 를 존재하지 않는 경로로 돌려서, 이 실제 기기에
    이미 있는 `~/.openclaw/openclaw.json` 의 진짜 봇 토큰이 테스트에 새어 들어와
    실제 Slack API 를 건드리는 일이 없게 한다.
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


# ── 인자 파싱 / 종료 코드 ─────────────────────────────────────────────────


def test_no_subcommand_exits_2():
    with pytest.raises(SystemExit) as exc_info:
        cli.main([])
    assert exc_info.value.code == 2


def test_unknown_subcommand_exits_2():
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["no-such-command"])
    assert exc_info.value.code == 2


def test_bad_config_path_returns_1(capsys):
    code = _run(["--config", "/no/such/path/lifetrainer.toml", "doctor"])
    assert code == 1
    captured = capsys.readouterr()
    assert "설정 로드 실패" in captured.err


# ── doctor ────────────────────────────────────────────────────────────


def test_doctor_fails_when_db_missing(config_path, capsys, monkeypatch):
    """AW/LLM 은 네트워크를 안 타도록 패치. DB 가 없으므로 FAIL 이 있어야 하고 종료코드 1."""
    monkeypatch.setattr("lifetrainer.collect.aw_client.AWClient.ping", lambda self: False)
    monkeypatch.setattr("lifetrainer.llm.client.LLMClient.health", lambda self: False)

    code = _run(["--config", config_path, "doctor"])
    captured = capsys.readouterr()
    assert "[FAIL] DB" in captured.out
    assert "init-db" in captured.out
    assert code == 1


def test_doctor_db_ok_after_init(config_path, capsys, monkeypatch):
    monkeypatch.setattr("lifetrainer.collect.aw_client.AWClient.ping", lambda self: False)
    monkeypatch.setattr("lifetrainer.llm.client.LLMClient.health", lambda self: False)

    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "doctor"])
    captured = capsys.readouterr()
    assert "[  OK] DB" in captured.out or "[OK] DB" in captured.out.replace("  ", " ")
    # DB 만 통과하면 나머지가 WARN 이라도(AW/LLM 없음) 전체는 FAIL 이 없어 0이어야 한다.
    assert code == 0


def test_doctor_web_search_ok_with_serper_alone(config_path, capsys, monkeypatch):
    """Serper 하나가 정상 구성이다.

    네이버는 API HUB 이관으로 신규 발급이 막혔다(HISTORY/2026-08-25-it-worked-because-the-key-was-empty.md). 예전 doctor 는
    이때 WARN 을 내며 네이버 키를 권했는데, **못 하는 일을 권하는 경고**였다.
    """
    monkeypatch.setattr("lifetrainer.collect.aw_client.AWClient.ping", lambda self: False)
    monkeypatch.setattr("lifetrainer.llm.client.LLMClient.health", lambda self: False)
    monkeypatch.setattr(
        "lifetrainer.llm.websearch.providers_available",
        lambda cfg: {"serper": True},
    )

    _run(["--config", config_path, "doctor"])
    out = capsys.readouterr().out
    assert "[  OK] 웹 검색" in out
    assert "naver_client_id" not in out


def test_doctor_web_search_warn_points_at_serper_only(config_path, capsys, monkeypatch):
    """키가 하나도 없을 때의 안내가 발급 가능한 쪽을 가리켜야 한다."""
    monkeypatch.setattr("lifetrainer.collect.aw_client.AWClient.ping", lambda self: False)
    monkeypatch.setattr("lifetrainer.llm.client.LLMClient.health", lambda self: False)
    monkeypatch.setattr(
        "lifetrainer.llm.websearch.providers_available",
        lambda cfg: {"serper": False},
    )

    _run(["--config", config_path, "doctor"])
    out = capsys.readouterr().out
    assert "[WARN] 웹 검색" in out
    assert "serper_api_key" in out
    assert "naver_client_id" not in out


# ── init-db / synth / rollup / stats ─────────────────────────────────


def test_init_db_creates_file(config_path, cfg):
    assert not cfg.db_path.exists()
    code = _run(["--config", config_path, "init-db"])
    assert code == 0
    assert cfg.db_path.exists()


def test_synth_then_rollup_then_stats(config_path, cfg, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    assert _run(["--config", config_path, "synth", "--days", "3", "--end-day", "2026-08-15"]) == 0

    capsys.readouterr()
    code = _run(["--config", config_path, "rollup", "--range", "2026-08-13", "2026-08-15"])
    assert code == 0
    rollup_out = capsys.readouterr().out
    assert "2026-08-13" in rollup_out
    assert "2026-08-15" in rollup_out

    code = _run(["--config", config_path, "stats", "--day", "2026-08-15"])
    assert code == 0
    stats_out = capsys.readouterr().out
    assert "2026-08-15" in stats_out

    # 합성 데이터가 실제로 DB 에 들어갔는지도 직접 확인한다.
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        n_events = conn.execute("SELECT COUNT(*) FROM aw_event").fetchone()[0]
        assert n_events > 0
        n_slots = conn.execute("SELECT COUNT(*) FROM slot WHERE day = '2026-08-15'").fetchone()[0]
        assert n_slots == 144
    finally:
        conn.close()


def test_rollup_default_is_today(config_path, cfg):
    """플래그 없이 `lt rollup` 을 돌리면 오늘 날짜가 롤업된다."""
    assert _run(["--config", config_path, "init-db"]) == 0
    assert _run(["--config", config_path, "rollup"]) == 0

    today = timeutil.day_str(timeutil.now_ts(), cfg.tz)
    conn = db.connect(cfg.db_path, readonly=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM slot WHERE day = ?", (today,)).fetchone()[0]
        assert n == 144
    finally:
        conn.close()


# ── report daily/weekly (--post 없이) ───────────────────────────────


def test_report_daily_without_post_never_touches_slack(config_path, cfg, capsys, monkeypatch):
    called = {"post": False}

    class _BoomNotifier:
        def __init__(self, _cfg):
            called["post"] = True

    # SlackNotifier 를 아예 인스턴스화만 해도 실패하는 가짜로 바꿔서, --post 가
    # 없을 때 정말로 한 번도 만들어지지 않는지 확인한다.
    monkeypatch.setattr("lifetrainer.slackio.notify.SlackNotifier", _BoomNotifier)

    assert _run(["--config", config_path, "init-db"]) == 0
    assert _run(["--config", config_path, "synth", "--days", "3", "--end-day", "2026-08-15"]) == 0
    assert _run(["--config", config_path, "rollup", "--range", "2026-08-13", "2026-08-15"]) == 0

    capsys.readouterr()
    code = _run(["--config", config_path, "report", "daily", "--day", "2026-08-15"])
    assert code == 0
    out = capsys.readouterr().out
    assert "2026-08-15" in out
    assert "PNG:" in out
    assert called["post"] is False

    png_path = cfg.report.png_dir / "2026-08-15-timeline.png"
    assert png_path.exists()
    assert png_path.stat().st_size > 0


def test_report_weekly_without_post_never_touches_slack(config_path, cfg, capsys, monkeypatch):
    called = {"post": False}

    class _BoomNotifier:
        def __init__(self, _cfg):
            called["post"] = True

    monkeypatch.setattr("lifetrainer.slackio.notify.SlackNotifier", _BoomNotifier)

    assert _run(["--config", config_path, "init-db"]) == 0
    assert _run(["--config", config_path, "synth", "--days", "10", "--end-day", "2026-08-15"]) == 0
    assert _run(["--config", config_path, "rollup", "--range", "2026-08-06", "2026-08-15"]) == 0

    capsys.readouterr()
    code = _run(["--config", config_path, "report", "weekly", "--end", "2026-08-15"])
    assert code == 0
    out = capsys.readouterr().out
    assert "PNG:" in out
    assert called["post"] is False


# ── log (수동 입력 + 자동 재롤업) ──────────────────────────────────────


def test_log_inserts_manual_entry_and_reollups(config_path, cfg):
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "log", "운동", "60m", "헬스장"])
    assert code == 0

    conn = db.connect(cfg.db_path, readonly=True)
    try:
        row = conn.execute("SELECT category, note, source FROM manual_entry").fetchone()
        assert row["category"] == "운동"
        assert row["note"] == "헬스장"
        assert row["source"] == "cli"

        today = timeutil.day_str(timeutil.now_ts(), cfg.tz)
        n_slots = conn.execute("SELECT COUNT(*) FROM slot WHERE day = ?", (today,)).fetchone()[0]
        assert n_slots == 144  # 재롤업이 실제로 실행됨
    finally:
        conn.close()


def test_log_bad_duration_is_user_error(config_path):
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "log", "운동", "this-is-not-a-duration"])
    assert code == 2


# ── queue / backup / worker ──────────────────────────────────────────


def test_queue_stats_on_empty_db(config_path, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "queue", "stats"])
    assert code == 0
    assert "비어" in capsys.readouterr().out


def test_worker_once_on_empty_queue_is_a_noop(config_path, capsys):
    """큐가 비어 있으면 claim() 이 즉시 None 을 반환하므로 LLM 호출 없이 안전하게 끝난다."""
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "worker", "--once"])
    assert code == 0
    assert "처리한 잡: 0개" in capsys.readouterr().out


def test_backup_creates_file(config_path, cfg, tmp_path):
    assert _run(["--config", config_path, "init-db"]) == 0
    out_path = tmp_path / "backups" / "snapshot.db"
    code = _run(["--config", config_path, "backup", "--out", str(out_path)])
    assert code == 0
    assert out_path.exists()

    conn = sqlite3.connect(str(out_path))
    try:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert int(version) == db.SCHEMA_VERSION
    finally:
        conn.close()


# ── collect (네트워크 없이 wiring 만 검증) ────────────────────────────


def test_collect_wiring_without_network(config_path, capsys, monkeypatch):
    """`due_sources` 를 비워서 실제 네트워크(config/sources.yaml 의 진짜 URL들)를 타지 않게 한다."""
    monkeypatch.setattr("lifetrainer.collect.feeds.due_sources", lambda conn, **kw: [])

    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "collect"])
    assert code == 0
    out = capsys.readouterr().out
    assert "수집 완료" in out
    assert "소스 0개" in out


def test_score_on_empty_db(config_path, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "score"])
    assert code == 0
    assert "스코어링 완료: 0건" in capsys.readouterr().out


def test_digest_on_empty_db(config_path, capsys):
    """빈 DB 에서 빈손이라고 말한다.

    ★ 문구가 2026-09-04 에 *"새로 수집된"* → *"아직 안 보낸"* 으로 바뀌었다.
      전에는 전체 코퍼스를 훑으면서 "새로 수집된" 이라고 말했다 — 말과 코드가 달랐고,
      그 어긋남이 8일 연속 같은 문서를 보내는 동안 아무에게도 안 들켰다.
    """
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "digest"])
    assert code == 0
    assert "아직 안 보낸 관심 문서가 없습니다" in capsys.readouterr().out


# ── slack (토큰 없을 때 no-op, 네트워크 없음) ─────────────────────────


def test_slack_test_without_token_returns_1(config_path, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "slack", "test"])
    assert code == 1
    assert "비활성화" in capsys.readouterr().err


def test_slack_serve_without_bolt_mode_is_user_error(config_path):
    code = _run(["--config", config_path, "slack", "serve"])
    assert code == 2


# ── --config / -v 가 서브커맨드 뒤에 와도 동작하는지 ─────────────────


def test_config_flag_after_subcommand_also_works(config_path, cfg):
    code = _run(["init-db", "--config", config_path])
    assert code == 0
    assert cfg.db_path.exists()


def test_verbose_flag_after_subcommand_does_not_crash(config_path):
    code = _run(["init-db", "--config", config_path, "-v"])
    assert code == 0


# ── doctor 의 프라이빗 줄 ────────────────────────────────────────────────


def test_doctor_는_프라이빗을_WARN_으로_올리지_않는다(tmp_path, capsys):
    """★ 프라이빗이 켜져 있는 것은 **고장이 아니다.**

    노란불을 켜면 "켤 때마다 doctor 가 운다"가 되고, 그러면 사람이 doctor 를 안 본다
    (CLAUDE.md §1). 그래도 적기는 한다 — 커버리지가 왜 낮은지 물었을 때 답이
    여기 있어야 한다.
    """
    import argparse
    import dataclasses

    from lifetrainer import db, privacy
    from lifetrainer.cli import cmd_doctor
    from lifetrainer.config import load_config

    cfg = dataclasses.replace(load_config(), db_path=tmp_path / "lt.db")
    conn = db.open_db(cfg)
    privacy.begin(conn, 60)
    conn.close()

    cmd_doctor(argparse.Namespace(), cfg)
    out = capsys.readouterr().out

    # ★ 라벨 칸으로 찾는다. 그냥 "프라이빗" 으로 찾으면 **이 테스트 이름이 들어간
    #   tmp 경로**가 DB 줄에 찍혀서 그게 먼저 걸린다 (실제로 걸렸다).
    line = next(l for l in out.splitlines() if "] 프라이빗" in l)
    assert "OK" in line, f"프라이빗이 WARN/FAIL 로 올라갔다: {line}"
    assert "켜짐" in line and "남음" in line


# ── 롤업 신선도 ──────────────────────────────────────────────────────────


def _doctor_line(cfg, label: str, capsys) -> str:
    import argparse

    from lifetrainer.cli import cmd_doctor

    cmd_doctor(argparse.Namespace(), cfg)
    out = capsys.readouterr().out
    return next(l for l in out.splitlines() if f"] {label}" in l)


def _cfg_with_slot(tmp_path, name: str, age_sec: float | None):
    """slot 한 줄을 `age_sec` 초 전에 갱신된 것으로 심는다. None 이면 아예 안 심는다."""
    import dataclasses
    import time

    from lifetrainer import db
    from lifetrainer.config import load_config

    cfg = dataclasses.replace(load_config(), db_path=tmp_path / name)
    conn = db.open_db(cfg)
    if age_sec is not None:
        conn.execute(
            "INSERT INTO slot(day, slot, start_ts, category, active_sec, afk_sec, gap_sec, "
            "winner_sec, updated_at) VALUES ('2026-09-01', 0, 0, 'coding', 600, 0, 0, 600, ?)",
            (time.time() - age_sec,),
        )
        conn.commit()
    conn.close()
    return cfg


def test_롤업이_멈추면_신선도가_WARN_이다(tmp_path, capsys):
    """★ 2026-09-01 사고를 이 검사가 잡았을지 재현한다.

    그날 롤업이 10분마다 죽었는데 doctor 는 "롤업 08-12 ~ 09-01" 이라고 OK 를 찍었다.
    범위는 맞았고, **그 범위가 멈춰 있다는 것만** 몰랐다.
    """
    cfg = _cfg_with_slot(tmp_path, "stale.db", age_sec=3 * 3600)
    line = _doctor_line(cfg, "롤업 신선도", capsys)
    assert "WARN" in line, f"멈춘 롤업을 못 잡았다: {line}"
    assert "3.0시간 전" in line


def test_방금_돌았으면_조용하다(tmp_path, capsys):
    """언제 꺼지나 — 한 번 성공하면 즉시. 누적이 아니라 마지막 시각이라서."""
    cfg = _cfg_with_slot(tmp_path, "fresh.db", age_sec=300)
    line = _doctor_line(cfg, "롤업 신선도", capsys)
    assert "OK" in line and "5분 전" in line


def test_한_번도_롤업_안_한_DB_는_실패가_아니다(tmp_path, capsys):
    """★ 옳지만 아직 증명 못 한 상태를 실패로 세지 않는다 (CLAUDE.md §1).

    갓 만든 DB 에는 slot 이 없다. 그건 고장이 아니라 아직 안 돌린 것이다.
    """
    cfg = _cfg_with_slot(tmp_path, "empty.db", age_sec=None)
    line = _doctor_line(cfg, "롤업 신선도", capsys)
    assert "OK" in line and "아직 롤업한 적 없음" in line


# ── 워처 침묵 (2026-09-04) ──────────────────────────────────────────────
#
# 폰 워처 하나가 장시간 멈춰 있었는데 아무것도 안 잡았다.
# "롤업 신선도" 는 우리 쪽 파이프를 보는데, 이번 고장은 파이프는 도는데 원료가
# 끊긴 것이라 그대로 통과했다.
#
# ★ **안 울려야 하는 경우가 어려운 쪽이다** (CLAUDE.md §1). 아래 세 개 중 두 개가
#   그것이다 — 밤에는 모든 버킷이 같이 조용해지므로 절대 시간으로 재면 매일 아침 운다.


def _cfg_with_phone(
    tmp_path,
    name: str,
    *,
    session_ago_h: float | None,
    media_ago_h: float | None,
    unlock_ago_h: float | None = None,
):
    """폰 한 대와 그 버킷들의 마지막 이벤트를 심는다. None 이면 그 버킷을 안 만든다."""
    import dataclasses
    import time

    from lifetrainer import db
    from lifetrainer.config import load_config

    cfg = dataclasses.replace(load_config(), db_path=tmp_path / name)
    conn = db.open_db(cfg)
    now = time.time()
    device_id = db.upsert_device(conn, "s24", kind="phone", hostname="s24")

    def put(bucket_id: str, ago_h: float) -> None:
        # ★ unlock 버킷은 실제로 type='unlock' 이다. 검사는 타입이 아니라 **버킷 이름의
        #   접미사**로 가르므로(w-0028 과 같은 판정) 여기서도 실물과 같게 심는다.
        btype = "unlock" if bucket_id.endswith("-unlock") else "android"
        conn.execute(
            "INSERT INTO aw_bucket(bucket_id, host, client, type, hostname, device_id, "
            "first_seen, last_seen) VALUES (?, 's24', 'c', ?, 's24', ?, 0, ?)",
            (bucket_id, btype, device_id, now),
        )
        ts = now - ago_h * 3600.0
        conn.execute(
            "INSERT INTO aw_event(bucket_id, ts, ts_end, duration, app, title, url, status, "
            "data_json, synced_at) VALUES (?, ?, ?, 1, 'a', 't', NULL, NULL, '{}', ?)",
            (bucket_id, ts - 1, ts, now),
        )

    if session_ago_h is not None:
        put("aw-watcher-android", session_ago_h)
    if media_ago_h is not None:
        put("aw-watcher-android-media", media_ago_h)
    if unlock_ago_h is not None:
        put("aw-watcher-android-unlock", unlock_ago_h)
    conn.commit()
    conn.close()
    return cfg


def test_폰은_쓰는데_미디어_워처만_조용하면_WARN_이다(tmp_path, capsys):
    """익명화한 회귀 모양 — 세션은 계속 오는데 미디어만 임계값보다 오래 끊겼다."""
    cfg = _cfg_with_phone(tmp_path, "quiet.db", session_ago_h=0.1, media_ago_h=15.0)
    line = _doctor_line(cfg, "워처 침묵", capsys)
    assert "WARN" in line and "media" in line, line


def test_폰을_안_쓰면_아무_말도_안_한다(tmp_path, capsys):
    """★ 밤새 안 쓰면 모든 버킷이 같이 조용하다. 여기서 울면 매일 아침 운다."""
    cfg = _cfg_with_phone(tmp_path, "night.db", session_ago_h=9.0, media_ago_h=20.0)
    line = _doctor_line(cfg, "워처 침묵", capsys)
    assert "OK" in line, line


def test_한_번도_안_튼_기기는_고장이_아니다(tmp_path, capsys):
    """옳지만 아직 증명 못 한 상태를 실패로 세지 않는다 (CLAUDE.md §1)."""
    cfg = _cfg_with_phone(tmp_path, "never.db", session_ago_h=0.1, media_ago_h=None)
    line = _doctor_line(cfg, "워처 침묵", capsys)
    assert "OK" in line, line


def test_unlock_워처만_조용해도_잡는다(tmp_path, capsys):
    """★ 익명화한 회귀 모양 — **미디어는 정상인데 unlock 만 멈춰 있었다.**

    미디어는 멀쩡했다. 하나를 감시하면서 옆의 것을 안 봤다는 뜻이라, 목록을
    `WATCHED_PHONE_BUCKETS` 표로 빼고 이 테스트로 고정한다.

    임계값은 로컬 표본에서 정상적인 밤 간격은 통과하고 실제 장기 침묵만 잡도록 정했다.
    공개본에는 개인별 잠금해제 간격을 싣지 않는다.
    """
    cfg = _cfg_with_phone(
        tmp_path, "unlock.db", session_ago_h=0.1, media_ago_h=0.2, unlock_ago_h=23.0
    )
    line = _doctor_line(cfg, "워처 침묵", capsys)
    assert "WARN" in line and "unlock" in line, line
    assert "media" not in line, "멀쩡한 워처까지 같이 부르면 어느 것이 고장인지 안 보인다"


# ── lt private purge --day (2026-09-04) ────────────────────────────────
#
# "그날 기록을 통째로 지우고 화면에서는 자리비움으로 보이게" 를 위한 입구다.
# 지운 시간은 되돌아오지 않으므로, **막는 쪽**을 먼저 고정한다.


def test_purge_day_는_yes_없이는_안_지운다(config_path, capsys):
    assert _run(["--config", config_path, "init-db"]) == 0
    assert _run(["--config", config_path, "private", "purge", "--day", "2026-09-01"]) == 2
    assert "--yes 가 필요합니다" in capsys.readouterr().err


def test_purge_day_는_다른_구간_지정과_같이_못_쓴다(config_path, capsys):
    """둘을 같이 주면 **어느 쪽이 이겼는지 모른 채** 하루가 날아간다."""
    assert _run(["--config", config_path, "init-db"]) == 0
    code = _run(["--config", config_path, "private", "purge", "--day", "2026-09-01",
                 "--yes", "--minutes", "10"])
    assert code == 2
    assert "같이 못 씁니다" in capsys.readouterr().err


def test_purge_day_는_논리적_하루를_지운다(config_path, capsys):
    """★ 자정이 아니라 **06:00~다음날 06:00** 이다.

    자정 기준으로 지우면 새벽 활동이 어제 격자에 그대로 남아
    "지웠는데 아직 있다" 가 된다. 남은 `private_span` 의 경계로 확인한다.
    """
    import sqlite3
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from lifetrainer.config import load_config

    assert _run(["--config", config_path, "init-db"]) == 0
    assert _run(["--config", config_path, "private", "purge", "--day", "2026-09-01", "--yes"]) == 0

    cfg = load_config(config_path)
    conn = sqlite3.connect(cfg.db_path)
    row = conn.execute("SELECT start_ts, end_ts, kind FROM private_span").fetchone()
    conn.close()

    tz = ZoneInfo(cfg.timezone)
    expected_start = datetime(2026, 9, 1, cfg.rollup.day_boundary_hour, tzinfo=tz).timestamp()
    assert row is not None, "구간을 안 남기면 다음 sync 가 그대로 되살린다"
    assert row[0] == pytest.approx(expected_start)
    assert row[1] - row[0] == pytest.approx(86400.0)
    assert row[2] == "purge", "kind 가 purge 여야 화면에서 자리비움으로 그려진다"
