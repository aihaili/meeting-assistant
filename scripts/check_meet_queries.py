"""Show which synthetic minutes file answers each ground-truth question.

Run ``--write`` to rewrite the ``expected_queries`` block in the ground-truth JSON
with whatever the index actually returns, after eyeballing that the answers are
right. Guessing which meeting date contains which promise is exactly the kind of
assumption that produced a bogus "0/6 retrieval" result the first time.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rag.rag_core import RagIndex  # noqa: E402

DB = HERE.parent / "data" / "meet.db"
KB = HERE.parent / "data" / "synth-corpus"
GT = HERE.parent / "data" / "meeting-audio" / "ground-truth.json"

QUERIES = [
    "第三方测评人员什么时候进场",
    "软件部署调通什么时候完成",
    "VR 体验什么时候之前要完整",
    "已发货的物资要准备哪些材料",
    "没发货的物资要准备什么",
    "铺线以什么为重点",
    "验收大纲什么时候做",
    "铺线什么时候完成",
    "每天要报什么",
]


def main() -> int:
    idx = RagIndex(db_path=str(DB), kb_dir=str(KB))
    gt = json.loads(GT.read_text(encoding="utf-8"))
    known = {q["q"] for q in gt.get("expected_queries", [])}

    print(f"index: {idx.stats()['files']} files, {idx.stats()['chunks']} chunks\n")
    rows = []
    for q in QUERIES:
        hits = idx.search(q, top_k=2)
        top = hits[0] if hits else None
        rows.append({"q": q, "expect": top["title"] if top else ""})
        mark = "  (真值问题)" if q in known else ""
        print(f"{q}{mark}")
        for h in hits:
            print(f"    {h['score']:.3f}  {h['title']}")

    if "--write" in sys.argv:
        gt["expected_queries"] = rows
        GT.write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {GT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
