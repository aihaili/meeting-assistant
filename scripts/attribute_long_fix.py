"""Attribute the remaining long-speech fragmentation to the right fix.

Two independent changes are currently in ``streaming.py`` and both target the same
symptom, so the obvious question is which one earns the improvement:

* the **gap thresholds** (``_GAP_BARE_S`` 0.30 -> 0.45, ``_GAP_CLAUSE_S`` 0.75 -> 0.85),
  derived from ``probe_vad_cuts.py`` measuring the real inter-sentence gap distribution;
* the **coverage mask** (``coverage_mask``), which clips a re-heard span against the
  intervals already recorded so the same speech cannot be folded in twice.

The mask was added by a parallel investigation that was stopped mid-run, and both
changes were already in the file when the last stress number (94.1% retention, 11 short
rows) was measured -- so that number credits neither. This script rebuilds a
thresholds-only variant from the backup and runs both, so the result is attributable.

It rewrites the module in place between runs and restores the real file at the end,
including on failure. ``streaming.py.orig`` is kept as the last-resort copy.

Usage:
    python scripts/attribute_long_fix.py            # both variants, ~5 min
    python scripts/attribute_long_fix.py --speed 4  # faster, latency not meaningful
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

STREAMING = HERE / "phone_mic" / "streaming.py"
ORIG = HERE / "phone_mic" / "streaming.py.orig"
PY = sys.executable
ROOT = HERE.parent


def strip_mask(src: str) -> str:
    """Remove the coverage mask from a copy of streaming.py.

    Deleting the *call* is not enough -- the mask also trims spans at call time and
    gates ``update_row``. The variant has to behave as if the feature never existed, so
    the function is replaced by one that returns an empty interval list, which makes
    every span fully uncovered and reproduces the pre-mask behaviour exactly.
    """
    marker = "def coverage_mask(sentences"
    i = src.find(marker)
    if i < 0:
        raise SystemExit("coverage_mask not found -- has the file changed shape?")
    # Find the end of the function: the next top-level def/class after it.
    m = re.search(r"\n(?=(?:def |class |@))", src[i:])
    end = i + (m.start() + 1 if m else len(src) - i)
    stub = ('def coverage_mask(sentences, upto):\n'
            '    """Disabled by attribute_long_fix.py: thresholds-only variant."""\n'
            '    return []\n\n\n')
    return src[:i] + stub + src[end:]


def run(label: str, speed: float, windows: str) -> dict:
    out = ROOT / "data" / "stress" / f"attrib-{label}.json"
    cmd = [PY, str(HERE / "stress_long.py"), "--windows", windows,
           "--speed", str(speed), "--save", str(out)]
    print(f"\n{'=' * 74}\n▶ {label}\n{'=' * 74}", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(HERE))
    # The stress script prints a summary table; surface the lines that carry numbers.
    for line in (r.stdout or "").splitlines():
        if re.search(r"保留率|膨胀比|行数|最长行|修订总次数|^ *\d+ +[\d.]+%", line):
            print("   " + line.strip())
    if r.returncode != 0 and not out.exists():
        print(f"   [失败] exit {r.returncode}: {(r.stderr or '')[-300:]}")
    if out.exists():
        data = json.loads(out.read_text(encoding="utf-8"))
        return {"label": label, "runs": data}
    return {"label": label, "runs": []}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--windows", default="20")
    args = ap.parse_args()

    original = STREAMING.read_text(encoding="utf-8")
    shutil.copyfile(STREAMING, ORIG)
    print(f"已备份原文件到 {ORIG.name}")
    results = []
    try:
        # Variant A: the file as it stands (thresholds + coverage mask).
        results.append(run("with-mask", args.speed, args.windows))

        # Variant B: same file minus the mask.
        STREAMING.write_text(strip_mask(original), encoding="utf-8")
        print("\n已停用 coverage_mask，重跑…")
        results.append(run("no-mask", args.speed, args.windows))
    finally:
        STREAMING.write_text(original, encoding="utf-8")
        print(f"\n已恢复原文件（同时留了一份 {ORIG.name}）")

    print("\n" + "=" * 74)
    print("归因对比")
    print("=" * 74)
    print(f"{'变体':<12}{'窗口':>6}{'保留率':>9}{'膨胀':>8}{'行数':>7}{'碎行':>7}{'最长行':>9}")
    for res in results:
        for r in res["runs"]:
            print(f"{res['label']:<12}{r['window_s']:>6.0f}{r['retention_pct']:>8.1f}%"
                  f"{r['inflation']:>7.2f}x{r['rows']:>7}"
                  f"{r['fragmented_rows_under_3s']:>7}{r['longest_row_s']:>8.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
