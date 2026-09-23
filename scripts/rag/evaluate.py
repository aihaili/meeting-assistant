"""Retrieval evaluation on a real domain corpus (D:\\公共资料库, 25 md / 590 chunks).

Ground truth is defined as a *distinctive phrase* that must appear inside a correct
chunk, so a hit is objectively checkable rather than judged by eye.

Two query styles are measured, because the real-time assistant sees both:
  * ``nl``   — a full natural-language question
  * ``asr``  — a short colloquial fragment, the shape an ASR segment produces after
               keyword extraction (this is the mode the product actually uses)

Metrics: hit@1 / hit@3 per mode (bm25 / vector / hybrid).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, r"E:\markdown\meeting-assistant\scripts")

from rag.rag_core import RagIndex  # noqa: E402

DB = r"E:\markdown\meeting-assistant\data\ar.db"
KB = r"<公共资料库>"

# (query, style, ground-truth phrase that must appear in a correct chunk)
CASES: list[tuple[str, str, str]] = [
    # ---- 管理手册：身份甄别 ----
    ("俘虏身份甄别的基本流程是什么", "nl", "甄别教育"),
    # NOTE: ground truth must survive OCR whitespace. The source has
    # "不得少于 \t2 \t人" (tabs injected around digits), so "不得少于2人" never
    # matches as a substring. Anchor on the stable part only.
    ("审查讯问时我方人员不得少于几人", "nl", "不得少于"),
    ("身份甄别审核通常采取哪几种方法", "nl", "利用相关俘虏指认"),
    # ---- 管理手册：转送移交 ----
    ("重点俘虏转送可以使用什么工具", "nl", "武装直升机"),
    ("转送移交前第一件事是做什么", "nl", "制定转送计划"),
    ("俘虏转送的行军路线要注意什么", "nl", "绝对保密"),
    # ---- 管理手册：生活保障 ----
    ("俘虏生活保障包括哪几个方面", "nl", "小卖部"),
    ("患传染病的俘虏应该怎么处理", "nl", "隔离治疗"),
    ("俘虏的伙食和宗教活动怎么安排", "nl", "宗教信仰"),
    # ---- 管理手册：应对探访 ----
    ("俘虏管理所接受媒体采访需要谁批准", "nl", "军委联指批准"),
    ("俘虏能不能用电话跟亲属联系", "nl", "经战区联指批准"),
    # ---- 管理手册：教育感化 ----
    ("教育感化主要有哪几种方法", "nl", "宣教灌输"),
    # ---- 示例手册 ----
    ("宽待俘虏政策的内容是什么", "nl", "宽待俘虏政策"),
    ("俘虏是怎么界定的", "nl", "俘虏的界定"),
    # ---- 开设规范 ----
    ("俘虏管理机构有哪几种类型", "nl", "俘虏收容点"),
    # ---- 技术规格书（结构良好的原生 md）----
    ("这个系统的3D引擎用什么", "nl", "Three.js"),
    ("原型阶段用什么设备交互", "nl", "PICO手柄"),
    ("D区路径怎么展示犯人转移路线", "nl", "A→B→C"),
    # ---- 数字沙盘方案 ----
    ("示例项目资料的设计方案讲了什么", "nl", "沙盘"),
    # ---- ASR 风格口语片段（实时场景的真实输入形态）----
    ("身份甄别 流程 报一下", "asr", "甄别教育"),
    ("审查讯问 两个人 规定", "asr", "不得少于"),
    ("重点俘虏 怎么转送", "asr", "武装直升机"),
    ("传染病 俘虏 处理", "asr", "隔离治疗"),
    ("采访 探视 批准", "asr", "军委联指批准"),
    ("俘虏 打电话 亲属", "asr", "战区联指批准"),
    ("教育感化 方法", "asr", "宣教灌输"),
    ("3D引擎 Three.js Unity", "asr", "Three.js"),
    ("犯人转移路线 展示", "asr", "A→B→C"),
    ("生活保障 小卖部 伙食", "asr", "小卖部"),
]


def main() -> int:
    idx = RagIndex(db_path=DB, kb_dir=KB)
    print(f"corpus: {idx.stats()['chunks']} chunks, backend={idx.stats()['backend']}")
    print()

    modes = ("bm25", "vector", "hybrid")
    agg = {(s, m): [0, 0, 0] for s in ("nl", "asr") for m in modes}  # hit1, hit3, n

    print(f"{'query':38} {'style':5} " + "".join(f"{m:>16}" for m in modes))
    print("-" * 104)
    for q, style, needle in CASES:
        want = {r["id"] for r in idx.conn.execute(
            "select id from chunks where text like ?", (f"%{needle}%",))}
        cells = []
        for m in modes:
            hits = idx.search(q, top_k=3, mode=m)
            ids_by_path = {}
            for h in hits:
                ids_by_path[h["path"]] = {r["id"] for r in idx.conn.execute(
                    "select id from chunks where path=?", (h["path"],))}
            rank = 0
            for i, h in enumerate(hits, 1):
                if ids_by_path[h["path"]] & want:
                    rank = i
                    break
            a = agg[(style, m)]
            a[2] += 1
            a[0] += int(rank == 1)
            a[1] += int(rank >= 1)
            cells.append("  -" if rank == 0 else f"  @{rank}")
        print(f"{q:38} {style:5} " + "".join(f"{c:>16}" for c in cells))

    print("-" * 104)
    for style in ("nl", "asr"):
        for m in modes:
            h1, h3, n = agg[(style, m)]
            print(f"  {style:4} {m:8} hit@1 {h1:2}/{n:2} ({100*h1/n:3.0f}%)   "
                  f"hit@3 {h3:2}/{n:2} ({100*h3/n:3.0f}%)")
        print()
    idx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
