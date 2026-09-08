"""Life Trainer 설정 로더.

로드 순서: 내장 기본값 -> config/lifetrainer.toml(있으면) -> 환경변수(LT_<SECTION>_<KEY>).
모든 설정 dataclass 는 frozen 이다 — 로드 이후에는 값이 바뀌지 않는다.
전역 상태를 두지 않기 위해, 다른 모듈은 항상 이 모듈이 만든 Config 인스턴스를 인자로 받아 쓴다.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ★ 폴백이 없다 (2026-09-08). `requires-python` 이 `>=3.11` 이라 `tomllib` 은 항상 표준이다.
#   전에는 `tomli` 백포트로 떨어지는 갈래가 있었는데, 3.14 로 올린 뒤로는
#   **아무도 안 타는 길**이었다 — 그리고 그 백포트를 조건 없이 부른 자리가 하나 있어
#   실제로 죽었다(HISTORY 2026-09-07). 안 쓰는 갈래를 남기면 다음 사람이 그걸 보고 따라 쓴다.
import tomllib

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_HANDLER_MARK = "_lifetrainer_stderr_handler"
_FILE_HANDLER_MARK = "_lifetrainer_file_handler"
LOG_FILE_NAME = "lifetrainer.log"
_LOG_FILE_BYTES = 5 * 1024 * 1024
_LOG_FILE_BACKUPS = 3

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
        "base_url": "http://127.0.0.1:35600",
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
        "switch_absorb_sec": 300,
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
    "search": {
        "serper_api_key": "",
        "timeout_sec": 10.0,
        "max_results": 5,
    },
    "nightly": {
        # ★ 30 이다 (2026-09-07 사람이 정함, issues/0005).
        #   600 은 *"새벽 창 3.8시간에 물리적으로 넣을 수 있는 최대"* 였지
        #   *"사람이 읽을 만큼"* 이 아니었다. 08-23 에 운영값을 30 으로 내렸는데
        #   **코드 기본값은 그대로 뒀고**, 그 뒤로 새 기기는 이 저장소가 이미
        #   접은 동작(600)으로 돌 참이었다. 기본값은 "이 정도면 안전하다" 를 뜻해야 한다.
        "summary_limit": 30,
        "tag_limit": 20,
        "embed_limit": 1000,
        "job_retention_days": 14,
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
        "external_host": "",
        "url_prefix": "",
        "external_mask": True,
    },
    "embed": {
        "enabled": False,
        "base_url": "http://127.0.0.1:8081/v1",
        "model": "qwen3-embedding-0.6b",
        "query_instruct": "Instruct: Given a search query, retrieve relevant articles\nQuery:",
        "dim": 1024,
        "timeout_sec": 30.0,
        "batch_size": 16,
        "max_chars": 1600,
        "vector_weight": 0.5,
    },
    "ingest": {
        "enabled": False,
        "secret": "",
        "max_body_bytes": 4194304,
        "clock_skew_sec": 300,
    },
    "private": {
        "default_minutes": 60,
        "max_minutes": 480,
        "max_purge_minutes": 240,
        "poll_sec": 15,
        "purge_aw": False,
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
    switch_absorb_sec: float = 300.0
    # 기기 전환(=컴퓨터를 안 쓴 구간)을 별도 활동으로 기록할 최소 길이. 이보다 짧으면
    # 앞 활동에 흡수한다. "코딩 10분 → 폰 3분 → 코딩 10분" 을 코딩 23분으로 본다 —
    # 잠깐 딴짓한 것까지 전부 찍히면 플래너가 난잡해서 읽을 수 없다.


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
class EmbedConfig:
    """문서 임베딩 서버 (RAG). 대화 모델과 **다른 프로세스**다.

    llama.cpp 는 프로세스당 모델 하나라 임베딩 전용 서버를 따로 띄운다.
    0.6B Q8_0 이 약 640MB 라 8B(10.2GB) 옆에 상주시켜도 여유 안에 든다.
    꺼져 있으면 검색이 키워드(FTS5)만으로 떨어진다 — 죽지 않는다.
    """

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8081/v1"
    model: str = "qwen3-embedding-0.6b"
    dim: int = 1024
    timeout_sec: float = 30.0
    batch_size: int = 16  # 한 번에 보낼 문서 수. 젯슨 메모리를 고려한 값
    max_chars: int = 1600  # 임베딩할 원문 상한. 길면 잘라 넣는다
    # 하이브리드 융합에서 벡터 쪽 가중치 (0=키워드만, 1=벡터만)
    vector_weight: float = 0.5
    # ★ **질의에만** 붙이는 지시문. 문서 쪽에는 절대 붙이지 않는다.
    #
    # Qwen3-Embedding 계열은 질의를 `Instruct: {task}\nQuery:{q}` 로 받는 전제로
    # 학습됐다. 안 붙이면 질의 벡터가 문서 벡터와 어긋난 자리에 놓인다.
    # 실측(scripts/eval_search.py · 엔티티 20질의 hit@5):
    #   벡터 단독  44% → 86%   ·   하이브리드 76% → 98%
    # 붙이기 전에는 **하이브리드(76%)가 키워드 단독(96%)보다 나빴다.**
    #
    # 모델을 바꾸면 이 문자열도 바뀐다. 빈 문자열이면 접두 없이 맨 질의로 간다.
    # 문서 벡터는 접두와 무관하므로 이 값을 바꿔도 **재색인이 필요 없다.**
    query_instruct: str = (
        "Instruct: Given a search query, retrieve relevant articles\nQuery:"
    )


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
class SearchConfig:
    """웹 검색 공급자 키. **한국어는 네이버, 그 외는 Serper** 로 나눈다.

    구글 Custom Search JSON API 는 신규 가입이 닫혀 있다 (2026-08-19 확인).
    근거와 대안 비교는 `docs/issues/h-0006-the-model-cites-one-source-and-stays-there.md`.
    """

    serper_api_key: str = ""
    timeout_sec: float = 10.0
    max_results: int = 5


@dataclass(frozen=True)
class NightlyConfig:
    """야간 배치(02:00)가 한 번에 큐에 넣는 양. 전부 요약하면 14시간짜리다."""

    # 실측 22.2초/건. 새벽 창 02:00~06:00 = 4시간 = 약 700건이 상한이고,
    # 600건이면 약 3.3시간이라 05:50 중단 시각 전에 끝난다.
    summary_limit: int = 30   # ★ _DEFAULTS 와 같이 움직인다 (issues/0005)
    tag_limit: int = 20  # 미분류 상위 N개를 한 번의 호출로 묶어 태깅

    # ★ 임베딩 한도는 요약 한도에서 파생시키지 않는다 (2026-08-28).
    #   전에는 `summary_limit * 2` 였는데 둘은 성격이 다르다 — 요약은 GPU 를 잡고
    #   건당 22초지만, 임베딩은 CPU(-ngl 0) 이고 건당 1초 미만이다. 묶어 두면
    #   운영값 30 이 임베딩 한도 60 을 낳고, **유입이 하루 약 400건이라 구조적으로 밀린다.**
    #   실제로 미임베딩이 1,700건까지 쌓였고 가장 오래된 것이 10일 전이었다.
    embed_limit: int = 1000

    # ★ 종료된 잡을 며칠 뒤에 걷어내나. `purge_done` 이 `--stop` 에서 이 값을 쓴다.
    #
    #   2026-09-01 까지 `purge_done` 은 **아무도 안 부르는 함수**였다. `lt doctor` 의
    #   큐 판정 주석은 *"purge_done 이 14일 뒤에 걷어가므로 done 은 회전한다"* 를
    #   전제로 쓰여 있었는데, 실측하니 가장 오래된 done 이 17일 전이었다 — 회전한 적이
    #   없다. 주석이 코드보다 낙관적이었다.
    #
    #   0 이면 안 지운다. 그때는 doctor 가 "회전 안 함"을 알고 말해야 한다.
    job_retention_days: int = 14


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
    # ── 인터넷 노출 (2026-08-22) ──────────────────────────────────────
    # external_host  이 호스트로 들어온 요청은 **인터넷에서 온 것**으로 본다.
    #                (Cloudflare Tunnel 의 호스트명). 비어 있으면 그런 요청이 없다는 뜻.
    #                tailnet 직결(IP:포트)과 구분하려고 있는 값이다 —
    #                `external` 하나로 묶어 두면 tailnet 에서도 제목이 사라지고,
    #                반대로 http 인 tailnet 때문에 Secure 쿠키를 못 건다.
    # url_prefix     앱을 이 경로 아래로 mount 한다 (예: "/planner").
    #                터널·Access 를 **경로 하나로** 막기 위한 것이다 — 새 라우트를
    #                추가했을 때 공개 목록 갱신을 잊어 노출되는 사고를 막는다.
    # external_mask  인터넷 요청에 창 제목·앱 이름을 지울지. 기본은 지운다.
    external_host: str = ""
    url_prefix: str = ""
    external_mask: bool = True


@dataclass(frozen=True)
class IngestConfig:
    """폰이 밀어 넣는 수신 엔드포인트(`POST /ingest/aw`) 설정.

    **비밀키를 `web.session_secret` 과 분리한다.** 폰이 들고 있는 값이라,
    새더라도 플래너 세션까지 열리면 안 된다. 비어 있으면 `web/auth.py` 가
    `<data_dir>/ingestsecret` 에 0600 으로 만들어 쓴다.

    `enabled=False` 가 기본이다 — 이 엔드포인트는 공개 인터넷(Cloudflare Tunnel)
    으로 열리는 유일한 경로라, 켜는 것이 명시적인 행위여야 한다.
    """

    enabled: bool = False
    secret: str = ""
    max_body_bytes: int = 4 * 1024 * 1024
    clock_skew_sec: int = 300


@dataclass(frozen=True)
class PrivateConfig:
    """프라이빗 모드 — 이 시간은 재지 않기로 한 것.

    ★ **여기 있는 것은 기본값과 상한뿐이다. 켜져 있는지 여부는 DB(`private_span`)가 갖는다.**
    상시 프로세스(`lt web`)는 기동 시 설정을 클로저에 못박으므로, 런타임에 켜고 끄는
    상태를 TOML 에 두면 재시작 없이는 못 바꾼다.

    `max_minutes` 에 상한을 두는 이유: 무기한이 없어야 "끄는 걸 깜빡해도 하루가
    통째로 비지 않는다"가 성립한다. 연장은 다시 누르면 된다.

    `purge_aw` 는 **남의 기기(PC)의 ActivityWatch DB 를 지우는** 동작이라 기본이 꺼짐이다.
    """

    default_minutes: int = 60
    max_minutes: int = 480
    max_purge_minutes: int = 240
    poll_sec: int = 15
    purge_aw: bool = False


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
    nightly: NightlyConfig = NightlyConfig()
    # 기본값을 준다 — 기존 생성 코드(테스트 포함)를 깨지 않는다. 끄면 검색이 키워드로 간다.
    embed: EmbedConfig = EmbedConfig()
    search: SearchConfig = SearchConfig()
    ingest: IngestConfig = IngestConfig()
    private: PrivateConfig = PrivateConfig()

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


# 이 파일이 `Config` 로 안 옮기고, **그 모듈이 직접 읽는** 섹션들.
#
# `[agent]` 는 `lifetrainer/agent/config.py` 가 읽는다. 여기 dataclass 에 안 넣은
# 이유는 그 파일 주석에 있다 — `Config` 는 테스트 수십 개가 직접 생성한다.
# 다만 **모른다고 경고하면 안 된다**: 설정은 멀쩡한데 서비스 로그마다 경고가
# 찍히면, 진짜 오타를 잡으라고 만든 이 경고가 무시당하게 된다.
_SECTIONS_OWNED_ELSEWHERE = frozenset({"agent"})


def _merge_toml(raw: dict[str, dict[str, Any]], loaded: dict[str, Any]) -> None:
    """TOML 에서 읽은 값을 raw 에 in-place 로 덮어쓴다. 알려진 section/key 만 반영한다."""
    for section, values in loaded.items():
        if section in _SECTIONS_OWNED_ELSEWHERE:
            continue  # 다른 모듈이 직접 읽는다 (아래 상수 주석)
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
        switch_absorb_sec=float(raw["rollup"]["switch_absorb_sec"]),
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

    embed = EmbedConfig(
        enabled=bool(raw["embed"]["enabled"]),
        base_url=str(raw["embed"]["base_url"]),
        model=str(raw["embed"]["model"]),
        dim=int(raw["embed"]["dim"]),
        timeout_sec=float(raw["embed"]["timeout_sec"]),
        batch_size=int(raw["embed"]["batch_size"]),
        max_chars=int(raw["embed"]["max_chars"]),
        vector_weight=float(raw["embed"]["vector_weight"]),
        query_instruct=str(raw["embed"]["query_instruct"]),
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

    search = SearchConfig(
        serper_api_key=str(raw["search"]["serper_api_key"]),
        timeout_sec=float(raw["search"]["timeout_sec"]),
        max_results=int(raw["search"]["max_results"]),
    )

    nightly = NightlyConfig(
        summary_limit=int(raw["nightly"]["summary_limit"]),
        tag_limit=int(raw["nightly"]["tag_limit"]),
        job_retention_days=int(raw["nightly"]["job_retention_days"]),
        embed_limit=int(raw["nightly"]["embed_limit"]),
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
        external_host=str(raw["web"]["external_host"]).strip().lower(),
        url_prefix="/" + str(raw["web"]["url_prefix"]).strip().strip("/") if str(raw["web"]["url_prefix"]).strip().strip("/") else "",
        external_mask=bool(raw["web"]["external_mask"]),
    )

    ingest = IngestConfig(
        enabled=bool(raw["ingest"]["enabled"]),
        secret=str(raw["ingest"]["secret"]),
        max_body_bytes=int(raw["ingest"]["max_body_bytes"]),
        clock_skew_sec=int(raw["ingest"]["clock_skew_sec"]),
    )

    private = PrivateConfig(
        default_minutes=int(raw["private"]["default_minutes"]),
        max_minutes=int(raw["private"]["max_minutes"]),
        max_purge_minutes=int(raw["private"]["max_purge_minutes"]),
        poll_sec=int(raw["private"]["poll_sec"]),
        purge_aw=bool(raw["private"]["purge_aw"]),
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
        embed=embed,
        collect=collect,
        web=web,
        nightly=nightly,
        search=search,
        ingest=ingest,
        private=private,
    )


def setup_logging(cfg: Config) -> None:
    """루트 로거에 stderr + 파일 핸들러를 단다. 중복 호출해도 핸들러가 쌓이지 않는다.

    ★ 파일 핸들러가 있는 이유 — **프로세스마다 stderr 가 가는 곳이 다르다.**
      `lt …` 의 stderr 는 journald 로 가지만, MCP 서버는 게이트웨이(OpenClaw)가
      삼켜서 어디에도 안 남는다. 그래서 바깥으로 나간 검색 30여 건이 **우리 로그에
      한 줄도 없었고, 남의 대시보드로만 발견됐다** (`docs/issues/0002`).
      어느 프로세스가 부르든 같은 파일에 남게 한다.

    파일은 `<data_dir>/lifetrainer.log` 다. 질의어·창 제목이 실리므로 **0600** 으로 둔다.
    파일을 못 열어도 죽지 않는다 — stderr 로만 남기고 경고한다.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(cfg.log_level)

    if not any(getattr(h, _LOG_HANDLER_MARK, False) for h in root_logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        setattr(handler, _LOG_HANDLER_MARK, True)
        root_logger.addHandler(handler)

    if not any(getattr(h, _FILE_HANDLER_MARK, False) for h in root_logger.handlers):
        try:
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            path = cfg.data_dir / LOG_FILE_NAME
            file_handler = RotatingFileHandler(
                path,
                maxBytes=_LOG_FILE_BYTES,
                backupCount=_LOG_FILE_BACKUPS,
                encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            setattr(file_handler, _FILE_HANDLER_MARK, True)
            root_logger.addHandler(file_handler)
            with contextlib.suppress(OSError):
                path.chmod(0o600)
        except OSError as exc:  # 디스크 문제로 앱이 죽으면 안 된다
            root_logger.warning("로그 파일을 열지 못했다 (%s) — stderr 로만 남는다", exc)

    for handler in root_logger.handlers:
        if getattr(handler, _LOG_HANDLER_MARK, False) or getattr(handler, _FILE_HANDLER_MARK, False):
            handler.setLevel(cfg.log_level)
