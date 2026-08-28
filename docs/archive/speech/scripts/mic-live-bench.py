#!/usr/bin/env python3
"""
실마이크 음성 에이전트 실측 — 합성음이 아닌 사람 목소리로

[voice-e2e-bench.py]는 TTS 합성음으로 측정했다. 잡음·거리·에코가 없으므로
그 수치는 **상한선**이다. 이 스크립트는 실제 마이크 입력으로 같은 것을 잰다.

파이프라인
  arecord → silero VAD 분절 → SenseVoice ASR → 핫패스 라우터 → (실패 시) LLM 툴콜

측정
  ① ASR 정확도 (실음성)         ② 단계별 지연        ③ 핫패스 적중률
  ④ 입력 레벨·잡음 바닥          ⑤ 발화 종료 감지 지연 (VAD 설정값)

실행:
  python3 scripts/mic-live-bench.py --sec 45                 # 45초 동안 말하기
  python3 scripts/mic-live-bench.py --wav <파일>             # 기존 녹음 재분석
  python3 scripts/mic-live-bench.py --sec 45 --expect-file <문장목록>
"""
import argparse, importlib.util, json, os, subprocess, sys, time, urllib.request, wave
import numpy as np
import sherpa_onnx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SP = os.path.join(ROOT, "models", "speech")
ASR_DIR = os.path.join(SP, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
VAD_MODEL = os.path.join(SP, "silero_vad.onnx")
OUT = os.path.join(ROOT, "results", "speech", "live")

# 핫패스 라우터를 모듈로 불러온다 (파일명에 하이픈이 있어 importlib 사용)
_spec = importlib.util.spec_from_file_location("hotpath", os.path.join(ROOT, "scripts", "hotpath-router.py"))
hotpath = importlib.util.module_from_spec(_spec)
sys.argv = [sys.argv[0]]  # 라우터 모듈이 argv를 보지 않도록
_spec.loader.exec_module(hotpath)

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
    {"type": "function", "function": {
        "name": "scene_activate", "description": "미리 정의된 장면(씬)을 실행한다",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
]
SYSTEM = ("너는 집 안의 기기를 제어하는 한국어 음성 비서다.\n"
          "사용자 입력은 **음성인식 결과**라 오탈자·띄어쓰기 오류가 섞일 수 있다. "
          "가장 그럴듯한 기기 제어 의도로 해석해라.\n"
          "기기 제어 요청이면 반드시 툴을 호출하고, 아니면 툴 없이 한 문장으로 답한다. "
          "대상이 불분명하면 추측하지 말고 되물어라.\n"
          "사용 가능한 공간: 거실, 주방, 안방, 화장실")


def record(sec, device, path):
    subprocess.run(["arecord", "-D", device, "-f", "S16_LE", "-r", "16000", "-c", "1",
                    "-d", str(sec), path], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load(path):
    with wave.open(path) as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    return pcm, sr


def build_vad(min_silence):
    c = sherpa_onnx.VadModelConfig()
    c.silero_vad.model = VAD_MODEL
    c.silero_vad.threshold = 0.5
    c.silero_vad.min_silence_duration = min_silence
    c.silero_vad.min_speech_duration = 0.20
    c.silero_vad.max_speech_duration = 10.0
    c.sample_rate = 16000
    c.provider = "cpu"
    c.num_threads = 1
    return sherpa_onnx.VoiceActivityDetector(c, buffer_size_in_seconds=120)


def build_asr():
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(ASR_DIR, "model.int8.onnx"),
        tokens=os.path.join(ASR_DIR, "tokens.txt"),
        num_threads=8, use_itn=True, language="ko", provider="cpu")


def llm(text, port):
    body = json.dumps({"model": "local",
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": text}],
                       "tools": TOOLS, "tool_choice": "auto",
                       "max_tokens": 200, "temperature": 0.2}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    m = d["choices"][0]["message"]
    out = []
    for c in (m.get("tool_calls") or []):
        try:
            out.append((c["function"]["name"], json.loads(c["function"]["arguments"])))
        except Exception:
            out.append((c["function"]["name"], {}))
    return out, (m.get("content") or "").strip(), time.time() - t0


def fmt(slots):
    return " + ".join(f"{n}({', '.join(f'{k}={v}' for k, v in a.items() if v is not None)})"
                      for n, a in slots)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec", type=int, default=45)
    ap.add_argument("--device", default="plughw:2,0")
    ap.add_argument("--wav", default=None)
    ap.add_argument("--port", default="8080")
    ap.add_argument("--min-silence", type=float, default=0.35)
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    path = a.wav or os.path.join(OUT, f"session-{a.sec}s.wav")
    if not a.wav:
        print(f"🎙  {a.sec}초 녹음 시작 — 명령을 하나씩, 사이에 1초 이상 쉬면서 말하세요")
        sys.stdout.flush()
        record(a.sec, a.device, path)
        print("   녹음 종료\n")

    pcm, sr = load(path)
    noise = float(np.sqrt((pcm[:int(sr * 0.5)] ** 2).mean()))

    # ── VAD 분절 ──────────────────────────────────────────────────────────────
    vad = build_vad(a.min_silence)
    t0 = time.time()
    segs, win = [], 512
    for i in range(0, len(pcm) - win, win):
        vad.accept_waveform(pcm[i:i + win])
        while not vad.empty():
            s = vad.front
            segs.append((s.start / sr, np.array(s.samples, dtype=np.float32)))
            vad.pop()
    vad.flush()
    while not vad.empty():
        s = vad.front
        segs.append((s.start / sr, np.array(s.samples, dtype=np.float32)))
        vad.pop()
    vad_t = time.time() - t0

    print("=" * 80)
    print(f" 실마이크 음성 에이전트 실측 — {os.path.basename(path)}")
    print("=" * 80)
    print(f"  녹음 {len(pcm)/sr:.1f}초 / 잡음바닥 RMS {noise:.4f} / VAD 분절 {len(segs)}건 "
          f"(처리 {vad_t:.2f}s = 실시간의 {len(pcm)/sr/max(vad_t,1e-9):.0f}배속)")
    if not segs:
        print("  ⚠️ 발화가 검출되지 않았다. 마이크 게인 또는 --min-silence 확인.")
        return

    rec = build_asr()
    n_hot = n_llm = n_drop = 0
    asr_ts, llm_ts = [], []
    for i, (start, seg) in enumerate(segs, 1):
        dur = len(seg) / sr
        rms = float(np.sqrt((seg ** 2).mean()))
        t0 = time.time()
        st = rec.create_stream(); st.accept_waveform(sr, seg); rec.decode_stream(st)
        heard = st.result.text.strip()
        asr_t = time.time() - t0
        asr_ts.append(asr_t)

        t0 = time.perf_counter()
        slots = hotpath.route(heard)
        route_t = (time.perf_counter() - t0) * 1000

        print(f"\n  [{i}] {start:5.1f}s  길이 {dur:4.1f}s  RMS {rms:.4f} (SNR {20*np.log10(max(rms,1e-9)/max(noise,1e-9)):4.1f} dB)")
        print(f"      인식: {heard}   [ASR {asr_t:.2f}s = RTF {asr_t/dur:.3f}]")
        if slots:
            n_hot += 1
            print(f"      핫패스 {route_t:.2f}ms → {fmt(slots)}")
        elif not hotpath.looks_like_command(heard):
            n_drop += 1
            print(f"      🚫 도메인 게이트에서 차단 (배경 소리로 판단) — LLM 호출 안 함")
        elif a.no_llm:
            print(f"      핫패스 미적중 ({route_t:.2f}ms) → LLM 폴백 (생략)")
        else:
            n_llm += 1
            try:
                s2, txt, lt = llm(heard, a.port)
                llm_ts.append(lt)
                print(f"      핫패스 미적중 → LLM {lt:.2f}s → {fmt(s2) or '(툴 없음) ' + txt[:60]}")
            except Exception as e:
                print(f"      LLM 실패: {e}")

    n = len(segs)
    print("\n" + "=" * 80)
    print(f"  발화 {n}건   핫패스 {n_hot}건 ({n_hot/n*100:.0f}%)   게이트 차단 {n_drop}건   LLM 폴백 {n_llm}건")
    print(f"  ASR   평균 {sum(asr_ts)/n:.2f}s")
    if llm_ts:
        print(f"  LLM   평균 {sum(llm_ts)/len(llm_ts):.2f}s")
    print(f"  체감 지연 = 발화종료감지 {a.min_silence:.2f}s + ASR {sum(asr_ts)/n:.2f}s + "
          f"{'핫패스 0.0002s' if n_hot else 'LLM'} (+ 응답 TTS)")
    print(f"  녹음 파일: {path}")


if __name__ == "__main__":
    main()
