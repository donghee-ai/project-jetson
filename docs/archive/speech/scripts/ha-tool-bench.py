#!/usr/bin/env python3
"""
한국어 음성 에이전트 — 스마트홈 툴 콜링 정확도 측정

[bench/tool-bench.py]는 일반 에이전트 적합성을 봤다. 이 스크립트는 음성 비서에
특화된 조건을 본다. 음성 명령은 텍스트 명령과 다르다.

  ① 구어체·줄임말   "불 좀", "아 덥다 에어컨 좀"
  ② 대상 생략       "불 꺼줘"      ← 어느 방인지 없음
  ③ 상대적 지시     "좀 더 밝게"    ← 절대값이 없음
  ④ 부정문          "불 켜지 마"    ← 호출하면 오작동
  ⑤ 지연이 곧 품질   음성 UX는 1초를 넘기면 체감이 급격히 나빠진다

실행: python3 scripts/ha-tool-bench.py            (기본 http://127.0.0.1:8080)
      python3 scripts/ha-tool-bench.py <서버URL>
"""
import json, sys, time, urllib.request

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080") + "/v1/chat/completions"

# Home Assistant 실제 서비스에 대응하는 최소 툴셋
TOOLS = [
    {"type": "function", "function": {
        "name": "light_set", "description": "지정한 공간의 조명을 켜거나 끄고 밝기를 조절한다",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string", "description": "거실, 주방, 안방, 화장실 등"},
            "state": {"type": "string", "enum": ["on", "off"]},
            "brightness_pct": {"type": "integer", "description": "밝기 0-100"}},
            "required": ["area", "state"]}}},
    {"type": "function", "function": {
        "name": "climate_set", "description": "냉난방기의 온도와 모드를 설정한다",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string"},
            "temperature": {"type": "number"},
            "mode": {"type": "string", "enum": ["cool", "heat", "off"]}},
            "required": ["area"]}}},
    {"type": "function", "function": {
        "name": "sensor_get", "description": "센서 값을 조회한다 (온도, 습도)",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string"},
            "kind": {"type": "string", "enum": ["temperature", "humidity"]}},
            "required": ["area", "kind"]}}},
    {"type": "function", "function": {
        "name": "scene_activate", "description": "미리 정의된 장면(씬)을 실행한다",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "영화, 취침, 외출, 귀가"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "timer_start", "description": "타이머를 설정한다",
        "parameters": {"type": "object", "properties": {
            "minutes": {"type": "integer"}, "label": {"type": "string"}},
            "required": ["minutes"]}}},
]

SYSTEM = (
    "너는 집 안의 기기를 제어하는 한국어 음성 비서다. "
    "사용자의 말이 기기 제어 요청이면 반드시 툴을 호출하고, "
    "제어 요청이 아니면 툴 없이 한 문장으로 짧게 답한다. "
    "정보가 부족하면 추측하지 말고 되물어라. "
    "사용 가능한 공간: 거실, 주방, 안방, 화장실"
)

# (분류, 발화, 기대 툴, 필수 인자 조건 — dict{툴명: {키: 값}} / None이면 인자 미검사)
CASES = [
    ("기본 제어",   "거실 불 켜줘",                    ["light_set"],
     {"light_set": {"area": "거실", "state": "on"}}),
    ("기본 제어",   "안방 불 꺼",                      ["light_set"],
     {"light_set": {"area": "안방", "state": "off"}}),
    ("병렬 2건",    "거실이랑 주방 불 다 꺼줘",         ["light_set", "light_set"], None),
    ("수치 인자",   "거실 불 30퍼센트로 낮춰줘",        ["light_set"],
     {"light_set": {"brightness_pct": 30}}),
    ("복합 2종",    "에어컨 24도로 맞추고 거실 불 꺼줘", ["climate_set", "light_set"], None),
    ("상태 조회",   "안방 지금 몇 도야?",              ["sensor_get"],
     {"sensor_get": {"area": "안방", "kind": "temperature"}}),
    ("씬 실행",     "영화 볼 거야",                    ["scene_activate"], None),
    ("타이머",      "십 분 뒤에 알려줘",               ["timer_start"],
     {"timer_start": {"minutes": 10}}),
    ("구어체",      "아 덥다 에어컨 좀",               ["climate_set"], None),
    ("★대상 생략",  "불 꺼줘",                         [], None),
    ("★부정문",     "불 켜지 마",                      [], None),
    ("★잡담",       "오늘 기분 어때?",                 [], None),
]


def run(utterance):
    body = json.dumps({
        "model": "local",
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": utterance}],
        "tools": TOOLS, "tool_choice": "auto",
        "max_tokens": 300, "temperature": 0.2}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return d, time.time() - t0


def check_args(calls, want):
    """필수 인자가 기대값과 일치하는지. 숫자는 값 비교, 문자열은 포함 비교."""
    if want is None:
        return True, ""
    for tool, conds in want.items():
        for c in calls:
            if c["function"]["name"] != tool:
                continue
            try:
                a = json.loads(c["function"]["arguments"])
            except Exception:
                return False, "인자 JSON 파싱 실패"
            for k, v in conds.items():
                got = a.get(k)
                if got is None:
                    return False, f"{k} 누락"
                if isinstance(v, (int, float)):
                    if float(got) != float(v):
                        return False, f"{k}={got} (기대 {v})"
                elif str(v) not in str(got):
                    return False, f"{k}={got} (기대 {v})"
            return True, ""
        return False, f"{tool} 미호출"
    return True, ""


def main():
    print("=" * 78)
    print(" 한국어 스마트홈 툴 콜링 — 음성 에이전트 적합성")
    print("=" * 78)
    ok_n, lat = 0, []
    for kind, utt, expect, want in CASES:
        try:
            d, el = run(utt)
        except Exception as e:
            print(f"\n[{kind}] 실패: {e}")
            continue
        lat.append(el)
        m = d["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        got = [c["function"]["name"] for c in calls]
        args_ok, why = check_args(calls, want)
        ok = sorted(got) == sorted(expect) and args_ok
        ok_n += ok

        print(f"\n[{'✅' if ok else '❌'}] {kind}  ({el:.2f}s)")
        print(f"     발화: {utt}")
        print(f"     기대: {expect or '(툴 없음)'}   실제: {got or '(툴 없음)'}")
        for c in calls:
            print(f"       └ {c['function']['name']}({c['function']['arguments']})")
        if not calls and m.get("content"):
            print(f"       └ 응답: {m['content'].strip()[:100]}")
        if not args_ok and why:
            print(f"       ⚠️ {why}")

    print("\n" + "=" * 78)
    print(f"  통과 {ok_n}/{len(CASES)}   지연 평균 {sum(lat)/len(lat):.2f}s / 최대 {max(lat):.2f}s")
    print("  ※ ★ 항목은 '툴을 부르지 않아야' 정답 (되묻기·거부·잡담)")
    print("  ※ 음성 UX 기준: LLM 단계 1초 이내가 바람직")


if __name__ == "__main__":
    main()
