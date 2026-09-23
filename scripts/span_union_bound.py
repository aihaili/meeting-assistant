"""Upper bound on retention: stitch ALL spans seen across all ticks and measure.

If the union of every span's text already retains ~100% of the ground truth, then
nothing is being lost in the audio or in the ASR -- the loss happens in
``fold_span``/``update_row``, which decide what to do with those spans. That is a
decisive test between "the ring buffer dropped the head" and "the fold dropped it".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STRIP = re.compile(r"[\s，。、；：？！,.;:?!\"'“”‘’()（）\[\]【】—…·-]+")


def norm(s: str) -> str:
    return STRIP.sub("", s or "")


def lcs_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", default="data/probe/trace_w12_before.json")
    ap.add_argument("--gt", default="data/long-meeting/ground-truth.json")
    args = ap.parse_args()

    data = json.loads(Path(args.trace).read_text(encoding="utf-8"))
    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    want = norm(gt["full_text"])

    for run in data:
        w = run["window_s"]
        spans = [r for r in run["trace"] if r.get("kind") == "span"]
        # Union of everything the ASR ever said, in one string.
        union = norm("".join(r["text"] for r in spans))
        # Same, but only spans that are not themselves substrings of another span's
        # text (i.e. drop pure re-hearings to give an honest, non-inflated number).
        texts = sorted({r["text"] for r in spans}, key=len, reverse=True)
        maximal = [t for t in texts if not any(t != o and norm(t) in norm(o) for o in texts)]
        maximal_txt = norm("".join(maximal))
        got = norm("".join(r["text"] for r in run["rows"]))

        print("=" * 90)
        print(f"window={w}s")
        print("=" * 90)
        print(f"  source chars                 : {len(want)}")
        print(f"  ASR spans seen               : {len(spans)} (unique texts {len(texts)})")
        print(f"  UNION of all span texts      : {len(union)} chars, "
              f"LCS vs source = {100 * lcs_len(want, union) / len(want):.1f}%")
        print(f"  union of MAXIMAL spans only  : {len(maximal_txt)} chars, "
              f"LCS vs source = {100 * lcs_len(want, maximal_txt) / len(want):.1f}%")
        print(f"  final rows (what fold kept)  : {len(got)} chars, "
              f"LCS vs source = {100 * lcs_len(want, got) / len(want):.1f}%")

        # Per paragraph: is the head present *anywhere* in the span union?
        print("\n  per-paragraph: head present in ASR output at all?")
        for u in gt["utterances"]:
            p = norm(u["text"])
            head = p[:14]
            in_union = head in union
            in_rows = head in got
            print(f"    head {head}...  in_raw_spans={in_union!s:<5} in_final_rows={in_rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
