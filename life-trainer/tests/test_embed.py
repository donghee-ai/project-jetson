"""lifetrainer.llm.embed — 문서 임베딩(RAG) 테스트.

**네트워크 금지.** 임베딩 서버 호출(`embed_texts`)은 monkeypatch 로 막는다.
여기서 검증하는 것은 "서버가 무엇을 돌려주든 우리가 옳게 저장하고 옳게 고르는가"다.

★ 이 파일이 지키는 가장 중요한 성질: **임베딩이 없어도 죽지 않는다.**
이 기기는 메모리가 빠듯해서 임베딩 서버를 내려야 할 날이 온다.
"""

from __future__ import annotations

import dataclasses
import struct
import time

import pytest

from lifetrainer import db
from lifetrainer.config import load_config
from lifetrainer.llm import embed as E

DIM = 8


@pytest.fixture()
def cfg(tmp_path):
    base = load_config()
    return dataclasses.replace(
        base,
        db_path=tmp_path / "lt.db",
        data_dir=tmp_path,
        embed=dataclasses.replace(base.embed, enabled=True, dim=DIM, batch_size=2, max_chars=100),
    )


@pytest.fixture()
def conn(cfg):
    c = db.open_db(cfg)
    yield c
    c.close()


def _doc(conn, doc_id, title, abstract="", summary=None, score=1.0, dup_of=None):
    now = time.time()
    conn.execute(
        "INSERT INTO doc(id, source_id, kind, url, title, abstract, summary, score, dup_of, "
        "fetched_at, content_hash, state) "
        "VALUES (?, NULL, 'rss', ?, ?, ?, ?, ?, ?, ?, ?, 'fetched')",
        (doc_id, f"https://x/{doc_id}", title, abstract, summary, score, dup_of, now, f"h{doc_id}"),
    )
    conn.commit()


def _fake_vectors(monkeypatch, value=1.0):
    """서버 대신 고정 벡터를 준다. 첫 성분만 다르게 해 문서를 구분한다."""
    calls = []

    def _embed(cfg, texts):
        calls.append(list(texts))
        return [[value + i] + [0.0] * (DIM - 1) for i, _ in enumerate(texts)]

    monkeypatch.setattr(E, "embed_texts", _embed)
    return calls


# ── 원문 만들기 ───────────────────────────────────────────────────────────


def test_원문은_fts_와_같은_세_칼럼을_잇는다(conn, cfg):
    _doc(conn, 1, "제목", "초록", "요약")
    row = conn.execute("SELECT title, abstract, summary FROM doc WHERE id=1").fetchone()
    assert E.source_text(row, 999) == "제목\n초록\n요약"


def test_원문은_상한에서_잘린다(conn, cfg):
    _doc(conn, 1, "가" * 500, "", None)
    row = conn.execute("SELECT title, abstract, summary FROM doc WHERE id=1").fetchone()
    assert len(E.source_text(row, 100)) == 100


def test_빈_칼럼은_건너뛴다(conn, cfg):
    _doc(conn, 1, "제목", "", None)
    row = conn.execute("SELECT title, abstract, summary FROM doc WHERE id=1").fetchone()
    assert E.source_text(row, 999) == "제목"


# ── 무엇을 다시 임베딩하나 ────────────────────────────────────────────────


def test_요약이_붙으면_다시_임베딩한다(conn, cfg, monkeypatch):
    """★ 이 테스트가 이 기능의 핵심이다.

    야간 배치가 요약을 나중에 채운다. 원문이 바뀐 것을 못 잡으면 검색이 영영
    요약을 못 본다 — 구현 초기에 SQL 이 해시를 안 봐서 실제로 그랬다.
    """
    _doc(conn, 1, "제목", "초록", None)
    _fake_vectors(monkeypatch)
    assert E.embed_pending(conn, cfg).embedded == 1
    assert E.pending_docs(conn, cfg, 10) == []

    conn.execute("UPDATE doc SET summary='새 요약' WHERE id=1")
    conn.commit()
    assert len(E.pending_docs(conn, cfg, 10)) == 1, "요약이 붙었는데 재임베딩 대상이 아니다"


def test_같은_원문은_다시_안_한다(conn, cfg, monkeypatch):
    _doc(conn, 1, "제목", "초록", "요약")
    calls = _fake_vectors(monkeypatch)
    E.embed_pending(conn, cfg)
    E.embed_pending(conn, cfg)
    assert len(calls) == 1, "원문이 그대로인데 서버를 두 번 불렀다"


def test_모델이_바뀌면_전부_다시(conn, cfg, monkeypatch):
    _doc(conn, 1, "제목")
    _fake_vectors(monkeypatch)
    E.embed_pending(conn, cfg)
    other = dataclasses.replace(cfg, embed=dataclasses.replace(cfg.embed, model="다른모델"))
    assert len(E.pending_docs(conn, other, 10)) == 1


def test_중복_문서는_임베딩하지_않는다(conn, cfg, monkeypatch):
    _doc(conn, 1, "원본")
    _doc(conn, 2, "복제본", dup_of=1)
    _fake_vectors(monkeypatch)
    assert E.embed_pending(conn, cfg).embedded == 1


# ── 저장과 검색 ───────────────────────────────────────────────────────────


def test_차원이_다르면_저장하지_않는다(conn, cfg, monkeypatch):
    """모델을 바꿨는데 설정을 안 고치면 조용히 틀린 유사도가 나온다."""
    _doc(conn, 1, "제목")
    monkeypatch.setattr(E, "embed_texts", lambda cfg, texts: [[0.0] * (DIM + 3) for _ in texts])
    r = E.embed_pending(conn, cfg)
    assert r.embedded == 0 and r.failed == 1
    assert conn.execute("SELECT COUNT(*) FROM doc_embedding").fetchone()[0] == 0


def test_가장_가까운_것을_점수순으로_준다(conn, cfg, monkeypatch):
    for i in (1, 2, 3):
        _doc(conn, i, f"문서{i}")
    _fake_vectors(monkeypatch)
    E.embed_pending(conn, cfg)

    hits = E.nearest(conn, cfg, [1.0] + [0.0] * (DIM - 1), 3)
    assert len(hits) == 3
    assert hits[0][1] >= hits[1][1] >= hits[2][1], "점수가 내림차순이 아니다"


def test_벡터가_없으면_빈손을_준다(conn, cfg):
    assert E.nearest(conn, cfg, [1.0] + [0.0] * (DIM - 1), 3) == []


# ── 죽지 않는가 ───────────────────────────────────────────────────────────


def test_꺼져_있으면_아무것도_안_한다(conn, cfg, monkeypatch):
    off = dataclasses.replace(cfg, embed=dataclasses.replace(cfg.embed, enabled=False))
    _doc(conn, 1, "제목")
    monkeypatch.setattr(E, "embed_texts", lambda *a, **k: pytest.fail("불러선 안 된다"))
    assert E.embed_pending(conn, off).embedded == 0


def test_질의_임베딩이_실패해도_None_을_준다(conn, cfg, monkeypatch):
    """★ 검색이 죽으면 안 된다. 키워드 쪽으로 떨어져야 한다."""
    def _boom(cfg, texts):
        raise E.EmbedUnavailable("서버 없음")

    monkeypatch.setattr(E, "embed_texts", _boom)
    assert E.embed_query(cfg, "무엇이든") is None


def test_배치_하나가_실패해도_나머지는_계속한다(conn, cfg, monkeypatch):
    for i in (1, 2, 3, 4):
        _doc(conn, i, f"문서{i}")

    state = {"n": 0}

    def _flaky(cfg, texts):
        state["n"] += 1
        if state["n"] == 1:
            raise E.EmbedError("이 배치만 실패")
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    monkeypatch.setattr(E, "embed_texts", _flaky)
    r = E.embed_pending(conn, cfg)
    assert r.failed == 2 and r.embedded == 2, "한 배치 실패로 전부 멈췄다"


def test_서버가_죽으면_멈춘다(conn, cfg, monkeypatch):
    """배치 실패는 넘어가지만 **서버가 없으면** 4천 건을 헛돌 이유가 없다."""
    for i in (1, 2):
        _doc(conn, i, f"문서{i}")
    monkeypatch.setattr(
        E, "embed_texts", lambda *a, **k: (_ for _ in ()).throw(E.EmbedUnavailable("없음"))
    )
    with pytest.raises(E.EmbedUnavailable):
        E.embed_pending(conn, cfg)


def test_저장_형식은_float32_리틀엔디언(conn, cfg, monkeypatch):
    """`nearest` 가 numpy 로 한 번에 읽으므로 형식이 어긋나면 조용히 틀린다."""
    _doc(conn, 1, "제목")
    _fake_vectors(monkeypatch)
    E.embed_pending(conn, cfg)
    blob = conn.execute("SELECT vec FROM doc_embedding WHERE doc_id=1").fetchone()[0]
    assert len(blob) == DIM * 4
    assert struct.unpack(f"<{DIM}f", blob)[0] == pytest.approx(1.0)


# ── 하이브리드 검색 (tools.search_docs) ───────────────────────────────────
# ★ 지키려는 성질: 벡터가 없거나 서버가 죽어도 **검색이 계속 된다.**


def _ctx(conn, cfg):
    from lifetrainer.llm.tools import ToolContext

    return ToolContext(conn=conn, cfg=cfg, today="2026-08-23", actor="t", channel=None)


def test_임베딩_없이도_키워드로_찾는다(conn, cfg, monkeypatch):
    from lifetrainer.llm.tools import run_tool

    _doc(conn, 1, "젯슨 오린 벤치마크", "메모리 대역폭 실측")
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)  # 서버 없음
    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "젯슨", "limit": 3})
    assert "젯슨 오린 벤치마크" in out
    assert "키워드" in out


def test_검색_방식을_결과에_밝힌다(conn, cfg, monkeypatch):
    """무엇으로 찾았는지 밝히지 않으면 모델이 그것을 사실로 취급한다."""
    from lifetrainer.llm.tools import run_tool

    _doc(conn, 1, "젯슨 오린 벤치마크")
    _fake_vectors(monkeypatch)
    E.embed_pending(conn, cfg)
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: [1.0] + [0.0] * (DIM - 1))
    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "젯슨", "limit": 3})
    assert "키워드+벡터" in out


def test_요약이_결과에_실린다(conn, cfg, monkeypatch):
    """rag-plan 2단계. 제목·URL 만 주면 '링크 목록'이지 근거가 아니다."""
    from lifetrainer.llm.tools import run_tool

    _doc(conn, 1, "제목", "초록", "이것이 요약이다")
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)
    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "제목", "limit": 3})
    assert "이것이 요약이다" in out


def test_요약의_마크업은_걷어낸다(conn, cfg, monkeypatch):
    """수집한 글의 요약에 <ul><li> 가 그대로 들어 있다 — 토큰을 먹고 모델이 따라 쓴다."""
    from lifetrainer.llm.tools import run_tool

    _doc(conn, 1, "태그문서", "", "<ul><li><strong>굵게</strong> 그리고 &amp; 기호</li></ul>")
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)
    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "태그문서", "limit": 3})
    assert "<" not in out and "&amp;" not in out
    assert "굵게" in out and "& 기호" in out


def test_벡터_검색이_터져도_키워드로_답한다(conn, cfg, monkeypatch):
    from lifetrainer.llm.tools import run_tool

    _doc(conn, 1, "젯슨 오린 벤치마크")
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: [1.0] + [0.0] * (DIM - 1))
    monkeypatch.setattr(E, "nearest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("터짐")))
    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "젯슨", "limit": 3})
    assert "젯슨 오린 벤치마크" in out


# ── 기간 필터 (search_docs period) ────────────────────────────────────────
# ★ 지키려는 성질 셋:
#   ① 날짜 계산을 모델에게 시키지 않는다 (라벨만 받고 경계는 파이썬이 만든다)
#   ② 스키마에 `pattern` 이 없다 — 있으면 llama.cpp 가 요청 전체를 400 으로 죽인다
#   ③ "기간 때문에 0건" 과 "아예 0건" 을 구분해 말한다


def _dated(conn, cfg, doc_id, title, *, days_ago: float, abstract=""):
    """`published_at` 을 논리적 하루 경계 기준으로 days_ago 만큼 과거에 둔다."""
    from lifetrainer import timeutil

    _, end = timeutil.day_bounds(
        "2026-08-23", cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour
    )
    _doc(conn, doc_id, title, abstract)
    conn.execute(
        "UPDATE doc SET published_at=? WHERE id=?", (end - days_ago * 86400.0 - 1.0, doc_id)
    )
    conn.commit()


def test_스키마에_pattern_이_없다(cfg):
    """계약 회귀 방지. `pattern` 이 섞이면 요청 전체가 400 이 된다 (GBNF 변환 실패)."""
    from lifetrainer.llm.client import _check_no_pattern
    from lifetrainer.llm.tools import REGISTRY

    for tool in REGISTRY.values():
        _check_no_pattern(tool.schema)  # 하나라도 pattern 이면 ValueError


def test_period_는_enum_으로만_받는다(cfg):
    from lifetrainer.llm.tools import DOC_PERIODS, REGISTRY

    props = REGISTRY["search_docs"].schema["function"]["parameters"]["properties"]
    assert props["period"]["enum"] == list(DOC_PERIODS)
    assert "pattern" not in props["period"]


def test_기간_안의_문서만_돌려준다(conn, cfg, monkeypatch):
    from lifetrainer.llm.tools import run_tool

    _dated(conn, cfg, 1, "젯슨 최신 소식", days_ago=0.2)
    _dated(conn, cfg, 2, "젯슨 옛날 소식", days_ago=40)
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)

    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "젯슨", "period": "today"})
    assert "젯슨 최신 소식" in out
    assert "젯슨 옛날 소식" not in out
    assert "오늘" in out  # 어떤 범위로 찾았는지 밝힌다


def test_기간을_넓히면_옛_문서도_들어온다(conn, cfg, monkeypatch):
    from lifetrainer.llm.tools import run_tool

    _dated(conn, cfg, 1, "젯슨 최신 소식", days_ago=0.2)
    _dated(conn, cfg, 2, "젯슨 옛날 소식", days_ago=20)
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)

    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "젯슨", "period": "month"})
    assert "젯슨 최신 소식" in out and "젯슨 옛날 소식" in out


def test_기간_때문에_0건이면_그렇다고_말한다(conn, cfg, monkeypatch):
    """'없다' 로 뭉뚱그리면 모델이 '그런 문서는 없습니다' 로 단정한다."""
    from lifetrainer.llm.tools import run_tool

    _dated(conn, cfg, 1, "젯슨 옛날 소식", days_ago=40)
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)

    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "젯슨", "period": "today"})
    assert "기간 제한을 빼면" in out
    assert "1건" in out


def test_아예_0건인_것과_기간_탓_0건을_구분한다(conn, cfg, monkeypatch):
    from lifetrainer.llm.tools import run_tool

    _dated(conn, cfg, 1, "젯슨 옛날 소식", days_ago=40)
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)

    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "발로란트", "period": "today"})
    assert "기간 제한을 빼면" not in out
    assert "오늘" in out  # 어떤 범위에서 못 찾았는지는 밝힌다


def test_모르는_period_는_all_로_떨어진다(conn, cfg, monkeypatch):
    """8B 가 '이번주'·'this week' 같은 변형을 넣어도 검색이 실패하면 안 된다."""
    from lifetrainer.llm.tools import run_tool

    _dated(conn, cfg, 1, "젯슨 옛날 소식", days_ago=40)
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)

    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "젯슨", "period": "이번주"})
    assert "젯슨 옛날 소식" in out


def test_벡터_쪽도_기간을_지킨다(conn, cfg, monkeypatch):
    """키워드만 거르고 벡터가 새면 기간 밖 문서가 그대로 올라온다."""
    from lifetrainer.llm.tools import run_tool

    _dated(conn, cfg, 1, "가나다", days_ago=0.2)
    _dated(conn, cfg, 2, "라마바", days_ago=40)
    _fake_vectors(monkeypatch)
    E.embed_pending(conn, cfg)

    out = run_tool(_ctx(conn, cfg), "search_docs", {"query": "라마바", "period": "today"})
    assert "라마바" not in out.split("검색 결과")[-1] or "기간 제한을 빼면" in out


def test_published_at_이_없는_문서는_기간_필터에서_빠진다(conn, cfg, monkeypatch):
    """날짜를 모르는 문서를 '오늘' 에 넣으면 조용히 틀린 답이 된다."""
    from lifetrainer.llm.tools import run_tool

    _doc(conn, 1, "날짜없는 젯슨 글")  # published_at NULL
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)

    assert "날짜없는" not in run_tool(
        _ctx(conn, cfg), "search_docs", {"query": "젯슨", "period": "today"}
    )
    assert "날짜없는" in run_tool(_ctx(conn, cfg), "search_docs", {"query": "젯슨"})


def test_사용자가_시점을_안_말했으면_모델이_period_를_넣어도_무시한다(conn, cfg, monkeypatch):
    """실측 사고: '젯슨 관련 글 있어?' 에 8B 가 period=week 을 붙여 63% 를 잘랐다.

    기간이 조용히 걸리는 것은 빈손보다 나쁘다 — 결과가 나오므로 눈치채지 못한다.
    """
    from lifetrainer.llm.tools import ToolContext, run_tool

    _dated(conn, cfg, 1, "젯슨 옛날 소식", days_ago=40)
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)
    ctx = ToolContext(
        conn=conn, cfg=cfg, today="2026-08-23", user_text="젯슨 관련해서 읽을 만한 글 있어?"
    )
    out = run_tool(ctx, "search_docs", {"query": "젯슨", "period": "week"})
    assert "젯슨 옛날 소식" in out  # 기간이 무시돼 40일 전 문서가 살아 있다


def test_사용자가_시점을_말했으면_period_가_산다(conn, cfg, monkeypatch):
    from lifetrainer.llm.tools import ToolContext, run_tool

    _dated(conn, cfg, 1, "젯슨 옛날 소식", days_ago=40)
    monkeypatch.setattr(E, "embed_query", lambda *a, **k: None)
    ctx = ToolContext(
        conn=conn, cfg=cfg, today="2026-08-23", user_text="이번 주에 나온 젯슨 글 있어?"
    )
    out = run_tool(ctx, "search_docs", {"query": "젯슨", "period": "week"})
    assert "기간 제한을 빼면" in out


def test_시점_낱말_판정(cfg):
    from lifetrainer.llm.tools import _mentions_time

    for yes in ("오늘 뭐 올라왔어", "이번 주 논문", "최근 소식", "요즘 뭐 나왔어", "latest papers"):
        assert _mentions_time(yes), yes
    for no in ("젯슨 관련해서 읽을 만한 글 있어?", "추론 최적화 자료", "코딩 에이전트 뭐 있어"):
        assert not _mentions_time(no), no


# ── 질의 지시문 접두 (Qwen3-Embedding) ────────────────────────────────────
# ★ 실측: 접두 없이는 하이브리드(76%)가 키워드 단독(96%)보다 **나빴다**.
#   붙인 뒤 98%. 재현: scripts/eval_search.py


def test_질의에는_지시문이_붙는다(cfg):
    q = E.query_text(cfg, "jetson")
    assert q.startswith(cfg.embed.query_instruct)
    assert q.endswith("jetson")


def test_문서에는_지시문이_안_붙는다(conn, cfg):
    """문서 쪽에 붙이면 질의와 문서가 같은 편향을 공유해 이득이 사라진다."""
    row = {"title": "젯슨", "abstract": "", "summary": None}
    assert not E.source_text(row, cfg.embed.max_chars).startswith("Instruct:")


def test_지시문이_비면_맨_질의로_간다(cfg):
    import dataclasses

    c = dataclasses.replace(cfg, embed=dataclasses.replace(cfg.embed, query_instruct=""))
    assert E.query_text(c, "jetson") == "jetson"


def test_지시문이_max_chars_를_먹지_않는다(cfg):
    """접두를 자르기 전에 붙이면 긴 질의의 뒷부분이 지시문에 밀려 사라진다."""
    import dataclasses

    c = dataclasses.replace(cfg, embed=dataclasses.replace(cfg.embed, max_chars=10))
    out = E.query_text(c, "abcdefghijKLMNOP")
    assert out.endswith("abcdefghij")  # 질의는 10자로 잘리되
    assert out.startswith(c.embed.query_instruct)  # 지시문은 온전하다


def test_embed_query_가_지시문을_실어_보낸다(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(E, "embed_texts", lambda c, texts: sent.extend(texts) or [[0.0] * DIM])
    E.embed_query(cfg, "jetson")
    assert sent and sent[0].startswith(cfg.embed.query_instruct)


# ── 서버가 죽었다 살아나는 것을 배치가 견디는가 ────────────────────────────
#
# 이 서버는 누수가 있고 **일부러 죽게 두는 설계**다 (MemoryMax + MemorySwapMax=0 +
# Restart=on-failure). 스왑으로 기어들어가 기기 전체를 끌어내리는 대신 빨리 죽고
# 빨리 살아난다 — HISTORY/2026-08-23-raising-the-memory-cap-made-it-worse.md
#
# 그 설계는 맞았는데 **배치가 그걸 몰라서**, 계획된 재시작 한 번에 그날 밤 임베딩이
# 통째로 끝났다. 아래가 그 계약이다: 죽음은 사고가 아니라 밸브이므로 기다렸다 잇는다.


def test_배치는_서버가_한_번_죽어도_기다렸다_잇는다(conn, cfg, monkeypatch):
    for i in range(1, 5):
        _doc(conn, i, f"문서 {i}", abstract="본문")

    calls = []

    def _embed(cfg_, texts):
        calls.append(list(texts))
        if len(calls) == 2:          # 두 번째 배치에서 OOM 으로 끊긴다
            raise E.EmbedUnavailable("connection aborted")
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    monkeypatch.setattr(E, "embed_texts", _embed)
    monkeypatch.setattr(E, "wait_for_server", lambda *a, **k: True)

    r = E.embed_pending(conn, cfg, limit=10)

    assert r.restarts == 1, "재기동을 세어 둬야 누수 천장이 보인다"
    assert r.embedded == 4, "죽은 배치를 포함해 전부 임베딩돼야 한다"


def test_서버가_안_살아나면_그때는_멈춘다(conn, cfg, monkeypatch):
    """기다리는 것과 무한정 버티는 것은 다르다. 진짜로 죽었으면 올린다."""
    _doc(conn, 1, "문서", abstract="본문")

    monkeypatch.setattr(E, "embed_texts", _fail_unavailable)
    monkeypatch.setattr(E, "wait_for_server", lambda *a, **k: False)

    with pytest.raises(E.EmbedUnavailable):
        E.embed_pending(conn, cfg, limit=10)


def _fail_unavailable(cfg_, texts):
    raise E.EmbedUnavailable("서버 없음")


def test_임베딩_한도는_요약_한도에서_파생되지_않는다():
    """둘은 성격이 다르다 — 요약은 GPU 22초/건, 임베딩은 CPU 1초 미만/건.

    묶어 두었더니 운영값 30 이 임베딩 한도 60 을 낳았고, 문서 유입이 하루 수백 건이라
    구조적으로 밀려 미임베딩이 1,700건까지 쌓였다 (2026-08-28).
    """
    cfg = load_config()

    assert cfg.nightly.embed_limit != cfg.nightly.summary_limit * 2
    assert cfg.nightly.embed_limit >= 500, "하루 유입보다 커야 밀리지 않는다"
