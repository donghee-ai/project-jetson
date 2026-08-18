#!/usr/bin/env python3
"""
핫패스 라우터 — 자주 쓰는 명령에서 LLM을 빼면 얼마나 빨라지는가

[voice-e2e-bench.py] 결과: 음성 에이전트 지연의 **95%가 LLM**이다.
  ASR 0.12s + LLM 2.45s (4B) / 3.54s (8B)

그런데 집에서 쓰는 음성 명령은 종류가 몇 개 안 된다. 조명 On/Off, 밝기, 온도,
센서 조회, 타이머 — 이 소수가 발화의 대부분을 차지한다.
**그 소수를 규칙으로 즉시 처리하고, 나머지만 LLM에 넘긴다.**

  핫패스 (규칙)   → 수 밀리초      ← 명령의 대부분
  폴백  (LLM)     → 2~4초          ← 애매하거나 처음 보는 표현

[Understudy](../docs/project-proposal.md)의 선생-학생 구조와 같다.
학생(규칙)이 확신하면 즉답하고, 못 하면 선생(LLM)에게 넘긴다.
나아가 **LLM이 처리한 표현을 규칙으로 승격**시키면 학생이 계속 자란다.

실행: python3 scripts/hotpath-router.py        (LLM 폴백 비교 포함)
      python3 scripts/hotpath-router.py --no-llm
"""
import json, re, sys, time, urllib.request

LLM = "http://127.0.0.1:8080/v1/chat/completions"

AREAS = {
    "거실": ["거실"],
    "주방": ["주방", "부엌"],
    "안방": ["안방", "침실"],
    "화장실": ["화장실", "욕실"],
}
# ASR이 자주 흘리는 변형 — 실측에서 관찰된 것만 넣는다 (추측 금지)
ASR_FIX = [("볼꺼", "불 꺼"), ("불켜", "불 켜"), ("불꺼", "불 꺼"), ("부 다", "불 다")]

KO_NUM = {"영": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5,
          "육": 6, "칠": 7, "팔": 8, "구": 9, "십": 10}


def ko2num(s):
    """'이십사' → 24, '십' → 10, '2십4' → 24 (백 미만)

    ★ 실마이크 측정에서 ASR이 '이십사도'를 **'2십4도'** 로 받아썼다.
      숫자와 한글이 섞인 표기를 못 읽으면 24를 4로 해석해 에어컨을 4도로 맞춘다.
      실제로 발생한 오류이므로 혼합 표기를 반드시 처리한다.
    """
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch.isdigit():
            cur = cur * 10 + int(ch) if cur < 10 else int(ch)
        elif ch == "십":
            cur = (cur or 1) * 10
            total += cur
            cur = 0
        elif ch in KO_NUM:
            cur = KO_NUM[ch]
    return total + cur


NUM = r"(\d*[영일이삼사오육칠팔구십]+\d*|\d+)"

# 명령 종결 어미 뒤에서 문장을 자른다 — VAD가 여러 명령을 한 발화로 묶었을 때 필요
CLAUSE = re.compile(r"(?<=줘)\s+|(?<=꺼)\s+|(?<=켜)\s+|(?<=도야)\s+|(?<=해)\s+")


def normalize(t):
    t = t.strip().rstrip(".?!")
    for a, b in ASR_FIX:
        t = t.replace(a, b)
    return t


def find_areas(t):
    """등장 순서대로 공간을 찾는다 (여러 개면 전부)"""
    hits = []
    for canon, words in AREAS.items():
        for w in words:
            i = t.find(w)
            if i >= 0:
                hits.append((i, canon))
                break
    return [a for _, a in sorted(hits)]


# 이 집에서 제어 가능한 대상·동작 어휘. 하나도 없으면 기기 명령일 수 없다.
DOMAIN = re.compile(
    r"불|조명|등\b|에어컨|냉방|난방|온도|습도|몇\s*도|타이머|알려|분\s*(뒤|후)|"
    r"밝게|어둡게|퍼센트|%|영화|취침|외출|귀가|거실|주방|안방|화장실|침실|부엌|욕실")


def looks_like_command(text):
    """LLM에 넘길 가치가 있는가 — 배경 대화·TV 소리를 걸러낸다.

    ★ 실마이크 측정에서 VAD가 잡은 4건 중 3건이 배경 소리였고, 그때마다
      LLM이 1.5~5.7초씩 헛돌았다. 상시 가동 기기에서는 이것이 치명적이다.
      (근본 해법은 웨이크워드. 이 게이트는 그 전까지의 저비용 방어선이다.)
    """
    return bool(DOMAIN.search(normalize(text)))


def route(text):
    """여러 명령이 한 발화에 붙어 있을 수 있으므로 절 단위로 나눠 각각 처리한다.

    ★ 실마이크 측정에서 VAD가 명령 4개를 한 발화(7.8초)로 묶었다.
      절 분리 없이 첫 규칙만 적용하면 나머지 명령이 통째로 사라진다.
    """
    parts = [p for p in CLAUSE.split(normalize(text)) if p.strip()]
    if len(parts) <= 1:
        return route_one(text)
    out = []
    for p in parts:
        r = route_one(p)
        if r is None:
            return None          # 하나라도 확신 못 하면 전체를 LLM에 넘긴다
        out.extend(r)
    return out or None


def route_one(text):
    """규칙으로 슬롯을 뽑는다. 확신 없으면 None(→LLM 폴백)."""
    t = normalize(text)
    areas = find_areas(t)

    # 타이머: "10분 뒤에 알려줘"
    m = re.search(NUM + r"\s*분\s*(뒤|후)", t)
    if m:
        return [("timer_start", {"minutes": ko2num(m.group(1))})]

    # 센서 조회: "몇 도야"
    if re.search(r"몇\s*도", t):
        return [("sensor_get", {"area": areas[0] if areas else None,
                                "kind": "temperature"})] if areas else None

    # 냉난방: "에어컨 24도"
    m = re.search(NUM + r"\s*도", t)
    if m and re.search(r"에어컨|냉방|난방|온도", t):
        mode = "heat" if "난방" in t else "cool"
        return [("climate_set", {"area": areas[0] if areas else "거실",
                                 "temperature": ko2num(m.group(1)), "mode": mode})]

    # 밝기: "거실 불 30%로 낮춰줘"
    m = re.search(NUM + r"\s*(퍼센트|%)", t)
    if m and areas:
        return [("light_set", {"area": areas[0], "state": "on",
                               "brightness_pct": ko2num(m.group(1))})]

    # 조명 On/Off — 공간마다 개별 판정 (혼합 명령 지원)
    if "불" in t or "조명" in t:
        if not areas:
            return None                      # 대상 없음 → LLM에 되묻게 넘긴다
        calls = []
        for a in areas:
            seg = t[t.find(next(w for w in AREAS[a] if w in t)):]
            nxt = min([seg.find(w) for c in AREAS if c != a
                       for w in AREAS[c] if seg.find(w) > 0] or [len(seg)])
            seg = seg[:nxt]
            if re.search(r"꺼|끄|소등", seg):
                calls.append(("light_set", {"area": a, "state": "off"}))
            elif re.search(r"켜|점등", seg):
                calls.append(("light_set", {"area": a, "state": "on"}))
            else:                            # 한 구간에 동사가 없으면 전체 문장으로 판정
                if re.search(r"꺼|끄", t):
                    calls.append(("light_set", {"area": a, "state": "off"}))
                elif re.search(r"켜", t):
                    calls.append(("light_set", {"area": a, "state": "on"}))
                else:
                    return None
        return calls or None

    return None


# (발화, 기대 슬롯) — voice-e2e-bench.py 와 동일 + ASR 실측 인식문 포함
CASES = [
    ("거실 불 켜줘",              [("light_set", {"area": "거실", "state": "on"})]),
    ("안방 불꺼",                 [("light_set", {"area": "안방", "state": "off"})]),
    ("안방 볼꺼",                 [("light_set", {"area": "안방", "state": "off"})]),   # ★ASR 오인식
    ("거실이랑 주방불 다 꺼줘",    [("light_set", {"area": "거실", "state": "off"}),
                                   ("light_set", {"area": "주방", "state": "off"})]),
    ("주방 불켜고 안방불꺼",       [("light_set", {"area": "주방", "state": "on"}),
                                   ("light_set", {"area": "안방", "state": "off"})]),
    ("에어컨 24도로 맞춰줘",       [("climate_set", {"temperature": 24})]),
    ("에어컨 이십사도로 맞춰줘",   [("climate_set", {"temperature": 24})]),
    ("안방 지금 몇 도야",         [("sensor_get", {"area": "안방", "kind": "temperature"})]),
    ("10분 뒤에 알려줘",          [("timer_start", {"minutes": 10})]),
    ("십 분 뒤에 알려줘",         [("timer_start", {"minutes": 10})]),
    ("거실 불 30%로 낮춰줘",      [("light_set", {"area": "거실", "brightness_pct": 30})]),
    # ↓ 규칙이 처리하면 안 되는 것들 (LLM으로 넘어가야 정답)
    ("불 꺼줘",                   None),      # 대상 없음 → 되물어야 함
    ("영화 볼 거야",              None),      # 씬 — 규칙 밖
    ("아 덥다 에어컨 좀",         None),      # 온도 없음 → LLM 판단
    ("오늘 기분 어때?",           None),      # 잡담
]


def ok(got, want):
    if want is None:
        return got is None
    if got is None or len(got) != len(want):
        return False
    rest = list(got)
    for wn, wa in want:
        for i, (gn, ga) in enumerate(rest):
            if gn == wn and all(str(ga.get(k)) == str(v) for k, v in wa.items()):
                rest.pop(i); break
        else:
            return False
    return True


def llm_call(text):
    body = json.dumps({"model": "local",
                       "messages": [{"role": "system", "content": "너는 한국어 스마트홈 음성 비서다."},
                                    {"role": "user", "content": text}],
                       "max_tokens": 100, "temperature": 0.2}).encode()
    req = urllib.request.Request(LLM, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        r.read()
    return time.time() - t0


def main():
    print("=" * 78)
    print(" 핫패스 라우터 — 규칙으로 처리되는 비율과 지연")
    print("=" * 78)
    hit = fall = good = 0
    times = []
    for utt, want in CASES:
        t0 = time.perf_counter()
        got = route(utt)
        el = (time.perf_counter() - t0) * 1000
        times.append(el)
        correct = ok(got, want)
        good += correct
        if got is None:
            fall += 1
        else:
            hit += 1
        tag = "핫패스" if got is not None else "→LLM "
        print(f"  [{'✅' if correct else '❌'}] {tag} {el:6.3f}ms  {utt}")
        if got:
            print(f"            {got}")

    n = len(CASES)
    print("\n" + "=" * 78)
    print(f"  정확도    {good}/{n}")
    print(f"  핫패스    {hit}/{n} 건이 규칙으로 즉답 · LLM 폴백 {fall}/{n}")
    print(f"  지연      평균 {sum(times)/n:.3f} ms / 최대 {max(times):.3f} ms")

    if "--no-llm" not in sys.argv:
        try:
            t = llm_call("거실 불 켜줘")
            print(f"\n  대조군    같은 명령을 LLM으로: {t*1000:.0f} ms "
                  f"→ 핫패스가 약 {t*1000/max(sum(times)/n, 1e-6):,.0f}배 빠르다")
        except Exception as e:
            print(f"\n  (LLM 대조군 생략: {e})")


if __name__ == "__main__":
    main()
