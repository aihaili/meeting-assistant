r"""Is a knowledge graph needed, or does plain event structuring already cover it?

Context (measured on the 7-meeting synthetic corpus):
  A. clause lookup      14/15 = 93%   -- plain retrieval is fine
  B. cross-meeting      fully covered 3/6
  C. aggregation        STRUCTURALLY IMPOSSIBLE for chunk retrieval -- the answer is a
                        count/list, not a similar passage. "甲方一共提出多少项要求"
                        returned a 进度 clause.

The corpus already carries a `结构化事件` JSON block per meeting (written by the
generator). So test the cheap hypothesis first:

  H1  per-EVENT retrieval (one doc per event) makes the relevant events retrievable,
      so a client-side count/filter can answer the aggregation questions
  H2  chunk retrieval cannot, even with a bigger top-k

If H1 holds, a full knowledge graph is not needed -- a flat event table + aggregation
suffices. That is a very different amount of work from entity/relation extraction.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"E:\markdown\meeting-assistant\scripts")
from rag import embedder as E  # noqa: E402
from rag.rag_core import RagIndex, build_fts_query, segment  # noqa: E402

CORPUS = Path(r"E:\markdown\meeting-assistant\data\synth-corpus")
DB = r"E:\markdown\meeting-assistant\data\synth.db"

# ---- load every event from the generator's JSON blocks ----------------------
events: list[dict] = []
for p in sorted(CORPUS.glob("*.md")):
    txt = p.read_text(encoding="utf-8")
    m = re.search(r"## 结构化事件.*?```json\s*([\s\S]*?)```", txt, re.S)
    if not m:
        continue
    for e in json.loads(m.group(1)):
        e["meeting"] = re.search(r"（(\d{8})）", p.name).group(1)
        events.append(e)

print(f"events loaded: {len(events)}")
tc = Counter(e["type"] for e in events)
print(f"by type: {dict(tc)}")
print(f"by meeting: {dict(Counter(e['meeting'] for e in events))}\n")

emb = E.get_embedder(backend="bge-small-zh")


def build_event_index():
    import sqlite_vec

    con = sqlite3.connect(":memory:")
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute("create table ev(id integer primary key, meeting text, etype text, dl text, c text)")
    con.execute("create virtual table ev_fts using fts5(text, tokenize='unicode61')")
    con.execute("create virtual table ev_vec using vec0(embedding float[512])")
    # One document per event; type and date are carried IN THE TEXT so lexical
    # search can use them (a structured index would filter on columns instead).
    docs = [f"[{e['type']}] [{e['meeting']}] {e['content']}" +
            (f" [期限:{e['deadline']}]" if e["deadline"] else "")
            for e in events]
    vecs = emb.encode(docs)
    for i, (e, d, v) in enumerate(zip(events, docs, vecs), 1):
        con.execute("insert into ev(id,meeting,etype,dl,c) values (?,?,?,?,?)",
                    (i, e["meeting"], e["type"], e["deadline"], e["content"]))
        con.execute("insert into ev_fts(rowid,text) values (?,?)", (i, segment(d)))
        con.execute("insert into ev_vec(rowid,embedding) values (?,?)",
                    (i, sqlite_vec.serialize_float32(v)))
    con.commit()
    return con


ev = build_event_index()
idx = RagIndex(db_path=DB, kb_dir=str(CORPUS))


def ev_search(q, k=8):
    qv = emb.encode_one(q, is_query=True)
    import sqlite_vec
    rows = ev.execute(
        "select rowid from ev_vec where embedding match ? and k = ? order by distance",
        (sqlite_vec.serialize_float32(qv), k)).fetchall()
    return [r[0] for r in rows]


def chunk_search(q, k=8):
    return idx.search(q, top_k=k, mode="hybrid")


AGG = [
    ("甲方一共提出了多少项要求", "甲方要求", None),
    ("史塔克一共做过几次承诺", "己方承诺", None),
    ("一共有几个风险项被记录", "风险", None),
    ("整个项目一共涉及几次期限要求", None, "deadline"),
]

print("=" * 92)
print("聚合类查询：top-8 检索到的条目里，目标类型占多少？")
print("=" * 92)
print(f"{'问题':30} {'目标类型':10} {'事件索引命中':>12} {'chunk索引命中':>14}")
print("-" * 92)
for q, etype, field in AGG:
    # --- event-level index -------------------------------------------------
    ids = ev_search(q, k=8)
    got_types = Counter(ev.execute("select etype from ev where id=?", (i,)).fetchone()[0]
                        for i in ids)
    ev_hits = got_types.get(etype, 0) if etype else \
        sum(1 for i in ids if ev.execute("select dl from ev where id=?", (i,)).fetchone()[0])

    # --- chunk index -------------------------------------------------------
    hits = chunk_search(q, k=8)
    joined = " ".join(
        (h.get("snippets") or [""])[0] + " " +
        " ".join(c.get("text", "") for c in (h.get("chunks") or []))
        for h in hits)
    ch_hits = len(re.findall(rf"\[{etype}\]", joined)) if etype else \
        len(re.findall(r"（期限：", joined))

    print(f"{q:30} {str(etype or field):10} {ev_hits:>12} {ch_hits:>14}")

print()
print("真实总数（生成器元数据）：", dict(tc), " 期限非空:",
      sum(1 for e in events if e["deadline"]))

print("\n" + "=" * 92)
print("结论判据")
print("=" * 92)
print("  若『事件索引命中』随 k 增长、且能靠类型过滤直接汇总 → 平铺事件表即可，无需图谱")
print("  若『chunk索引命中』始终接近 0/不可用 → chunk 检索在聚合类查询上确实结构性失效")

# ---- does a flat filter answer it exactly? ---------------------------------
print("\n" + "=" * 92)
print("平铺事件表能否精确回答（直接按类型过滤，不依赖相似度）")
print("=" * 92)
for label, etype in (("甲方要求", "甲方要求"), ("己方承诺", "己方承诺"), ("风险", "风险")):
    n = ev.execute("select count(*) from ev where etype=?", (etype,)).fetchone()[0]
    print(f"  按类型过滤 [{etype}] -> {n} 条   （与生成器元数据一致: {tc[etype]}）")
n_dl = ev.execute("select count(*) from ev where dl != ''").fetchone()[0]
print(f"  按字段过滤 有期限 -> {n_dl} 条")

# ---- and the traversal case -------------------------------------------------
print("\n" + "=" * 92)
print("多跳/遍历：某实体在各会议中的演变")
print("=" * 92)
for entity in ("感应器", "VR", "验收大纲"):
    t0 = Counter()
    for r in ev.execute("select meeting, etype, c from ev"):
        if entity in r[2]:
            t0[r[0]] += 1
    print(f"  {entity:8} 出现在会议: {dict(sorted(t0.items()))}")
print("  —— 这只需要一个 LIKE 扫描就能给出完整时间线，不需要图遍历。")
