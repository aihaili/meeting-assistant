"""Evaluate keyword-extraction quality against keyword_eval.json.

The whole point of this eval is the "少而精" (few, sharp) goal, so the metrics
are split into two families:

  quality   precision  fraction of extracted keywords that match a gold hotspot
            recall     fraction of gold hotspots captured by the extraction
            f1         harmonic mean of the two

  economy   count      how many keywords were extracted per segment (want it LOW)
            noise      extracted keywords that match no gold hotspot
                       (this is the "到处是关键词" failure mode)

A good extraction is high-precision, high-recall AND low-count / low-noise.
Matching is lenient: an extracted keyword "hits" a gold term when one contains
the other after whitespace is stripped (so "VR体验" matches "VR 体验", and
"软件部署" matches "软件部署调通").

Usage:
    python asst/eval_keywords.py            # LLM path, 1 run
    python asst/eval_keywords.py --runs 3   # average over 3 stochastic runs
    python asst/eval_keywords.py --no-llm   # jieba fallback only (no LLM)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PARENT = _HERE.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from asst.keywords import extract_keywords  # noqa: E402

EVAL = _HERE / "keyword_eval.json"


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _hit(extracted: str, gold: str) -> bool:
    """Lenient match: one contains the other (whitespace-stripped)."""
    e, g = _norm(extracted), _norm(gold)
    return bool(e) and (e == g or e in g or g in e)


def _score_segment(segment: str, gold: list[str], kws: list[str]) -> dict:
    hits = [k for k in kws if any(_hit(k, g) for g in gold)]
    noise = len(kws) - len(hits)
    precision = len(hits) / len(kws) if kws else 0.0
    gold_hit = [g for g in gold if any(_hit(k, g) for k in kws)]
    recall = len(gold_hit) / len(gold) if gold else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "count": len(kws), "noise": noise,
        "precision": precision, "recall": recall, "f1": f1,
        "kws": kws, "hits": hits, "gold_hit": gold_hit,
    }


def evaluate(items: list[dict], unused_llm: bool, max_keywords: int,
             runs: int, verbose: bool = True) -> tuple[dict, list[dict]]:
    sums = {"precision": 0.0, "recall": 0.0, "f1": 0.0, "count": 0.0, "noise": 0.0}
    per_seg: list[dict] = []
    for r in range(runs):
        for it in items:
            seg, gold = it["segment"], it["gold"]
            kws, ms = extract_keywords(seg, unused_llm=unused_llm,
                                      max_keywords=max_keywords)
            s = _score_segment(seg, gold, kws)
            for k in sums:
                sums[k] += s[k]
            if r == 0:  # keep a per-segment view from the first run
                s["id"], s["segment"], s["gold"], s["ms"] = it["id"], seg, gold, ms
                per_seg.append(s)
    denom = runs * len(items)
    agg = {k: sums[k] / denom for k in sums}
    return agg, per_seg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=1,
                    help="stochastic runs to average over (default 1)")
    ap.add_argument("--no-llm", action="store_true",
                    help="use the jieba fallback only (no LLM call)")
    ap.add_argument("--max-keywords", type=int, default=6)
    args = ap.parse_args()

    data = json.loads(EVAL.read_text(encoding="utf-8"))
    items = data["items"]
    mode = "jieba" if args.no_llm else "LLM"
    t0 = time.time()
    agg, per_seg = evaluate(items, unused_llm=args.no_llm,
                           max_keywords=args.max_keywords, runs=args.runs)
    dt = time.time() - t0

    print(f"\n=== keyword eval: {len(items)} segs, {args.runs} run(s), "
          f"{mode}, max_kw={args.max_keywords}  ({dt:.1f}s) ===")
    print(f"{'id':>3} {'n':>2} {'noise':>5} {'P':>5} {'R':>5} {'F1':>5}  "
          f"extracted  (gold)")
    for s in per_seg:
        ext = ", ".join(s["kws"]) or "—"
        print(f"{s['id']:>3} {s['count']:>2} {s['noise']:>5} "
              f"{s['precision']:>5.2f} {s['recall']:>5.2f} {s['f1']:>5.2f}  "
              f"{ext}   (gold: {', '.join(s['gold'])})")
    print(f"\n  mean  precision {agg['precision']:.3f}   recall {agg['recall']:.3f}"
          f"   F1 {agg['f1']:.3f}")
    print(f"  mean  count   {agg['count']:.2f}     noise {agg['noise']:.2f}"
          f"   (少而精 = count 低 + noise 低 + precision 高, recall 不塌)")


if __name__ == "__main__":
    main()
