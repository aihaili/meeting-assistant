"""How usable are the retrieval scores as a confidence signal?

The multi-index run returned 1.0 for a relevant minutes file *and* 1.0 for an unrelated
regulation on a meeting question, which means the number cannot be shown as "相关度" with
a straight face and cannot drive a threshold.

This measures the actual spread: for each query, the score of the correct document versus
the best score among documents that have nothing to do with it. If a wrong document
routinely ties the right one, the score is a ranking device only, and the UI must convey
that rather than implying confidence.

Usage:
    python scripts/probe_score_spread.py
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rag.multi_index import MultiIndex  # noqa: E402

ROOT = HERE.parent

# (query, substring that identifies the right document)
CASES = [
    ("第三方测评人员什么时候进场", "20260907"),
    ("软件部署调通什么时候完成", "20260831"),
    ("VR 体验什么时候之前要完整", "20260831"),
    ("已发货的物资要准备哪些材料", "20260831"),
    ("铺线以什么为重点", "20260831"),
    ("审查讯问时我方人员不得少于几人", "开设规范"),
    ("心理危机干预的处置流程", "示例手册"),
    ("AR 展训平台的交互设计", "PRD"),
]


def main() -> int:
    mi = MultiIndex([
        {"name": "公共", "db": str(ROOT / "data" / "ar.db"), "kb": r"<公共资料库>"},
        {"name": "项目", "db": str(ROOT / "data" / "meet.db"),
         "kb": str(ROOT / "data" / "synth-corpus")},
    ])

    print("=" * 84)
    print("检索分数的区分度：正确文档 vs 无关文档")
    print("=" * 84)
    print(f"{'问题':<28}{'正确':>9}{'无关最高':>10}{'差':>8}{'正确排名':>9}  结论")

    tied = 0
    correct_top1 = 0
    gaps = []
    for q, expect in CASES:
        hits = mi.search(q, top_k=8)
        right = next((h["score"] for h in hits if expect in (h["title"] or "")), None)
        wrong = [h["score"] for h in hits if expect not in (h["title"] or "")]
        rank = next((i for i, h in enumerate(hits, 1) if expect in (h["title"] or "")), None)
        wmax = max(wrong) if wrong else 0.0
        gap = (right - wmax) if right is not None else float("nan")
        if right is not None and abs(gap) < 1e-6:
            tied += 1
        if rank == 1:
            correct_top1 += 1
        if right is not None:
            gaps.append(gap)
        verdict = ("并列，分数无法区分" if right is not None and abs(gap) < 1e-6
                   else ("正确领先" if right is not None and gap > 0 else "错误文档更高"))
        print(f"{q:<28}{(right if right is not None else float('nan')):>9.4f}"
              f"{wmax:>10.4f}{gap:>8.4f}{str(rank):>9}  {verdict}")

    print()
    print(f"  正确文档排第一: {correct_top1}/{len(CASES)}")
    print(f"  与无关文档并列: {tied}/{len(CASES)}")
    if gaps:
        print(f"  领先幅度: 中位 {statistics.median(gaps):+.4f}  "
              f"最小 {min(gaps):+.4f}  最大 {max(gaps):+.4f}")

    print()
    print("=" * 84)
    if tied >= len(CASES) / 2:
        print("结论：分数几乎没有区分度，不能作为「相关度」展示，也不能用作阈值。")
        print("      正确文档能排第一，靠的是并列时的排序，不是分数本身。")
        print("      界面应显示「命中来源」而不是一个看起来像置信度的数字。")
    elif correct_top1 == len(CASES):
        print("结论：正确文档稳定排第一，但分数间距很小，只能当排序用。")
    else:
        print("结论：有题目正确答案未排第一，需要看具体哪几道。")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    sys.exit(main())
