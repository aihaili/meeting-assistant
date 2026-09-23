"""Bring a folder's index up to date without re-embedding what has not changed.

Why this exists rather than calling ``ingest.build``
---------------------------------------------------
``ingest.build`` defaults to ``rebuild=True``, which deletes every chunk and re-embeds the
whole folder. Running that on startup would work and be useless: opening the software
would re-embed the entire project every time, and for a long-running project that cost
grows without bound. ``RagIndex.index_file`` already skips a file whose mtime and size are
unchanged and refreshes bookkeeping without re-embedding when only the mtime moved -- but
``ingest.build`` writes its own chunks in a loop and never goes through it.

So this module is the missing wrapper: walk the folder, hand each file to the existing
incremental path, prune what has been deleted, and report what actually happened. The
index is a cache of the folder; the folder is the source of truth.

Two traps worth naming:

* **Raw restatements must not be re-imported — but minutes must.** The earlier wording here
  said "transcripts, session JSON, **generated minutes**", and that was **wrong about what
  the code does**: minutes were never denylisted. The distinction is by *kind of product*,
  not by *who wrote it*:

  * ``*-transcript.md`` / session JSON / audio are **verbatim restatements** of speech the
    session already holds. Re-importing them makes the index grow with every meeting while
    adding no new information. Denylisted by name and pattern.
  * ``会议纪要/`` is the **distilled record of decisions and commitments** — project history,
    and the most valuable thing to retrieve when someone asks "what did we agree last time?".
    Indexed like any other project document.

  Getting this wrong in the docs is not harmless: it made a reader (the agent, in fact)
  believe minutes were excluded, and act on that.
* **Deletions have to be pruned.** Otherwise a note the user removed keeps answering
  questions forever, which is worse than a missing answer because it looks authoritative.
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

from rag.rag_core import RagIndex  # noqa: E402

# Directories never worth indexing: Obsidian's own state, version control, virtualenvs,
# and the caches every editor leaves behind.
SKIP_DIRS = {".obsidian", ".trash", ".git", ".svn", "__pycache__", "node_modules",
             "venv", ".venv", ".idea", ".vscode", "rag", ".plaud"}

# Verbatim exports of the conversation itself. Re-importing them would put machine-written
# restatements back into the corpus that produced them. **会议纪要 is deliberately NOT in
# here**: minutes are the distilled record of what was decided — exactly what a later
# meeting wants to retrieve. Only verbatim restatements are skipped.
SKIP_NAMES = {"session.json", "transcript.txt", "segments.json"}
SKIP_PATTERNS = ("-transcript.md", "-segments.json", ".pcm", ".wav", ".ogg", ".mp3")


def _is_skipped(path: Path, root: Path) -> str | None:
    """Return a reason to skip, or None to index the file."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return "在根目录之外"
    for part in parts[:-1]:
        if part in SKIP_DIRS:
            return f"在跳过目录 {part}/ 内"
    name = path.name
    if name in SKIP_NAMES:
        return "是本软件生成的中间产物"
    low = name.lower()
    for pat in SKIP_PATTERNS:
        if low.endswith(pat):
            return "是本软件生成的产物"
    return None


def sync_folder(
    root: str | Path,
    db_path: str | Path,
    name: str | None = None,
    pattern: str = "**/*.md",
    backend: str = "bge-small-zh",
    dim: int | None = None,
    min_chars: int = 40,
    prune: bool = True,
    verbose: bool = False,
) -> dict:
    """Index new/changed files under ``root``, drop deleted ones, embed what is missing.

    Returns a report with counts, so a caller can show the user what happened on startup
    instead of silently doing work: ``{"added", "updated", "unchanged", "removed",
    "embedded", "skipped", "elapsed_s", "files", "chunks"}``.
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(root)
    name = name or root.name

    t0 = time.time()
    idx = RagIndex(db_path=db_path, kb_dir=root, sources=[(name, name)],
                   backend=backend, dim=dim, with_vectors=True)

    # A backend change invalidates every stored vector, and the incremental path cannot
    # detect that per file -- the text is unchanged, only its meaning-vector is wrong.
    if idx.backend_mismatch:
        if verbose:
            print(f"嵌入模型变了（{idx.backend_mismatch} -> {idx.backend}），需要全量重建")
        idx._reset_vectors()
        idx.conn.execute("delete from chunks_fts")
        idx.conn.execute("delete from chunks")
        idx.conn.execute("delete from files")
        idx.conn.commit()

    # What the index currently believes it holds.
    known = {r["path"]: (r["mtime"], r["size"])
             for r in idx.conn.execute("select path, mtime, size from files")}
    on_disk: set[str] = set()
    added = updated = unchanged = 0
    skipped: list[str] = []

    for p in sorted(root.glob(pattern)):
        if not p.is_file():
            continue
        reason = _is_skipped(p, root)
        if reason:
            skipped.append(f"{p.name}: {reason}")
            continue
        try:
            st = p.stat()
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            skipped.append(f"{p.name}: {e}")
            continue
        if len(raw.strip()) < min_chars:
            skipped.append(f"{p.name}: 太短（{len(raw.strip())} 字）")
            continue

        key = str(p)
        on_disk.add(key)
        prev = known.get(key)
        # The same test index_file uses: mtime AND size, so a file edited within the same
        # second is still noticed when its length changed.
        if prev is not None and abs(prev[0] - st.st_mtime) < 1e-6 and prev[1] == st.st_size:
            unchanged += 1
            continue
        idx.index_file(name, p)
        if prev is None:
            added += 1
        else:
            updated += 1

    removed = 0
    # 默认 0：下面只在 prune 分支里算。之前这个变量只在 prune=True 时被赋值，而 report
    # 字典无条件引用它 —— 于是 `--no-prune`（CLI 是可达路径）直接 UnboundLocalError。
    report_orphans = 0
    if prune:
        for path in set(known) - on_disk:
            # Only prune what belongs to this folder: a database shared with another root
            # must not lose that root's entries just because this folder no longer has them.
            if not path.startswith(str(root)):
                continue
            idx._delete_file(path)
            removed += 1

        # Sweep rows whose chunks are gone. ``_delete_file`` used to leave the ``files`` row
        # behind, so an index built before that fix still carries phantom entries -- and a
        # phantom entry makes the index look bigger than it is and hides a real deletion.
        orphans = [r["path"] for r in idx.conn.execute(
            "select path from files where path not in (select distinct path from chunks)")]
        for path in orphans:
            idx.conn.execute("delete from files where path=?", (path,))
        if orphans:
            idx.conn.commit()
            report_orphans = len(orphans)

    embedded = idx.embed_missing(verbose=verbose)
    st = idx.stats()

    report = {
        "root": str(root), "db": str(db_path), "name": name,
        "added": added, "updated": updated, "unchanged": unchanged,
        "removed": removed, "embedded": embedded,
        "orphans_cleaned": report_orphans,
        "skipped": len(skipped), "skip_reasons": skipped[:20],
        "files": st.get("files"), "chunks": st.get("chunks"),
        "elapsed_s": round(time.time() - t0, 3),
    }
    idx.close()
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rag.sync")
    ap.add_argument("--root", required=True, help="项目文件夹（真值来源）")
    ap.add_argument("--db", required=True, help="索引文件路径")
    ap.add_argument("--name", default=None)
    ap.add_argument("--glob", default="**/*.md")
    ap.add_argument("--backend", default="zh-small")
    ap.add_argument("--min-chars", type=int, default=40)
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（供程序调用）")
    args = ap.parse_args()

    from rag import embedder as embedder_mod

    backend = args.backend if args.backend != "zh-small" else embedder_mod.DEFAULT_BACKEND

    rep = sync_folder(args.root, args.db, name=args.name, pattern=args.glob,
                      backend=backend, min_chars=args.min_chars,
                      prune=not args.no_prune, verbose=not args.json)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False))
    else:
        print(f"新增 {rep['added']} · 更新 {rep['updated']} · 未变 {rep['unchanged']} · "
              f"删除 {rep['removed']} · 嵌入 {rep['embedded']}")
        print(f"索引现有 {rep['files']} 文件 / {rep['chunks']} chunks，"
              f"耗时 {rep['elapsed_s']}s")
        if rep["skip_reasons"]:
            print(f"跳过 {rep['skipped']} 个：")
            for s in rep["skip_reasons"]:
                print(f"  - {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
