#!/usr/bin/env python3
"""
음성 에이전트 엔드투엔드 실측 — 슬롯 정확도 기준

★ 채점 기준을 바꾼 이유
  [asr-source-test.py]에서 문자열 완전일치로 채점했더니 "에어컨 24도로 맞춰줘"가
  **오답 처리**됐다. 원문이 "이십사도"였기 때문이다. 하지만 ASR의 숫자 정규화(ITN)
  결과이므로 의미는 정확하다.

  실제 시스템에서 중요한 것은 받아쓴 글자가 아니라 **최종적으로 어떤 기기가
  어떻게 동작했는가**다. 그래서 채점 단위를 툴 호출의 슬롯(tool + 인자)으로 바꾼다.

측정 경로 2개를 같은 기준으로 비교한다.
  ① 텍스트 직행  : 명령문 → LLM            ← 상한선 (ASR 오류 없음)
  ② 음성 경로    : 명령문 → TTS → ASR → LLM ← 실제 시스템
  ②와 ①의 차이가 **ASR 오류가 깎아먹는 정확도**다.

실행: python3 scripts/voice-e2e-bench.py [--port 8080] [--tts super|mimic]
"""
import json, os, sys, time, urllib.request, wave
import numpy as np
import sherpa_onnx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SP = os.path.join(ROOT, "models", "speech")
ASR_DIR = os.path.join(SP, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
MIMIC = os.path.join(SP, "vits-mimic3-ko_KO-kss_low")
SUPER = os.path.join(SP, "sherpa-onnx-supertonic-3-tts-int8-2026-05-11")

PORT = "8080"
TTS_KIND = "super"
for i, a in enumerate(sys.argv):
    if a == "--port": PORT = sys.argv[i + 1]
    if a == "--tts": TTS_KIND = sys.argv[i + 1]
LLM = f"http://127.0.0.1:{PORT}/v1/chat/completions"

TOOLS = [
    {"type": "function", "function": {
        "name": "light_set", "description": "지정한 공간의 조명을 켜거나 끄고 밝기를 조절한다",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string"}, "state": {"type": "string", "enum": ["on", "off"]},
            "brightness_pct": {"type": "integer"}}, "required": ["area", "state"]}}},
    {"type": "function", "function": {
        "name": "climate_set", "description": "냉난방기의 온도와 모드를 설정한다",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string"}, "temperature": {"type": "number"},
            "mode": {"type": "string", "enum": ["cool", "heat", "off"]}}, "required": ["area"]}}},
    {"type": "function", "function": {
        "name": "sensor_get", "description": "센서 값을 조회한다",
        "parameters": {"type": "object", "properties": {
            "area": {"type": "string"}, "kind": {"type": "string", "enum": ["temperature", "humidity"]}},
            "required": ["area", "kind"]}}},
    {"type": "function", "function": {
        "name": "timer_start", "description": "타이머를 설정한다",
        "parameters": {"type": "object", "properties": {
            "minutes": {"type": "integer"}, "label": {"type": "string"}}, "required": ["minutes"]}}},
]

# ★ 음성 경로 전용 시스템 프롬프트 — 입력이 음성인식 결과임을 명시
SYSTEM = (
    "너는 집 안의 기기를 제어하는 한국어 음성 비서다.\n"
    "사용자 입력은 **음성인식 결과**라 오탈자·띄어쓰기 오류가 섞일 수 있다. "
    "가장 그럴듯한 기기 제어 의도로 해석해라. (예: '볼꺼'→'불 꺼', '거실 켜줘'→거실 조명)\n"
    "기기 제어 요청이면 반드시 툴을 호출하고, 아니면 툴 없이 한 문장으로 답한다.\n"
    "사용 가능한 공간: 거실, 주방, 안방, 화장실"
)

# (발화, 기대 슬롯 목록)  — 슬롯은 (툴명, {인자: 값}) 리스트
CASES = [
    ("거실 불 켜줘",            [("light_set", {"area": "거실", "state": "on"})]),
    ("안방 불 꺼",              [("light_set", {"area": "안방", "state": "off"})]),
    ("거실이랑 주방 불 다 꺼줘", [("light_set", {"area": "거실", "state": "off"}),
                                 ("light_set", {"area": "주방", "state": "off"})]),
    ("에어컨 이십사도로 맞춰줘", [("climate_set", {"temperature": 24})]),
    ("안방 지금 몇 도야",       [("sensor_get", {"area": "안방", "kind": "temperature"})]),
    ("십 분 뒤에 알려줘",       [("timer_start", {"minutes": 10})]),
    ("거실 불 삼십 퍼센트로 낮춰줘", [("light_set", {"area": "거실", "brightness_pct": 30})]),
    ("주방 불 켜고 안방 불 꺼",  [("light_set", {"area": "주방", "state": "on"}),
                                 ("light_set", {"area": "안방", "state": "off"})]),
]


def build_tts(kind):
    m = sherpa_onnx.OfflineTtsModelConfig(provider="cpu", num_threads=8)
    if kind == "super":
        m.supertonic = sherpa_onnx.OfflineTtsSupertonicModelConfig(
            duration_predictor=os.path.join(SUPER, "duration_predictor.int8.onnx"),
            text_encoder=os.path.join(SUPER, "text_encoder.int8.onnx"),
            vector_estimator=os.path.join(SUPER, "vector_estimator.int8.onnx"),
            vocoder=os.path.join(SUPER, "vocoder.int8.onnx"),
            tts_json=os.path.join(SUPER, "tts.json"),
            unicode_indexer=os.path.join(SUPER, "unicode_indexer.bin"),
            voice_style=os.path.join(SUPER, "voice.bin"))
    else:
        m.vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=os.path.join(MIMIC, "ko_KO-kss_low.onnx"), lexicon="",
            tokens=os.path.join(MIMIC, "tokens.txt"),
            data_dir=os.path.join(MIMIC, "espeak-ng-data"))
    return sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=m, max_num_sentences=1))


def build_asr():
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(ASR_DIR, "model.int8.onnx"),
        tokens=os.path.join(ASR_DIR, "tokens.txt"),
        num_threads=8, use_itn=True, language="ko", provider="cpu")


def llm(text):
    body = json.dumps({"model": "local",
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": text}],
                       "tools": TOOLS, "tool_choice": "auto",
                       "max_tokens": 200, "temperature": 0.2}).encode()
    req = urllib.request.Request(LLM, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    el = time.time() - t0
    m = d["choices"][0]["message"]
    out = []
    for c in (m.get("tool_calls") or []):
        try:
            out.append((c["function"]["name"], json.loads(c["function"]["arguments"])))
        except Exception:
            out.append((c["function"]["name"], {}))
    return out, el


def slots_ok(got, want):
    """기대 슬롯이 전부 충족되고 개수가 같으면 성공. 인자는 부분집합 비교."""
    if len(got) != len(want):
        return False
    rest = list(got)
    for wname, wargs in want:
        for i, (gname, gargs) in enumerate(rest):
            if gname != wname:
                continue
            ok = True
            for k, v in wargs.items():
                g = gargs.get(k)
                if g is None:
                    ok = False; break
                if isinstance(v, (int, float)):
                    try:
                        if float(g) != float(v): ok = False; break
                    except (TypeError, ValueError):
                        ok = False; break
                elif str(v) not in str(g):
                    ok = False; break
            if ok:
                rest.pop(i); break
        else:
            return False
    return True


def fmt(slots):
    return " + ".join(f"{n}({', '.join(f'{k}={v}' for k, v in a.items())})" for n, a in slots) or "(툴 없음)"


def main():
    tts, rec = build_tts(TTS_KIND), build_asr()
    print("=" * 80)
    print(f" 음성 에이전트 E2E — 슬롯 정확도 기준 (TTS={TTS_KIND}, LLM port={PORT})")
    print("=" * 80)

    txt_ok = voice_ok = 0
    lat_asr, lat_llm, lat_tts = [], [], []
    for utt, want in CASES:
        # ① 텍스트 직행 (상한선)
        g_txt, t_txt = llm(utt)
        a = slots_ok(g_txt, want)
        txt_ok += a

        # ② 음성 경로
        t0 = time.time(); au = tts.generate(utt, sid=0, speed=1.0); tts_t = time.time() - t0
        dur = len(au.samples) / au.sample_rate
        lat_tts.append((tts_t, dur))
        t0 = time.time()
        st = rec.create_stream()
        st.accept_waveform(au.sample_rate, np.asarray(au.samples, dtype=np.float32))
        rec.decode_stream(st)
        heard = st.result.text.strip()
        asr_t = time.time() - t0
        g_v, llm_t = llm(heard)
        b = slots_ok(g_v, want)
        voice_ok += b
        lat_asr.append(asr_t); lat_llm.append(llm_t)

        print(f"\n  발화: {utt}")
        print(f"    [{'✅' if a else '❌'}] 텍스트 직행  {t_txt:5.2f}s  {fmt(g_txt)[:78]}")
        print(f"    [{'✅' if b else '❌'}] 음성 경로    ASR {asr_t:.2f}s + LLM {llm_t:5.2f}s")
        print(f"         인식: {heard}")
        print(f"         결과: {fmt(g_v)[:78]}")

    n = len(CASES)
    A = sum(lat_asr) / n; L = sum(lat_llm) / n
    tg = sum(x[0] for x in lat_tts); td = sum(x[1] for x in lat_tts)
    print("\n" + "=" * 80)
    print(f"  슬롯 정확도   텍스트 직행 {txt_ok}/{n} ({txt_ok/n*100:.0f}%)"
          f"   음성 경로 {voice_ok}/{n} ({voice_ok/n*100:.0f}%)"
          f"   ← ASR 손실 {(txt_ok-voice_ok)/n*100:.0f}%p")
    print(f"  지연          ASR {A:.2f}s + LLM {L:.2f}s = {A+L:.2f}s  (+ 발화종료감지 0.3~0.7s + 응답 TTS)")
    print(f"  TTS RTF       {tg/td:.3f} (실시간 {td/tg:.1f}배속) — 응답 음성 생성 비용")


if __name__ == "__main__":
    main()
