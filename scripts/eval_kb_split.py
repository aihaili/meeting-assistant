"""Measure whether meeting minutes in the same index actually degrade retrieval.

The question is whether a project-based meeting assistant should keep *one* knowledge
base or *two* (public documents: contracts, bids, certificates; project documents:
minutes, progress) -- and whether adding the constantly-growing meeting record to the
stable public corpus makes retrieval worse.

That is a measurable claim, so it is measured rather than argued. Two indexes are built
from the same corpus and the same queries are run against both:

* **mixed** -- public documents *and* meeting minutes in one index
* **public-only** -- just the stable documents

and separately, the meeting queries are run against a **minutes-only** index to model the
two-index arrangement the user described.

What would count as evidence for splitting:
* mixed retrieval losing the right public document that public-only finds (dilution by
  newly added chunks), or
* a two-index search returning noticeably better answers than one mixed index.

What would count as evidence against:
* mixed retrieval matching public-only on every query -- because then the separation buys
  nothing for *retrieval*, whatever it may buy for lifecycle management.

Usage:
    python scripts/eval_kb_split.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rag.rag_core import RagIndex  # noqa: E402

ROOT = HERE.parent

# Queries that must be answered from the *public* corpus. Taken from the real project
# domain (regulations, manuals, the PRD) so a wrong answer is a real wrong answer.
PUBLIC_QUERIES = [
    ("审查讯问时我方人员不得少于几人", "开设规范"),
    ("讯问过程应当录音录像", "开设规范"),
    ("被监管人员入所时应当进行哪些检查", "管理手册"),
    ("心理危机干预的处置流程", "示例手册"),
    ("AR 展训平台的交互设计", "PRD"),
    ("示例项目资料的实施方案", "示例项目资料设计实施方案"),
]

# Queries that must be answered from the *meeting* corpus.
MINUTES_QUERIES = [
    ("第三方测评人员什么时候进场", "项目会议纪要（20260907）"),
    ("软件部署调通什么时候完成", "项目会议纪要（20260831）"),
    ("VR 体验什么时候之前要完整", "项目会议纪要（20260831）"),
    ("已发货的物资要准备哪些材料", "项目会议纪要（20260831）"),
]


def hits(idx: RagIndex, q: str, k: int = 3) -> list[str]:
    return [h["title"] for h in idx.search(q, top_k=k)]


def first_hit(idx: RagIndex, q: str) -> str:
    r = idx.search(q, top_k=1)
    return r[0]["title"] if r else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public-db", default=str(ROOT / "data" / "ar.db"))
    ap.add_argument("--public-kb", default=r"<公共资料库>")
    ap.add_argument("--minutes-db", default=str(ROOT / "data" / "meet.db"))
    ap.add_argument("--minutes-kb", default=str(ROOT / "data" / "synth-corpus"))
    args = ap.parse_args()

    pub = RagIndex(db_path=args.public_db, kb_dir=args.public_kb)
    mins = RagIndex(db_path=args.minutes_db, kb_dir=args.minutes_kb)
    print(f"公共库 {pub.stats()['files']} 文件 / {pub.stats()['chunks']} chunks")
    print(f"会议库 {mins.stats()['files']} 文件 / {mins.stats()['chunks']} chunks")
    print()

    # ── 1. does the split lose retrieval quality on public queries? ─────
    print("=" * 78)
    print("① 公共类问题：分库检索 vs 只查公共库")
    print("=" * 78)
    print(f"{'问题':<26}{'期望':<14}{'分库命中':<14}{'结果'}")
    ok_split = 0
    for q, expect in PUBLIC_QUERIES:
        # In the two-index arrangement a public question is answered from the public
        # index, so this is literally what the split gives.
        top = first_hit(pub, q)
        good = expect in top
        ok_split += good
        print(f"{q:<26}{expect:<14}{top[:12]:<14}{'✓' if good else '✗ 落到了 ' + top}")
    print(f"\n  公共问题命中 {ok_split}/{len(PUBLIC_QUERIES)}")

    # ── 2. does the mixed index answer the same? ────────────────────────
    # A mixed index is simulated by searching both and merging by score, which is what a
    # single index containing both corpora would rank. If the public answer survives
    # having minutes in the same index, separation buys nothing for retrieval quality.
    print("\n" + "=" * 78)
    print("② 公共类问题：混库（公共 + 会议）检索")
    print("=" * 78)
    print(f"{'问题':<26}{'期望':<14}{'混库前3':<40}{'结果'}")
    ok_mixed = 0
    for q, expect in PUBLIC_QUERIES:
        merged = []
        for h in pub.search(q, top_k=3):
            merged.append((h["score"], h["title"]))
        for h in mins.search(q, top_k=3):
            merged.append((h["score"], h["title"]))
        merged.sort(key=lambda x: -x[0])
        titles = [t for _, t in merged[:3]]
        good = expect in titles[0]
        ok_mixed += good
        print(f"{q:<26}{expect:<14}{' | '.join(t[:11] for t in titles):<40}"
              f"{'✓' if good else '✗'}")
    print(f"\n  混库命中 {ok_mixed}/{len(PUBLIC_QUERIES)}")

    # ── 3. would a public query ever be answered by minutes? ────────────
    print("\n" + "=" * 78)
    print("③ 会议类问题：如果只查公共库（即会议内容去别的库找会怎样）")
    print("=" * 78)
    print(f"{'问题':<26}{'期望(会议)':<22}{'只查公共库得到'}")
    for q, expect in MINUTES_QUERIES:
        top = first_hit(pub, q)
        print(f"{q:<26}{expect:<22}{top}")

    # ── 4. cross-contamination check ───────────────────────────────────
    print("\n" + "=" * 78)
    print("④ 交叉污染：会议类问题在公共库里的得分")
    print("=" * 78)
    for q, _ in MINUTES_QUERIES:
        r = pub.search(q, top_k=1)
        s = r[0]["score"] if r else 0
        print(f"  {q:<26} 公共库最高分 {s}")

    print("\n" + "=" * 78)
    verdict = ("分库与混库在检索质量上无差别" if ok_split == ok_mixed else
               f"有差别：分库 {ok_split} vs 混库 {ok_mixed}")
    print(f"结论（检索质量）：{verdict}")
    print("=" * 78)

    out = ROOT / "data" / "kb-split-eval.json"
    out.write_text(json.dumps({
        "public_queries": len(PUBLIC_QUERIES),
        "split_hits": ok_split,
        "mixed_hits": ok_mixed,
        "verdict": verdict,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
