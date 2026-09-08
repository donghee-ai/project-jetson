"""대화 오케스트레이터 — 3층 프롬프트를 조립하고 툴 루프를 돈다.

    ① 시스템   역할 + 오늘 계획 + 관심사      (하루 단위로만 변함)   context.build_day_context
    ② 툴       트리거가 고른 것만            (턴마다 변할 수 있음)  trigger.select_tools
    ③ user     현재 시각 + 질문              (매 턴 변함)          context.build_turn_context

배치 근거는 `context.py` 모듈 docstring 에 있다 — 변하는 주기가 짧은 것일수록 뒤에
두어야 프롬프트 캐시가 앞에서부터 재사용된다.

## 툴 루프를 왜 3바퀴로 묶는가

한 바퀴가 곧 프롬프트 재처리 + 생성이다. 8B 의 생성 속도가 8~9 tok/s 라
바퀴마다 수 초가 붙는다. 소형 모델은 목적 없이 툴을 반복 호출하는 경향이 있어
상한을 두지 않으면 사용자가 답을 못 받는다. 상한에 걸리면 그 사실을 숨기지 않고
"정리하지 못했다"고 말한다 — 조용히 그럴듯한 문장을 내놓는 것이 최악이다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING

from lifetrainer.llm.client import LLMClient, LLMError, LLMUnavailable
from lifetrainer.llm.context import build_day_context, build_turn_context
from lifetrainer.llm.interactive import interactive_turn
from lifetrainer.llm.websearch import SearchError, providers_available
from lifetrainer.llm.websearch import search as web_search
from lifetrainer.llm.tools import ToolContext, run_tool
from lifetrainer.llm.trigger import is_personal, select_tools

if TYPE_CHECKING:
    from lifetrainer.config import Config

logger = logging.getLogger(__name__)

MAX_ROUNDS = 3
MAX_PREFETCH_HITS = 3  # 주입 결과 수. 1건당 약 40토큰이라 3건이면 120토큰

# 검색 없이도 `fetch_url` 로 닿을 수 있는 질문 — 툴 설명에 주소가 박혀 있는 것들이다.
# 이 낱말이 있으면 검색 키가 없어도 모델에게 맡긴다 (08-18 에 실제로 동작했다).
_KNOWN_SOURCE = re.compile(
    r"https?://|깃허브|github|트렌딩|trending|해커\s*뉴스|hacker\s*news|급상승|구글\s*트렌드",
    re.I,
)

_NO_SEARCH_MESSAGE = (
    "지금은 웹 검색을 쓸 수 없어서 바깥 정보를 확인하지 못했습니다. "
    "(`config/lifetrainer.toml` 의 `[search]` 에 네이버 또는 Serper 키를 넣으면 됩니다.)"
)
MAX_HISTORY_MESSAGES = 6

# ── 짧은 되묻기의 검색어 ─────────────────────────────────────────────────
# "tft는 뭐야?" 는 앞 턴이 "롤체" 였다는 것을 모르면 검색이 안 된다 — 실제로
# Task Force Team(회사 용어)과 박막트랜지스터를 물어왔다 (2026-08-21 실측).
# 그래서 질문이 짧을 때만 **직전 사용자 질문의 낱말**을 앞에 붙인다.
#
# ★ 붙이는 것은 **사용자가 직접 쓴 질문**뿐이다. 모델 답변·활동 기록·창 제목·계획은
#   절대 싣지 않는다 (계약서의 "검색은 질의어만 내보낸다"). `history` 에는 원문만
#   들어 있어서(`slackio/app.py`) 활동 요약이 섞여 나갈 길이 없다.
SHORT_FOLLOWUP_CHARS = 15  # 이보다 짧을 때만 맥락을 붙인다
MAX_CONTEXT_TERMS = 2  # 낱말 2개까지. 길어지면 검색이 오히려 좁아진다

_JOSA = re.compile(r"(은|는|이|가|을|를|의|에서|에|으로|로|와|과|랑|이란|란)$")
_NOT_A_TERM = frozenset(
    """뭐 어디 언제 누구 왜 어떻게 그건 그거 이거 이건 저거 저건
    검색 대해 대한 관련 좀 다시 지금 오늘""".split()
)
# 한국어는 띄어쓰기가 헐거워서 물음말이 명사에 붙어 한 덩어리로 온다 —
# "무슨게임이야" 가 통째로 검색어에 실렸다. 물음·요청 꼬리가 섞인 토큰은 버린다.
_QUESTION_TOKEN = re.compile(r"(무슨|어떤|뭐야|뭐지|뭔지|뭔데|이야$|인가|일까|맞아|알려|알아봐|찾아)")


def _context_terms(history: list[dict] | None) -> list[str]:
    """직전 **사용자** 질문에서 검색에 쓸 낱말을 뽑는다 (최대 `MAX_CONTEXT_TERMS` 개).

    ★ 그 턴 자체가 **바깥 조회로 판정되지 않았으면 아무것도 주지 않는다.**
      개인 질문("어제 뭐 했지?")의 낱말이 다음 턴 검색어 앞에 붙어 나간 것이
      `docs/issues/0001` 이다. 빼는 낱말 목록(`_NOT_A_TERM`)으로 거르고 있었는데
      `어제` 가 목록에 없어서 그대로 통과했다 — **뺄 것을 세는 방식은 빠뜨리면 샌다.**
      "이 말이 바깥으로 나가도 되는가" 는 이미 게이트가 답하고 있으므로 그것을 쓴다.
    """
    for msg in reversed(history or []):
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content") or "")
        if "web" not in select_tools(content).fired:
            return []
        terms: list[str] = []
        for raw in content.split():
            # 양끝 문장부호만 떼고 안쪽 `.`·`+`·`-`·`#` 은 남긴다 — `llama.cpp`·`C++`
            # 같은 이름이 뭉개지면 검색어로서 값이 없다.
            word = re.sub(r"^[^0-9A-Za-z가-힣]+|[^0-9A-Za-z가-힣]+$", "", raw)
            word = _JOSA.sub("", re.sub(r"[^0-9A-Za-z가-힣.+#-]", "", word))
            if len(word) < 2 or word in _NOT_A_TERM or _QUESTION_TOKEN.search(word):
                continue
            terms.append(word)
            if len(terms) >= MAX_CONTEXT_TERMS:
                break
        return terms
    return []


def _search_query(user_text: str, history: list[dict] | None) -> str | None:
    """검색에 실제로 나갈 문자열. **내보내면 안 되는 것이면 `None`.**

    ★ 조립한 결과를 **게이트에 다시 물린다.** 게이트는 `user_text` 하나를 보고
      판정하는데 나가는 값은 여기서 다시 만들어진다. 그 둘이 달라서 새어나갔다
      (`docs/issues/0001`). 판정한 것과 보내는 것이 같아야 한다.
    """
    query = (user_text or "").strip()
    if len(query) < SHORT_FOLLOWUP_CHARS:
        lowered = query.lower()
        extra = [t for t in _context_terms(history) if t.lower() not in lowered]
        if extra:
            query = f"{' '.join(extra)} {query}".strip()

    if not query or is_personal(query):
        return None
    return query
MAX_TOKENS = 400
TEMPERATURE = 0.3

# 짧게 유지하는 것이 이 프롬프트의 존재 이유다. OpenClaw 는 시스템 프롬프트가
# 12,541 토큰이라 매 턴 25~40초였다 (`operate/notes/agent-gateway.md §3`). 여기에 문장을
# 더하고 싶어지면, 그 문장이 몇 초짜리인지 먼저 생각할 것.
SYSTEM_PREAMBLE = """너는 사용자의 활동 기록과 일정을 아는 개인 비서다.
- 아래 [오늘 계획]과 [관심사]는 이미 알고 있는 정보다. 그것들은 툴로 다시 조회하지 마라.
- 다른 날짜의 기록이나 문서가 필요하면 툴을 부른다.
- 3문장 이내로 짧고 구체적으로 답한다.
- **[배경]은 참고 자료지 대화 상대가 아니다.** 사용자가 묻지 않은 계획·활동을 먼저 꺼내지 마라.
  "심심해" 같은 잡담·인사에는 계획을 읊지 말고 그냥 사람처럼 대화한다.
- **직전에 한 답을 그대로 반복하지 마라.** 사용자가 되물으면("그래서?", "그래서 못한다고?")
  같은 문장을 다시 쓰지 말고 그 되물음에 답한다.
- 툴 결과와 위 정보에 없는 내용은 지어내지 마라. 모르면 모른다고 한다.
- 숫자는 주어진 값을 그대로 인용한다. 비율·합계·차이를 새로 계산하지 마라.
- **바깥 세상 정보(순위·트렌드·뉴스·조회수·최신 버전)는 툴로 직접 확인한 것만 말한다.**
  기억으로 답하지 마라. 툴이 못 가져왔으면 "확인하지 못했다"고 그대로 말한다.
- 툴 결과를 근거로 답할 때는 **출처 URL 을 함께 적는다.**
- 질문이 지목한 곳에서 못 가져와 다른 출처로 대신했으면, 무엇으로 대신했는지 밝힌다.
  (예: 유튜브 급상승을 못 읽어 구글 검색어 트렌드를 봤다면 그렇게 말한다)"""

_UNAVAILABLE_MESSAGE = "지금 LLM 서버에 연결할 수 없습니다. 잠시 뒤에 다시 물어봐 주세요."
_ROUNDS_EXHAUSTED_MESSAGE = "조회는 했는데 답을 정리하지 못했습니다. 좀 더 구체적으로 물어봐 주세요."


@dataclass
class ConverseResult:
    """대화 한 턴의 결과와 계측값."""

    text: str
    tools_used: list[str] = field(default_factory=list)
    tools_offered: list[str] = field(default_factory=list)
    triggers_fired: list[str] = field(default_factory=list)
    rounds: int = 0
    latency_ms: int = 0
    ok: bool = True


def converse(
    conn: sqlite3.Connection,
    cfg: "Config",
    client: LLMClient,
    *,
    user_text: str,
    day: str,
    channel: str | None = None,
    actor: str = "user",
    history: list[dict] | None = None,
    max_rounds: int = MAX_ROUNDS,
    on_progress: "Callable[[str], None] | None" = None,
) -> ConverseResult:
    """자연어 한 턴을 처리한다. 툴이 필요하면 부르고, 결과를 문장으로 돌려준다.

    `on_progress` 는 **이번 라운드까지 쌓인 본문 전체**를 받는다(조각이 아니다).
    라운드가 바뀌면 빈 문자열부터 다시 시작한다 — 툴을 부르느라 버려지는 라운드의
    글이 화면에 남지 않게 하려는 것이다. 호출부는 받은 것을 그대로 그리기만 하면
    되고, 마지막에 오는 `ConverseResult.text` 가 최종본이다.

    `history` 는 이전 턴들의 `{"role", "content"}` 목록이다 (툴 메시지는 넣지 않는다 —
    다음 턴에 다시 실을 가치보다 토큰 비용이 크다).

    턴 전체를 `interactive_turn` 안에서 돈다 — 야간 배치가 도는 중에도 워커가
    다음 GPU 잡을 집지 않게 해서 이 턴이 요약 뒤에 줄 서지 않게 한다.
    """
    with interactive_turn(cfg):
        return _converse_turn(
            conn,
            cfg,
            client,
            user_text=user_text,
            day=day,
            channel=channel,
            actor=actor,
            history=history,
            max_rounds=max_rounds,
            on_progress=on_progress,
        )


def _must_refuse_web(cfg: "Config", user_text: str) -> bool:
    """바깥 정보를 물었는데 **닿을 방법이 아예 없는가.**

    검색 키가 하나도 없고, 사용자가 주소를 준 것도 아니고, 툴 설명에 주소가 박힌
    출처(깃허브 트렌딩·해커뉴스 등)를 가리킨 것도 아니면 — 모델이 할 수 있는 건
    도메인을 지어내거나 기억으로 답하는 것뿐이다. 둘 다 이미 사고를 냈다.

    키가 하나라도 있으면 여기서 막지 않는다. 즉 이 분기는 **키를 넣는 순간 죽는다.**
    """
    if any(providers_available(cfg).values()):
        return False
    return not _KNOWN_SOURCE.search(user_text or "")


def _prefetch_web(cfg: "Config", user_text: str, history: list[dict] | None = None) -> str:
    """웹 트리거가 걸린 턴에 검색을 **미리 돌려** 결과를 user 메시지에 실을 문자열로.

    ## 왜 툴에 맡기지 않는가

    계약서의 오래된 규칙 그대로다 — **항상 필요한 정보는 툴이 아니라 주입한다.**
    소형 모델은 (a) 툴을 안 부르고 아는 척하거나, (b) 부르더라도 그 전에 자기 답을
    한 문단 써 버린다. (b) 는 실측으로 확인했다: "롤체가 뭐야" 에 틀린 답 300토큰을
    쓰고 나서 `web_search` 를 불렀다. 버려지는 프로즈지만 8~9 tok/s 이므로 35초다.

    선주입하면 첫 호출에 이미 사실이 들어 있어 툴 한 바퀴(8~20초)가 통째로 사라진다.

    ## 실패해도 문장을 남긴다

    키가 없거나 검색이 실패하면 **그 사실을 적어 넣는다.** 아무 말도 안 남기면
    모델이 빈자리를 자기 기억으로 채운다 — 실제로 "롤체는 사용자의 활동 분석 도구"
    라고 답했다(주입된 활동 요약을 읽고 지어낸 것이다).
    """
    if not any(providers_available(cfg).values()):
        return (
            "[웹 검색 불가] 검색 키가 설정돼 있지 않아 바깥 정보를 확인할 수 없다. "
            "추측하지 말고 확인하지 못했다고 답한다.\n"
        )
    query = _search_query(user_text, history)
    if query is None:
        # 내보내면 안 되는 말이다. **검색 실패가 아니므로 실패 문장을 넣지 않는다** —
        # 넣으면 모델이 "확인하지 못했다"로 답한다. 자기 기록으로 답하면 되는 질문이다.
        logger.info("검색 안 함 — 개인 기록 질문 (%r)", user_text)
        return ""
    try:
        result = web_search(cfg, query, limit=MAX_PREFETCH_HITS)
    except SearchError as exc:
        logger.warning("선주입 검색 실패: %s", exc)
        return f"[웹 검색 실패: {exc}] 추측하지 말고 확인하지 못했다고 답한다.\n"

    if not result.hits:
        return (
            f"[{result.label}] '{result.query}' 검색 결과 0건. "
            "추측하지 말고 확인하지 못했다고 답한다.\n"
        )

    lines = [f"[{result.label}] '{result.query}' 검색 결과:"]
    for i, hit in enumerate(result.hits, 1):
        lines.append(f"{i}. {hit.title} — {hit.url}")
        if hit.snippet:
            lines.append(f"   {hit.snippet}")
    lines.append(
        "위 검색 결과만 근거로 답하고 출처 URL 을 함께 적는다. 결과가 질문과 맞지 않으면 "
        "확인하지 못했다고 답한다. 본문이 더 필요하면 fetch_url 로 연다.\n"
    )
    return "\n".join(lines)


def _last_assistant(history: list[dict] | None) -> str:
    for msg in reversed(history or []):
        if msg.get("role") == "assistant":
            return str(msg.get("content") or "")
    return ""


def _is_repeat(text: str, previous: str) -> bool:
    """직전 답을 사실상 그대로 되풀이했는가.

    ★ 8B 는 되묻기("그래서?", "그래서 못한다고?")에 **직전 답을 그대로 복사한다.**
    프롬프트에 "반복하지 마라"를 넣어도 줄어들 뿐 안 없어졌다 — 그래서 생성 뒤에
    한 번 보고, 걸리면 되물음에 답하라는 지시를 붙여 **딱 한 번** 다시 생성한다.
    (프롬프트로 안 되는 것은 구조로 막는다 — 이 저장소가 네 번째로 겪는 부류다.)
    """
    if not text or not previous:
        return False
    norm = lambda t: re.sub(r"[\s.!?~…,]+", "", t)
    a, b = norm(text), norm(previous)
    if not a or not b:
        return False
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.9


# 프롬프트 뼈대가 그대로 답에 실려 나오는 것을 잡는다.
#
# ★ 실측 사고 (2026-08-23): "내일 계획 뭐 있어?" 에 8B 가
#   "[오늘 2026-08-23 계획 — 사람이 세운 의도] 12:00~13:00 예시 일정 …" 를
#   **그대로 출력**했다. 주입 블록을 답으로 착각한 것이다.
#
#   근본 원인(물어본 날 계획이 안 실렸던 것)은 `context.referenced_day` 로 고쳤다.
#   이건 그 뒤에 두는 **안전망**이다 — 라벨은 사용자가 볼 것이 아니므로,
#   무엇이 원인이든 밖으로 나가면 안 된다.
_SCAFFOLD_RE = re.compile(
    r"^\s*\[[^\]\n]*(사람이 세운 의도|기계 측정|배경 —|사용자의 말|관심사)[^\]\n]*\]",
    re.MULTILINE,
)


def _has_scaffolding(text: str) -> bool:
    return bool(_SCAFFOLD_RE.search(text or ""))


def _strip_scaffolding(text: str) -> str:
    """라벨이 붙은 줄을 통째로 지운다. 라벨만 떼면 남은 값이 답인 척한다."""
    kept = [ln for ln in (text or "").splitlines() if not _SCAFFOLD_RE.match(ln)]
    return "\n".join(kept).strip()


_SCAFFOLD_NUDGE = (
    "방금 만든 답에 `[...]` 로 감싼 **내부 라벨**이 그대로 들어갔다. "
    "그건 너에게 주는 배경 표시이지 사용자에게 보일 문장이 아니다. "
    "라벨 없이, 사용자가 물어본 날짜의 내용만 자연스러운 문장으로 다시 쓴다."
)

_RETRY_NUDGE = (
    "방금 만든 답이 직전 답과 똑같았다. **그 문장을 버리고 완전히 다르게 쓴다.** "
    "사용자는 되묻고 있으므로, 계획·활동을 다시 읊지 말고 그 되물음 자체에 답한다."
)
RETRY_TEMPERATURE = 0.8  # 재생성은 결정성을 깨야 한다
_REPEAT_MESSAGE = (
    "같은 답을 반복하고 있습니다. 무엇을 알고 싶은지 조금만 더 구체적으로 말해 주세요 "
    "(예: \"오늘 코딩 얼마나 했어?\", \"지금 뭐 해야 해?\")."
)


def _converse_turn(
    conn: sqlite3.Connection,
    cfg: "Config",
    client: LLMClient,
    *,
    user_text: str,
    day: str,
    channel: str | None,
    actor: str,
    history: list[dict] | None,
    max_rounds: int,
    on_progress: "Callable[[str], None] | None" = None,
) -> ConverseResult:
    """`converse` 의 본체. 우선권 락 밖에서는 직접 부르지 않는다."""
    started = time.monotonic()
    selection = select_tools(user_text)
    tool_ctx = ToolContext(
        conn=conn, cfg=cfg, today=day, actor=actor, channel=channel, user_text=user_text
    )

    day_ctx = build_day_context(conn, cfg, day)
    system = f"{SYSTEM_PREAMBLE}\n\n{day_ctx.to_prompt()}"
    turn_ctx = build_turn_context(conn, cfg, day, user_text=user_text)

    # ★ 바깥 정보를 묻는 턴은 **검색을 먼저 해서 결과를 싣는다.** 툴로 맡기지 않는다.
    web_block = ""
    if "web" in selection.fired:
        if _must_refuse_web(cfg, user_text):
            # ★ 모델을 아예 부르지 않는다. 검색 키가 없고 주소도 없으면 이 질문에
            # 닿을 방법이 **구조적으로 없다.** 그 상태에서 모델을 부르면 빈자리를
            # 자기 기억으로 채운다 — 주입 문장으로 막아봤지만 8B 는 무시하고
            # "롤체는 인공지능 모델의 파라미터를 조정하는 기술" 이라고 답했다.
            # 프롬프트로 안 되는 것은 구조로 막는다(이 저장소가 세 번째로 겪는 부류).
            return ConverseResult(
                text=_NO_SEARCH_MESSAGE,
                tools_offered=selection.tool_names,
                triggers_fired=selection.fired,
                rounds=0,
                latency_ms=int((time.monotonic() - started) * 1000),
                ok=False,
            )
        web_block = _prefetch_web(cfg, user_text, history)

    messages: list[dict] = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-MAX_HISTORY_MESSAGES:])
    # ★ 배경과 사용자의 말을 **눈에 보이게 가른다.**
    #
    # 전에는 `{배경}\n{질문}` 으로 이어 붙였다. 모델 입장에서 "심심해" 는 마지막 한 줄이고
    # 그 앞의 계획·활동 세 줄이 더 크게 보여서, 잡담에도 "지금은 문서 정리 시간입니다" 를
    # 읊었다(2026-08-22 실측). 라벨을 붙여 무엇이 질문인지 분명히 한다.
    messages.append(
        {
            "role": "user",
            "content": f"[배경 — 참고용. 묻지 않았으면 먼저 꺼내지 마라]\n{turn_ctx}\n"
            f"{web_block}[사용자의 말]\n{user_text}",
        }
    )

    schemas = selection.schemas
    tools_used: list[str] = []
    retried = False  # 반복 감지 재생성은 한 번만 한다 (턴당 비용 상한)

    for round_index in range(max_rounds):
        # ★ `tool_choice="required"` 를 **기본으로 쓰지 않는다** (2026-08-19 정정).
        #
        # 08-18 에 "required 면 첫 바퀴에 툴을 부른다" 고 적었는데, 실측해 보니
        # llama.cpp 는 **본문을 먼저 쓰고 그 뒤에** 툴 호출을 붙인다. "롤체가 뭐야"
        # 에서 모델이 틀린 답을 300토큰 쓰고 나서야 툴을 불렀다 — 그 프로즈는
        # 버려지지만 생성 8~9 tok/s 라 **35초가 그냥 날아간다.** 예산(400토큰)이
        # 먼저 끝나면 툴 호출은 아예 안 나오고 그 틀린 프로즈가 답이 된다.
        #
        # 그래서 바깥 정보는 위에서 **선주입**하고, 강제는 사용자가 URL 을 직접 준
        # 경우에만 남긴다 (그때는 열어야 할 대상이 명확하다).
        force_tool = round_index == 0 and "http" in user_text.lower()

        # 라운드마다 처음부터 다시 쌓는다. 툴을 부르느라 버려질 라운드의 글이
        # 화면에 남으면 안 된다 — 호출부는 마지막으로 받은 것만 그리면 된다.
        on_delta = None
        if on_progress is not None:
            buf: list[str] = []
            on_progress("")

            def on_delta(piece: str, _buf=buf) -> None:  # noqa: ANN001
                _buf.append(piece)
                on_progress("".join(_buf))

        try:
            response = client.chat(
                messages,
                tools=schemas,
                max_tokens=MAX_TOKENS,
                temperature=RETRY_TEMPERATURE if retried else TEMPERATURE,
                purpose="converse",
                tool_choice="required" if force_tool else None,
                on_delta=on_delta,
            )
        except LLMUnavailable:
            logger.warning("대화 중 LLM 서버에 연결할 수 없습니다")
            return ConverseResult(
                text=_UNAVAILABLE_MESSAGE,
                tools_used=tools_used,
                tools_offered=selection.tool_names,
                triggers_fired=selection.fired,
                rounds=round_index,
                latency_ms=int((time.monotonic() - started) * 1000),
                ok=False,
            )
        except LLMError as exc:
            logger.exception("대화 중 LLM 오류")
            return ConverseResult(
                text=f"답을 만들지 못했습니다: {exc}",
                tools_used=tools_used,
                tools_offered=selection.tool_names,
                triggers_fired=selection.fired,
                rounds=round_index,
                latency_ms=int((time.monotonic() - started) * 1000),
                ok=False,
            )

        if not response.tool_calls:
            answer = response.text.strip()
            if answer and retried and _is_repeat(answer, _last_assistant(history)):
                # 한 번 더 시켰는데도 같은 문장이다. **중복을 내보내지 않는다** —
                # 사용자가 보는 것은 "또 같은 말"이고, 그게 이 기능의 불만이었다.
                logger.warning("재생성 후에도 직전 답과 동일 — 되묻기 안내로 대체한다")
                return ConverseResult(
                    text=_REPEAT_MESSAGE,
                    tools_used=tools_used,
                    tools_offered=selection.tool_names,
                    triggers_fired=selection.fired,
                    rounds=round_index + 1,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    ok=False,
                )
            if answer and _has_scaffolding(answer):
                if not retried:
                    logger.info("프롬프트 뼈대 유출 감지 — 한 번 다시 생성한다")
                    retried = True
                    messages.append({"role": "system", "content": _SCAFFOLD_NUDGE})
                    continue
                # 다시 시켜도 그대로다. 남은 문장만 내보낸다 — 라벨을 사용자에게
                # 보이느니 짧은 답이 낫다.
                logger.warning("재생성 후에도 뼈대가 남아 있어 걷어낸다")
                answer = _strip_scaffolding(answer)
                if not answer:
                    answer = "지금 계획을 제대로 읽지 못했습니다. 다시 물어봐 주세요."
            if answer and not retried and _is_repeat(answer, _last_assistant(history)):
                logger.info("직전 답 반복 감지 — 되물음에 답하도록 한 번 다시 생성한다")
                retried = True
                # 사용자 메시지에 덧붙이는 것만으로는 안 먹혔다(실측: 같은 답 재생산).
                # 별도 system 지시로 올리고 온도도 함께 올려 결정성을 깬다.
                messages.append({"role": "system", "content": _RETRY_NUDGE})
                continue
            return ConverseResult(
                text=answer or _ROUNDS_EXHAUSTED_MESSAGE,
                tools_used=tools_used,
                tools_offered=selection.tool_names,
                triggers_fired=selection.fired,
                rounds=round_index + 1,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        # 어시스턴트 턴은 서버가 준 원문을 그대로 되돌려 준다 — 우리가 재조립하면
        # tool_calls 의 id/형식이 어긋나 다음 요청이 400 이 될 수 있다.
        messages.append(response.raw["choices"][0]["message"])

        for call in response.tool_calls:
            if call.parse_error:
                result = (
                    f"오류: 인자를 읽지 못했습니다 ({call.parse_error}). "
                    "인자를 올바른 JSON 으로 다시 주세요."
                )
            else:
                result = run_tool(tool_ctx, call.name, call.arguments)
                tools_used.append(call.name)
            logger.info("툴 %s(%s) → %s", call.name, call.arguments, result[:120])
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )

    # 상한까지 돌았는데도 문장이 안 나왔다. 지어내지 말고 그대로 말한다.
    logger.warning("툴 루프 상한(%d)에 걸렸습니다 (사용한 툴: %s)", max_rounds, tools_used)
    return ConverseResult(
        text=_ROUNDS_EXHAUSTED_MESSAGE,
        tools_used=tools_used,
        tools_offered=selection.tool_names,
        triggers_fired=selection.fired,
        rounds=max_rounds,
        latency_ms=int((time.monotonic() - started) * 1000),
        ok=False,
    )
