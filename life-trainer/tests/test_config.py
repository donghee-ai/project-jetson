"""lifetrainer.config 테스트. 네트워크 없음. 파일은 전부 tmp_path 에 만든다."""

from __future__ import annotations

import json
import logging
from zoneinfo import ZoneInfo

import pytest

from lifetrainer import config as config_mod
from lifetrainer.config import load_config, setup_logging


# ── 기본값 로드 ───────────────────────────────────────────────────────────


def test_load_config_defaults_no_file():
    # 저장소에는 config/lifetrainer.toml 이 없다 (example 만 있다) -> 내장 기본값만으로 로드돼야 함.
    cfg = load_config()
    assert cfg.timezone == "Asia/Seoul"
    assert isinstance(cfg.tz, ZoneInfo)
    assert cfg.rollup.slot_minutes == 10
    assert cfg.slots_per_day == 144
    assert cfg.aw.hosts == ()
    assert cfg.aw.base_url.startswith("http")
    assert cfg.llm.enable_thinking is False
    assert cfg.collect.respect_robots is True


def test_load_config_root_is_project_root():
    cfg = load_config()
    assert (cfg.root / "pyproject.toml").exists()
    assert (cfg.root / "lifetrainer" / "schema.sql").exists()


def test_load_config_paths_are_absolute():
    cfg = load_config()
    assert cfg.data_dir.is_absolute()
    assert cfg.db_path.is_absolute()
    assert cfg.rollup.rules_path.is_absolute()
    assert cfg.report.png_dir.is_absolute()
    assert cfg.collect.sources_path.is_absolute()
    assert cfg.db_path == cfg.root / "data" / "lifetrainer.db"


def test_config_dataclasses_are_frozen():
    cfg = load_config()
    with pytest.raises(Exception):
        cfg.timezone = "UTC"  # frozen dataclass -> FrozenInstanceError
    with pytest.raises(Exception):
        cfg.aw.base_url = "http://x"


# ── TOML 오버라이드 ───────────────────────────────────────────────────────


def test_toml_override_changes_only_specified_keys(tmp_path):
    toml_path = tmp_path / "lifetrainer.toml"
    toml_path.write_text(
        """
[general]
timezone = "UTC"

[rollup]
slot_minutes = 15

[llm]
max_input_tokens = 999
""",
        encoding="utf-8",
    )
    cfg = load_config(path=toml_path)
    assert cfg.timezone == "UTC"
    assert cfg.rollup.slot_minutes == 15
    assert cfg.llm.max_input_tokens == 999
    # 지정 안 한 키는 기본값 유지
    assert cfg.rollup.afk_category == "away"
    assert cfg.llm.model == "qwen3-8b"


def test_toml_override_relative_paths_resolved_against_root(tmp_path):
    toml_path = tmp_path / "lifetrainer.toml"
    toml_path.write_text(
        """
[general]
data_dir = "custom_data"
db_path  = "custom_data/lt.db"
""",
        encoding="utf-8",
    )
    cfg = load_config(path=toml_path)
    assert cfg.data_dir == cfg.root / "custom_data"
    assert cfg.db_path == cfg.root / "custom_data" / "lt.db"


def test_load_config_missing_explicit_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(path=tmp_path / "does-not-exist.toml")


# ── 환경변수 오버라이드 ────────────────────────────────────────────────────


def test_env_override_beats_toml_and_defaults(tmp_path, monkeypatch):
    toml_path = tmp_path / "lifetrainer.toml"
    toml_path.write_text(
        """
[rollup]
slot_minutes = 15
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LT_ROLLUP_SLOT_MINUTES", "20")
    monkeypatch.setenv("LT_GENERAL_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LT_LLM_ENABLE_THINKING", "true")
    monkeypatch.setenv("LT_ACTIVITYWATCH_HOSTS", "pc1, pc2")
    monkeypatch.setenv("LT_SLACK_BOT_TOKEN", "xoxb-fromenv")

    cfg = load_config(path=toml_path)

    assert cfg.rollup.slot_minutes == 20  # env 가 toml(15) 을 이긴다
    assert cfg.log_level == "DEBUG"
    assert cfg.llm.enable_thinking is True
    assert cfg.aw.hosts == ("pc1", "pc2")
    assert cfg.slack.bot_token == "xoxb-fromenv"


def test_env_var_name_pattern_matches_contract_examples(tmp_path, monkeypatch):
    monkeypatch.setenv("LT_ACTIVITYWATCH_BASE_URL", "http://100.64.0.1:5600")
    monkeypatch.setenv("LT_GENERAL_DB_PATH", "elsewhere/lt.db")
    cfg = load_config()
    assert cfg.aw.base_url == "http://100.64.0.1:5600"
    assert cfg.db_path == cfg.root / "elsewhere" / "lt.db"


# ── Slack 토큰 openclaw 폴백 ────────────────────────────────────────────


def test_slack_token_fallback_from_openclaw_config(tmp_path):
    openclaw_path = tmp_path / "openclaw.json"
    openclaw_path.write_text(
        json.dumps({"channels": {"slack": {"botToken": "xoxb-fake", "appToken": "xapp-fake"}}}),
        encoding="utf-8",
    )
    toml_path = tmp_path / "lifetrainer.toml"
    toml_path.write_text(
        f"""
[slack]
bot_token = ""
app_token = ""
openclaw_config = "{openclaw_path.as_posix()}"
""",
        encoding="utf-8",
    )
    cfg = load_config(path=toml_path)
    assert cfg.slack.bot_token == "xoxb-fake"
    assert cfg.slack.app_token == "xapp-fake"


def test_slack_token_fallback_skipped_when_bot_token_set(tmp_path):
    openclaw_path = tmp_path / "openclaw.json"
    openclaw_path.write_text(
        json.dumps({"channels": {"slack": {"botToken": "xoxb-fake", "appToken": "xapp-fake"}}}),
        encoding="utf-8",
    )
    toml_path = tmp_path / "lifetrainer.toml"
    toml_path.write_text(
        f"""
[slack]
bot_token = "xoxb-explicit"
openclaw_config = "{openclaw_path.as_posix()}"
""",
        encoding="utf-8",
    )
    cfg = load_config(path=toml_path)
    assert cfg.slack.bot_token == "xoxb-explicit"
    assert cfg.slack.app_token == ""  # 명시 안 했지만 bot_token 이 이미 있어 폴백 자체를 안 탐


def test_slack_token_fallback_missing_file_is_silent(tmp_path):
    toml_path = tmp_path / "lifetrainer.toml"
    toml_path.write_text(
        """
[slack]
bot_token = ""
openclaw_config = "/nonexistent/path/openclaw.json"
""",
        encoding="utf-8",
    )
    cfg = load_config(path=toml_path)  # 예외 없이 빈 문자열
    assert cfg.slack.bot_token == ""
    assert cfg.slack.app_token == ""


def test_slack_token_fallback_missing_keys_in_json_is_silent(tmp_path):
    openclaw_path = tmp_path / "openclaw.json"
    openclaw_path.write_text(json.dumps({"channels": {}}), encoding="utf-8")
    toml_path = tmp_path / "lifetrainer.toml"
    toml_path.write_text(
        f"""
[slack]
bot_token = ""
openclaw_config = "{openclaw_path.as_posix()}"
""",
        encoding="utf-8",
    )
    cfg = load_config(path=toml_path)
    assert cfg.slack.bot_token == ""
    assert cfg.slack.app_token == ""


def test_slack_token_fallback_malformed_json_is_silent(tmp_path):
    openclaw_path = tmp_path / "openclaw.json"
    openclaw_path.write_text("{not valid json", encoding="utf-8")
    toml_path = tmp_path / "lifetrainer.toml"
    toml_path.write_text(
        f"""
[slack]
bot_token = ""
openclaw_config = "{openclaw_path.as_posix()}"
""",
        encoding="utf-8",
    )
    cfg = load_config(path=toml_path)  # 예외 없이 빈 문자열로
    assert cfg.slack.bot_token == ""


# ── setup_logging ────────────────────────────────────────────────────────


def test_setup_logging_does_not_stack_handlers():
    root = logging.getLogger()
    pre_existing = [
        h for h in root.handlers if getattr(h, config_mod._LOG_HANDLER_MARK, False)
    ]
    for h in pre_existing:
        root.removeHandler(h)

    cfg = load_config()
    setup_logging(cfg)
    setup_logging(cfg)
    setup_logging(cfg)

    marked = [h for h in root.handlers if getattr(h, config_mod._LOG_HANDLER_MARK, False)]
    assert len(marked) == 1

    for h in marked:
        root.removeHandler(h)


# ── example.toml 과 코드 기본값의 키 집합 (issues/0004) ──────────────────


def _example_keys() -> set[str]:
    import pathlib

    try:
        import tomllib
    except ModuleNotFoundError:  # py3.10
        import tomli as tomllib

    root = pathlib.Path(__file__).resolve().parent.parent
    raw = tomllib.loads((root / "config" / "lifetrainer.example.toml").read_text(encoding="utf-8"))
    return {f"{s}.{k}" for s, v in raw.items() if isinstance(v, dict) for k in v}


def _default_keys() -> set[str]:
    from lifetrainer.config import _DEFAULTS

    return {f"{s}.{k}" for s, v in _DEFAULTS.items() for k in v}


def test_example_toml_이_코드가_읽는_키를_다_담는다():
    """★ 사람이 눈으로 대조하던 것을 검사로 바꾼다 (issues/0004).

    ## 왜 필요한가

    2026-08-23 에 전수 대조로 발견하고, 08-27 에 재확인하고도 **09-03 까지 열려 있었다.**
    그동안 빠진 키가 6개에서 **18개로 늘었다** — 그중 5개(`[private]`)는 이 검사를
    붙이기 직전에 내가 넣은 것이다. 규칙만 있고 검사가 없으면 이렇게 된다.

    ## 무엇이 아까운가

    동작은 안 깨진다 — `config.py` 에 기본값이 있다. 잃는 것은 **알 기회**다.
    `embed.batch_size` 는 4 → 64 로 재색인이 24.6분에서 2.9분이 되는 값인데,
    템플릿에 없으면 있는 줄도 모른다.

    ## 언제 안 우나

    `_DEFAULTS` 에 키를 넣으면서 example.toml 에도 넣으면 조용하다.
    **값이 같을 필요는 없다** — 이 검사는 키 집합만 본다. example 이 코드 기본값을
    담고 주석이 이 기기 운영값을 말하는 것이 이 저장소의 관례다(`[nightly]` 참고).
    """
    missing = sorted(_default_keys() - _example_keys())
    assert not missing, (
        "코드는 읽는데 example.toml 에 없는 키:\n  "
        + "\n  ".join(missing)
        + "\n\n템플릿을 복사해 설치하는 사람은 이 설정이 있는 줄도 모른다."
    )


def test_example_toml_이_코드가_안_읽는_키를_안_담는다():
    """반대 방향. 이게 없으면 목록이 한쪽으로만 자란다.

    example 에만 있는 키는 **더 나쁘다** — 적어 놨는데 아무 효과가 없으니
    설정한 사람은 적용된 줄 안다. 이름을 바꾸고 한쪽만 고치면 이렇게 된다.
    """
    orphan = sorted(_example_keys() - _default_keys())
    assert not orphan, (
        "example.toml 에만 있고 코드가 안 읽는 키:\n  "
        + "\n  ".join(orphan)
        + "\n\n적어 놔도 아무 일도 안 일어난다 — 설정한 사람은 적용된 줄 안다."
    )


def test_백포트를_조건_없이_import_하는_곳이_없다():
    """★ 2026-09-07 에 파이썬 3.14 로 올리다 실제로 밟았다.

    `agent/config.py` 가 `import tomli` 를 조건 없이 하고 있었다. 3.10 에서는
    백포트가 깔려 있어 돌았지만 **3.11+ 에는 표준 `tomllib` 이 있어 백포트를 안 깐다** —
    올리자마자 `No module named 'tomli'` 로 죽었고, 증상은 *에이전트 설정이 통째로
    안 읽히는 것*이었다.

    ★ 이 검사는 파이썬을 올릴 때만 값을 하는 게 아니다. 백포트는 **언젠가 사라지는
      의존성**이라, 조건 없이 부르는 자리는 그때마다 같은 방식으로 터진다.
      `lifetrainer.config` 가 어느 파서를 쓸지 이미 정해 두었으니 거기서 받아 쓴다.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "lifetrainer"
    # 조건부 import 는 `try:` 블록 안에 있다 — 들여쓰기 없는 최상단 import 만 잡는다.
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not re.match(r"^import (tomli|exceptiongroup)\b", stripped):
                continue
            if path.name == "config.py" and path.parent == root:
                continue  # 파서를 정하는 당사자. 여기가 유일한 예외다
            offenders.append(f"{path.relative_to(root)}:{num}  {stripped}")
    assert not offenders, (
        "백포트를 직접 부르는 곳: " + " · ".join(offenders)
        + " — `from lifetrainer.config import tomllib` 처럼 정해진 것을 받아 쓰세요"
    )


# ── example.toml 은 키뿐 아니라 **값도** 코드 기본값과 같다 (2026-09-07) ────


def test_example_toml_의_값이_코드_기본값과_같다():
    """★ 위 `test_example_toml_이_코드가_읽는_키를_다_담는다` 는 **이름만** 본다.

    그래서 `summary_limit` 이 코드 600 · example 600 · 운영 30 으로 갈라져 있어도
    조용했다 (issues/0005). 한쪽만 고치면 다음 사람이 또 갈린 값을 본다 —
    `0004` 가 이미 같은 부류였다.

    ★ 왜 예외를 목록으로 두나: `example.toml` 은 **읽는 사람에게 보여주는 문서**라
      실제 주소처럼 "그럴듯한 예시" 가 나아 보이는 키가 있다. 이유를 못 쓰면
      허용하지 않는다 (저장소 규칙 §1 — 목록이 늘기만 하면 검사가 없는 것과 같다).
    """
    import tomllib as _toml
    from pathlib import Path

    from lifetrainer.config import _DEFAULTS

    # 키 → 왜 달라도 되는가
    ALLOWED = {
        "activitywatch.base_url": (
            "코드 기본값은 localhost 지만, example 은 **테일넷 주소 예시**를 보여준다 — "
            "노트북이 다른 기기라는 것이 이 설정의 요점이라 127.0.0.1 은 오해를 부른다."
        ),
        "slack.openclaw_config": (
            "코드 기본값은 실행 사용자의 홈을 동적으로 쓰고, example 은 공개용 사용자 "
            "경로를 명시해 설정 형식을 보여준다."
        ),
    }

    root = Path(__file__).resolve().parent.parent
    raw = _toml.loads((root / "config" / "lifetrainer.example.toml").read_text(encoding="utf-8"))

    mismatched = []
    for section, values in _DEFAULTS.items():
        for key, code_value in values.items():
            if section not in raw or key not in raw[section]:
                continue  # 위 테스트가 따로 본다
            if raw[section][key] == code_value:
                continue
            name = f"{section}.{key}"
            if name in ALLOWED:
                continue
            mismatched.append(f"{name}: 코드 {code_value!r} · example {raw[section][key]!r}")

    assert not mismatched, (
        "example.toml 과 코드 기본값이 다르다: " + " · ".join(mismatched)
        + " — 둘을 같이 바꾸거나, 달라야 하는 이유를 ALLOWED 에 적으세요"
    )
    for name, why in ALLOWED.items():
        assert len(why.strip()) > 20, f"{name} 의 예외 사유가 너무 짧다"
