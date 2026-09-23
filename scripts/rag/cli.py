"""CLI for the plaud RAG module.

Usage (from the meeting-assistant repo root, with the venv interpreter)::

    python -m rag.cli stats
    python -m rag.cli index                # incremental
    python -m rag.cli index --no-vector    # FTS5 only (no model load)
    python -m rag.cli rebuild
    python -m rag.cli search "会议行动项" --mode hybrid -k 5
    python -m rag.cli search "五出六进" --mode bm25
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # direct-script invocation
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.rag_core import DEFAULT_KB, RagIndex, default_db_path  # noqa: E402


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--kb", default=str(DEFAULT_KB), help="knowledge base root")
    p.add_argument("--db", default=None, help="sqlite db path (default <kb>/rag/rag.db)")
    p.add_argument("--dim", type=int, default=512, help="Matryoshka dim (default 512)")
    p.add_argument("--tokenizer", default="unicode61",
                   help="FTS5 tokenizer over the jieba-segmented text (default unicode61)")
    p.add_argument("--no-vector", action="store_true", help="disable embedding/vector index")


def _index(args) -> int:
    idx = RagIndex(db_path=args.db, kb_dir=args.kb, dim=args.dim,
                   tokenizer=args.tokenizer, with_vectors=not args.no_vector)
    print(f"db: {idx.db_path}")
    print(f"vectors: {'on' if idx.vec_ready else 'off'} | dim={idx.dim} | fts={idx.tokenizer}")
    stats = idx.rebuild(verbose=True) if args.rebuild else idx.index_all(verbose=True)
    print("\nresult:", json.dumps(stats, ensure_ascii=False))
    s = idx.stats()
    print(f"files={s['files']} chunks={s['chunks']} vectors={s['vectors']} "
          f"db={s['db_bytes']/1024:.1f}KB")
    idx.close()
    return 0


def _search(args) -> int:
    idx = RagIndex(db_path=args.db, kb_dir=args.kb, dim=args.dim,
                   tokenizer=args.tokenizer, with_vectors=not args.no_vector)
    hits = idx.search(args.query, top_k=args.k, source=args.source, mode=args.mode)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
    else:
        if not hits:
            print("(no hits)")
        for i, h in enumerate(hits, 1):
            print(f"\n[{i}] score={h['score']}  {h['title']}")
            print(f"    label={h['label']}  rel={h['rel']}")
            for sn in h["snippets"]:
                print(f"    · {sn[:150]}")
    idx.close()
    return 0


def _stats(args) -> int:
    idx = RagIndex(db_path=args.db, kb_dir=args.kb, dim=args.dim,
                   tokenizer=args.tokenizer, with_vectors=not args.no_vector)
    print(json.dumps(idx.stats(), ensure_ascii=False, indent=2))
    idx.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rag.cli", description="plaud RAG index/search CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_idx = sub.add_parser("index", help="incrementally index the KB")
    _add_common(p_idx)
    p_idx.add_argument("--rebuild", action="store_true", help="wipe and re-index")
    p_idx.set_defaults(func=_index)

    p_s = sub.add_parser("search", help="query the index")
    _add_common(p_s)
    p_s.add_argument("query")
    p_s.add_argument("-k", type=int, default=5)
    p_s.add_argument("--mode", choices=["hybrid", "bm25", "vector"], default="hybrid")
    p_s.add_argument("--source", default=None, help="restrict to one label, e.g. wiki")
    p_s.add_argument("--json", action="store_true")
    p_s.set_defaults(func=_search)

    p_st = sub.add_parser("stats", help="show index statistics")
    _add_common(p_st)
    p_st.set_defaults(func=_stats)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
