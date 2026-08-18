"""Life Trainer 설정 로더.

로드 순서: 내장 기본값 -> config/lifetrainer.toml(있으면) -> 환경변수(LT_<SECTION>_<KEY>).
모든 설정 dataclass 는 frozen 이다 — 로드 이후에는 값이 바뀌지 않는다.
전역 상태를 두지 않기 위해, 다른 모듈은 항상 이 모듈이 만든 Config 인스턴스를 인자로 받아 쓴다.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError as exc:  # pragma: no cover - 설치 환경 문제 안내용
        raise RuntimeError(
            "TOML 파서를 찾을 수 없습니다. `pip install tomli` 로 설치하세요 "
            "(Python 3.11+ 이면 표준 라이브러리 tomllib 이 있어 불필요합니다)."
        ) from exc

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_HANDLER_MARK = "_lifetrainer_stderr_handler"

# 내장 기본값. config/lifetrainer.example.toml 의 값과 맞춘다.
# 경로류 값은 여기서는 아직 문자열이다 — root 확정 후 절대 경로로 바뀐다.
_DEFAULTS: dict[str, dict[str, Any]] = {
    "general": {
        "timezone": "Asia/Seoul",
        "data_dir": "data",
        "db_path": "data/lifetrainer.db",
        "log_level": "INFO",
    },
    "activitywatch": {
        "base_url": "http://127.0.0.1:5600",
        "api_key": "",
        "timeout_sec": 10.0,
        "poll_interval_sec": 600,
        "overlap_sec": 900,
        "backfill_days": 7,
        "hosts": [],
    },
    "rollup": {
        "slot_minutes": 10,
        "afk_category": "away",
        "no_data_category": "off",
        "min_active_ratio": 0.05,
        "rules_path": "config/rules.yaml",
        "day_boundary_hour": 6,
    },
    "report": {
        "png_dir": "data/png",
        "font_family": "NanumGothic",
        "daily_at": "23:30",
        "weekly_at": "22:00",
        "morning_at": "07:30",
    },
    "slack": {
        "mode": "notify",
        "bot_token": "",
        "app_token": "",
        "default_channel": "",
        "openclaw_config": "/home/user/.openclaw/openclaw.json",
    },
    "llm": {
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "qwen3-8b",
        "timeout_sec": 300.0,
        "max_input_tokens": 4000,
        "enable_thinking": False,
    },
    "collect": {
        "user_agent": "LifeTrainer/0.1 (+https://github.com/; personal research bot)",
        "per_domain_min_interval_sec": 2.0,
        "per_domain_concurrency": 1,
        "arxiv_min_interval_sec": 3.0,
        "max_fail_before_disable": 10,
        "sources_path": "config/sources.yaml",
        "respect_robots": True,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8770,
        "base_url": "",
        "read_only": False,
        "session_secret": "",
        "link_ttl_sec": 600,
        "session_ttl_sec": 2592000,
        "external": False,
    },
}


@dataclass(frozen=True)
class AWConfig:
    base_url: str
    api_key: str  # aw-server v0.14.0b1+ 의 [auth] api_key. 없으면 빈 문자열
    timeout_sec: float
    poll_interval_sec: int
    overlap_sec: int
    backfill_days: int
    hosts: tuple[str, ...]


@dataclass(frozen=True)
class RollupConfig:
    slot_minutes: int  # 기본 10 -> 하루 144 슬롯
    afk_category: str
    no_data_category: str
    min_active_ratio: float
    rules_path: Path
    day_boundary_hour: int = 6  # 논리적 하루 경계 (0=자정, 기본 06:00).
    # 기본값을 둔 이유: 이 필드는 기존 필드들 뒤에 "추가"된 것이라, 이미
    # RollupConfig(...) 를 키워드 인자로 직접 만드는 테스트(E 등)가 있다.
    # 기본값이 없으면 그 테스트들이 전부 깨진다 — "기존 시그니처를 바꾸지 마라"
    # 원칙을 지키려면 새 필드는 항상 안전한 기본값을 가져야 한다.
    # timeutil.* 의 boundary_hour 기본값(6)과 반드시 일치시킬 것 — 값을
    # 두 곳에 따로 두면 언젠가 갈라진다(계약서 §2).


@dataclass(frozen=True)
class ReportConfig:
    png_dir: Path
    font_family: str
    daily_at: str
    weekly_at: str
    morning_at: str
    # grid_start_hour 는 여기 없다 — RollupConfig.day_boundary_hour 와 같은 의미의
    # 값을 두 곳에 두면 반드시 갈라지므로 V1 에서 제거했다(계약서 §2). 플래너
    # 격자 시작 시각이 필요하면 cfg.rollup.day_boundary_hour 를 쓴다.


@dataclass(frozen=True)
class SlackConfig:
    mode: str  # 'notify' | 'bolt'
    bot_token: str  # 비어 있으면 openclaw_config 에서 로드된 값이 채워진다
    app_token: str
    default_channel: str
    openclaw_config: Path


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    timeout_sec: float
    max_input_tokens: int
    enable_thinking: bool


@dataclass(frozen=True)
class CollectConfig:
    user_agent: str
    per_domain_min_interval_sec: float
    per_domain_concurrency: int
    arxiv_min_interval_sec: float
    max_fail_before_disable: int
    sources_path: Path
    respect_robots: bool


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"  # 실수로 외부에 노출되지 않게
    port: int = 8770
    base_url: str = ""  # Slack 링크에 쓸 외부 주소. 미설정이면 링크를 안 붙인다
    read_only: bool = False
    # 아래 4개는 V3(웹 인증)가 쓸 필드다. V1 이 미리 넣어둔다 — config.py 를
    # 두 담당이 동시에 고치면 충돌하기 때문(계약서 §4). session_secret 이 비어
    # 있을 때 생성해 data/websecret 에 0600 으로 저장하는 로직은 V3 가
    # web/auth.py 안에서 처리한다. 여기서는 빈 문자열을 그대로 둔다.
    session_secret: str = ""
    link_ttl_sec: int = 600
    session_ttl_sec: int = 2592000
    external: bool = False


@dataclass(frozen=True)
class Config:
    root: Path  # 프로젝트 루트 (절대 경로)
    timezone: str
    data_dir: Path  # 절대 경로로 정규화됨
    db_path: Path  # 절대 경로
    log_level: str
    aw: AWConfig
    rollup: RollupConfig
    report: ReportConfig
    slack: SlackConfig
    llm: LLMConfig
    collect: CollectConfig
    web: WebConfig = WebConfig()  # 새로 추가된 필드 — 기본값으로 기존 생성 코드와 호환 유지

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def slots_per_day(self) -> int:
        return 1440 // self.rollup.slot_minutes


def _find_root() -> Path:
    """`config.py` 위치에서 위로 올라가며 `pyproject.toml` 을 찾아 프로젝트 루트를 결정한다."""
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    raise RuntimeError(
        f"프로젝트 루트를 찾을 수 없습니다 ({here} 위로 pyproject.toml 이 없음)"
    )


def _abs_path(root: Path, value: str) -> Path:
    """상대 경로를 프로젝트 루트 기준으로 절대화한다. 이미 절대 경로면 그대로 둔다."""
    p = Path(value)
    return p if p.is_absolute() else (root / p)


def _resolve_config_path(path: str | Path | None, root: Path) -> Path | None:
    """설정 파일 경로 우선순위: 인자 > $LT_CONFIG > <root>/config/lifetrainer.toml.

    명시적으로 지정된 경로(인자·환경변수)가 존재하지 않으면 에러를 올린다.
    기본 위치(`<root>/config/lifetrainer.toml`)는 없으면 조용히 건너뛴다 —
    아직 `example.toml` 에서 복사하지 않은 최초 설치 상태를 정상으로 취급한다.
    """
    if path is not None:
        p = Path(path)
        p = p if p.is_absolute() else (root / p)
        if not p.exists():
            raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {p}")
        return p
    env_val = os.environ.get("LT_CONFIG")
    if env_val:
        p = Path(env_val)
        p = p if p.is_absolute() else (root / p)
        if not p.exists():
            raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다 ($LT_CONFIG): {p}")
        return p
    default = root / "config" / "lifetrainer.toml"
    return default if default.exists() else None


def _merge_toml(raw: dict[str, dict[str, Any]], loaded: dict[str, Any]) -> None:
    """TOML 에서 읽은 값을 raw 에 in-place 로 덮어쓴다. 알려진 section/key 만 반영한다."""
    for section, values in loaded.items():
        if section not in raw:
            logger.warning("알 수 없는 설정 섹션 무시: [%s]", section)
            continue
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if key not in raw[section]:
                logger.warning("알 수 없는 설정 키 무시: [%s].%s", section, key)
                continue
            raw[section][key] = value


def _cast_env(value_str: str, sample: Any) -> Any:
    """환경변수 문자열을 대상 필드의 타입(sample 로부터 추론)으로 캐스팅한다."""
    if isinstance(sample, bool):
        return value_str.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(sample, int):
        return int(value_str)
    if isinstance(sample, float):
        return float(value_str)
    if isinstance(sample, (list, tuple)):
        return [p.strip() for p in value_str.split(",") if p.strip()]
    return value_str


def _apply_env(raw: dict[str, dict[str, Any]]) -> None:
    """`LT_<SECTION>_<KEY>` 형태의 환경변수로 raw 를 in-place 덮어쓴다."""
    for section, values in raw.items():
        prefix = f"LT_{section.upper()}_"
        for key in values:
            env_name = f"{prefix}{key.upper()}"
            if env_name in os.environ:
                values[key] = _cast_env(os.environ[env_name], values[key])


def _load_slack_tokens_from_openclaw(openclaw_path: Path) -> tuple[str, str]:
    """openclaw_config JSON 에서 (botToken, appToken) 을 읽는다.

    파일이 없거나 형식이 어긋나거나 키가 없으면 예외 없이 ('', '') 를 반환한다.
    """
    try:
        data = json.loads(openclaw_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 폴백은 절대 예외를 올리면 안 됨
        logger.debug("openclaw_config(%s) 파싱 실패, 무시: %s", openclaw_path, exc)
        return "", ""
    slack_section = data.get("channels", {}).get("slack", {})
    if not isinstance(slack_section, dict):
        return "", ""
    bot = slack_section.get("botToken") or ""
    app = slack_section.get("appToken") or ""
    return str(bot), str(app)


def load_config(path: str | Path | None = None) -> Config:
    """설정을 로드한다. 순서: 내장 기본값 -> TOML 파일(있으면) -> 환경변수."""
    root = _find_root()
    raw = copy.deepcopy(_DEFAULTS)

    cfg_path = _resolve_config_path(path, root)
    if cfg_path is not None:
        text = cfg_path.read_text(encoding="utf-8")
        loaded = tomllib.loads(text)
        _merge_toml(raw, loaded)

    _apply_env(raw)

    aw = AWConfig(
        base_url=str(raw["activitywatch"]["base_url"]),
        api_key=str(raw["activitywatch"]["api_key"]),
        timeout_sec=float(raw["activitywatch"]["timeout_sec"]),
        poll_interval_sec=int(raw["activitywatch"]["poll_interval_sec"]),
        overlap_sec=int(raw["activitywatch"]["overlap_sec"]),
        backfill_days=int(raw["activitywatch"]["backfill_days"]),
        hosts=tuple(raw["activitywatch"]["hosts"]),
    )

    rollup = RollupConfig(
        slot_minutes=int(raw["rollup"]["slot_minutes"]),
        afk_category=str(raw["rollup"]["afk_category"]),
        no_data_category=str(raw["rollup"]["no_data_category"]),
        min_active_ratio=float(raw["rollup"]["min_active_ratio"]),
        rules_path=_abs_path(root, str(raw["rollup"]["rules_path"])),
        day_boundary_hour=int(raw["rollup"]["day_boundary_hour"]),
    )

    report = ReportConfig(
        png_dir=_abs_path(root, str(raw["report"]["png_dir"])),
        font_family=str(raw["report"]["font_family"]),
        daily_at=str(raw["report"]["daily_at"]),
        weekly_at=str(raw["report"]["weekly_at"]),
        morning_at=str(raw["report"]["morning_at"]),
    )

    slack_raw = raw["slack"]
    bot_token = str(slack_raw["bot_token"])
    app_token = str(slack_raw["app_token"])
    # openclaw_config 자체도 root 기준으로 절대화한다 (계약서 목록에는 없지만,
    # 상대경로로 두면 CWD 에 따라 존재 여부 판정이 흔들려 폴백이 불안정해진다).
    openclaw_path = _abs_path(root, str(slack_raw["openclaw_config"]))
    if bot_token == "" and openclaw_path.exists():
        fallback_bot, fallback_app = _load_slack_tokens_from_openclaw(openclaw_path)
        if bot_token == "":
            bot_token = fallback_bot
        if app_token == "":
            app_token = fallback_app

    slack = SlackConfig(
        mode=str(slack_raw["mode"]),
        bot_token=bot_token,
        app_token=app_token,
        default_channel=str(slack_raw["default_channel"]),
        openclaw_config=openclaw_path,
    )

    llm = LLMConfig(
        base_url=str(raw["llm"]["base_url"]),
        model=str(raw["llm"]["model"]),
        timeout_sec=float(raw["llm"]["timeout_sec"]),
        max_input_tokens=int(raw["llm"]["max_input_tokens"]),
        enable_thinking=bool(raw["llm"]["enable_thinking"]),
    )

    collect = CollectConfig(
        user_agent=str(raw["collect"]["user_agent"]),
        per_domain_min_interval_sec=float(raw["collect"]["per_domain_min_interval_sec"]),
        per_domain_concurrency=int(raw["collect"]["per_domain_concurrency"]),
        arxiv_min_interval_sec=float(raw["collect"]["arxiv_min_interval_sec"]),
        max_fail_before_disable=int(raw["collect"]["max_fail_before_disable"]),
        sources_path=_abs_path(root, str(raw["collect"]["sources_path"])),
        respect_robots=bool(raw["collect"]["respect_robots"]),
    )

    web = WebConfig(
        host=str(raw["web"]["host"]),
        port=int(raw["web"]["port"]),
        base_url=str(raw["web"]["base_url"]),
        read_only=bool(raw["web"]["read_only"]),
        session_secret=str(raw["web"]["session_secret"]),
        link_ttl_sec=int(raw["web"]["link_ttl_sec"]),
        session_ttl_sec=int(raw["web"]["session_ttl_sec"]),
        external=bool(raw["web"]["external"]),
    )

    return Config(
        root=root,
        timezone=str(raw["general"]["timezone"]),
        data_dir=_abs_path(root, str(raw["general"]["data_dir"])),
        db_path=_abs_path(root, str(raw["general"]["db_path"])),
        log_level=str(raw["general"]["log_level"]),
        aw=aw,
        rollup=rollup,
        report=report,
        slack=slack,
        llm=llm,
        collect=collect,
        web=web,
    )


def setup_logging(cfg: Config) -> None:
    """루트 로거에 stderr 핸들러를 단다. 중복 호출해도 핸들러가 쌓이지 않는다."""
    root_logger = logging.getLogger()
    root_logger.setLevel(cfg.log_level)

    for handler in root_logger.handlers:
        if getattr(handler, _LOG_HANDLER_MARK, False):
            handler.setLevel(cfg.log_level)
            return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.setLevel(cfg.log_level)
    setattr(handler, _LOG_HANDLER_MARK, True)
    root_logger.addHandler(handler)
