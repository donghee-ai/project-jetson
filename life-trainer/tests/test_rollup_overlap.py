"""겹치는 원본 이벤트에 대한 롤업 방어 — 실데이터에서 나온 회귀 테스트.

## 왜 이 파일이 있는가

조사 문서(`docs/research/activitywatch.md §3`)는 aw-server-rust 소스를 근거로
"한 버킷 안에서 이벤트는 겹치지 않는다"고 정리했다. 롤업은 그 전제 위에서 겹친 초를
단순 합산했고, 합성 데이터로는 아무 문제가 없었다 (테스트 584개 전부 통과).

**실제 노트북(파이썬 aw-server v0.13.2)을 붙이자마자 전제가 깨졌다.**

서버가 잠깐 죽어 있는 동안 워처는 이벤트를 `persistqueue` 에 쌓는다. 서버가 살아나면
자라던 하트비트 하나가 **성장 단계마다 개별 이벤트로** 한꺼번에 들어온다.
시작 시각이 마이크로초 단위로만 다르고 duration 이 조금씩 큰 행이 23개 생겼고,
그걸 전부 더해 **2시간짜리 자리비움이 22시간 24분으로 부풀었다.**

하루가 24시간인데 자리비움이 22시간 24분 + 활동 57분이면 관측 구간(9시간)을
한참 넘는다. 눈으로 보고서야 이상하다는 걸 알았다.
"""

from __future__ import annotations


from lifetrainer.rollup.rollup import _dedupe_overlaps


def _ev(ts: float, ts_end: float, status: str = "afk", btype: str = "afk") -> dict:
    return {
        "bucket_id": "aw-watcher-afk_TEST",
        "ts": ts,
        "ts_end": ts_end,
        "app": None,
        "title": None,
        "url": None,
        "status": status,
        "btype": btype,
    }


def _span(events: list[dict]) -> float:
    return sum(e["ts_end"] - e["ts"] for e in events)


def test_growing_heartbeat_duplicates_collapse() -> None:
    """실제로 겪은 형태: 시작이 거의 같고 duration 만 자라는 중복 23건."""
    base = 1_786_957_272.836
    events = [_ev(base + i * 0.001, base + 600 + i * 300) for i in range(23)]

    # 정규화 전: 단순 합산하면 관측 구간을 몇 배로 넘긴다.
    assert _span(events) > 50_000

    out = _dedupe_overlaps(events)

    # 정규화 후: 가장 멀리 뻗은 끝까지의 실제 경과 시간을 넘지 않는다.
    real_span = max(e["ts_end"] for e in events) - min(e["ts"] for e in events)
    assert _span(out) <= real_span + 0.001
    assert _span(out) > real_span - 1.0  # 구간이 통째로 사라지지도 않아야 한다


def test_non_overlapping_events_are_untouched() -> None:
    """정상 데이터는 손대지 않는다 — 이게 깨지면 기존 집계가 전부 틀어진다."""
    events = [_ev(100, 200), _ev(200, 350), _ev(400, 500)]
    out = _dedupe_overlaps(events)
    assert [(e["ts"], e["ts_end"]) for e in out] == [(100, 200), (200, 350), (400, 500)]
    assert _span(out) == 350


def test_later_event_wins_on_partial_overlap() -> None:
    """부분 겹침: 뒤 이벤트가 앞을 자른다. 같은 순간에 두 상태가 참일 수 없다."""
    out = _dedupe_overlaps([_ev(100, 300, "afk"), _ev(200, 400, "not-afk")])
    assert [(e["ts"], e["ts_end"], e["status"]) for e in out] == [
        (100, 200, "afk"),
        (200, 400, "not-afk"),
    ]
    assert _span(out) == 300  # 겹친 100초가 두 번 세어지지 않는다


def test_fully_covered_event_disappears() -> None:
    """완전히 덮인 이벤트는 길이 0 이 되어 사라진다."""
    out = _dedupe_overlaps([_ev(100, 100.5), _ev(100, 500)])
    assert len(out) == 1
    assert (out[0]["ts"], out[0]["ts_end"]) == (100, 500)


def test_empty_and_single_are_passthrough() -> None:
    assert _dedupe_overlaps([]) == []
    one = [_ev(100, 200)]
    assert _dedupe_overlaps(one) == one


def test_rollup_end_to_end_does_not_inflate(tmp_path) -> None:
    """롤업 전체를 통과시켜도 슬롯 불변식이 깨지지 않는지 — 이게 최종 방어선이다."""
    from lifetrainer import db as D
    from lifetrainer import timeutil as tu
    from lifetrainer.config import load_config
    from lifetrainer.rollup.classify import Classifier
    from lifetrainer.rollup.rollup import rollup_day

    cfg = load_config()
    dbp = tmp_path / "t.db"
    conn = D.connect(dbp)
    D.init_db(conn)

    day = "2026-08-17"
    start, _end = tu.day_bounds(day, cfg.tz, boundary_hour=cfg.rollup.day_boundary_hour)
    now = tu.now_ts()
    conn.execute(
        "INSERT INTO aw_bucket(bucket_id, host, client, type, first_seen, last_seen) "
        "VALUES ('aw-watcher-afk_T','T','aw-watcher-afk','afk',?,?)",
        (start, start + 86400),
    )
    # 같은 구간을 덮는 중복 20건 (실데이터에서 본 형태)
    for i in range(20):
        conn.execute(
            "INSERT INTO aw_event(bucket_id, ts, ts_end, duration, status, data_json, synced_at) "
            "VALUES ('aw-watcher-afk_T',?,?,?,'not-afk','{}',?)",
            (start + 3600 + i * 0.001, start + 3600 + 1800 + i * 60, 1800 + i * 60, now),
        )
    conn.commit()

    rollup_day(conn, cfg, Classifier.from_yaml(cfg.rollup.rules_path), day)

    bad = conn.execute(
        "SELECT COUNT(*) FROM slot WHERE abs(active_sec + afk_sec + gap_sec - 600) > 0.5"
    ).fetchone()[0]
    assert bad == 0, "슬롯 시간 합이 600초를 벗어났다 — 겹침이 합산된 것이다"

    total_active = conn.execute("SELECT COALESCE(SUM(active_sec),0) FROM slot").fetchone()[0]
    assert total_active <= 24 * 3600, "하루 활동이 24시간을 넘었다"
    # 중복 20건이 덮은 실제 구간은 최대 약 1800+19*60 초다.
    assert total_active < 1800 + 20 * 60 + 60

    conn.close()
