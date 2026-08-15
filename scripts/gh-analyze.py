#!/usr/bin/env python3
"""
GitHub 조사 결과 통합·분류 — 젯슨 적합성 기준

4개 소스(API 1 + Playwright 3)를 병합하고 다음 기준으로 점수화한다.
  ① 유지보수    최근 푸시일
  ② 라이선스    상업적 사용 가능 여부
  ③ 젯슨 적합   메모리·아키텍처·엣지 언급
  ④ 관련도      검색어 매칭 횟수

실행: python3 scripts/gh-analyze.py
"""
import json, pathlib, re
from collections import Counter

R = pathlib.Path("/home/user/project/project-jetson/results")

# ── 상업적 사용 안전 라이선스
SAFE = {"MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "ISC",
        "MPL-2.0", "Unlicense", "0BSD", "Zlib"}
COPYLEFT = {"GPL-3.0", "GPL-2.0", "AGPL-3.0", "LGPL-3.0", "LGPL-2.1"}

# ── 자율주행 제외 (사용자 요청)
EXCLUDE = re.compile(
    r"autonomous.?driv|self.?driv|adas|carla|autoware|lidar.?slam|"
    r"lane.?detect|traffic.?sign|vehicle.?control", re.I)

# ── 젯슨/엣지 적합 신호
JETSON_HINT = re.compile(
    r"jetson|orin|xavier|tensorrt|deepstream|edge|arm64|aarch64|"
    r"embedded|raspberry|on.?device|local.?first|offline|gguf|llama\.cpp|"
    r"quantiz|cuda", re.I)


def load():
    repos = {}
    # API (권위 소스 — 라이선스·날짜 정확)
    for r in json.load(open(R / "gh-api.json")):
        repos[r["repo"]] = {**r, "src": {"api"}, "cats": set(r["cats"])}
    # Playwright 3종 (보조 — API에 없는 것만 추가)
    for fn in ("gh-research.json", "gh-research2.json", "gh-research3.json"):
        p = R / fn
        if not p.exists():
            continue
        for r in json.load(open(p)):
            k = r["repo"]
            if k in repos:
                repos[k]["src"].add("pw")
                repos[k]["cats"] |= set(r.get("cats", []))
            else:
                s = str(r.get("stars", "0")).replace(",", "")
                try:
                    n = int(float(s[:-1]) * 1000) if s.endswith("k") else int(float(s))
                except ValueError:
                    n = 0
                repos[k] = {
                    "repo": k, "stars": n, "lang": r.get("lang", ""),
                    "desc": r.get("desc", ""), "license": "?",
                    "updated": "", "archived": False, "topics": [],
                    "cats": set(r.get("cats", [])), "src": {"pw"},
                }
    return repos


def score(r):
    """젯슨 프로젝트 참고 가치 점수"""
    s = 0
    # 유지보수 (최대 30)
    u = r.get("updated", "")
    if u >= "2026-05-15": s += 30
    elif u >= "2026-02-15": s += 22
    elif u >= "2025-08-15": s += 12
    elif u: s += 4
    # 라이선스 (최대 20)
    lic = r.get("license", "?")
    if lic in SAFE: s += 20
    elif lic in COPYLEFT: s += 10
    elif lic == "?": s += 5
    # 인기 (최대 25) — 로그 스케일
    st = r.get("stars", 0)
    s += min(25, (st ** 0.42) / 3) if st > 0 else 0
    # 젯슨 적합 신호 (최대 20)
    text = f"{r.get('desc','')} {' '.join(r.get('topics',[]))}"
    hits = len(set(m.group(0).lower() for m in JETSON_HINT.finditer(text)))
    s += min(20, hits * 7)
    # 다중 카테고리 매칭 (최대 5)
    s += min(5, len(r["cats"]))
    return round(s, 1)


def main():
    repos = load()
    print(f"통합 전: {len(repos)}개")

    out = []
    dropped = Counter()
    for r in repos.values():
        text = f"{r['repo']} {r.get('desc','')} {' '.join(r.get('topics',[]))}"
        if EXCLUDE.search(text):
            dropped["자율주행"] += 1; continue
        if r.get("archived"):
            dropped["아카이브"] += 1; continue
        if r.get("updated") and r["updated"] < "2025-02-15":
            dropped["1년6개월+방치"] += 1; continue
        if r.get("stars", 0) < 3 and "jetson" not in r["cats"]:
            dropped["별 3 미만"] += 1; continue
        r["score"] = score(r)
        r["cats"] = sorted(r["cats"]); r["src"] = sorted(r["src"])
        out.append(r)

    print("제외:", dict(dropped))
    print(f"최종: {len(out)}개")
    out.sort(key=lambda r: -r["score"])
    json.dump(out, open(R / "gh-final.json", "w"), ensure_ascii=False, indent=1)

    # 카테고리별 상위 출력
    for cat in ["jetson", "agent", "mcp", "serving", "vision", "rag",
                "dataset", "voice", "edge", "eval", "ocr", "korean",
                "home", "workflow", "embed", "ops"]:
        rows = [r for r in out if cat in r["cats"]][:10]
        if not rows: continue
        print(f"\n═══ {cat} ═══")
        for r in rows:
            lic = r["license"] if r["license"] in SAFE else \
                  (f"⚠{r['license']}" if r["license"] not in ("?", "NONE") else "?")
            print(f"  {r['score']:>5.1f} {r['stars']:>7,}★ {r['repo']:<40} "
                  f"{lic:<14} {r['updated']:<10} {r['desc'][:48]}")


if __name__ == "__main__":
    main()
