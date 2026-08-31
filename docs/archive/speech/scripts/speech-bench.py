#!/usr/bin/env python3
"""
한국어 음성 스택 실측 — TTS · ASR · 엔드투엔드

마이크가 없어도 전 구간을 측정할 수 있다. **TTS로 명령 음성을 합성해서 ASR에 먹인다.**
합성 음성이므로 실제 마이크 환경(잡음·거리·에코)보다 유리하다 — 상한선 측정이다.

측정 항목
  ① TTS RTF   — 음성 1초를 만드는 데 걸리는 시간
  ② ASR RTF   — 음성 1초를 알아듣는 데 걸리는 시간
  ③ E2E       — 발화 종료 → ASR → LLM 툴콜까지 총 지연

전제: refs/models/speech/ 에 sherpa-onnx 모델 3종, llama-server 가동 중(툴콜 측정 시)
실행: python3 scripts/speech-bench.py [--no-llm]
"""
import json, os, sys, time, urllib.request, wave

import numpy as np
import sherpa_onnx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEECH = os.path.join(ROOT, "models", "speech")
ASR_DIR = os.path.join(SPEECH, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
TTS_DIR = os.path.join(SPEECH, "vits-mimic3-ko_KO-kss_low")
OUT = os.path.join(ROOT, "results", "speech")
LLM = "http://127.0.0.1:8080/v1/chat/completions"

# 스마트홈 음성 비서에서 실제로 나올 법한 발화
COMMANDS = [
    "거실 불 켜줘",
    "안방 불 꺼",
    "거실이랑 주방 불 다 꺼줘",
    "에어컨 이십사도로 맞춰줘",
    "안방 지금 몇 도야",
    "십 분 뒤에 알려줘",
]

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
SYSTEM = ("너는 집 안의 기기를 제어하는 한국어 음성 비서다. 기기 제어 요청이면 반드시 툴을 호출하고, "
          "아니면 툴 없이 한 문장으로 짧게 답한다. 사용 가능한 공간: 거실, 주방, 안방, 화장실")


def build_tts(num_threads):
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=os.path.join(TTS_DIR, "ko_KO-kss_low.onnx"),
                lexicon="", tokens=os.path.join(TTS_DIR, "tokens.txt"),
                data_dir=os.path.join(TTS_DIR, "espeak-ng-data")),
            provider="cpu", num_threads=num_threads),
        max_num_sentences=1)
    return sherpa_onnx.OfflineTts(cfg)


def build_asr(model_file, num_threads):
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(ASR_DIR, model_file),
        tokens=os.path.join(ASR_DIR, "tokens.txt"),
        num_threads=num_threads, use_itn=True, language="ko", provider="cpu")


def read_wav(path):
    with wave.open(path) as w:
        n, sr = w.getnframes(), w.getframerate()
        pcm = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768
    return pcm, sr


def write_wav(path, samples, sr):
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.asarray(samples) * 32767).astype(np.int16).tobytes())


def asr_once(rec, pcm, sr):
    t0 = time.time()
    st = rec.create_stream()
    st.accept_waveform(sr, pcm)
    rec.decode_stream(st)
    return st.result.text.strip(), time.time() - t0


def llm_tool_call(text):
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
    calls = [(c["function"]["name"], c["function"]["arguments"]) for c in (m.get("tool_calls") or [])]
    return calls, el


def main():
    use_llm = "--no-llm" not in sys.argv
    os.makedirs(OUT, exist_ok=True)
    print("=" * 78)
    print(" 한국어 음성 스택 실측 (sherpa-onnx, CPU) — 마이크 없이 TTS 합성 음성으로 측정")
    print("=" * 78)

    # ── ① TTS ────────────────────────────────────────────────────────────────
    print("\n[1] TTS — 한국어 음성 합성 (vits mimic3 ko_KO-kss_low, 64MB)")
    wavs = []
    for nt in (2, 4, 8):
        tts = build_tts(nt)
        tot_gen = tot_aud = 0.0
        for i, cmd in enumerate(COMMANDS):
            t0 = time.time()
            a = tts.generate(cmd, sid=0, speed=1.0)
            gen = time.time() - t0
            dur = len(a.samples) / a.sample_rate
            tot_gen += gen; tot_aud += dur
            if nt == 4:
                p = os.path.join(OUT, f"cmd{i}.wav")
                write_wav(p, a.samples, a.sample_rate)
                wavs.append((cmd, p, dur))
        print(f"    threads={nt}:  RTF {tot_gen/tot_aud:.3f}  "
              f"(음성 {tot_aud:.1f}초 생성에 {tot_gen:.2f}초, 실시간의 {tot_aud/tot_gen:.0f}배속)")

    # ── ② ASR ────────────────────────────────────────────────────────────────
    print("\n[2] ASR — 한국어 인식 (SenseVoice-Small)")
    ko_ref = os.path.join(ASR_DIR, "test_wavs", "ko.wav")
    for model_file in ("model.int8.onnx", "model.onnx"):
        for nt in (4, 8):
            rec = build_asr(model_file, nt)
            pcm, sr = read_wav(ko_ref)
            txt, el = asr_once(rec, pcm, sr)   # 워밍업 겸 원어민 샘플
            dur = len(pcm) / sr
            print(f"    {model_file:<17} threads={nt}:  RTF {el/dur:.3f}  "
                  f"({dur:.1f}초 음성 → {el:.2f}초)")
            if model_file == "model.int8.onnx" and nt == 8:
                print(f"      원어민 샘플 인식: {txt}")

    print("\n[3] ASR 정확도 — TTS 합성 명령 인식 (int8, threads=8)")
    rec = build_asr("model.int8.onnx", 8)
    asr_results = []
    for cmd, path, dur in wavs:
        pcm, sr = read_wav(path)
        txt, el = asr_once(rec, pcm, sr)
        hit = "✅" if txt.replace(" ", "") == cmd.replace(" ", "") else "⚠️"
        asr_results.append((cmd, txt, el, dur))
        print(f"    {hit} [{el:.2f}s / 음성{dur:.1f}s] 원문: {cmd}")
        print(f"           인식: {txt}")

    # ── ③ E2E ────────────────────────────────────────────────────────────────
    if use_llm:
        print("\n[4] 엔드투엔드 — ASR + LLM 툴콜 (llama-server)")
        tot = []
        for cmd, txt, asr_t, dur in asr_results:
            try:
                calls, llm_t = llm_tool_call(txt)
            except Exception as e:
                print(f"    LLM 실패: {e}"); break
            tot.append((asr_t, llm_t))
            names = ", ".join(f"{n}({a})" for n, a in calls) or "(툴 없음)"
            print(f"    {cmd}")
            print(f"      ASR {asr_t:.2f}s + LLM {llm_t:.2f}s = {asr_t+llm_t:.2f}s  → {names[:80]}")
        if tot:
            a = sum(x[0] for x in tot) / len(tot); l = sum(x[1] for x in tot) / len(tot)
            print(f"\n    평균: ASR {a:.2f}s + LLM {l:.2f}s = {a+l:.2f}s")
            print(f"    ※ 여기에 발화 종료 감지(0.3~0.7s)와 응답 TTS가 더해진다")

    print("\n" + "=" * 78)
    print(f"  합성 음성 저장: {OUT}/")


if __name__ == "__main__":
    main()
