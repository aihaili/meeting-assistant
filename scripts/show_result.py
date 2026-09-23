"""Show a stress_long.py result file: rows, and exactly which source chars are missing."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STRIP = re.compile(r"[\s，。、；：？！,.;:?!\"'“”‘’()（）\[\]【】—…·-]+")


def norm(s: str) -> str:
    return STRIP.sub("", s or "")


def lcs_table(a: str, b: str):
    n, m = len(a), len(b)
    prev = [0] * (m + 1)
    rows = []
    for ca in a:
        cur = [0] * (m + 1)
        for j, cb in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1])
        rows.append(cur)
        prev = cur
    return rows


def mark_missing(want: str, got: str) -> str:
    """Render `want` with chars that LCS could not match in [brackets]."""
    rows = lcs_table(want, got)
    keep = [False] * len(want)
    i, j = len(want), len(got)
    while i > 0:
        cur = rows[i - 1]
        if j > 0 and want[i - 1] == got[j - 1] and cur[j] == cur[j - 1] + 1:
            keep[i - 1] = True
            i -= 1
            j -= 1
        elif j > 0 and cur[j] == cur[j - 1]:
            j -= 1
        else:
            i -= 1
    out = []
    for ch, k in zip(want, keep):
        out.append(ch if k else f"[{ch}]")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("result")
    ap.add_argument("--gt", default="data/long-meeting/ground-truth.json")
    args = ap.parse_args()

    data = json.loads(Path(args.result).read_text(encoding="utf-8"))
    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    want = norm(gt["full_text"])

    for run in data:
        print("=" * 100)
        print(f"window={run['window_s']}  retention={run['retention_pct']}%  rows={run['rows']} "
              f"frag<3s={run['fragmented_rows_under_3s']} longest={run['longest_row_s']}s "
              f"rev={run['revisions']} inflation={run['inflation']}")
        print("=" * 100)
        got = ""
        for r in run.get("rows_detail", []):
            d = r["end"] - r["start"]
            print(f"  [{r['start']:>7.2f}-{r['end']:>7.2f}] {d:>6.2f}s rev={r['revisions']:<2} {r['text']}")
            got += norm(r["text"])
        print()
        print("  source with DROPPED characters in [brackets]:")
        marked = mark_missing(want, got)
        for i in range(0, len(marked), 100):
            print("   ", marked[i:i + 100])
    return 0


if __name__ == "__main__":
    sys.exit(main())
