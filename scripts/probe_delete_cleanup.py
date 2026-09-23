"""Inspect an index after a deletion: do the deleted file's rows actually go away?

"Deletion appears to work but the content still answers queries" is the kind of bug that
makes an assistant state something the user already removed -- worse than no result, because
it looks authoritative. Fixing it by reading the code is not enough: the three places a
chunk lives (``chunks``, ``chunks_fts``, ``chunk_vec``) can each keep a copy, and which one
resurrects a deleted row is only visible by counting.

This prints the row counts and then runs a search for a phrase unique to the deleted file,
so "still retrievable" is decided by evidence.

Usage:
    python scripts/probe_delete_cleanup.py --db <index.db> --kb <folder> [--phrase TEXT]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rag.rag_core import RagIndex, segment  # noqa: E402


def counts(conn: sqlite3.Connection) -> dict:
    def one(sql: str, *a) -> int:
        try:
            return conn.execute(sql, a).fetchone()[0]
        except sqlite3.Error as e:
            return -1

    out = {
        "chunks": one("select count(*) from chunks"),
        "files": one("select count(*) from files"),
        "chunks_fts": one("select count(*) from chunks_fts"),
        "chunk_vec": one("select count(*) from chunk_vec"),
    }
    # An FTS row whose chunk no longer exists would still be matched by a keyword query.
    out["fts_orphans"] = one(
        "select count(*) from chunks_fts where rowid not in (select id from chunks)")
    out["vec_orphans"] = one(
        "select count(*) from chunk_vec where rowid not in (select id from chunks)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--kb", default=None)
    ap.add_argument("--phrase", default="验收大纲要提前编制完成",
                    help="只出现在已删除文件里的短语")
    args = ap.parse_args()

    print("=" * 78)
    print(f"索引 {args.db}")
    print("=" * 78)

    conn = sqlite3.connect(args.db)
    c = counts(conn)
    for k, v in c.items():
        flag = ""
        if k == "fts_orphans" and v > 0:
            flag = "   ← 关键词检索仍会命中已删除内容"
        if k == "vec_orphans" and v > 0:
            flag = "   ← 向量检索仍会命中已删除内容"
        print(f"  {k:<14} {v}{flag}")

    print("\n  chunks 表里的文件：")
    for row in conn.execute("select rel, count(*) from chunks group by rel order by rel"):
        print(f"    {row[0]}  ({row[1]} chunks)")
    print("  files 表里的文件：")
    for row in conn.execute("select rel from files order by rel") if False else []:
        print(row)
    for row in conn.execute("select path from files order by path"):
        print(f"    {Path(row[0]).name}")

    print("\n  已删除文件是否仍在 chunks 里：")
    hit = conn.execute("select count(*) from chunks where text like ?",
                       (f"%{args.phrase[:12]}%",)).fetchone()[0]
    print(f"    chunks 命中 {hit} 行")
    conn.close()

    print("\n  检索『" + args.phrase + "』：")
    idx = RagIndex(db_path=args.db, kb_dir=args.kb or str(Path(args.db).parent))
    for h in idx.search(args.phrase, top_k=3):
        print(f"    {h['title']}  score={h['score']}")
        print(f"      {h['snippet'][:70]}")
    print()

    if c["fts_orphans"] > 0 or c["vec_orphans"] > 0:
        print("结论：删除不彻底，孤儿行会把已删内容召回。")
    elif hit:
        print("结论：chunks 里仍有已删文件的内容。")
    else:
        print("结论：删除干净，检索结果来自其它文件（可能是巧合的用词重叠）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
