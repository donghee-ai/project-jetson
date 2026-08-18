#!/usr/bin/env python3
"""
ASR 실패 원인 분리 — 인식기가 나쁜가, 합성음이 나쁜가

[speech-bench.py]에서 TTS 합성 명령의 인식률이 무너졌다. 그런데 같은 인식기가
**원어민 녹음 샘플은 완벽히 받아썼다.** 원인이 ASR인지 TTS인지 갈라야 한다.

방법: 같은 문장을 TTS 두 종류로 합성해 동일 ASR에 먹인다.
  ① vits-mimic3 ko_KO-kss "low"   (64MB, espeak 음소 기반)
  ② Supertonic 3 int8             (123MB, 31개 언어, 온디바이스 상용급)

실행: python3 scripts/asr-source-test.py
"""
import os, time, wave
import numpy as np
import sherpa_onnx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SP = os.path.join(ROOT, "models", "speech")
ASR_DIR = os.path.join(SP, "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
MIMIC = os.path.join(SP, "vits-mimic3-ko_KO-kss_low")
SUPER = os.path.join(SP, "sherpa-onnx-supertonic-3-tts-int8-2026-05-11")
OUT = os.path.join(ROOT, "results", "speech")

COMMANDS = ["거실 불 켜줘", "안방 불 꺼", "거실이랑 주방 불 다 꺼줘",
            "에어컨 이십사도로 맞춰줘", "안방 지금 몇 도야", "십 분 뒤에 알려줘"]


def tts_mimic():
    return sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=os.path.join(MIMIC, "ko_KO-kss_low.onnx"), lexicon="",
                tokens=os.path.join(MIMIC, "tokens.txt"),
                data_dir=os.path.join(MIMIC, "espeak-ng-data")),
            provider="cpu", num_threads=8), max_num_sentences=1))


def tts_super():
    return sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            supertonic=sherpa_onnx.OfflineTtsSupertonicModelConfig(
                duration_predictor=os.path.join(SUPER, "duration_predictor.int8.onnx"),
                text_encoder=os.path.join(SUPER, "text_encoder.int8.onnx"),
                vector_estimator=os.path.join(SUPER, "vector_estimator.int8.onnx"),
                vocoder=os.path.join(SUPER, "vocoder.int8.onnx"),
                tts_json=os.path.join(SUPER, "tts.json"),
                unicode_indexer=os.path.join(SUPER, "unicode_indexer.bin"),
                voice_style=os.path.join(SUPER, "voice.bin")),
            provider="cpu", num_threads=8), max_num_sentences=1))


def asr():
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=os.path.join(ASR_DIR, "model.int8.onnx"),
        tokens=os.path.join(ASR_DIR, "tokens.txt"),
        num_threads=8, use_itn=True, language="ko", provider="cpu")


def write_wav(path, samples, sr):
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.asarray(samples) * 32767).astype(np.int16).tobytes())


def norm(s):
    return "".join(ch for ch in s if ch.isalnum())


def main():
    os.makedirs(OUT, exist_ok=True)
    rec = asr()
    print("=" * 78)
    print(" ASR 실패 원인 분리 — 동일 문장 · 동일 인식기 · TTS만 교체")
    print("=" * 78)

    engines = [("mimic3-kss(low)", tts_mimic(), "m"), ("Supertonic-3", tts_super(), "s")]
    score = {}
    for name, tts, tag in engines:
        ok = 0
        gen_t = aud_t = 0.0
        print(f"\n── {name} ──")
        for i, cmd in enumerate(COMMANDS):
            t0 = time.time()
            a = tts.generate(cmd, sid=0, speed=1.0)
            gen = time.time() - t0
            dur = len(a.samples) / a.sample_rate
            gen_t += gen; aud_t += dur
            p = os.path.join(OUT, f"{tag}{i}.wav")
            write_wav(p, a.samples, a.sample_rate)

            st = rec.create_stream()
            st.accept_waveform(a.sample_rate, np.asarray(a.samples, dtype=np.float32))
            rec.decode_stream(st)
            got = st.result.text.strip()
            hit = norm(got) == norm(cmd)
            ok += hit
            print(f"  {'✅' if hit else '❌'} {cmd}")
            if not hit:
                print(f"       → {got}")
        score[name] = ok
        print(f"  정확 {ok}/{len(COMMANDS)}   TTS RTF {gen_t/aud_t:.3f} (실시간 {aud_t/gen_t:.0f}배속)")

    # 원어민 녹음 대조군
    ref = os.path.join(ASR_DIR, "test_wavs", "ko.wav")
    with wave.open(ref) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        sr = w.getframerate()
    st = rec.create_stream(); st.accept_waveform(sr, pcm); rec.decode_stream(st)

    print("\n" + "=" * 78)
    print("  대조군 — 원어민 실제 녹음:", st.result.text.strip())
    for k, v in score.items():
        print(f"  {k:<18} {v}/{len(COMMANDS)}")
    print("  → 원어민은 되는데 합성음만 틀린다면, 문제는 ASR이 아니라 TTS다")


if __name__ == "__main__":
    main()
