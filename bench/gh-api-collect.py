#!/usr/bin/env python3
"""
GitHub REST API 기반 저장소 수집 — Playwright 스크래핑 보완

웹 검색(github.com)이 미인증 요청을 70% 차단해서 API로 전환.
API는 별도 리밋(미인증 검색 10회/분)이고 라이선스·갱신일·별 수를
정확히 제공한다. 분류 기준에 필요한 필드가 스크래핑보다 완전하다.

실행: python3 bench/gh-api-collect.py <출력.json>
"""
import json
import pathlib, sys, time, urllib.request, urllib.error

OUT = sys.argv[1] if len(sys.argv) > 1 else \
    str(pathlib.Path(__file__).resolve().parent.parent / "results" / "gh-api.json")

QUERIES = [
    # ── 에이전트 프레임워크
    ("agent", "ai agent framework local llm"),
    ("agent", "topic:ai-agent"),
    ("agent", "topic:llm-agent"),
    ("agent", "topic:agentic-ai"),
    ("agent", "topic:autonomous-agents"),
    ("agent", "topic:agent-framework"),
    ("agent", "self-hosted ai agent tools"),
    ("agent", "coding agent cli terminal"),
    ("agent", "computer use agent automation"),
    ("agent", "multi agent orchestration llm"),
    ("agent", "agent memory persistent llm"),
    # ── MCP
    ("mcp", "topic:mcp-server"),
    ("mcp", "topic:model-context-protocol"),
    ("mcp", "model context protocol tools"),
    # ── 로컬 LLM 서빙·UI
    ("serving", "topic:local-llm"),
    ("serving", "topic:llamacpp"),
    ("serving", "topic:ollama"),
    ("serving", "openai compatible api server local"),
    ("serving", "chat ui self hosted llm"),
    ("serving", "llm router gateway proxy"),
    # ── Jetson / 엣지
    ("jetson", "topic:jetson"),
    ("jetson", "jetson orin inference"),
    ("jetson", "topic:jetson-nano"),
    ("jetson", "nvidia jetson containers"),
    ("jetson", "topic:edge-ai"),
    ("jetson", "tensorrt inference optimization"),
    ("jetson", "deepstream pipeline"),
    # ── 비전 (자율주행 제외)
    ("vision", "video analytics search llm"),
    ("vision", "vlm video understanding"),
    ("vision", "topic:object-detection edge"),
    ("vision", "cctv surveillance ai open source"),
    # ── 공개 학습 데이터
    ("dataset", "topic:dataset language-model"),
    ("dataset", "open llm training dataset"),
    ("dataset", "korean nlp dataset"),
    ("dataset", "synthetic data generation llm"),
    ("dataset", "instruction tuning dataset"),
    ("dataset", "topic:open-data"),
    # ── RAG / 문서
    ("rag", "topic:rag"),
    ("rag", "topic:retrieval-augmented-generation"),
    ("rag", "document parsing layout extraction"),
    ("rag", "pdf to markdown converter llm"),
    ("rag", "knowledge base self hosted"),
    # ── 음성
    ("voice", "whisper realtime transcription"),
    ("voice", "topic:speech-to-text"),
    ("voice", "voice assistant offline local"),
    ("voice", "topic:text-to-speech"),
    # ── 워크플로우
    ("workflow", "topic:workflow-automation"),
    ("workflow", "low code ai workflow"),
    # ── 평가·관측
    ("eval", "llm evaluation framework"),
    ("eval", "agent benchmark tool calling"),
    ("eval", "topic:llm-observability"),
    # ── 임베딩·벡터
    ("embed", "topic:vector-database"),
    ("embed", "embedding model multilingual"),
    # ── OCR
    ("ocr", "topic:ocr"),
    ("ocr", "table extraction document ai"),
    # ── 홈 오토메이션
    ("home", "topic:home-automation"),
    ("home", "home assistant llm"),
    # ── 한국어
    ("korean", "korean language model"),
    ("korean", "korean nlp library"),
    # ── 운영
    ("ops", "gpu monitoring nvidia dashboard"),
    ("ops", "topic:mlops self-hosted"),
    # ── 엣지 최적화
    ("edge", "speculative decoding"),
    ("edge", "topic:quantization llm"),
    ("edge", "kv cache optimization"),
]

API = "https://api.github.com/search/repositories"


def fetch(q, per_page=15):
    url = f"{API}?q={urllib.parse.quote(q)}&sort=stars&order=desc&per_page={per_page}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "jetson-research",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            remaining = r.headers.get("x-ratelimit-remaining", "?")
            return json.loads(r.read()).get("items", []), remaining
    except urllib.error.HTTPError as e:
        if e.code == 403:
            reset = e.headers.get("x-ratelimit-reset")
            wait = max(10, int(reset) - int(time.time()) + 2) if reset else 60
            print(f"    403 리밋 — {wait}초 대기", file=sys.stderr)
            time.sleep(min(wait, 90))
            return None, "0"
        print(f"    HTTP {e.code}", file=sys.stderr)
        return [], "?"
    except Exception as e:
        print(f"    실패: {e}", file=sys.stderr)
        return [], "?"


def main():
    import urllib.parse  # noqa
    all_repos, ok, fail = {}, 0, 0
    for cat, q in QUERIES:
        print(f"[{cat}] {q}", file=sys.stderr)
        items, rem = fetch(q)
        if items is None:                      # 리밋 → 1회 재시도
            items, rem = fetch(q)
        if not items:
            fail += 1
            print("  → 0건", file=sys.stderr)
        else:
            ok += 1
            print(f"  → {len(items)}건 (남은 호출 {rem})", file=sys.stderr)
        for it in items or []:
            k = it["full_name"]
            if k not in all_repos:
                all_repos[k] = {
                    "repo": k,
                    "stars": it.get("stargazers_count", 0),
                    "lang": it.get("language") or "",
                    "desc": (it.get("description") or "")[:220],
                    "license": (it.get("license") or {}).get("spdx_id") or "NONE",
                    "updated": (it.get("pushed_at") or "")[:10],
                    "archived": it.get("archived", False),
                    "topics": it.get("topics", [])[:8],
                    "cats": [],
                }
            if cat not in all_repos[k]["cats"]:
                all_repos[k]["cats"].append(cat)
        time.sleep(7)                          # 미인증 검색 10회/분

    res = sorted(all_repos.values(), key=lambda r: -r["stars"])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"\n성공 {ok} / 실패 {fail}\n총 {len(res)}개 → {OUT}", file=sys.stderr)


if __name__ == "__main__":
    import urllib.parse
    main()
