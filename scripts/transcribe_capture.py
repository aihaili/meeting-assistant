"""Transcribe a recorded capture to check it is actually usable speech.

A capture may be 48 kHz stereo or 16 kHz mono; everything downstream wants 16 kHz
mono, so this runs the same conversion the live audio path does and then prints the
transcript with sentence spans -- which is also a quick end-to-end check of the
resampling path on real recorded audio.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from phone_mic import audio as A  # noqa: E402
from phone_mic.streaming import StreamingASR  # noqa: E402


def main() -> int:
    wav = Path(sys.argv[1])
    audio, sr = A.read_wav(wav)
    print(f"input : {wav.name}  {len(audio) / sr:.2f}s @ {sr} Hz mono")
    print(f"level : {A.rms_dbfs(audio):.1f} dBFS mean")

    asr = StreamingASR()
    print(f"loading models ...", flush=True)
    print(f"        {asr.load():.2f}s")

    t0 = time.time()
    # Push the whole capture as one "stream" and flush: the sliding window is
    # irrelevant here, the point is just whether the words come out.
    asr.push(audio)
    got = asr.flush()
    dt = time.time() - t0

    print(f"\nasr   : {dt:.2f}s for {len(audio) / sr:.1f}s audio "
          f"({len(audio) / sr / max(dt, 1e-9):.1f}x realtime)")
    print(f"stats : {asr.stats()}")
    print("\n=== sentences ===")
    for s in asr.sentences:
        print(f"[{s.start:6.2f} - {s.end:6.2f}] {s.text}")
    print(f"\n=== transcript ===\n{asr.transcript()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
