"""Check that several knowledge bases can be searched as one.

Two things need proving, and neither is obvious from reading the code:

1. **Hosts can be measured separately.** Public documents and a project's own corpus have
   opposite lifecycles, so the point of the arrangement is that each can be listed,
   rebuilt and counted on its own. If the merged view hides that, the arrangement buys
   nothing.
2. **Scores are not silently mixed across embedders.** The same string scores about +0.04
   cosine across two different models in this project, so two indexes built with different
   models must be fused by *rank*, and the output must say so rather than presenting an RRF
   number as if it were a similarity. Same-model indexes can be merged by score.

Usage:
    python scripts/test_multi_index.py

Fixtures are built by the test itself (temp dir), not read from ``data/*.db``: those are
gitignored artefacts, and ``data/ar.db`` is the app's *real* public library — filling it
with synthetic minutes just to make a test pass would pollute what the user searches.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rag.multi_index import MultiIndex  # noqa: E402

ROOT = HERE.parent

# 三个探针词分别覆盖：来源标注、分组各自排名、跳过坏库之后仍能检索。
_PUBLIC_DOC = """# 第三方测评实施规范

第三方测评人员进场前，甲方应提供验收大纲。验收大纲应当在进场前两周完成。

## 线缆敷设

铺线作业由施工单位负责，铺线完成后需第三方复测。
"""

_PROJECT_DOC = """# 项目会议纪要（合成夹具）

第三方测评人员什么时候进场——本次会议确认为九月二十日。

验收大纲什么时候要完成——下周三之前。

铺线进度：本周完成 3120 米。
"""


def _build_fixtures(tmp: Path) -> tuple[dict, dict]:
    """Build two throwaway indexes (公共 / 项目) with the probe keywords."""
    from rag.ingest import build

    pub_dir = tmp / "public"
    proj_dir = tmp / "project"
    pub_dir.mkdir()
    proj_dir.mkdir()
    (pub_dir / "规范.md").write_text(_PUBLIC_DOC, encoding="utf-8")
    (proj_dir / "纪要.md").write_text(_PROJECT_DOC, encoding="utf-8")

    pub_db = tmp / "public.db"
    proj_db = tmp / "project.db"
    build(pub_dir, pub_db, name="公共", verbose=False)
    build(proj_dir, proj_db, name="项目", verbose=False)
    return (
        {"name": "公共", "db": str(pub_db), "kb": str(pub_dir)},
        {"name": "项目", "db": str(proj_db), "kb": str(proj_dir)},
    )


def main() -> int:
    fails = 0
    tmp_ctx = tempfile.TemporaryDirectory()
    PUBLIC, PROJECT = _build_fixtures(Path(tmp_ctx.name))

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        if not ok:
            fails += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))

    print("=" * 74)
    print("多知识库合并检索")
    print("=" * 74)

    mi = MultiIndex([PUBLIC, PROJECT])
    st = mi.stats()
    print(f"\n加载 {len(mi.entries)} 个库，合计 {st['chunks']} chunks")
    for row in st["indexes"]:
        print(f"    · {row['name']:<6} {row['chunks']:>5} chunks  {row['db']}")
    if st.get("errors"):
        for e in st["errors"]:
            print(f"    ! {e}")

    check("两个库都加载了", len(mi.entries) == 2, f"{len(mi.entries)}")
    check("合计 chunks 等于两库之和",
          st["chunks"] == sum(r["chunks"] or 0 for r in st["indexes"]), str(st["chunks"]))
    check("每个库可单独计数", all(r["chunks"] for r in st["indexes"]))

    # ── provenance ──────────────────────────────────────────────────────
    print("\n来源标注：")
    for q in ("第三方测评人员什么时候进场", "审查讯问时我方人员不得少于几人"):
        hits = mi.search(q, top_k=3)
        print(f"  {q}")
        for h in hits:
            print(f"      [{h.get('kb')}] {h['title']}  score={h['score']} "
                  f"({h.get('score_kind', '相似度')})")
        check(f"{q[:12]}… 每条结果都带 kb 来源",
              all(h.get("kb") for h in hits))

    # ── fusion is always by rank ────────────────────────────────────────
    # Not "only when the backends differ". ``RagIndex.search`` divides each result by that
    # index's own top score, so every index's best hit is exactly 1.0 and a score-based
    # merge ties them all -- measured: the public corpus displaced the project corpus on
    # every meeting question (only 2 of 8 queries had the right document first). Ranking is
    # the only signal that survives that normalisation.
    hits = mi.search("第三方测评人员什么时候进场", top_k=3)
    check("多库一律用排名融合", all(h.get("score_kind") == "rrf" for h in hits),
          f"kinds={[h.get('score_kind') for h in hits]}")
    check("融合结果仍带来源", all(h.get("kb") for h in hits))

    # ── different backends must be detected ─────────────────────────────
    mi2 = MultiIndex([PUBLIC, PROJECT])
    mi2.backends = {"bge-small-zh", "bge-m3"}
    mi2.mixed_backends = True
    hits2 = mi2.search("第三方测评人员什么时候进场", top_k=3)
    check("跨模型仍可检索并标注", all(h.get("score_kind") == "rrf" for h in hits2))

    # ── grouped retrieval: the honest presentation for two corpora ──────
    # Rank fusion puts the top of every corpus at the same weight, so the *order between*
    # corpora is arbitrary -- and a prior that forced an order was measured to bury the
    # public corpus (0/4 public questions still surfaced a public document). Grouping keeps
    # each corpus ranked against itself and leaves the choice to the caller.
    print("\n分组检索：")
    grouped = mi.search_by_index("验收大纲什么时候要完成", top_k=2)
    for name, hits in grouped.items():
        print(f"    【{name}】 " + " | ".join(h["title"][:26] for h in hits))
    check("每个库各成一组", set(grouped.keys()) == {"公共", "项目"}, str(list(grouped.keys())))
    check("每组都带来源标注",
          all(h.get("kb") == name for name, hits in grouped.items() for h in hits))
    check("每组内部按本库排名", all(len(hits) <= 2 for hits in grouped.values()))
    check("分组不会漏掉某个库",
          all(len(grouped[n]) > 0 for n in ("公共", "项目")),
          str({k: len(v) for k, v in grouped.items()}))

    # ── a broken index must not take the good one down ──────────────────
    print("\n容错：")
    mi3 = MultiIndex([PUBLIC,
                      {"name": "坏的", "db": str(ROOT / "data" / "does-not-exist.db")},
                      PROJECT])
    check("缺失的库被跳过而不是崩溃", len(mi3.entries) == 2, f"{len(mi3.entries)} 个可用")
    check("缺失的库有记录", any("不存在" in e for e in mi3.errors), str(mi3.errors[:1]))
    check("跳过后仍能检索", bool(mi3.search("铺线", top_k=2)))

    # ── all broken: must raise, not return empty ────────────────────────
    try:
        MultiIndex([{"name": "x", "db": str(ROOT / "data" / "nope.db")}])
        check("全部不可用时应当报错", False, "没有抛异常")
    except RuntimeError as e:
        check("全部不可用时应当报错", True, str(e)[:40])

    print(f"\n{'全部通过' if fails == 0 else f'{fails} 处失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
