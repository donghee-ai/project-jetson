#!/usr/bin/env python3
"""
툴 콜링 정확도 측정 — 에이전트 적합성 판단용

에이전트는 (1) 언제 툴을 부를지 (2) 어떤 툴을 (3) 인자를 정확히
세 가지를 다 맞춰야 한다. 하나라도 틀리면 에이전트 루프가 깨진다.

실행: python3 bench/tool-bench.py
"""
import json, urllib.request

URL = "http://127.0.0.1:8080/v1/chat/completions"

TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather", "description": "도시의 현재 날씨를 조회한다",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "도시명"}}, "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "calculate", "description": "수식을 계산한다",
        "parameters": {"type": "object", "properties": {
            "expr": {"type": "string", "description": "계산할 수식"}}, "required": ["expr"]}}},
    {"type": "function", "function": {
        "name": "search_grants", "description": "지원사업 공고를 검색한다",
        "parameters": {"type": "object", "properties": {
            "region": {"type": "string"}, "min_years": {"type": "integer"},
            "field": {"type": "string"}}, "required": ["region"]}}},
    {"type": "function", "function": {
        "name": "send_email", "description": "이메일을 발송한다",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"}}, "required": ["to", "subject", "body"]}}},
]

# (설명, 프롬프트, 기대 툴 목록 — 빈 리스트면 "툴 부르지 말아야 함")
CASES = [
    ("단일 호출", "서울 날씨 알려줘.", ["get_weather"]),
    ("병렬 2건", "부산 날씨 알려주고 37*24도 계산해줘.", ["get_weather", "calculate"]),
    ("인자 3개", "경기도에서 업력 3년 이상 바이오헬스 지원사업 찾아줘.", ["search_grants"]),
    ("복합 호출", "대전 날씨 확인하고 그 내용을 boss@corp.com 으로 '오늘 날씨' 제목으로 보내줘.",
     ["get_weather", "send_email"]),
    ("★툴 불필요", "너는 어떤 모델이야? 한 문장으로 답해.", []),
    ("★모호한 요청", "뭐 좀 알아봐줘.", []),
]


def run(prompt):
    body = json.dumps({"model": "local",
                       "messages": [{"role": "user", "content": prompt}],
                       "tools": TOOLS, "tool_choice": "auto",
                       "max_tokens": 400, "temperature": 0.3}).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def main():
    print("=" * 76)
    print(" 툴 콜링 정확도 — 에이전트 적합성")
    print("=" * 76)
    ok_n = 0
    for name, prompt, expect in CASES:
        try:
            d = run(prompt)
        except Exception as e:
            print(f"\n[{name}] 실패: {e}")
            continue
        m = d["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        got = [c["function"]["name"] for c in calls]

        # 판정: 기대 툴 집합과 일치하는가 + 인자가 파싱 가능한 JSON인가
        args_ok = True
        for c in calls:
            try:
                json.loads(c["function"]["arguments"])
            except Exception:
                args_ok = False
        ok = (sorted(got) == sorted(expect)) and args_ok
        ok_n += ok

        print(f"\n[{'✅' if ok else '❌'}] {name}")
        print(f"     요청: {prompt[:56]}")
        print(f"     기대: {expect or '(툴 없음)'}")
        print(f"     실제: {got or '(툴 없음)'}")
        for c in calls:
            print(f"       └ {c['function']['name']}({c['function']['arguments']})")
        if not calls and m.get("content"):
            print(f"       └ 텍스트 응답: {m['content'].strip()[:90]}")
        if not args_ok:
            print("       ⚠️ 인자가 유효한 JSON이 아님")

    print("\n" + "=" * 76)
    print(f"  통과 {ok_n}/{len(CASES)}")
    print("  ※ ★ 항목은 '툴을 부르지 않아야' 정답 — 과잉 호출 여부를 본다")


if __name__ == "__main__":
    main()
