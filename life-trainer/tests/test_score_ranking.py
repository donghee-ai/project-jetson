"""아침 다이제스트 랭킹 — 왜 이 테스트들이 여기 있나.

2026-09-04 에 **8일 연속 같은 문서 5건**이 나갔다. 문서는 그동안 하루 217~461건씩
들어오고 있었고 미채점은 0건이었다 — 수집이 아니라 랭킹이 멈춰 있었다.

원인이 둘이었고, 이 파일은 **둘 다** 고정한다.

| | 무엇이 잘못됐나 | 지키는 테스트 |
|---|---|---|
| ① | 최신성 보정을 **채점할 때 곱해 저장**해서 문서가 늙어도 점수가 안 내려갔다 | `test_stored_score_has_no_time_in_it` · `test_ranking_ages_without_rescoring` |
| ② | **보낸 문서를 거를 방법이 없었다.** 반감기 14일이라 1등이 2주 동안 1등이다 | `test_a_sent_doc_does_not_come_back` |

경위: `HISTORY/2026-09-04-the-decay-was-frozen-into-the-score.md`

**이 테스트가 깨진다는 것은** 랭킹이 다시 굳었다는 뜻이다 — 화면은 멀쩡해 보이고
매일 무언가가 나가지만, 그 무언가가 어제와 같다. 증상이 조용해서 8일이 걸렸다.
"""

from __future__ import annotations

import time

import pytest

from lifetrainer import db
from lifetrainer.collect.score import (
    RECENCY_HALF_LIFE_DAYS,
    mark_digested,
    rescore_all,
    score_pending,
    top_docs,
)

DAY = 86400.0


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "lt.db")
    db.init_db(c)
    c.execute(
        "INSERT INTO interest(term, weight, source, updated_at) VALUES ('jetson', 3.0, 'test', ?)",
        (time.time(),),
    )
    c.commit()
    yield c
    c.close()


class _Cfg:
    """`load_interests` 는 conn 만 쓴다 — 설정 전체를 만들 이유가 없다."""


def add_doc(conn, *, title, published_days_ago, url=None, state="new", abstract=None):
    now = time.time()
    cur = conn.execute(
        "INSERT INTO doc(url, title, abstract, published_at, fetched_at, content_hash, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            url or f"https://example.com/{title}",
            title,
            abstract,
            now - published_days_ago * DAY,
            now,
            f"hash-{title}",
            state,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_stored_score_has_no_time_in_it(conn):
    """★ ①의 뿌리. **같은 글이면 나이와 무관하게 같은 점수로 저장된다.**

    전에는 `score_pending` 이 `base * recency(now)` 를 저장했다. 그러면 저장된 값이
    *채점한 순간의 나이*를 영원히 들고 있어, 갓 나온 글로 채점된 문서가 한 달 뒤에도
    갓 나온 글의 점수를 갖는다.
    """
    fresh = add_doc(conn, title="jetson fresh", published_days_ago=0)
    old = add_doc(conn, title="jetson old", published_days_ago=60)
    score_pending(conn, _Cfg())
    conn.commit()

    scores = {
        r["id"]: r["score"] for r in conn.execute("SELECT id, score FROM doc").fetchall()
    }
    assert scores[fresh] == scores[old] > 0, "저장된 점수에 시간이 섞였다 — 그게 굳는다"


def test_ranking_ages_without_rescoring(conn):
    """★ ①이 실제로 고쳐졌나. **다시 채점하지 않아도 순위가 늙는다.**

    옛 문서를 반감기 4배(=배율 하한 0.2)만큼 늙혀 두면, 기준점수가 더 높아도
    갓 나온 문서에 밀려야 한다. `top_docs` 가 읽는 시점에 보정을 곱하기 때문이다.
    """
    # 기준점수는 제목 매칭 ×2 + 초록 매칭 ×1 이다 — 낱말 반복으로는 안 오른다.
    old = add_doc(
        conn, title="jetson old", abstract="jetson 이야기", published_days_ago=RECENCY_HALF_LIFE_DAYS * 4
    )
    fresh = add_doc(conn, title="jetson fresh", published_days_ago=0)
    score_pending(conn, _Cfg())
    conn.commit()

    base = {r["id"]: r["score"] for r in conn.execute("SELECT id, score FROM doc").fetchall()}
    assert base[old] > base[fresh], "전제가 깨졌다 — 옛 문서의 기준점수가 더 높아야 하는 표본이다"

    ranked = top_docs(conn, limit=2)
    assert [r["id"] for r in ranked] == [fresh, old]
    assert ranked[0]["ranked_score"] > ranked[1]["ranked_score"]


def test_a_sent_doc_does_not_come_back(conn):
    """★ ②. 한 번 나간 문서는 다음 날 후보에서 빠진다.

    최신성만으로는 부족하다 — 반감기가 14일이라 오늘 1등은 2주 동안 1등이다.
    그게 *"이미 아는 것을 매일 다시 알리는"* 부류다 (저장소 규칙 §1 의 4번).
    """
    top = add_doc(conn, title="jetson top", abstract="jetson 이야기", published_days_ago=0)
    second = add_doc(conn, title="jetson second", published_days_ago=0)
    score_pending(conn, _Cfg())
    conn.commit()

    assert [r["id"] for r in top_docs(conn, limit=1)] == [top]

    mark_digested(conn, [top])
    conn.commit()

    assert [r["id"] for r in top_docs(conn, limit=1)] == [second]
    # 미리보기용 스위치는 그대로 볼 수 있어야 한다
    assert [r["id"] for r in top_docs(conn, limit=1, include_digested=True)] == [top]


def test_mark_digested_does_not_move_an_earlier_stamp(conn):
    """두 번 표시해도 **처음 나간 시각**이 남는다.

    표식은 "언제 나갔나" 라는 사실이다. 다시 부를 때마다 now 로 갱신하면
    `aw_bucket.last_seen` 이 죽은 워처를 신선해 보이게 했던 것과 같은 병이 된다
    (HISTORY 2026-09-04 워처 침묵).
    """
    doc_id = add_doc(conn, title="jetson once", published_days_ago=0)
    score_pending(conn, _Cfg())
    mark_digested(conn, [doc_id], now=1000.0)
    mark_digested(conn, [doc_id], now=2000.0)
    conn.commit()

    row = conn.execute("SELECT digested_at FROM doc WHERE id = ?", (doc_id,)).fetchone()
    assert row["digested_at"] == 1000.0


# ── 안 울려야 하는 쪽 ────────────────────────────────────────────────
#
# 저장소 규칙 §1: 만드는 건 쉽고 **"언제 안 울리나"가 어려운 쪽**이다.
# 여기서 그건 "재료가 없을 때 아무 문서나 끌어오지 않는다" 이다.


def test_empty_when_everything_was_already_sent(conn):
    """보낼 것이 없으면 **빈손을 빈손이라고 말한다.**

    옛 문서를 다시 꺼내 채우면 그게 바로 08-28 부터 고쳐 온 실패다 —
    사람이 채널을 끄게 만드는 쪽.
    """
    ids = [add_doc(conn, title=f"jetson {i}", published_days_ago=i) for i in range(3)]
    score_pending(conn, _Cfg())
    mark_digested(conn, ids)
    conn.commit()

    assert top_docs(conn, limit=5) == []


def test_unscored_docs_never_reach_the_digest(conn):
    """`state='new'` 는 후보가 아니다. 채점 전 문서는 점수가 NULL 이다."""
    add_doc(conn, title="jetson pending", published_days_ago=0)
    assert top_docs(conn, limit=5) == []


def test_rescore_keeps_the_pipeline_state(conn):
    """재채점은 **점수만** 만진다.

    `state='new'` 로 되돌리는 방식으로 만들었으면 요약이 끝난 문서가 파이프라인
    앞으로 돌아가 GPU 잡이 다시 돈다. 실DB 에는 그런 문서가 1,896건 있었다.
    """
    doc_id = add_doc(conn, title="jetson summarized", published_days_ago=30, state="summarized")
    conn.execute("UPDATE doc SET score = 999.0, summary = '요약본' WHERE id = ?", (doc_id,))
    conn.commit()

    n = rescore_all(conn, _Cfg())
    conn.commit()

    row = conn.execute("SELECT state, score, summary FROM doc WHERE id = ?", (doc_id,)).fetchone()
    assert n == 1
    assert row["state"] == "summarized"
    assert row["summary"] == "요약본"
    assert row["score"] < 999.0, "굳어 있던 점수를 씻어내지 못했다"


def test_rescore_refuses_to_run_with_no_interests(conn):
    """관심사가 비면 **아무것도 안 한다.**

    돌려 버리면 8,689건이 전부 0점이 되고, 그건 되돌릴 방법이 있어도
    다음 다이제스트가 빈손으로 나가는 사고다.
    """
    doc_id = add_doc(conn, title="jetson keep", published_days_ago=1)
    conn.execute("UPDATE doc SET score = 42.0, state = 'scored' WHERE id = ?", (doc_id,))
    conn.execute("DELETE FROM interest")
    conn.commit()

    assert rescore_all(conn, _Cfg()) == 0
    row = conn.execute("SELECT score FROM doc WHERE id = ?", (doc_id,)).fetchone()
    assert row["score"] == 42.0
