"""Check that prep topics which cannot be deduplicated are rejected rather than merged.

Found through a real failure: a client sending mangled UTF-8 produced four distinct
notes that all normalised to the empty string, so the dedup key matched and all four
merged into one. The fix rejects a topic whose normalised key is too short to compare.
This script pins that behaviour down, including the boundary cases (punctuation-only,
single character, Latin punctuation).

Written as a file because passing this through a PowerShell here-string mangles the
Chinese punctuation before Python ever sees it -- which is, ironically, the same class
of encoding problem that caused the bug.

Usage:
    python scripts/test_prep_topics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meeting.session import MeetingSession, _norm  # noqa: E402

NORM_CASES = [
    ("测试要点", "测试要点", "正常中文"),
    ("，。、", "", "纯中文标点 -> 空"),
    ("好", "好", "单字 -> 归一化后 1 字"),
    ("   ", "", "纯空格 -> 空"),
    ("??", "", "纯英文标点 -> 空"),
    ("a！", "a", "单字母 + 标点 -> 1 字"),
    ("铺线，以机房为重点。", "铺线以机房为重点", "标点被剥离"),
]

# (topic, should_be_accepted)
ADD_CASES = [
    ("测试要点", True),
    ("，。、", False),
    ("好", False),
    ("   ", False),
    ("a！", False),
    ("铺线以机房为重点", True),
]


def main() -> int:
    fails = 0

    print("归一化：")
    for raw, expect, note in NORM_CASES:
        got = _norm(raw)
        ok = got == expect
        if not ok:
            fails += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {raw!r:14} -> {got!r:14} {note}")

    print("\nadd_prep 接受/拒绝：")
    s = MeetingSession()
    accepted = 0
    for topic, should in ADD_CASES:
        r = s.add_prep(topic)
        got = r is not None
        ok = got == should
        if not ok:
            fails += 1
        if got:
            accepted += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {topic!r:14} -> "
              f"{'接受' if got else '拒绝'}  (期望 {'接受' if should else '拒绝'})")

    n = len(s.list_plan("prep"))
    ok = n == accepted
    if not ok:
        fails += 1
    print(f"\n  {'PASS' if ok else 'FAIL'}  便签数 {n} == 被接受数 {accepted}")

    # The merged-into-one failure mode: several unusable topics must not collapse into
    # a single note, because that is what hid the original bug.
    s2 = MeetingSession()
    for t in ("，。、", "??", "   ", "！？"):
        s2.add_prep(t)
    empties = len(s2.list_plan("prep"))
    ok = empties == 0
    if not ok:
        fails += 1
    print(f"  {'PASS' if ok else 'FAIL'}  4 个不可用主题产生 {empties} 张便签（期望 0）")

    print(f"\n{'全部通过' if fails == 0 else f'{fails} 处失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
