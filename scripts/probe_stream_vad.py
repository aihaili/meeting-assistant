"""Probe: what exactly does FunASR return for chunked calls, and are VAD
timestamps relative to the chunk or to some internal absolute clock?

The real-time meeting feed depends on being able to answer "which sentences are
now final?" after every ~1 s chunk. That requires knowing whether
``res[0]["timestamp"]`` (VAD output) and ``sentence_info[i]["timestamp"]`` are
measured from the start of the *input array we just passed*, or from something
else. Guessing wrong silently duplicates or drops text.

This script answers it by construction: it feeds the SAME audio twice, once as
one long array and once split into chunks, and compares the timestamps.

Usage:
    python scripts/probe_stream_vad.py <16k-mono.wav> [--chunk-sec 4]
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np


def read_wav16k(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a PCM WAV as float32 mono. Rejects anything that is not 16 kHz mono."""
    with wave.open(str(path), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise SystemExit(f"expected 16-bit PCM, got sampwidth={sw}")
    if ch != 1:
        raise SystemExit(f"expected mono, got {ch} channels")
    if sr != 16000:
        raise SystemExit(f"expected 16000 Hz, got {sr}")
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, sr


def build_model():
    from funasr import AutoModel

    t0 = time.time()
    model = AutoModel(
        model="paraformer-zh",
        model_revision="v2.0.4",
        vad_model="fsmn-vad",
        vad_model_revision="v2.0.4",
        punc_model="ct-punc-c",
        punc_model_revision="v2.0.4",
        disable_update=True,
    )
    print(f"[probe] model loaded in {time.time() - t0:.2f}s", flush=True)
    return model


def describe(res: list) -> None:
    """Dump the structure of a FunASR result without assuming its shape."""
    r = res[0]
    print(f"  top-level keys: {sorted(r.keys())}")
    vad = r.get("timestamp")
    if isinstance(vad, list):
        print(f"  VAD timestamp: {len(vad)} spans, first 3 = {vad[:3]}")
    else:
        print(f"  VAD timestamp: {type(vad).__name__} = {vad!r}")
    si = r.get("sentence_info")
    if isinstance(si, list):
        print(f"  sentence_info: {len(si)} sentences")
        for s in si[:3]:
            ts = s.get("timestamp")
            if isinstance(ts, list) and ts:
                span = (ts[0][0], ts[-1][1])
            else:
                span = ts
            print(f"    spk={s.get('spk')!r} span={span} text={s.get('text', '')[:40]!r}")
    else:
        print(f"  sentence_info: {type(si).__name__} = {si!r}")
    txt = r.get("text", "")
    print(f"  text ({len(txt)} chars): {txt[:120]!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--chunk-sec", type=float, default=4.0)
    ap.add_argument("--seconds", type=float, default=24.0,
                    help="how much audio to probe with (keeps the run short)")
    args = ap.parse_args()

    audio, sr = read_wav16k(args.wav)
    total = min(len(audio), int(args.seconds * sr))
    audio = audio[:total]
    print(f"[probe] audio: {total / sr:.1f}s @ {sr} Hz")

    model = build_model()

    # ── A: whole array in one call ──────────────────────────────────────
    print("\n=== A. one shot (whole array) ===")
    t0 = time.time()
    res_a = model.generate(input=audio, cache={}, batch_size_s=300, sentence_timestamp=True)
    print(f"  elapsed {time.time() - t0:.2f}s")
    describe(res_a)

    # ── B: chunked, each call given only the new slice ──────────────────
    step = int(args.chunk_sec * sr)
    print(f"\n=== B. chunked ({args.chunk_sec:.0f}s slices, fresh call each time) ===")
    for i in range(0, total, step):
        piece = audio[i:i + step]
        if len(piece) < sr // 2:
            break
        t0 = time.time()
        res = model.generate(input=piece, cache={}, batch_size_s=300, sentence_timestamp=True)
        dt = time.time() - t0
        vad = res[0].get("timestamp") or []
        si = res[0].get("sentence_info") or []
        print(f"\n  --- slice @ {i / sr:6.1f}s  ({len(piece) / sr:.1f}s audio, {dt:.2f}s wall) ---")
        print(f"      VAD spans: {vad}")
        for s in si:
            ts = s.get("timestamp") or []
            span = (ts[0][0], ts[-1][1]) if ts else None
            print(f"      sent span={span} text={s.get('text', '')[:44]!r}")

    # ── C: growing buffer, one call each time (what we would actually do) ──
    print(f"\n=== C. growing buffer (cumulative call, {args.chunk_sec:.0f}s steps) ===")
    for end in range(step, total + 1, step):
        buf = audio[:end]
        t0 = time.time()
        res = model.generate(input=buf, cache={}, batch_size_s=300, sentence_timestamp=True)
        dt = time.time() - t0
        vad = res[0].get("timestamp") or []
        si = res[0].get("sentence_info") or []
        last = None
        if si:
            ts = si[-1].get("timestamp") or []
            if ts:
                last = (ts[0][0], ts[-1][1])
        print(f"  buf={end / sr:5.1f}s wall={dt:5.2f}s | vad_spans={len(vad)} "
              f"n_sent={len(si)} last_sent_span={last}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
