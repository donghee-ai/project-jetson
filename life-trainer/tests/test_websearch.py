"""lifetrainer.llm.websearch + web_search 툴 테스트. 네트워크 호출 없음.

여기서 지키려는 계약:

- **한국어 질의는 한국 구글로 간다** — 공급자는 Serper 하나이고, 한글이 섞이면
  `gl=kr&hl=ko` 를 붙인다. 실패 사례("롤체")가 국내 게임 용어였다.
- **어디로 갔는지 라벨에 드러난다** — 출처를 안 밝히는 것이
  `HISTORY/2026-08-18-search-fallback-noise.md` 의 사고였다.
- **0건은 0건이라고 말한다** — 무관한 것으로 채우지 않는다.
- **질의어는 바깥으로 나간다** — 길이를 잘라 맥락이 통째로 실리지 않게 한다.
"""

from __future__ import annotations

import dataclasses

import pytest

from lifetrainer import db
from lifetrainer.config import load_config
from lifetrainer.llm import tools as T
from lifetrainer.llm import websearch as W


@pytest.fixture()
def cfg(tmp_path):
    """검색 키를 **명시적으로 비운** 설정.

    ★ 전에는 `load_config()` 를 그대로 썼다. 그래서 이 기기의 실제
    `config/lifetrainer.toml` 에 키가 없다는 것에 조용히 기대고 있었고, 2026-08-21
    에 Serper 키를 넣자 "키 없음" 을 전제로 한 테스트 3개가 한꺼번에 깨졌다.
    키가 필요한 테스트는 `_with_keys` 로 직접 넣는다 — 기기 상태와 무관해야 한다.
    """
    base = load_config()
    return dataclasses.replace(
        base,
        db_path=tmp_path / "lt.db",
        data_dir=tmp_path,
        search=dataclasses.replace(base.search, serper_api_key=""),
    )


@pytest.fixture()
def conn(cfg):
    c = db.connect(cfg.db_path)
    db.init_db(c)
    yield c
    c.close()


def _with_keys(cfg, *, serper=True):
    return dataclasses.replace(
        cfg,
        search=dataclasses.replace(cfg.search, serper_api_key="key" if serper else ""),
    )


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


# ── 언어 판정 ────────────────────────────────────────────────────────────


def test_korean_query_detected():
    assert W.is_korean("롤체가 뭐야") is True
    assert W.is_korean("Qwen3 벤치마크 결과") is True  # 섞이면 한국어로 본다


def test_english_query_not_korean():
    assert W.is_korean("what is teamfight tactics") is False
    assert W.is_korean("") is False


# ── 공급자 ───────────────────────────────────────────────────────────────
# 2026-08-25 에 네이버 경로를 지웠다. 여기서 지키는 것은 "한국어도 같은 곳으로
# 가되 한국 구글로 간다" 와 "죽은 키가 되살아나지 않는다" 둘이다.


def test_korean_and_english_both_go_to_serper(cfg, monkeypatch):
    c = _with_keys(cfg)
    monkeypatch.setattr(
        W, "_serper", lambda *a, **k: W.SearchResults("serper", "구글 검색 (Serper 경유)", "q")
    )
    assert W.search(c, "롤토체스 챔피언").provider == "serper"
    assert W.search(c, "teamfight tactics patch notes").provider == "serper"


def test_the_dead_naver_path_is_gone(cfg):
    """★ 죽은 분기가 되살아나지 않게 못박는다 (2026-08-25).

    네이버 검색 API 는 NAVER API HUB 로 이관됐고 개발자센터 신규 신청이 닫혀
    우리는 키를 못 받는다. 코드에 남아 있던 옛 엔드포인트는 HUB 키로 401 이
    나므로, 설정 키가 살아 있으면 **누가 값을 채우는 순간 한국어 질의만 죽는다**
    (`HISTORY/2026-08-25-it-worked-because-the-key-was-empty.md`). 함수도 설정 키도 없어야 그 함정이 없다.
    """
    assert not hasattr(W, "_naver")
    assert not hasattr(W, "NAVER_ENDPOINT")
    assert not hasattr(cfg.search, "naver_client_id")


def test_no_key_raises_with_actionable_message(cfg):
    with pytest.raises(W.SearchError) as exc:
        W.search(_with_keys(cfg, serper=False), "무엇이든")
    assert "serper_api_key" in str(exc.value)


def test_providers_available_reports_serper(cfg):
    assert W.providers_available(_with_keys(cfg)) == {"serper": True}
    assert W.providers_available(_with_keys(cfg, serper=False))["serper"] is False


# ── 응답 파싱 ────────────────────────────────────────────────────────────


def test_serper_http_error_becomes_search_error(cfg, monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp({}, status=401))
    with pytest.raises(W.SearchError):
        W._serper(_with_keys(cfg), "질의", 5)


def test_serper_reads_organic_results(cfg, monkeypatch):
    payload = {"organic": [{"title": "TFT", "link": "https://x.com/a", "snippet": "auto battler"}]}
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(payload))
    result = W._serper(_with_keys(cfg), "tft", 5)
    assert result.hits[0].url == "https://x.com/a"
    assert result.provider == "serper"


def test_serper_pins_korea_locale_for_korean_query(cfg, monkeypatch):
    """한국어 질의는 gl=kr·hl=ko 로 나간다.

    Serper 기본값은 구글 미국/영어다. 네이버가 막힌 뒤(HISTORY/2026-08-25-it-worked-because-the-key-was-empty.md) 한국어
    질의가 전부 이리로 오므로, 이걸 안 붙이면 "롤체" 사고가 그대로 재현된다.
    """
    sent = {}

    def _post(url, **kwargs):
        sent.update(kwargs.get("json") or {})
        return _Resp({"organic": []})

    monkeypatch.setattr("requests.post", _post)
    W._serper(_with_keys(cfg), "롤체가 뭐야", 5)
    assert sent["gl"] == "kr" and sent["hl"] == "ko"


def test_serper_leaves_locale_alone_for_english_query(cfg, monkeypatch):
    sent = {}

    def _post(url, **kwargs):
        sent.update(kwargs.get("json") or {})
        return _Resp({"organic": []})

    monkeypatch.setattr("requests.post", _post)
    W._serper(_with_keys(cfg), "teamfight tactics patch notes", 5)
    assert "gl" not in sent and "hl" not in sent


def test_results_without_link_are_dropped(cfg, monkeypatch):
    payload = {"organic": [{"title": "제목만", "snippet": "..."}, {"title": "정상", "link": "https://ok"}]}
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(payload))
    assert len(W._serper(_with_keys(cfg), "q", 5).hits) == 1


# ── 질의어 보호 ──────────────────────────────────────────────────────────


def test_long_query_is_truncated_before_leaving_the_device(cfg, monkeypatch):
    """질의어는 바깥으로 나간다 — 맥락을 통째로 싣지 못하게 자른다."""
    seen = {}

    def fake(cfg_, query, limit):
        seen["query"] = query
        return W.SearchResults("serper", "구글 검색 (Serper 경유)", query)

    monkeypatch.setattr(W, "_serper", fake)
    W.search(_with_keys(cfg), "가" * 500)
    assert len(seen["query"]) <= W.MAX_QUERY_CHARS


def test_empty_query_raises(cfg):
    with pytest.raises(W.SearchError):
        W.search(_with_keys(cfg), "   ")


# ── 툴 계층 ──────────────────────────────────────────────────────────────


def test_tool_labels_the_engine_used(conn, cfg, monkeypatch):
    hits = [W.SearchHit("롤토체스 가이드", "https://ex.com/1", "전략적 팀 전투")]
    monkeypatch.setattr(
        W, "search", lambda *a, **k: W.SearchResults("serper", "구글 검색 (Serper 경유)", "롤체", hits)
    )
    ctx = T.ToolContext(conn=conn, cfg=_with_keys(cfg), today="2026-08-19")
    out = T.run_tool(ctx, "web_search", {"query": "롤체"})
    assert "[검색 · 구글 검색 (Serper 경유)]" in out
    assert "https://ex.com/1" in out
    assert "fetch_url" in out  # 본문은 따로 열어야 한다고 알려준다


def test_tool_says_zero_when_zero(conn, cfg, monkeypatch):
    monkeypatch.setattr(W, "search", lambda *a, **k: W.SearchResults("serper", "구글 검색 (Serper 경유)", "없는말", []))
    ctx = T.ToolContext(conn=conn, cfg=_with_keys(cfg), today="2026-08-19")
    out = T.run_tool(ctx, "web_search", {"query": "없는말"})
    assert "0건" in out
    assert "확인하지 못했다" in out


def test_tool_reports_missing_keys_as_text_not_exception(conn, cfg):
    """툴은 예외를 던지지 않는다 — 실패도 문장이어야 모델이 되물을 수 있다."""
    ctx = T.ToolContext(conn=conn, cfg=cfg, today="2026-08-19")  # 키 없음
    out = T.run_tool(ctx, "web_search", {"query": "무엇이든"})
    assert "검색 API 키" in out


def test_web_search_is_gated_and_paired_with_fetch_url():
    from lifetrainer.llm.trigger import select_tools

    assert "web_search" not in T.base_tool_names()  # 항상 싣지 않는다
    sel = select_tools("롤체가 뭐야?")
    assert "web_search" in sel.tool_names
    assert "fetch_url" in sel.tool_names  # 검색과 읽기는 함께 실린다


def test_personal_question_does_not_load_web_tools():
    """내 기록을 묻는 말에 웹 툴이 실리면 tool_choice=required 로 한 바퀴가 낭비된다."""
    from lifetrainer.llm.trigger import select_tools

    for q in ("내 오늘 일정이 뭐야", "이번 주 달성률 뭐야", "오늘 뭐 했지"):
        assert "web_search" not in select_tools(q).tool_names, q


def test_time_words_alone_do_not_block_web_tools():
    """★ "오늘자 뉴스 뭐 있어?" 는 바깥 조회다. 시간 낱말만으로 빼면 이게 죽는다.

    개인 기록 제외 규칙을 처음 넣었을 때 정확히 이 문장이 걸려 테스트가 잡아냈다.
    """
    from lifetrainer.llm.trigger import select_tools

    for q in ("오늘자 뉴스 뭐 있어?", "오늘 깃허브 트렌딩 1위", "어제 나온 릴리스 뭐야?"):
        assert "web_search" in select_tools(q).tool_names, q


# ── 검색이 불가능할 때 (2026-08-19 실사용 사고) ──────────────────────────
#
# 사용자가 Slack 에서 "롤체가 뭐야?" 를 물었을 때 모델이 **주입된 활동 요약을 읽고**
# "롤체는 사용자의 활동 분석 도구" 라고 답했다. 프롬프트로 막아봤지만("추측하지 마라")
# 8B 는 무시했다. 그래서 모델을 아예 부르지 않는 구조로 바꿨다.


def _converse(conn, cfg, text):
    from lifetrainer.llm.converse import converse

    class _NeverCalled:
        def chat(self, *a, **k):  # pragma: no cover - 불리면 그게 실패다
            raise AssertionError("검색이 불가능한데 LLM 을 불렀다")

    return converse(conn, cfg, _NeverCalled(), user_text=text, day="2026-08-19")


def test_refuses_without_calling_llm_when_no_search_and_no_url(conn, cfg):
    result = _converse(conn, cfg, "롤체가 뭐야?")
    assert result.ok is False
    assert result.rounds == 0
    assert "검색" in result.text and "[search]" in result.text  # 고치는 법까지 알려준다


def test_refusal_does_not_apply_to_known_sources(conn, cfg, monkeypatch):
    """툴 설명에 주소가 박힌 출처(깃허브 트렌딩 등)는 키 없이도 fetch_url 로 닿는다."""
    from lifetrainer.llm import converse as C

    assert C._must_refuse_web(cfg, "깃허브 트렌딩 1위 뭐야?") is False
    assert C._must_refuse_web(cfg, "https://example.com 열어줘") is False
    assert C._must_refuse_web(cfg, "롤체가 뭐야?") is True


def test_refusal_disappears_once_a_key_exists(cfg):
    """이 분기는 키를 넣는 순간 죽는다 — 임시 방편이라는 것을 테스트로 못박는다."""
    from lifetrainer.llm import converse as C

    assert C._must_refuse_web(_with_keys(cfg, serper=True), "롤체가 뭐야?") is False


# ── 짧은 되묻기의 검색어 (2026-08-21) ────────────────────────────────────
# "tft는 뭐야?" 가 앞 턴 맥락 없이 나가서 Task Force Team 을 물어왔다. 실측 사고다.


def _hist(*turns):
    """(user, assistant) 쌍을 history 형식으로."""
    out = []
    for user, assistant in turns:
        out.append({"role": "user", "content": user})
        out.append({"role": "assistant", "content": assistant})
    return out


def test_짧은_후속_질문에_직전_질문의_낱말이_붙는다():
    from lifetrainer.llm import converse as C

    h = _hist(("롤체는 무슨게임이야?", "롤체는 롤의 캐릭터입니다"))
    assert C._search_query("tft는 뭐야?", h) == "롤체 tft는 뭐야?"


def test_물음말이_붙은_덩어리는_검색어에_안_실린다():
    """한국어는 띄어쓰기가 헐거워 "무슨게임이야" 가 한 토큰으로 온다."""
    from lifetrainer.llm import converse as C

    assert "무슨게임이야" not in C._search_query("tft는 뭐야?", _hist(("롤체는 무슨게임이야?", "…")))


def test_모델_답변은_검색어에_안_실린다():
    """★ 붙이는 것은 사용자가 직접 쓴 질문뿐이다. 틀린 답이 다음 검색을 오염시키면 안 된다."""
    from lifetrainer.llm import converse as C

    h = _hist(("롤체는 무슨게임이야?", "롤체는 발로란트의 캐릭터입니다"))
    assert "발로란트" not in C._search_query("tft는 뭐야?", h)


def test_긴_질문은_그대로_나간다():
    from lifetrainer.llm import converse as C

    q = "인터넷 검색을 통해 롤체가 어떤 게임인지 알아봐"
    assert C._search_query(q, _hist(("전혀 다른 질문", "답"))) == q


def test_history_가_없으면_원문_그대로():
    from lifetrainer.llm import converse as C

    assert C._search_query("tft는 뭐야?", None) == "tft는 뭐야?"


def test_이미_있는_낱말은_중복해서_안_붙인다():
    from lifetrainer.llm import converse as C

    assert C._search_query("롤체는?", _hist(("롤체 좋아해?", "네"))).count("롤체") == 1


def test_기술_이름의_점은_살린다():
    """`llama.cpp` 가 `llamacpp` 로 뭉개지면 검색어로서 값이 없다."""
    from lifetrainer.llm import converse as C

    assert "llama.cpp" in C._search_query("그건 언제 나왔어?", _hist(("llama.cpp 최신 릴리스 뭐야?", "…")))


def test_prefetch_block_carries_results_and_source_label(cfg, monkeypatch):
    from lifetrainer.llm import converse as C

    hits = [W.SearchHit("롤토체스 - 나무위키", "https://namu.wiki/w/TFT", "전략적 팀 전투")]
    monkeypatch.setattr(
        C, "web_search", lambda *a, **k: W.SearchResults("serper", "구글 검색 (Serper 경유)", "롤체", hits)
    )
    block = C._prefetch_web(_with_keys(cfg), "롤체가 뭐야?")
    assert "구글 검색 (Serper 경유)" in block
    assert "https://namu.wiki/w/TFT" in block
    assert "확인하지 못했다" in block  # 결과가 안 맞으면 어떻게 할지도 적는다


def test_prefetch_says_so_when_search_fails(cfg, monkeypatch):
    from lifetrainer.llm import converse as C

    def boom(*a, **k):
        raise W.SearchError("네이버 검색 실패 (HTTP 401)")

    monkeypatch.setattr(C, "web_search", boom)
    block = C._prefetch_web(_with_keys(cfg), "롤체가 뭐야?")
    assert "실패" in block and "추측하지" in block


def test_prefetch_zero_hits_is_stated_not_filled(cfg, monkeypatch):
    from lifetrainer.llm import converse as C

    monkeypatch.setattr(
        C, "web_search", lambda *a, **k: W.SearchResults("serper", "구글 검색 (Serper 경유)", "없는말", [])
    )
    block = C._prefetch_web(_with_keys(cfg), "없는말")
    assert "0건" in block


# ── 나간 것은 반드시 기록에 남는다 (docs/issues/0002) ────────────────────
#
# 유출 30여 건이 우리 로그에 한 줄도 안 남고 남의 대시보드로만 발견됐다.
# 로그는 "몇 건 받았나"가 아니라 **무엇이 바깥으로 나갔나**를 남겨야 한다.


def test_검색은_보내기_전에_질의를_남긴다(cfg, monkeypatch, caplog):
    c = _with_keys(cfg)
    monkeypatch.setattr(
        W, "_serper", lambda *a, **k: W.SearchResults(
            provider="serper", label="구글 검색 (Serper 경유)", query="파이썬", hits=[]
        )
    )
    with caplog.at_level("INFO", logger="lifetrainer.llm.websearch"):
        W.search(c, "파이썬")

    나간기록 = [r for r in caplog.records if "바깥으로 나감" in r.getMessage()]
    assert len(나간기록) == 1
    assert "파이썬" in 나간기록[0].getMessage()


def test_검색이_실패해도_나간_질의가_남는다(cfg, monkeypatch, caplog):
    """★ 성공 뒤에만 남기면 실패한 요청이 기록에서 사라진다. 나간 것은 실패해도 나간 것이다."""
    c = _with_keys(cfg)

    def _boom(*a, **k):
        raise RuntimeError("연결 끊김")

    monkeypatch.setattr(W, "_serper", _boom)
    with caplog.at_level("INFO", logger="lifetrainer.llm.websearch"):
        with pytest.raises(W.SearchError):
            W.search(c, "실패할질의")

    나간기록 = [r for r in caplog.records if "바깥으로 나감" in r.getMessage()]
    assert len(나간기록) == 1, "실패한 요청도 나간 것은 나간 것이다"
    assert "실패할질의" in 나간기록[0].getMessage()


def test_키가_없으면_아무것도_안_나가고_기록도_없다(cfg, caplog):
    with caplog.at_level("INFO", logger="lifetrainer.llm.websearch"):
        with pytest.raises(W.SearchError):
            W.search(cfg, "안나가야함")

    assert not [r for r in caplog.records if "바깥으로 나감" in r.getMessage()]


# ── 게이트와 발송이 같은 문자열을 본다 (docs/issues/0001) ────────────────
#
# 게이트는 `user_text` 하나를 보고 판정하는데 나가는 값은 그 뒤에 다시 조립됐다.
# "오늘은?" 이 게이트를 열고, 직전 질문의 낱말이 앞에 붙어 개인 발화가 나갔다.


def test_게이트가_막는_문자열은_발송단계에서_만들어지지_않는다():
    """★ 이 저장소가 실제로 겪은 유출의 재현이다. 조립된 값이 게이트를 통과해야 한다."""
    from lifetrainer.llm import converse as C
    from lifetrainer.llm.trigger import select_tools

    h = _hist(("어제 뭐 했지?", "코딩 2시간."))
    나가는값 = C._search_query("오늘은?", h)

    assert 나가는값 is None, "게이트가 막는 말이 조립돼 나가면 안 된다"
    # 옛 동작을 못박아 둔다 — 이 문자열이 실제로 Serper 로 나갔다
    assert "web" not in select_tools("어제 했지 오늘은?").fired


def test_개인질문_이력의_낱말은_재료로_안_쓴다():
    """뺄 낱말 목록으로 거르던 것이 `어제` 를 빠뜨려 샜다. 이제 게이트로 판정한다."""
    from lifetrainer.llm import converse as C

    assert C._context_terms(_hist(("어제 뭐 했지?", "…"))) == []
    assert C._context_terms(_hist(("오늘 코딩 얼마나 했어?", "…"))) == []


def test_바깥질문_이력은_그대로_재료가_된다():
    """★ 막느라 정상 경로를 죽이면 안 된다 — 짧은 되묻기가 맥락을 잃는다."""
    from lifetrainer.llm import converse as C

    assert C._search_query("tft는 뭐야?", _hist(("롤체는 무슨 게임이야?", "…"))) == "롤체 tft는 뭐야?"


def test_개인질의는_나가기_직전에_막힌다(cfg, monkeypatch):
    """모든 경로가 합류하는 `search()` 에서 막는다 — 새 호출자가 생겨도 못 빠져나간다."""
    c = _with_keys(cfg)

    def _네트워크금지(*a, **k):
        raise AssertionError("개인 질의가 바깥으로 나갔다")

    monkeypatch.setattr(W, "_serper", _네트워크금지)
    for q in ("어제 했지 오늘은?", "오늘은?", "오늘 뭐 했지?"):
        with pytest.raises(W.SearchError, match="바깥으로 내보내지 않았다"):
            W.search(c, q)


def test_바깥질의는_막히지_않는다(cfg, monkeypatch):
    c = _with_keys(cfg)
    불린질의 = []
    monkeypatch.setattr(
        W, "_serper",
        lambda cfg, query, limit: 불린질의.append(query) or W.SearchResults(
            provider="serper", label="구글 검색 (Serper 경유)", query=query, hits=[]
        ),
    )
    for q in ("롤체는?", "게임 tft는?", "오늘자 뉴스 뭐 있어?"):
        W.search(c, q)
    assert 불린질의 == ["롤체는?", "게임 tft는?", "오늘자 뉴스 뭐 있어?"]
