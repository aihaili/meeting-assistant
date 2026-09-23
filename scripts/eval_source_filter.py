"""Does a source filter isolate projects as reliably as separate indexes?

Two ways to stop project A answering project B's questions:

* **filter** — one index holding every project, queries restricted to the asked-about
  project via ``search(source=...)``
* **separate indexes** — one index per project, only the relevant one searched

Both stop contamination *if they work*. The filtered approach is far more convenient (one
file, one rebuild, projects discoverable by listing sources), so it is worth checking
rather than assuming the separate-index arrangement is the only option.

This runs the same ambiguous queries that exposed cross-project contamination and reports,
for each arrangement, whether the top hit belongs to the asked-about project.

Usage:
    python scripts/eval_source_filter.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rag.rag_core import RagIndex  # noqa: E402

ROOT = HERE.parent
DATA = ROOT / "data" / "multi-project"

GENERIC = [
    "{wiring} 什么时候完成",
    "{asset} 什么时候之前必须完整",
    "{reviewer} 什么时候进场",
    "{equipment} 到货之后要做什么",
    "验收大纲什么时候要完成",
    "薪资和人员稳定性有什么风险",
]

# The subtle one: a question that names no entity of any project at all, which is what an
# LLM extracting live-speech keywords would most often produce.
VAGUE = ["什么时候之前必须完成", "验收之前要准备什么", "进场之后谁负责配合",
         "材料要准备哪些", "什么时候要到货"]


def main() -> int:
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    mixed = RagIndex(db_path=str(ROOT / "data" / "mp-mixed.db"), kb_dir=str(DATA))

    # Which source names does the mixed index actually hold?
    sources = sorted({r["source"] for r in mixed.conn.execute(
        "select distinct source from chunks")})
    print(f"混库里的 source 名: {sources}")
    print()

    def proj_of(rel: str) -> str:
        first = str(rel or "").replace("\\", "/").split("/")[0]
        for p in manifest["projects"]:
            if p["key"] == first:
                return p["name"]
        return "?"

    per = {}
    for p in manifest["projects"]:
        db = ROOT / "data" / f"mp-{p['key']}.db"
        if db.exists():
            per[p["name"]] = RagIndex(db_path=str(db), kb_dir=str(DATA / p["key"]))

    # A per-project index cannot return another project's document, so "which project does
    # this hit belong to" is answered by the index it came from, not by its path. Reading
    # the path gave "?" for every per-project hit and made a working arrangement look
    # broken -- the hit's rel is relative to *its own* kb_dir, so it carries no project.
    def proj_of_per(hit: dict, owner: str) -> str:
        return owner if hit else "?"

    print("=" * 90)
    print("A. 通用问句（每个项目都有、答案不同）")
    print("=" * 90)
    print(f"{'问的是':<10}{'问句':<30}{'不带过滤':<16}{'带 source 过滤':<18}{'独立库'}")

    stats = {"nofilter_own": 0, "filter_own": 0, "sep_own": 0, "n": 0}
    filter_failed = []
    for p in manifest["projects"]:
        for tmpl in GENERIC:
            q = tmpl.format(wiring=p["wiring"], asset=p["asset"],
                            reviewer=p["reviewer"], equipment=p["equipment"])
            h1 = mixed.search(q, top_k=1)
            got_free = proj_of(h1[0].get("rel") or "") if h1 else "?"
            # Each project's own index was built with sources=[(key, key)], so the filter
            # value is the project key.
            h2 = mixed.search(q, top_k=1, source=p["key"])
            got_filt = proj_of(h2[0].get("rel") or "") if h2 else "（无结果）"
            idx = per.get(p["name"])
            h3 = idx.search(q, top_k=1) if idx else []
            got_sep = proj_of_per(h3[0] if h3 else None, p["name"])

            stats["n"] += 1
            stats["nofilter_own"] += got_free == p["name"]
            stats["filter_own"] += got_filt == p["name"]
            stats["sep_own"] += got_sep == p["name"]
            if got_filt != p["name"]:
                filter_failed.append((p["name"], q, got_filt))
            print(f"{p['name']:<10}{q[:28]:<30}{got_free:<16}{got_filt:<18}{got_sep}")

    n = max(1, stats["n"])
    print()
    print(f"  不带过滤   top1 属于本项目 {stats['nofilter_own']}/{n}"
          f" = {100 * stats['nofilter_own'] / n:.0f}%")
    print(f"  带 source 过滤 {stats['filter_own']}/{n}"
          f" = {100 * stats['filter_own'] / n:.0f}%")
    print(f"  独立索引   {stats['sep_own']}/{n} = {100 * stats['sep_own'] / n:.0f}%")
    if filter_failed:
        print("\n  过滤后仍失败的：")
        for who, q, got in filter_failed[:6]:
            print(f"    {who}: {q}  -> {got}")

    print()
    print("=" * 90)
    print("B. 完全不含任何项目实体词的问句（LLM 抽关键词的典型产物）")
    print("=" * 90)
    for q in VAGUE:
        h = mixed.search(q, top_k=3)
        free = [proj_of(x.get("rel") or "") for x in h]
        # No project is "correct" here; what matters is whether a filter still works.
        f = mixed.search(q, top_k=2, source="xinjiang")
        filt = [proj_of(x.get("rel") or "") for x in f]
        print(f"  {q:<22} 不带过滤 {str(free):<40} 过滤后 {filt}")

    print()
    out = ROOT / "data" / "source-filter-eval.json"
    out.write_text(json.dumps({"sources": sources, "stats": stats,
                               "filter_failed": filter_failed,
                               "generic_queries": GENERIC,
                               "vague_queries": VAGUE},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
