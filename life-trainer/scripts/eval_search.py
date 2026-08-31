#!/usr/bin/env python3
"""검색 품질 비교 하니스 — `docs/rag-plan.md` §5 가 요구한 그 측정.

## 왜 있나

4단계(임베딩)는 "같은 질문 20개로 FTS5 vs 임베딩을 비교한 다음에 붙인다"는
조건이 붙어 있었는데, 그 비교 없이 붙었다. 이 스크립트가 그 빚을 갚는다.

## 무엇을 재나

| 방식 | 설명 |
|---|---|
| `kw` | FTS5 BM25 만 |
| `vec` | 벡터만, **맨 질의** (현재 코드) |
| `vec+i` | 벡터만, `Instruct: …\nQuery:` 접두 |
| `rrf` | 현재 하이브리드 (kw + vec, w=0.5) |
| `rrf+i` | 하이브리드, 벡터 쪽에 접두 |

## 판정을 어떻게 객관화했나

**엔티티 질의**는 정답이 기계로 판정된다 — 찾는 낱말이 제목·초록에 있으면 관련이다.
"jetson" 을 물었는데 jetson 이 한 글자도 없는 문서가 상위에 오면 그건 실패다.
개념 질의는 그렇게 못 하므로 **점수만 찍고 판정은 사람이 한다** (`--show`).

정밀도 지표는 `hit@5` — 상위 5건 중 해당 낱말을 담은 문서의 비율.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lifetrainer import db  # noqa: E402
from lifetrainer.config import load_config  # noqa: E402
from lifetrainer.llm import embed as E  # noqa: E402

WORD = re.compile(r"[0-9A-Za-z가-힣_.]+")
INSTRUCT = "Instruct: Given a search query, retrieve relevant articles\nQuery:"
K = 60  # RRF 상수. tools._hybrid_search 와 같은 값이어야 비교가 성립한다

# (질의, 관련 판정 낱말들) — 하나라도 담고 있으면 관련으로 센다.
ENTITY = [
    ("jetson", ("jetson", "orin")),
    ("llama.cpp", ("llama.cpp", "llama cpp", "gguf")),
    ("qwen", ("qwen",)),
    ("arxiv 논문", ("arxiv",)),
    ("양자화 quantization", ("quantiz", "양자화", "int8", "int4", "gguf")),
    ("embedding 임베딩", ("embedding", "임베딩", "embed")),
    ("robot 로봇", ("robot", "로봇")),
    ("speech 음성인식", ("speech", "asr", "음성", "voice")),
    ("diffusion 이미지 생성", ("diffusion", "이미지 생성")),
    ("inference 추론 속도", ("inference", "추론", "latency", "throughput")),
]
CONCEPT = [
    "온디바이스에서 큰 언어모델 돌리는 방법",
    "코딩 에이전트가 도구를 부르는 방식",
    "작은 모델로 큰 모델 성능 내기",
    "임베딩 검색 품질 올리는 법",
    "메모리가 적은 기기에서 추론하기",
    "에이전트가 기억을 관리하는 방법",
    "실시간 음성 처리 지연 줄이기",
    "오픈소스 모델 라이선스 문제",
    "학습 데이터를 적게 쓰는 방법",
    "전력 효율이 좋은 하드웨어",
]


def fts_top(conn, query: str, n: int) -> list[int]:
    words = WORD.findall(query)
    if not words:
        return []
    match = " OR ".join(f'"{w}"' for w in words[:8])
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT d.id FROM doc_fts f JOIN doc d ON d.id = f.rowid "
                "WHERE doc_fts MATCH ? AND d.dup_of IS NULL ORDER BY bm25(doc_fts) LIMIT ?",
                (match, n),
            )
        ]
    except Exception:
        return []


def vec_top(conn, cfg, query: str, n: int, *, instruct: bool) -> list[tuple[int, float]]:
    text = (INSTRUCT + query) if instruct else query
    try:
        vecs = E.embed_texts(cfg, [text[: cfg.embed.max_chars]])
    except E.EmbedError:
        return []
    return E.nearest(conn, cfg, vecs[0], n) if vecs else []


def rrf(kw: list[int], vec: list[int], w: float, n: int) -> list[int]:
    sc: dict[int, float] = {}
    for i, d in enumerate(kw):
        sc[d] = sc.get(d, 0.0) + (1 - w) / (K + i)
    for i, d in enumerate(vec):
        sc[d] = sc.get(d, 0.0) + w / (K + i)
    return sorted(sc, key=lambda d: -sc[d])[:n]


def relevant(text: str, terms: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(t.lower() in low for t in terms)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pool", type=int, default=20, help="각 방식이 뽑는 후보 수")
    ap.add_argument("--top", type=int, default=5, help="정밀도를 재는 상위 N")
    ap.add_argument("--weight", type=float, default=None, help="RRF 벡터 가중치 (기본=설정값)")
    ap.add_argument("--show", action="store_true", help="개념 질의의 상위 제목까지 출력")
    args = ap.parse_args()

    cfg = load_config()
    conn = db.open_db(cfg)
    conn.row_factory = __import__("sqlite3").Row
    w = cfg.embed.vector_weight if args.weight is None else args.weight
    titles = {
        r[0]: f"{r[1] or ''} {r[2] or ''} {r[3] or ''}"
        for r in conn.execute("SELECT id, title, abstract, summary FROM doc")
    }
    title_only = {r[0]: (r[1] or "") for r in conn.execute("SELECT id, title FROM doc")}

    methods = ("kw", "vec", "vec+i", "rrf", "rrf+i")
    tot = {m: [0, 0] for m in methods}  # [맞은 수, 전체]
    overlaps = []

    print(f"■ 엔티티 질의 {len(ENTITY)}개 — hit@{args.top} (상위 N 중 관련 문서 비율)")
    print(f"  pool={args.pool} · vector_weight={w} · 벡터 {len(titles):,}건 대상\n")
    print(f"  {'질의':26} " + " ".join(f"{m:>7}" for m in methods))
    print("  " + "-" * 26 + " " + " ".join("-" * 7 for _ in methods))

    for q, terms in ENTITY:
        kw = fts_top(conn, q, args.pool)
        v_raw = [d for d, _ in vec_top(conn, cfg, q, args.pool, instruct=False)]
        v_ins = [d for d, _ in vec_top(conn, cfg, q, args.pool, instruct=True)]
        overlaps.append(len(set(kw) & set(v_raw)))
        lists = {
            "kw": kw[: args.top],
            "vec": v_raw[: args.top],
            "vec+i": v_ins[: args.top],
            "rrf": rrf(kw, v_raw, w, args.top),
            "rrf+i": rrf(kw, v_ins, w, args.top),
        }
        cells = []
        for m in methods:
            ids = lists[m]
            hit = sum(1 for d in ids if relevant(titles.get(d, ""), terms))
            tot[m][0] += hit
            tot[m][1] += len(ids)
            cells.append(f"{hit}/{len(ids)}" if ids else "  -  ")
        print(f"  {q:26} " + " ".join(f"{c:>7}" for c in cells))

    print("  " + "-" * 26 + " " + " ".join("-" * 7 for _ in methods))
    print(
        f"  {'합계 hit@%d' % args.top:26} "
        + " ".join(f"{(tot[m][0]/tot[m][1]*100 if tot[m][1] else 0):6.0f}%" for m in methods)
    )
    print(f"\n  키워드∩벡터 겹침: 총 {sum(overlaps)}건 / 풀 {args.pool}×2×{len(ENTITY)}")

    print(f"\n■ 개념 질의 {len(CONCEPT)}개 — 코사인 점수 분포 (컷오프를 정하려면 이게 필요하다)")
    print(f"  {'질의':30} {'맨질의 1위':>10} {'5위':>7} {'접두 1위':>9} {'5위':>7}")
    for q in CONCEPT:
        raw = vec_top(conn, cfg, q, args.top, instruct=False)
        ins = vec_top(conn, cfg, q, args.top, instruct=True)
        print(
            f"  {q:30} {raw[0][1] if raw else 0:>10.3f} {raw[-1][1] if raw else 0:>7.3f}"
            f" {ins[0][1] if ins else 0:>9.3f} {ins[-1][1] if ins else 0:>7.3f}"
        )
        if args.show:
            for tag, lst in (("맨질의", raw), ("접두", ins)):
                for d, s in lst[:3]:
                    print(f"        {tag:5} [{s:.3f}] {title_only.get(d,'')[:58]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
