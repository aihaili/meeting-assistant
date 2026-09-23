r"""Cross-meeting evaluation on the synthetic corpus -- the test my earlier attempt botched.

Corpus: 7 meetings (1 real + 6 clearly-marked synthetic), each with DISTINCT facts
(17 defects / 3120 m cable / report YS-2026-1020 / ...). Uniqueness is what makes a
question have exactly one right answer; my first attempt reused the same nouns across
meetings and therefore measured nothing.

Metric: top-1 must be BOTH the right meeting AND the clause holding the answer.
Questions come in three flavours, because they stress retrieval differently:

  A. clause lookup      -- "9月5日前要完成什么"          (answer in one clause)
  B. cross-meeting      -- "未发货物资的到货时间改了几次"  (needs SEVERAL meetings)
  C. aggregation        -- "整个项目一共登记了多少项缺陷"  (needs a sum/traversal)

B and C are exactly the shapes a chunk-based index is expected to fail, and exactly
what a knowledge graph is supposed to fix -- so the result decides the KG question.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, r"E:\markdown\meeting-assistant\scripts")
from rag.rag_core import RagIndex  # noqa: E402

DB = r"E:\markdown\meeting-assistant\data\synth.db"
KB = r"E:\markdown\meeting-assistant\data\synth-corpus"


def meeting_of(rel: str) -> str:
    """20260907 -> 2026-09-07"""
    import re
    m = re.search(r"(\d{8})", rel)
    return f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}" if m else rel


idx = RagIndex(db_path=DB, kb_dir=KB)
print(f"corpus: {idx.stats()['chunks']} chunks / {idx.stats()['files']} files, "
      f"backend={idx.stats()['backend']}\n")

# ── A. clause lookup: one clause answers it, one meeting owns it ──────────────
A_CASES = [
    ("软件部署什么时候调通", "9月5日前软件部署调通", "2026-08-31"),
    ("未发货物资要准备什么材料", "采购计划", "2026-08-31"),
    ("第三方测评组几个人", "共4人", "2026-09-07"),
    ("登记了多少项缺陷", "17项缺陷", "2026-09-07"),
    ("缺陷整改什么时候完成", "9月12日前完成整改", "2026-09-07"),
    ("感应器什么时候发货", "9月18日发出", "2026-09-14"),
    ("沙盘第二次打印的尺寸", "1.8米", "2026-09-14"),
    ("甲方认可了什么", "认可VR体验整改结果", "2026-09-21"),
    ("验收大纲有几个一级条目", "5个一级条目", "2026-09-21"),
    ("联调联试连续运行多久", "48小时", "2026-09-28"),
    ("专家验收会什么时候", "10月20日举行", "2026-10-12"),
    ("验收报告编号是多少", "YS-2026-1020", "2026-10-20"),
    ("验收结论是什么", "验收结论为合格", "2026-10-20"),
    ("现场保障安排几个人", "5人现场保障", "2026-10-12"),
    ("验收材料要交几份光盘", "光盘介质2份", "2026-10-20"),
]

# ── B. cross-meeting: the answer spans several meetings ──────────────────────
B_CASES = [
    ("未发货物资的到货时间前后变过几次", ["2026-08-31", "2026-09-14", "2026-09-21"]),
    ("VR体验的完成时间要求变化过程", ["2026-08-31", "2026-09-07", "2026-09-21"]),
    ("感应器设备到货延迟影响了什么", ["2026-09-21"]),
    ("第三方测评从进场到复测通过的过程", ["2026-09-07", "2026-09-14"]),
    ("从铺线到联调联试的推进过程", ["2026-08-31", "2026-09-14", "2026-09-28"]),
    ("验收前的准备工作有哪些节点", ["2026-09-21", "2026-09-28", "2026-10-12"]),
]

# ── C. aggregation: needs a count/traversal across meetings ─────────────────
C_CASES = [
    ("整个项目一共涉及几次期限要求", None),
    ("甲方一共提出了多少项要求", None),
    ("史塔克一共做过几次承诺", None),
    ("一共有几个风险项被记录", None),
    ("这个项目从第一次会到验收共跨了多少天", None),
]


def top1(con, q, mode="hybrid"):
    hits = idx.search(q, top_k=3, mode=mode)
    return hits[0] if hits else None


print("=" * 96)
print("A. 单条款查询（一个条款就能答，一场会议拥有它）")
print("=" * 96)
print(f"{'问题':30} {'期望会议':12} {'top-1 会议':12} {'判定':6} {'延迟'}")
print("-" * 96)
a_ok = a_n = 0
for q, needle, want in A_CASES:
    t0 = time.time()
    h = top1(idx, q)
    dt = (time.time() - t0) * 1000
    got = meeting_of(h["rel"]) if h else "-"
    # correct requires right meeting AND the clause text present in the payload
    payload = " ".join(c.get("text", "") for c in (h.get("chunks") or [])) if h else ""
    hit = (got == want) and (needle in payload)
    a_ok += hit
    a_n += 1
    flag = "✓" if hit else ("会议错" if got != want else "条款错")
    print(f"{q:30} {want:12} {got:12} {flag:6} {dt:.0f}ms")
print(f"\n  A 合计: {a_ok}/{a_n} = {100*a_ok/a_n:.0f}%\n")

print("=" * 96)
print("B. 跨会议查询（答案分散在数场会议里 —— chunk 检索天生做不好的形态）")
print("=" * 96)
print(f"{'问题':36} {'涉及的会议':34} {'top-1 命中':10} {'覆盖'}")
print("-" * 96)
b_full = b_partial = b_none = 0
for q, want_dates in B_CASES:
    h = top1(idx, q)
    got = meeting_of(h["rel"]) if h else "-"
    # does the top-3 cover ALL the meetings involved?
    hits3 = idx.search(q, top_k=3)
    covered = {meeting_of(x["rel"]) for x in hits3}
    n_cov = len(covered & set(want_dates))
    if n_cov == len(want_dates):
        b_full += 1
    elif n_cov:
        b_partial += 1
    else:
        b_none += 1
    print(f"{q:36} {','.join(d[-5:] for d in want_dates):34} {got:10} "
          f"{n_cov}/{len(want_dates)}")
print(f"\n  B 全部覆盖: {b_full}/{len(B_CASES)}   部分覆盖: {b_partial}   "
      f"完全未覆盖: {b_none}")
print("  （top-1 只能给出一场会议；要完整回答必须把多场会议聚合起来）\n")

print("=" * 96)
print("C. 聚合查询（需要计数/遍历，而不是找相似段落）")
print("=" * 96)
from collections import Counter
for q, _ in C_CASES:
    h = top1(idx, q)
    got = meeting_of(h["rel"]) if h else "-"
    sn = (h["snippets"][0][:70] if h and h.get("snippets") else "")
    print(f"  {q:34} top-1={got:12} {sn}")
print("\n  这些查询的正确回答是一个统计量（次数/数量/跨度），")
print("  而 top-1 只能返回一个最相似的段落 —— 结构上无法回答。")

# ground truth counts, computed from the generated corpus metadata
print("\n  实际统计（从生成器的结构化数据算得）:")
import re
txt = " ".join(r[0] for r in idx.conn.execute("select text from chunks"))
tc = Counter(re.findall(r"\*\*\[(甲方要求|己方承诺|风险|进度)\]\*\*", txt))
for k, v in tc.most_common():
    print(f"    {k:8} {v}")

idx.close()
