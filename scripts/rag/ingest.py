"""Ingest an arbitrary local folder of Markdown into its own RAG index.

Keeps domain corpora (this one: D:\\公共资料库) separate from the plaud knowledge base, so
retrieval quality can be measured on real, content-rich documents without touching
production state.

Usage:
    python -m rag.ingest --root "D:\\公共资料库" --db <path> [--name ar] [--glob "**/*.md"]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import chunking  # noqa: E402
from rag import embedder as embedder_mod  # noqa: E402
from rag.rag_core import RagIndex, segment  # noqa: E402


def build(
    root: str | Path,
    db_path: str | Path,
    name: str = "corpus",
    pattern: str = "**/*.md",
    backend: str = "bge-small-zh",
    dim: int | None = None,
    rebuild: bool = True,
    min_chars: int = 40,
    verbose: bool = True,
) -> RagIndex:
    """Index every Markdown file under ``root`` into ``db_path``."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(root)

    # A single logical source: the folder itself.
    idx = RagIndex(db_path=db_path, kb_dir=root, sources=[(name, name)],
                   backend=backend, dim=dim, with_vectors=True)

    if rebuild:
        # Same as RagIndex.rebuild but against this corpus' own file list.
        idx._reset_vectors()
        idx.conn.execute("delete from chunks_fts")
        idx.conn.execute("delete from chunks")
        idx.conn.execute("delete from files")
        idx.conn.commit()

    files = sorted(p for p in root.glob(pattern) if p.is_file())
    t0 = time.time()
    n_chunks = 0
    n_files = 0
    skipped: list[str] = []
    # 和 sync_folder 用**同一套**跳过规则：`*-transcript.md`、session/segments JSON 与音频是
    # "对已有语音的逐字复述"，重导入只会让索引变大而不增加信息；而 `会议纪要/` 是决策与承诺的
    # 蒸馏记录，故意不排除。以前只有 sync 遵守这条规则，于是 `python -m rag.ingest` 会把逐字
    # 转写灌进索引——正好与设计原则相反。
    from rag.sync import _is_skipped

    for p in files:
        reason = _is_skipped(p, root)
        if reason:
            skipped.append(f"{p.name}: {reason}")
            continue
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            skipped.append(f"{p}: {e}")
            continue
        if len(raw.strip()) < min_chars:
            skipped.append(f"{p.name}: too short ({len(raw.strip())} chars)")
            continue
        chunks = chunking.chunk_markdown(raw)
        if not chunks:
            skipped.append(f"{p.name}: no chunks")
            continue
        rel = os.path.relpath(str(p), str(root)).replace("\\", "/")
        mtime = p.stat().st_mtime
        h = RagIndex._hash(raw)
        cur_ids = []
        for ch in chunks:
            cur = idx.conn.execute(
                "insert into chunks(source,path,rel,heading,text,mtime,content_hash) "
                "values (?,?,?,?,?,?,?)",
                (name, str(p), rel, ch.heading, ch.text, mtime, h))
            cid = cur.lastrowid
            cur_ids.append(cid)
            idx.conn.execute("insert into chunks_fts(rowid, text) values (?,?)",
                             (cid, segment(ch.text)))
        idx.conn.execute(
            "insert or replace into files(path,source,mtime,size,content_hash,n_chunks,indexed_at)"
            " values (?,?,?,?,?,?,?)",
            (str(p), name, mtime, p.stat().st_size, h, len(chunks), time.time()))
        idx.conn.commit()
        n_files += 1
        n_chunks += len(chunks)

    embedded = idx.embed_missing(verbose=verbose)
    if verbose:
        print(f"indexed {n_files} file(s), {n_chunks} chunk(s), {embedded} embedded "
              f"in {time.time()-t0:.1f}s")
        if skipped:
            print(f"skipped {len(skipped)}:")
            for s in skipped[:10]:
                print(f"  - {s}")
    return idx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rag.ingest")
    ap.add_argument("--root", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--glob", default="**/*.md")
    ap.add_argument("--backend", default=embedder_mod.DEFAULT_BACKEND)
    ap.add_argument("--dim", type=int, default=None)
    ap.add_argument("--no-rebuild", action="store_true")
    args = ap.parse_args(argv)

    name = args.name or Path(args.root).name
    idx = build(args.root, args.db, name=name, pattern=args.glob,
                backend=args.backend, dim=args.dim, rebuild=not args.no_rebuild)
    print(json.dumps(idx.stats(), ensure_ascii=False, indent=2))
    idx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
