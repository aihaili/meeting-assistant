"""Analyse a probe_long_trace.py dump: is the head lost by the ring buffer, by the
match anchor, or by _MIN_COVERAGE?

Reports, per tick: buffer clip state, and for each span the match decision.
Also answers the four questions in the task with numbers.
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
    ap.add_argument("--flow", type=float, nargs=2, default=None,
                    metavar=("T0", "T1"),
                    help="print chronological span/update flow inside this audio window")
    args = ap.parse_args()

    data = json.loads(Path(args.trace).read_text(encoding="utf-8"))
    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    want = norm(gt["full_text"])

    for run in data:
        w = run["window_s"]

        # ── chronological flow for one stretch of audio ───────────────────
        if args.flow:
            lo, hi = args.flow
            print("=" * 100)
            print(f"window={w}s  CHRONOLOGICAL FLOW in [{lo}, {hi}]s (spans + updates)")
            print("=" * 100)
            for r in run["trace"]:
                if r.get("kind") == "span":
                    if not (lo <= r["start"] <= hi or lo <= r["end"] <= hi):
                        continue
                    print(f"  t{r['tick']:>3} SPAN [{r['start']:>6.2f}-{r['end']:>6.2f}]"
                          f" {r['span_dur']:>5.2f}s -> {r.get('branch', '?'):<18}"
                          f" row{r.get('row_idx')}  {r['text']}")
                    for c in r.get("candidates", [])[:2]:
                        print(f"        cand row{c['idx']} [{c['start']}-{c['end']}]"
                              f" score={c['score']} {c['s_key_head']}")
                else:
                    print(f"  t{r['tick']:>3} UPD  row{r['row_idx']:>3} {r['branch']:<18}"
                          f" gap={r['start_gap']:>5.2f} cov={r['coverage']:>5.2f}"
                          f" | {r['text_before'][:22]} => {r['text_after'][:22]}")
            return 0

        print("=" * 100)
        print(f"window={w}s  ticks={len(run['ticks'])}  rows={len(run['rows'])}")
        print("=" * 100)

        # ── Q3: is the ring buffer clipped, and where does a span start? ──
        print("\n-- ring buffer per tick (is the window full / sliding?) --")
        for t in run["ticks"]:
            if t["n_spans"] == 0:
                continue
            print(f"  tick{t['i']:>3} total={t['total']:>6.2f} buf_start={t['buf_start']:>6.2f} "
                  f"buf_len={t['buf_s']:>5.2f} spans={t['n_spans']:>2} rows={t['n_rows']:>3}")

        # ── branch histogram ─────────────────────────────────────────────
        print("\n-- fold branches --")
        hist: dict[str, int] = {}
        for rec in run["trace"]:
            if rec.get("kind") == "span":
                hist[rec.get("branch", "?")] = hist.get(rec.get("branch", "?"), 0) + 1
        for k, v in sorted(hist.items(), key=lambda kv: -kv[1]):
            print(f"  {v:>4}  {k}")

        # ── Q1: anchor drift. For spans that matched a row, how big was start_gap?
        print("\n-- Q1: start_gap distribution for REPLACE/APPEND (anchor rule) --")
        gaps = [r["start_gap"] for r in run["trace"]
                if r.get("kind") == "update" and r.get("branch") in
                ("replace", "append_same_anchor")]
        if gaps:
            gaps_sorted = sorted(gaps)
            n = len(gaps_sorted)
            print(f"  n={n} min={gaps_sorted[0]:.2f} p50={gaps_sorted[n // 2]:.2f} "
                  f"p90={gaps_sorted[int(n * 0.9)]:.2f} max={gaps_sorted[-1]:.2f}")
            over = sum(1 for g in gaps if g > 0.35)
            print(f"  over _ANCHOR_S=0.35: {over}  ({100 * over / n:.0f}%)")
        print("  ALL update decisions (span_start - row_start):")
        for r in run["trace"]:
            if r.get("kind") == "update":
                print(f"    row{r['row_idx']:>3} {r['branch']:<18} gap={r['start_gap']:>6.2f} "
                      f"end_gap={r['end_gap']:>6.2f} cov={r['coverage']:>5.2f} "
                      f"| {r['text_before'][:16]} -> {r['text_after'][:16]}")

        # ── Q2: coverage of the spans that were ignored ───────────────────
        print("\n-- Q2: spans whose fold was 'ignore' (coverage / anchor) --")
        for r in run["trace"]:
            if r.get("kind") == "update" and r["branch"] == "ignore":
                print(f"    row{r['row_idx']:>3} gap={r['start_gap']:>6.2f} "
                      f"end_gap={r['end_gap']:>6.2f} cov={r['coverage']:>5.2f} "
                      f"| row: {r['text_before'][:20]}")

        # ── Q4: how far back did a span need to look? ────────────────────
        print("\n-- Q4: candidate pool size per span (rows within _CANDIDATE_BACK_S) --")
        pool = [(r["n_candidates"], r["scored_nonzero"])
                for r in run["trace"] if r.get("kind") == "span" and "n_candidates" in r]
        if pool:
            print(f"  spans scored: {len(pool)}  max candidates seen: {max(p[0] for p in pool)}"
                  f"  max with nonzero score: {max(p[1] for p in pool)}")
            # How often was the *best* candidate not the last row?
            for r in run["trace"]:
                cands = r.get("candidates") or []
                if cands and cands[0]["score"] > 0:
                    pass

        # ── chronological flow for one stretch of audio ───────────────────
        if args.flow:
            lo, hi = args.flow
            print(f"\n-- chronological flow in [{lo}, {hi}]s (spans + updates) --")
            for r in run["trace"]:
                if r.get("kind") == "span":
                    if not (lo <= r["start"] <= hi or lo <= r["end"] <= hi):
                        continue
                    print(f"  t{r['tick']:>3} SPAN [{r['start']:>6.2f}-{r['end']:>6.2f}]"
                          f" {r['span_dur']:>5.2f}s -> {r.get('branch', '?'):<18}"
                          f" row{r.get('row_idx')}  {r['text']}")
                    for c in r.get("candidates", [])[:2]:
                        print(f"        cand row{c['idx']} [{c['start']}-{c['end']}]"
                              f" score={c['score']} {c['s_key_head']}")
                else:
                    print(f"  t{r['tick']:>3} UPD  row{r['row_idx']:>3} {r['branch']:<18}"
                          f" gap={r['start_gap']:>5.2f} cov={r['coverage']:>5.2f}"
                          f" | {r['text_before'][:22]} => {r['text_after'][:22]}")
            return 0

        # ── final rows and per-paragraph retention vs the source span ─────
        print("\n-- final rows --")
        got = ""
        for row in run["rows"]:
            d = row["end"] - row["start"]
            print(f"  [{row['start']:>6.2f}-{row['end']:>6.2f}] {d:>5.2f}s "
                  f"rev={row['revisions']:<2} {row['text']}")
            got += norm(row["text"])
        print(f"\n  retention (LCS) = {100 * lcs_len(want, got) / len(want):.1f}%")
        print("\n  per-paragraph retention:")
        for u in gt["utterances"]:
            p = norm(u["text"])
            print(f"    {100 * lcs_len(p, got) / len(p):>5.1f}%  {p[:16]}")

        # What fraction of the source text is missing, and where?
        print("\n  -- which 20-char windows of the source were dropped --")
        missing = []
        step = 10
        for i in range(0, len(want) - step, step):
            chunk = want[i:i + step]
            if lcs_len(chunk, got) < step * 0.6:
                missing.append((i, chunk))
        for i, chunk in missing:
            print(f"    char {i:>4}: ...{want[max(0, i - 8):i]}[{chunk}]{want[i + step:i + step + 8]}...")

    return 0


if __name__ == "__main__":
    sys.exit(main())
