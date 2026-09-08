"""프라이빗 구간 — 이 시간은 재지 않기로 한 것.

## 무엇을 보장하나

**젯슨 DB 에 안 들어온다.** 창 제목·앱 이름·URL 이 `aw_event` 에 한 번도 안 적힌다.
`data_json` 이 원본 `data` 를 통째로 갖기 때문에(`collect/aw_sync.py`) 컬럼만 비우는
마스킹은 구멍이다 — **행 자체를 안 만든다.**

## 이건 세 층 중 하나다

| 층 | 어디 | 보장 |
|---|---|---|
| A 워처 정지 | Windows 헬퍼 · 안드로이드 타일 | 데이터가 **아예 안 생긴다** |
| **B 이 파일** | `upsert_events()` | A 가 실패해도 **젯슨에는 안 들어온다** |
| C 표시 | 롤업 · stats | 구멍이 "수집 실패"가 아니라 "재지 않기로 한 시간"으로 읽힌다 |

A 만으로는 폴링 지연(≤1주기)과 헬퍼 다운을 못 막고, B 만으로는 엔드포인트 로컬에
남는다. 그래서 둘 다 있다.

## 왜 `end_ts` 를 미리 박나 (열린 구간 금지)

`rollup_day` 는 10분마다 오늘을 다시 돈다. 끝이 열려 있으면 돌 때마다 "지금"을 다시
해석해 **구멍이 자라고 멱등성이 깨진다.** 젯슨이 재부팅되면 하루가 통째로 빈다.
끄기는 `end_ts` 를 `now` 로 **당기는** 것이다.

★ 끄면 **그 뒤로만** 다시 기록된다. 구간 안의 데이터는 안 돌아온다 — PC·폰 커서가
이미 전진했기 때문이다. 버그가 아니라 기능이다.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace as dc_replace

from zoneinfo import ZoneInfo

from lifetrainer import db, timeutil

Span = tuple[float, float]


@dataclass(frozen=True)
class PrivateState:
    """웹·PC·폰이 **같은 딕셔너리**를 받게 하는 단일 파생 지점."""

    active: bool
    until_ts: float | None
    server_ts: float
    spans: list[Span]

    def as_dict(self, *, poll_sec: int) -> dict:
        return {
            "active": self.active,
            # ★ "남은 초"가 아니라 **절대 시각**이다. 엔드포인트가 젯슨을 못 만나도
            #   자기 시계로 언제 워처를 되살릴지 알아야 하기 때문이다(페일세이프).
            "until_ts": self.until_ts,
            "server_ts": self.server_ts,
            "poll_sec": poll_sec,
            "spans": [{"start_ts": s, "end_ts": e} for s, e in self.spans],
        }


# ── 읽기 ──────────────────────────────────────────────────────────────────


def load_spans(
    conn: sqlite3.Connection,
    lo: float | None = None,
    hi: float | None = None,
    *,
    kinds: tuple[str, ...] | None = None,
) -> list[Span]:
    """`[lo, hi)` 와 겹치는 프라이빗 구간을 합쳐서 돌려준다.

    범위를 안 주면 전부. `revoked` 는 제외한다 — 잘못 켠 것을 취소할 수 있어야 한다.

    `kinds` 로 종류를 고른다. **`kind` 는 화면에서 뭐라고 보일지를 가른다** (2026-09-04):

    | kind | 만든 것 | 격자·차트에서 |
    |---|---|---|
    | `live` | 프라이빗 토글 (웹·타일·`/private`) | **프라이빗** — 일부러 안 잰 시간으로 보인다 |
    | `purge` | 소급 삭제 (`lt private purge`) | **자리비움** — 흔적을 안 남긴다 |

    ★ 둘 다 **이벤트를 자르는 데는 똑같이 쓴다.** 가르는 것은 표시뿐이다 —
      어느 쪽이든 지운 시간이 다음 sync 로 되살아나면 안 된다.
    """
    sql = "SELECT start_ts, end_ts FROM private_span WHERE revoked = 0"
    args: list = []
    if kinds is not None:
        # ★ 자리표시자 순서 = 바인딩 순서다. kind 절이 lo/hi 절보다 **앞**이므로
        #   값도 먼저 넣는다. (여기서 한 번 어긋뜨렸다 — 조용히 틀린 구간을 준다)
        sql += " AND kind IN (" + ", ".join("?" * len(kinds)) + ")"
        args += list(kinds)
    if lo is not None and hi is not None:
        sql += " AND end_ts > ? AND start_ts < ?"
        args += [lo, hi]
    rows = conn.execute(sql, args).fetchall()
    return timeutil.union_spans([(float(r["start_ts"]), float(r["end_ts"])) for r in rows])


def load_active_spans(conn: sqlite3.Connection, now: float | None = None) -> list[Span]:
    """**지금 저장을 막아야 하는** 구간.

    ★ 지금 활성인 것만이 아니라 **과거 구간까지 전부** 준다. 프라이빗이 끝난 뒤에도
    그 시간의 이벤트는 계속 막아야 한다 — PC 는 `overlap_sec`(900초) 만큼 겹쳐 다시
    읽고, 커서를 지우면 `backfill_days`(7일)까지 되돌아온다. "지금 켜져 있나"로
    판정하면 프라이빗이 끝나는 순간 다음 sync 가 그 구간을 통째로 실어 온다.
    """
    return load_spans(conn)


def state(conn: sqlite3.Connection, now: float | None = None) -> PrivateState:
    """지금 상태. 웹 UI·PC 헬퍼·폰 타일이 전부 이걸 받는다."""
    now = time.time() if now is None else now
    spans = load_spans(conn)
    live = [(s, e) for s, e in spans if e > now]
    until = max((e for _, e in live if _ <= now), default=None)
    return PrivateState(active=until is not None, until_ts=until, server_ts=now, spans=live)


# ── 쓰기 ──────────────────────────────────────────────────────────────────


def begin(
    conn: sqlite3.Connection,
    minutes: float,
    *,
    source: str = "web",
    device: str | None = None,
    note: str | None = None,
    now: float | None = None,
) -> PrivateState:
    """지금부터 `minutes` 분 동안 프라이빗. 이미 켜져 있으면 **연장**한다.

    연장은 새 행이 아니라 기존 행의 `end_ts` 를 미는 것이다 — 구간이 잘게 쪼개지면
    나중에 "언제 켰었나"를 읽기 어렵다.
    """
    now = time.time() if now is None else now
    end = now + minutes * 60.0
    row = conn.execute(
        "SELECT id, end_ts FROM private_span "
        "WHERE revoked = 0 AND kind = 'live' AND start_ts <= ? AND end_ts > ? "
        "ORDER BY end_ts DESC LIMIT 1",
        (now, now),
    ).fetchone()
    with db.transaction(conn) as tx:
        if row is not None:
            tx.execute(
                "UPDATE private_span SET end_ts = ?, updated_at = ? WHERE id = ?",
                (max(float(row["end_ts"]), end), now, row["id"]),
            )
        else:
            tx.execute(
                "INSERT INTO private_span(start_ts, end_ts, kind, source, device, note, "
                "created_at, updated_at) VALUES (?, ?, 'live', ?, ?, ?, ?, ?)",
                (now, end, source, device, note, now, now),
            )
    return state(conn, now)


def end_now(conn: sqlite3.Connection, *, now: float | None = None) -> PrivateState:
    """지금 끈다 — 활성 구간의 `end_ts` 를 `now` 로 **당긴다**.

    행을 지우지 않는다. 구멍의 근거가 남아야 나중에 "왜 여기가 비었나"를 답할 수 있고,
    무엇보다 **지운 뒤 다음 sync 가 그 구간을 다시 실어 오면 안 된다.**
    """
    now = time.time() if now is None else now
    with db.transaction(conn) as tx:
        tx.execute(
            "UPDATE private_span SET end_ts = ?, updated_at = ? "
            "WHERE revoked = 0 AND kind = 'live' AND start_ts <= ? AND end_ts > ?",
            (now, now, now, now),
        )
    return state(conn, now)


# ── 거르기 (안전망의 실물) ────────────────────────────────────────────────


def clip_events(events: list, spans: list[Span]) -> list:
    """프라이빗과 겹치는 부분을 **잘라낸다.** 통째로 버리지 않는다.

    ★ 왜 자르나: afk 이벤트는 긴 구간 하나로 들어올 수 있다. 끝 일부가 프라이빗이라고
    전체를 통째로 버리면 조용한 대량 손실이 된다 — 그건 기능이 아니라 버그로 보인다.

    ★ 왜 여기서(=`upsert_events` 안에서) 자르나: `aw_event` 의 upsert 가
    `ts_end = MAX(ts_end, excluded.ts_end)` 라 **줄이지 못한다.** 한 번이라도 안 자른
    값이 들어가면 MAX 가 그걸 영구히 잡는다. 저장 직전에 **매번** 잘라야 한다.

    입력은 `AWEvent`(dataclass) 또는 dict 둘 다 온다. 원본을 안 바꾸고 사본을 낸다.

    ★ **길이 0 이벤트는 구간이 아니라 점이다** (2026-09-04). unlock 은 전부 duration 0
    이고, 창·미디어 워처도 순간 전환을 0초로 낸다. 구간 빼기(`subtract_spans`)는
    `p[1] > p[0]` 인 조각만 돌려주므로 **점은 겹치지 않아도 결과에서 사라진다.**
    그래서 잘리지 않고 조용히 지워졌다 — 길이 0 이벤트가 통째로 사라지는 회귀가
    확인됐다 (unlock·미디어·PC 창 전부).
    점은 잘라낼 것이 없으므로 **구간 안이면 버리고 밖이면 그대로 둔다.**
    """
    if not spans:
        return list(events)
    cut = timeutil.union_spans(spans)
    out: list = []
    for ev in events:
        is_obj = hasattr(ev, "ts")          # AWEvent(dataclass) vs 롤업이 쓰는 dict
        ts = float(ev.ts if is_obj else ev["ts"])
        ts_end = float(ev.ts_end if is_obj else ev["ts_end"])
        if ts_end <= ts:
            # 경계는 구간 빼기와 같은 반열림 `[s, e)` 다 — 끝에 딱 걸친 점은 밖이다.
            if not any(s <= ts < e for s, e in cut):
                out.append(dc_replace(ev) if is_obj else {**ev})
            continue
        for s, e in timeutil.subtract_spans([(ts, ts_end)], cut):
            # AWEvent 의 `ts_end` 는 프로퍼티라 `duration` 을 고치면 따라온다.
            out.append(dc_replace(ev, ts=s, duration=e - s) if is_obj else {**ev, "ts": s, "ts_end": e})
    return out


# ── 소급 삭제 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PurgeReport:
    """지웠거나 지울 것. `dry_run` 이면 **아직 아무것도 안 바뀐 상태**의 예고편이다."""

    start_ts: float
    end_ts: float
    events: int
    seconds: float
    days: list[str]
    dry_run: bool
    aw_deleted: int | None = None   # None = 시도 안 함(cfg.private.purge_aw 꺼짐)
    aw_error: str | None = None
    span_id: int | None = None      # 되돌리기의 손잡이. `undo(span_id)` 가 이걸 받는다

    def as_dict(self) -> dict:
        return {
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "events": self.events,
            "seconds": self.seconds,
            "minutes": round(self.seconds / 60.0, 1),
            "days": list(self.days),
            "dry_run": self.dry_run,
            "aw_deleted": self.aw_deleted,
            "aw_error": self.aw_error,
        }


_EVENT_COLUMNS = (
    "bucket_id", "ts", "ts_end", "duration", "event_id",
    "app", "title", "url", "status", "data_json", "synced_at",
)


def _overlapping_rows(conn: sqlite3.Connection, start_ts: float, end_ts: float) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {', '.join(_EVENT_COLUMNS)} FROM aw_event WHERE ts_end > ? AND ts < ? ORDER BY bucket_id, ts",
        (start_ts, end_ts),
    ).fetchall()


def purge(
    conn: sqlite3.Connection,
    cfg,
    start_ts: float,
    end_ts: float,
    *,
    source: str = "web",
    device: str | None = None,
    note: str | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> PurgeReport:
    """`[start_ts, end_ts)` 를 **이미 저장된 것까지** 지운다.

    ★ 한 트랜잭션에서 두 가지를 같이 한다:
      ① `private_span(kind='purge')` 삽입 — **되살아남 방지**
      ② `aw_event` 에서 그 구간을 뺀다

    ①이 없으면 지워도 소용없다. PC 는 `overlap_sec`(900초)만큼 겹쳐 다시 읽고,
    커서를 잃으면 `backfill_days`(7일)까지 되돌아온다 — 다음 sync 가 그대로 다시
    넣는다. 구간을 남겨야 안전망(§`clip_events`)이 계속 막는다.

    걸친 이벤트는 **잘라서** 남긴다. 하트비트 병합 때문에 경계를 넘는 행이 흔한데
    통째로 지우면 프라이빗 밖의 정상 시간까지 날아간다. 잘린 조각의 창 제목은
    프라이빗을 켜기 **전에 이미 보이던 것**이라 새로 새는 정보가 없다.

    `dry_run` 이면 아무것도 안 바꾸고 세기만 한다 — 웹은 이걸 먼저 보여준 뒤
    `confirm` 을 받는다.
    """
    now = time.time() if now is None else now
    if end_ts <= start_ts:
        raise ValueError("end_ts 는 start_ts 보다 뒤여야 합니다")

    rows = _overlapping_rows(conn, start_ts, end_ts)
    removed_sec = sum(
        spans_overlap((float(r["ts"]), float(r["ts_end"])), start_ts, end_ts) for r in rows
    )
    days = affected_days(cfg, start_ts, end_ts)

    if dry_run:
        return PurgeReport(start_ts, end_ts, len(rows), removed_sec, days, True)

    with db.transaction(conn) as tx:
        cur = tx.execute(
            "INSERT INTO private_span(start_ts, end_ts, kind, source, device, note, "
            "created_at, updated_at) VALUES (?, ?, 'purge', ?, ?, ?, ?, ?)",
            (start_ts, end_ts, source, device, note, now, now),
        )
        span_id = int(cur.lastrowid)
        for row in rows:
            # ★ **지우지 않고 휴지통으로 옮긴다** (schema v10). 읽는 쪽은 `aw_event` 만
            #   보므로 화면·집계·LLM 어디에도 안 나온다. 진짜 삭제는 `forget()` 이
            #   사람 손으로 한다 — 되돌릴 수 없는 것을 자동으로 하지 않는다.
            tx.execute(
                f"INSERT OR REPLACE INTO purged_event({', '.join(_EVENT_COLUMNS)}, "
                "purge_span_id, purged_at) "
                f"VALUES ({', '.join('?' * len(_EVENT_COLUMNS))}, ?, ?)",
                (
                    row["bucket_id"], row["ts"], row["ts_end"], row["duration"], row["event_id"],
                    row["app"], row["title"], row["url"], row["status"],
                    row["data_json"], row["synced_at"], span_id, now,
                ),
            )
            tx.execute(
                "DELETE FROM aw_event WHERE bucket_id = ? AND ts = ?",
                (row["bucket_id"], row["ts"]),
            )
            for s_, e_ in timeutil.subtract_spans(
                [(float(row["ts"]), float(row["ts_end"]))], [(start_ts, end_ts)]
            ):
                # 구간 **밖**만 남긴 조각. 원본은 휴지통에 통째로 있으므로, 되돌릴 때는
                # 이 조각을 지우고 원본을 되살린다.
                tx.execute(
                    f"INSERT INTO aw_event({', '.join(_EVENT_COLUMNS)}) "
                    f"VALUES ({', '.join('?' * len(_EVENT_COLUMNS))})",
                    (
                        row["bucket_id"], s_, e_, e_ - s_, row["event_id"],
                        row["app"], row["title"], row["url"], row["status"],
                        row["data_json"], row["synced_at"],
                    ),
                )

    return PurgeReport(start_ts, end_ts, len(rows), removed_sec, days, False, span_id=span_id)


def undo(conn: sqlite3.Connection, span_id: int | None = None, *, now: float | None = None) -> int:
    """휴지통에서 되돌린다. 반환값은 되살린 이벤트 수.

    `span_id` 를 안 주면 **가장 최근의 purge** 를 되돌린다 (오클릭 직후가 대부분이다).

    세 가지를 한 트랜잭션에서 되돌린다:

      ① 그 purge 가 잘라 남긴 **조각을 지운다** — 안 지우면 되살아난 원본과 같은 시간을
         두 번 센다
      ② 휴지통의 원본을 `aw_event` 로 되돌린다
      ③ `private_span.revoked = 1` — 안 하면 다음 sync 가 그 구간을 다시 자른다.
         **여기가 빠지면 되돌린 것이 조용히 다시 사라진다**

    ★ ①과 ②의 **순서가 중요하다.** 왼쪽 조각은 원본과 **같은 `ts`** 를 갖는데
      `aw_event` 의 PK 가 `(bucket_id, ts)` 다 — 먼저 안 지우면 되돌리기가 충돌한다.
      (플래그 방식이 깨진 것도 정확히 이 PK 때문이었다.)

    ★ `forget()` 으로 이미 완전 삭제한 구간은 되돌릴 수 없다. 그때는 0 을 반환한다 —
      "되돌렸다" 고 말하고 아무 일도 안 하는 것보다 낫다.
    """
    now = time.time() if now is None else now
    if span_id is None:
        row = conn.execute(
            "SELECT id FROM private_span WHERE kind = 'purge' AND revoked = 0 "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0
        span_id = int(row["id"])

    with db.transaction(conn) as tx:
        # ① 조각 치우기. 원본이 휴지통에 통째로 있으므로 조각은 재료가 아니다.
        #
        # 조각은 정의상 **원본의 구간 안에** 있다. 그 구간에 다른 이벤트가 새로
        # 들어올 일은 없다 — purge 구간은 `clip_events` 가 계속 막고, 창 버킷의
        # 이벤트는 서로 안 겹친다.
        tx.execute(
            "DELETE FROM aw_event WHERE EXISTS ("
            "  SELECT 1 FROM purged_event p"
            "  WHERE p.purge_span_id = ?"
            "    AND p.bucket_id = aw_event.bucket_id"
            "    AND aw_event.ts >= p.ts AND aw_event.ts_end <= p.ts_end)",
            (span_id,),
        )
        # ② 원본 되살리기
        restored = tx.execute(
            f"INSERT OR REPLACE INTO aw_event({', '.join(_EVENT_COLUMNS)}) "
            f"SELECT {', '.join(_EVENT_COLUMNS)} FROM purged_event WHERE purge_span_id = ?",
            (span_id,),
        ).rowcount
        tx.execute("DELETE FROM purged_event WHERE purge_span_id = ?", (span_id,))
        # ③ 구간 취소
        tx.execute(
            "UPDATE private_span SET revoked = 1, updated_at = ? WHERE id = ?",
            (now, span_id),
        )
    return int(restored)


def forget(conn: sqlite3.Connection, *, before: float | None = None) -> int:
    """휴지통을 **진짜로** 비운다. 반환값은 지운 수.

    ★ 이 함수만이 되돌릴 수 없다. 그래서 **타이머가 부르지 않는다** — 보존기간을
      정하는 것은 사람의 결정이고, 정하지 않은 채 자동으로 돌리면 "며칠 뒤 사라지는 줄
      알았는데 안 사라진" 또는 그 반대가 된다 (migrations/010 머리말).

    `before` 를 주면 그 시각 **이전에 버린 것만** 지운다.
    """
    sql = "DELETE FROM purged_event"
    args: list = []
    if before is not None:
        sql += " WHERE purged_at < ?"
        args.append(before)
    with db.transaction(conn) as tx:
        n = tx.execute(sql, args).rowcount
    return int(n)


def trash_count(conn: sqlite3.Connection) -> tuple[int, float | None]:
    """(휴지통의 이벤트 수, 가장 오래된 버린 시각).

    **안 보이면 잊힌다.** `lt private status` 가 이걸 늘 말한다 — "지웠다" 고 생각한
    것이 디스크에 남아 있는 상태를 사람이 모르면 안 된다.
    """
    row = conn.execute(
        "SELECT count(*) AS n, MIN(purged_at) AS oldest FROM purged_event"
    ).fetchone()
    return int(row["n"]), (float(row["oldest"]) if row["oldest"] is not None else None)


def spans_overlap(span: Span, lo: float, hi: float) -> float:
    return max(0.0, min(span[1], hi) - max(span[0], lo))


def affected_days(cfg, start_ts: float, end_ts: float) -> list[str]:
    """다시 롤업해야 하는 **논리적 하루** 목록.

    ★ 달력 날짜가 아니다. 이 앱의 하루는 `day_start_hour`(기본 06시)에 시작하므로
      새벽 2시는 **전날**이다. 여기서 `int(ts / 86400)` 같은 걸 쓰면 자정을 넘긴
      구간이 엉뚱한 날을 재롤업한다.
    """
    tz = ZoneInfo(cfg.timezone)
    hour = cfg.rollup.day_boundary_hour
    days: list[str] = []
    t = start_ts
    while t < end_ts:
        d = timeutil.day_str(t, tz, boundary_hour=hour)
        if d not in days:
            days.append(d)
        t = timeutil.day_bounds(d, tz, boundary_hour=hour)[1]
    last = timeutil.day_str(end_ts, tz, boundary_hour=hour)
    if last not in days:
        days.append(last)
    return days


def purge_aw_local(client, start_ts: float, end_ts: float) -> int:
    """엔드포인트의 **AW 로컬 DB** 에서도 지운다. 반환값은 지운 건수.

    ★ 구간에 **완전히 들어간 것만** 지운다. 걸친 이벤트를 통째로 지우면 프라이빗
      밖의 정상 시간까지 날아가는데, aw-server 에는 "잘라서 저장"이 없다.
      그리고 걸친 이벤트의 제목은 프라이빗을 켜기 전에 이미 보이던 것이다.

    ★ 이것은 **남의 기기 데이터**다. 기본값으로 조용히 하지 않는다 —
      `cfg.private.purge_aw` 로 켜야 돌고, 실패해도 전체를 실패시키지 않는다
      (젯슨 DB 차단은 이미 성립했다).

    정상 동작에서는 지울 것이 거의 없다. 워처가 멈춰 있으면 aw-server 에 아무것도
    안 쌓이기 때문이다. 남는 것은 **폴링 지연(≤poll_sec)과 헬퍼가 죽어 있던 시간**뿐이고,
    이 함수는 그 구멍을 닫는다.
    """
    deleted = 0
    for bucket_id in client.buckets():
        for ev in client.events(bucket_id, start=start_ts, end=end_ts):
            if ev.event_id is None:
                continue
            if ev.ts >= start_ts and ev.ts_end <= end_ts:
                client.delete_event(bucket_id, ev.event_id)
                deleted += 1
    return deleted
