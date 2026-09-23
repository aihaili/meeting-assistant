"""Probe: where does the VAD actually cut this continuous speech?

The stress runs disagreed with each other in a way that no single explanation covered.
The same eight paragraphs produced 32 rows at a 12 s window, 35 at 20 s, and 71 at 50 s.
If the cuts came from window boundaries the row count would *fall* as the window grows,
and if they came from VAD pauses it would be identical at every window size (the pauses
are a property of the audio). Neither happened, so neither explanation is right and the
next step is to stop guessing and read the VAD's own output.

This dumps the raw VAD spans and sentence spans for the whole file in one pass, then
prints the gap and duration distributions. Those two numbers decide the segmentation
thresholds: a gap bigger than ``_GAP_BARE_S`` starts a new row, so knowing the real gap
distribution is the difference between tuning a number and inventing one.

Also probes the same audio in 12 s and 20 s windows to see whether the VAD's cuts move
when the input is a slice rather than the whole file -- which would mean the cuts are an
artefact of the sliding window and not of the speech.

Usage:
    python scripts/probe_vad_cuts.py
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from phone_mic import audio as A  # noqa: E402
from phone_mic.streaming import parse_span_time  # noqa: E402

ROOT = HERE.parent
WAV = ROOT / "data" / "long-meeting" / "long-meeting-16k.wav"


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    assert sr == 16000, sr
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def build():
    from funasr import AutoModel

    t0 = time.time()
    m = AutoModel(model="paraformer-zh", model_revision="v2.0.4",
                  vad_model="fsmn-vad", vad_model_revision="v2.0.4",
                  punc_model="ct-punc-c", punc_model_revision="v2.0.4",
                  disable_update=True)
    print(f"模型加载 {time.time() - t0:.1f}s\n", flush=True)
    return m


def spans_of(model, audio: np.ndarray) -> tuple[list, list]:
    res = model.generate(input=audio, cache={}, batch_size_s=300,
                         sentence_timestamp=True)
    r = res[0] or {}
    vad = r.get("timestamp") or []
    sents = []
    for s in (r.get("sentence_info") or []):
        text = (s.get("text") or "").strip()
        if text:
            sents.append(parse_span_time(s.get("timestamp"), text))
    return vad, sents


def report(label: str, vad: list, sents: list) -> None:
    print("=" * 74)
    print(f"{label}")
    print("=" * 74)
    print(f"VAD 段数 {len(vad)}   句子数 {len(sents)}")

    # VAD spans, as gaps between consecutive spans.
    gaps = []
    for i in range(1, len(vad)):
        gaps.append((vad[i][0] - vad[i - 1][1]) / 1000.0)
    durs = [(v[1] - v[0]) / 1000.0 for v in vad]
    if durs:
        print(f"\nVAD 段时长: 中位 {np.median(durs):.2f}s  最小 {min(durs):.2f}s  "
              f"最大 {max(durs):.2f}s")
    if gaps:
        g = sorted(gaps)
        print(f"VAD 段间静音: 中位 {np.median(gaps):.2f}s  "
              f"最小 {g[0]:.2f}s  最大 {g[-1]:.2f}s")
        for thr in (0.30, 0.40, 0.75, 1.0):
            n = sum(1 for x in gaps if x > thr)
            print(f"    超过 {thr:.2f}s 的静音: {n} 处  -> 会开 {n + 1} 行")

    print("\n句子（前 24 条）:")
    for s in sents[:24]:
        print(f"    [{s['start']:6.2f}-{s['end']:6.2f}] ({s['end'] - s['start']:5.2f}s) "
              f"{s['text'][:40]}")
    if len(sents) > 24:
        print(f"    ... 另有 {len(sents) - 24} 条")

    sd = [(s["end"] - s["start"]) for s in sents]
    if sd:
        print(f"\n句子时长: 中位 {np.median(sd):.2f}s  最小 {min(sd):.2f}s  "
              f"最大 {max(sd):.2f}s")
        short = sum(1 for x in sd if x < 3.0)
        print(f"短于 3s 的句子: {short}/{len(sd)}")
    sg = [sents[i]["start"] - sents[i - 1]["end"] for i in range(1, len(sents))]
    if sg:
        g = sorted(sg)
        print(f"句子间间隔: 中位 {np.median(sg):.2f}s  最小 {g[0]:.2f}s  "
              f"最大 {g[-1]:.2f}s")
        for thr in (0.30, 0.40, 0.75):
            print(f"    超过 {thr:.2f}s 的句子间隙: {sum(1 for x in sg if x > thr)} 处")
    print()


def main() -> int:
    if not WAV.exists():
        print(f"缺少音频 {WAV}；先跑 scripts/gen_long_meeting.py")
        return 1
    audio = load(WAV)
    print(f"音频 {len(audio) / 16000:.1f}s\n")
    model = build()

    vad, sents = spans_of(model, audio)
    report("① 整段一次送入（无滑动窗口）", vad, sents)

    # Same audio as sliding windows, to see whether the cuts move.
    for win in (12.0, 20.0):
        n = int(win * 16000)
        vad, sents = spans_of(model, audio[:n])
        report(f"② 只送前 {win:.0f}s（滑动窗口里的一次 tick）", vad, sents)

    return 0


if __name__ == "__main__":
    sys.exit(main())
