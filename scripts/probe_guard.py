"""Ad-hoc probe for the guard-band / open-flag rule.

Written as a file on purpose: passing this body through a PowerShell double-quoted
here-string silently interpolates ``$sents``, ``$g`` and friends before Python ever
sees them, which makes the probe report on corrupted code. That produced four
"failures" in the selftest assertions that were really failures of the harness.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from phone_mic.streaming import fold_span  # noqa: E402

CASES = [
    (21.5, 1.2, 18.0, 21.0, "距边缘 0.5s < guard 1.2s -> 保护带内 -> open"),
    (25.0, 1.2, 18.0, 21.0, "距边缘 4.0s > guard     -> 已定稿   -> not open"),
    (46.5, 1.2, 39.24, 45.0, "距边缘 1.5s > guard     -> 已定稿   -> not open"),
]

for window_end, guard, start, end, note in CASES:
    sents: list = []
    fold_span(sents, 0, {"text": "这是一段足够长的测试文本用于判定", "start": start, "end": end},
              offset=0.0, now=1.0, window_end=window_end, guard_s=guard)
    row = sents[0]
    expect_open = end > window_end - guard
    verdict = "OK " if row.open == expect_open else "BAD"
    print(f"{verdict} window_end={window_end:5.1f} guard={guard} end={end:5.2f} "
          f"-> open={str(row.open):5} (期望 {expect_open})  {note}")
