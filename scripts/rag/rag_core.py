"""SQLite-backed hybrid retrieval core for the plaud RAG module.

Storage layout (one DB, ``rag.db``):

  meta            schema version + index config
  chunks          id / source / path / rel / heading / text / mtime / content_hash
  chunks_fts      FTS5 over a jieba-segmented copy of ``text`` (unicode61)
  chunk_vec       sqlite-vec ``vec0`` over the Matryoshka-truncated embeddings
  files           per-file bookkeeping (mtime, size, hash) for incremental indexing

Retrieval is BM25 over FTS5 unioned with a KNN scan over vec0, fused with
Reciprocal Rank Fusion (k=60). The public ``search()`` returns the same shape the
legacy ``wiki_server.search_kb()`` produced so the server can swap implementations
without touching the frontend.

Why jieba instead of FTS5's ``trigram`` tokenizer (measured on this KB):

* ``unicode61`` treats an entire CJK run as a **single token**, so no Chinese
  substring ever matches -- unusable on its own.
* ``trigram`` matches contiguous >=3-char substrings but is **structurally blind to
  2-character words**, which is exactly how Chinese personal and company names appear
  (孙总 / 张伟 / 李强 all returned zero hits).
* Pre-segmenting both documents and queries with jieba and indexing with
  ``unicode61`` scored 15/16 on a 16-query probe, vs 12/16 for trigram, and is the
  only variant that resolves 2-character names.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path

# Import shim so both `python -m rag.cli` and `import rag_core` (wiki_server) work.
if __package__ in (None, ""):  # pragma: no cover - direct-script import
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import chunking  # noqa: E402
from rag import embedder as embedder_mod  # noqa: E402

SCHEMA_VERSION = 2

DEFAULT_KB = Path(os.path.expanduser("~/plaud-knowledge-base"))

# Searchable roots -> label, mirroring wiki_server.SEARCH_DIRS.
DEFAULT_SOURCES: list[tuple[str, str]] = [
    ("wiki", "wiki"),
    ("ai_notes_merged", "会议纪要"),
    ("concepts", "概念"),
]

# Where each searchable root actually lives inside the KB. The label is the display
# name, so it cannot be used to derive the directory (the 会议纪要 label maps to the
# ai_notes_merged directory).
SOURCE_DIRS: dict[str, str] = {
    "wiki": "wiki",
    "会议纪要": "ai_notes_merged",
    "概念": "concepts",
}

DEFAULT_DIM = 512
DEFAULT_TOKENIZER = "unicode61"
RRF_K = 60
CANDIDATES = 50


def default_db_path(kb_dir: str | Path = DEFAULT_KB) -> Path:
    return Path(kb_dir) / "rag" / "rag.db"


# ══════════════════════════════════════════════════════════════════════
# Text segmentation (jieba) for the FTS5 index and for queries
# ══════════════════════════════════════════════════════════════════════

_jieba = None


def _get_jieba():
    global _jieba
    if _jieba is None:
        import jieba

        jieba.setLogLevel(60)  # silence the build/cache banner
        _jieba = jieba
    return _jieba


def segment(text: str) -> str:
    """Return a space-joined jieba token stream suitable for unicode61 indexing."""
    if not text:
        return ""
    jb = _get_jieba()
    parts = [t.strip() for t in jb.cut(text)]
    return " ".join(p for p in parts if p)


def build_fts_query(query: str) -> str:
    """Turn a natural-language query into an FTS5 OR expression over jieba tokens.

    OR (not AND) because jieba occasionally splits a query differently from how the
    document was split (e.g. 没有业务结论 -> 没有/业务/结论 vs 无业务结论); AND scores
    0 in that case. The extra recall is handled downstream by RRF + vector ranking.
    """
    if not query or not query.strip():
        return ""
    jb = _get_jieba()
    toks: list[str] = []
    seen: set[str] = set()
    for t in jb.cut(query):
        t = t.strip()
        # Drop pure punctuation/whitespace tokens.
        if not t or not re.search(r"[0-9A-Za-z\u4e00-\u9fff\u3400-\u4dbf]", t):
            continue
        if t not in seen:
            seen.add(t)
            toks.append(t)
    if not toks:
        return ""

    def esc(t: str) -> str:
        return '"' + t.replace('"', '""') + '"'

    return " OR ".join(esc(t) for t in toks)


# ══════════════════════════════════════════════════════════════════════
# Index
# ══════════════════════════════════════════════════════════════════════


class RagIndex:
    """Owns the SQLite connection and all indexing / retrieval operations."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        kb_dir: str | Path = DEFAULT_KB,
        sources: list[tuple[str, str]] | None = None,
        dim: int | None = None,
        tokenizer: str = DEFAULT_TOKENIZER,
        with_vectors: bool = True,
        backend: str = "bge-small-zh",
    ) -> None:
        self.kb_dir = Path(kb_dir)
        self.db_path = Path(db_path) if db_path else default_db_path(self.kb_dir)
        self.sources = sources if sources is not None else DEFAULT_SOURCES
        self.backend = backend
        self.dim = dim if dim is not None else embedder_mod.BACKENDS[backend]["dim"]
        self.tokenizer = tokenizer
        self.with_vectors = with_vectors

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the intended deployment is a ThreadingHTTPServer, so
        # the connection is touched from whichever worker thread handles a request.
        # Correctness then depends on the caller serialising access -- RagIndex exposes
        # `self.lock` for that, and the assistant server takes it around every call.
        # (Each thread creating its own connection was the alternative, but the ONNX
        # embedder session is also not thread-safe, so serialising is needed anyway.)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._vec_loaded = False
        self._embedder: embedder_mod.Embedder | None = None
        self.lock = threading.RLock()
        self._init_schema()
        self._check_backend_match()

    # ── schema ───────────────────────────────────────────────────────────

    def _stored_backend(self) -> str | None:
        try:
            row = self.conn.execute("select value from meta where key='embedder'").fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None

    def _check_backend_match(self) -> None:
        """Refuse to serve a vector index built with a *different* embedder.

        Vectors from different models are not comparable: a BGE vector and a WeMM
        vector for the same sentence are unrelated points, so a KNN over a table that
        mixed them would return arbitrary neighbours. The index records which embedder
        produced its vectors; a mismatch disables the vector arm (and is reported in
        ``stats()``) rather than silently returning nonsense.
        """
        self.backend_mismatch: str | None = None
        if not self.vec_ready:
            return
        stored = self._stored_backend()
        if stored and stored != self.get_embedder().identity:
            n_vec = self.conn.execute("select count(*) from chunk_vec").fetchone()[0]
            if n_vec:
                self.backend_mismatch = stored
                print(
                    f"[rag] 向量索引由 '{stored}' 建立，当前后端为 "
                    f"'{self.get_embedder().identity}'；向量臂已停用。"
                    f"\n      重建请运行: python -m rag.cli index --rebuild",
                    file=sys.stderr,
                )

    def _load_vec(self) -> bool:
        if self._vec_loaded:
            return True
        try:
            import sqlite_vec

            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            self._vec_loaded = True
        except Exception:
            self._vec_loaded = False
        return self._vec_loaded

    def _init_schema(self) -> None:
        c = self.conn
        c.execute("""create table if not exists meta (
            key text primary key, value text)""")
        c.execute("""create table if not exists chunks (
            id integer primary key autoincrement,
            source text not null,
            path text not null,
            rel text not null,
            heading text not null default '',
            text text not null,
            mtime real not null,
            content_hash text not null)""")
        c.execute("create index if not exists idx_chunks_path on chunks(path)")
        c.execute("create index if not exists idx_chunks_source on chunks(source)")
        c.execute("""create table if not exists files (
            path text primary key,
            source text not null,
            mtime real not null,
            size integer not null,
            content_hash text not null,
            n_chunks integer not null,
            indexed_at real not null)""")

        # FTS5 mirror (external content kept simple: a plain table we sync by hand).
        #
        # The tokenizer is part of the *table definition*, so `if not exists` would
        # silently keep a stale tokenizer after the default changed (schema v1 used
        # trigram, v2 uses unicode61 over jieba-segmented text). Detect a mismatch and
        # rebuild the table instead of trusting the meta row.
        self._ensure_fts_table()

        if self.with_vectors and self._load_vec():
            c.execute(
                f"create virtual table if not exists chunk_vec using vec0("
                f"embedding float[{self.dim}])"
            )

        c.execute("insert or replace into meta(key,value) values ('schema_version',?)",
                  (str(SCHEMA_VERSION),))
        c.execute("insert or replace into meta(key,value) values ('dim',?)", (str(self.dim),))
        c.execute("insert or replace into meta(key,value) values ('tokenizer',?)",
                  (self.tokenizer,))
        # Only claim the embedder once vectors actually exist for it; otherwise the
        # first writer wins and a later reader with a different backend is warned.
        if self.vec_ready:
            n_vec = c.execute("select count(*) from chunk_vec").fetchone()[0]
            if n_vec:
                ident = self.get_embedder().identity
                if self._stored_backend() in (None, ident):
                    c.execute("insert or replace into meta(key,value) values ('embedder',?)",
                              (ident,))
        c.commit()

    def _live_fts_tokenizer(self) -> str | None:
        """Return the tokenizer actually baked into the existing chunks_fts table."""
        row = self.conn.execute(
            "select sql from sqlite_master where type='table' and name='chunks_fts'"
        ).fetchone()
        if not row or not row[0]:
            return None
        m = re.search(r"tokenize\s*=\s*'([^']+)'", row[0])
        return m.group(1) if m else "unicode61"  # FTS5 default

    def _ensure_fts_table(self) -> None:
        live = self._live_fts_tokenizer()
        if live is not None and live != self.tokenizer:
            # Tokenizer changed: drop and recreate, then repopulate from chunks.
            self.conn.execute("drop table if exists chunks_fts")
            self.conn.execute("delete from files")  # force a clean re-index
            self.conn.commit()
            live = None
        if live is None:
            self.conn.execute(
                f"create virtual table if not exists chunks_fts using fts5("
                f"text, tokenize='{self.tokenizer}')"
            )
            self.conn.commit()
            self._repopulate_fts()
        else:
            # Table exists: make sure it is not empty while chunks is not.
            n_fts = self.conn.execute("select count(*) from chunks_fts").fetchone()[0]
            n_chunks = self.conn.execute("select count(*) from chunks").fetchone()[0]
            if n_chunks and not n_fts:
                self._repopulate_fts()

    def _repopulate_fts(self) -> None:
        """Rebuild the FTS mirror from chunks (jieba-segmented)."""
        self.conn.execute("delete from chunks_fts")
        for cid, text in self.conn.execute("select id, text from chunks"):
            self.conn.execute("insert into chunks_fts(rowid, text) values (?,?)",
                              (cid, segment(text)))
        self.conn.commit()

    # ── helpers ──────────────────────────────────────────────────────────

    @property
    def vec_ready(self) -> bool:
        return self.with_vectors and self._load_vec()

    def get_embedder(self) -> embedder_mod.Embedder:
        if self._embedder is None:
            self._embedder = embedder_mod.get_embedder(backend=self.backend, dim=self.dim)
        return self._embedder

    def iter_files(self):
        """Yield ``(source_label, abs_path)`` for every indexable Markdown file."""
        for name, label in self.sources:
            root = self.kb_dir / name
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*.md")):
                if p.is_file():
                    yield label, p

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]

    # ── indexing ─────────────────────────────────────────────────────────

    def index_file(self, source: str, path: str | Path, force: bool = False) -> dict:
        """Index one file. Skips work when mtime+hash are unchanged."""
        p = Path(path)
        # `rel` is relative to the source root (e.g. "INDEX.md", "people/x.md"), not to
        # the KB root. wiki_server builds links as "/" + rel for wiki pages and
        # "/raw/{label}/{rel}" otherwise, and /raw resolves rel against per-label
        # directories -- so a KB-root-relative path would double the prefix. The label
        # is a display name, so the real directory comes from SOURCE_DIRS.
        src_root = self.kb_dir / SOURCE_DIRS.get(source, source)
        try:
            rel = os.path.relpath(str(p), str(src_root)).replace("\\", "/")
        except ValueError:
            rel = os.path.relpath(str(p), str(self.kb_dir)).replace("\\", "/")
        if rel.startswith("../"):
            rel = os.path.relpath(str(p), str(self.kb_dir)).replace("\\", "/")
        try:
            st = p.stat()
        except OSError as e:
            return {"path": rel, "status": "error", "error": str(e)}

        row = self.conn.execute("select * from files where path=?", (str(p),)).fetchone()
        if row and not force and abs(row["mtime"] - st.st_mtime) < 1e-6 and row["size"] == st.st_size:
            return {"path": rel, "status": "unchanged", "n_chunks": row["n_chunks"]}

        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return {"path": rel, "status": "error", "error": str(e)}

        content_hash = self._hash(raw)
        if row and not force and row["content_hash"] == content_hash:
            # Content identical, only mtime moved: refresh bookkeeping, no re-embed.
            self.conn.execute("update files set mtime=?, indexed_at=? where path=?",
                              (st.st_mtime, time.time(), str(p)))
            self.conn.commit()
            return {"path": rel, "status": "touched", "n_chunks": row["n_chunks"]}

        chunks = chunking.chunk_markdown(raw)
        self._delete_file(str(p))

        new_ids: list[int] = []
        for ch in chunks:
            cur = self.conn.execute(
                "insert into chunks(source,path,rel,heading,text,mtime,content_hash) "
                "values (?,?,?,?,?,?,?)",
                (source, str(p), rel, ch.heading, ch.text, st.st_mtime, content_hash))
            cid = cur.lastrowid
            new_ids.append(cid)
            # FTS holds a jieba-segmented copy; `chunks.text` keeps the original.
            self.conn.execute("insert into chunks_fts(rowid, text) values (?,?)",
                              (cid, segment(ch.text)))

        self.conn.execute(
            "insert or replace into files(path,source,mtime,size,content_hash,n_chunks,indexed_at)"
            " values (?,?,?,?,?,?,?)",
            (str(p), source, st.st_mtime, st.st_size, content_hash, len(chunks), time.time()))
        self.conn.commit()

        return {"path": rel, "status": "indexed", "n_chunks": len(chunks),
                "chunk_ids": new_ids, "source": source}

    def _delete_file(self, path: str) -> None:
        """Remove a file's chunks, vectors, FTS rows and bookkeeping.

        The ``files`` row is deleted unconditionally, not only when chunks existed. A file
        whose content was too short to chunk still gets a ``files`` row written at index
        time, so gating the cleanup on ``if not ids: return`` left permanent phantom
        entries: ``stats()`` reported more files than the corpus held, and an incremental
        sync's "what does the index already have" map kept treating the deleted path as
        present. The missing ``commit`` had the same effect for anything that read the
        database from another connection.
        """
        ids = [r[0] for r in self.conn.execute("select id from chunks where path=?", (path,))]
        if ids:
            qmarks = ",".join("?" * len(ids))
            self.conn.execute(f"delete from chunks_fts where rowid in ({qmarks})", ids)
            if self.vec_ready:
                self.conn.execute(f"delete from chunk_vec where rowid in ({qmarks})", ids)
            self.conn.execute(f"delete from chunks where id in ({qmarks})", ids)
        self.conn.execute("delete from files where path=?", (path,))
        self.conn.commit()

    def embed_missing(self, limit: int | None = None, verbose: bool = False) -> int:
        """Embed every chunk that has no vector yet. Returns how many were embedded."""
        if not self.vec_ready:
            return 0
        import sqlite_vec

        rows = self.conn.execute(
            "select id, text from chunks where id not in (select rowid from chunk_vec) "
            "order by id" + (f" limit {int(limit)}" if limit else "")
        ).fetchall()
        if not rows:
            return 0

        emb = self.get_embedder()
        n = 0
        batch = 16
        for i in range(0, len(rows), batch):
            part = rows[i:i + batch]
            vecs = emb.encode([r["text"] for r in part])
            for r, v in zip(part, vecs):
                self.conn.execute("insert into chunk_vec(rowid, embedding) values (?,?)",
                                  (r["id"], sqlite_vec.serialize_float32(v)))
            n += len(part)
            self.conn.commit()
            if verbose:
                print(f"    embedded {n}/{len(rows)}")
        return n

    def rebuild(self, verbose: bool = True) -> dict:
        """Drop all index state and re-index everything from disk.

        Also rebuilds the vec0 table when the embedder (or its dimension) changed:
        vector tables are created with a fixed ``float[N]`` width, so switching from a
        1024-dim backend to a 512-dim one must recreate the table rather than reuse it.
        """
        self._reset_vectors()
        self.conn.execute("delete from chunks_fts")
        self.conn.execute("delete from chunks")
        self.conn.execute("delete from files")
        self.conn.commit()
        return self.index_all(verbose=verbose)

    def _reset_vectors(self) -> None:
        """Drop and recreate the vector table for the CURRENT backend/dimension."""
        if not self.with_vectors:
            return
        have_vec = self.conn.execute(
            "select name from sqlite_master where type='table' and name='chunk_vec'"
        ).fetchone()
        if have_vec:
            self.conn.execute("drop table if exists chunk_vec")
        self.conn.execute("delete from meta where key='embedder'")
        self.conn.commit()
        self.backend_mismatch = None
        if self._load_vec():
            self.conn.execute(
                f"create virtual table if not exists chunk_vec using vec0("
                f"embedding float[{self.dim}])"
            )
            self.conn.execute("insert or replace into meta(key,value) values ('dim',?)",
                              (str(self.dim),))
            self.conn.execute("insert or replace into meta(key,value) values ('embedder',?)",
                              (self.get_embedder().identity,))
            self.conn.commit()

    def index_all(self, force: bool = False, verbose: bool = True) -> dict:
        t0 = time.time()
        stats = defaultdict(int)
        for source, p in self.iter_files():
            r = self.index_file(source, p, force=force)
            stats[r["status"]] += 1
            if verbose and r["status"] == "error":
                print(f"    ! {r['path']}: {r.get('error')}")
        embedded = 0
        if self.vec_ready:
            embedded = self.embed_missing(verbose=verbose)
        stats["embedded"] = embedded
        stats["seconds"] = round(time.time() - t0, 2)
        return dict(stats)

    # ── retrieval ────────────────────────────────────────────────────────

    def _bm25(self, query: str, limit: int) -> list[tuple[int, float]]:
        match = build_fts_query(query)
        if not match:
            return []
        try:
            rows = self.conn.execute(
                "select rowid, bm25(chunks_fts) as score from chunks_fts "
                "where chunks_fts match ? order by score limit ?", (match, limit)).fetchall()
        except sqlite3.OperationalError:
            # Malformed MATCH from odd input: degrade to no lexical hits.
            return []
        # bm25() returns negative values; more negative = better. Convert to positive.
        return [(r["rowid"], -float(r["score"])) for r in rows]

    def _knn(self, query: str, limit: int) -> list[tuple[int, float]]:
        if not self.vec_ready:
            return []
        if getattr(self, "backend_mismatch", None):
            # Vectors came from a different embedder; comparing would be meaningless.
            return []
        import sqlite_vec

        try:
            qv = self.get_embedder().encode_one(query, is_query=True)
        except Exception:
            return []
        if not qv:
            return []
        rows = self.conn.execute(
            "select rowid, distance from chunk_vec where embedding match ? "
            "and k = ? order by distance", (sqlite_vec.serialize_float32(qv), limit)).fetchall()
        # vec0's `distance` for float[] vectors is the *squared* L2 distance, so
        # cosine = 1 - d/2 for unit vectors. Reporting `1 - d` would push every real
        # match negative (a 0.5-cosine pair shows as -0.75). Ranking is unaffected,
        # but the score is shown in the UI, so get it right.
        return [(r["rowid"], 1.0 - float(r["distance"]) / 2.0) for r in rows]

    def search(
        self,
        query: str,
        top_k: int = 8,
        source: str | None = None,
        mode: str = "hybrid",
        candidates: int = CANDIDATES,
    ) -> list[dict]:
        """Hybrid BM25 + KNN search, RRF-fused, grouped by file.

        Returns the legacy ``search_kb`` shape::

            [{title, label, path, rel, snippets, score}, ...]

        ``mode`` is one of ``hybrid`` (default), ``bm25``, ``vector``.

        Thread-safe: takes ``self.lock`` for the whole call, because both the SQLite
        connection and the ONNX embedder session are shared and neither is reentrant.
        """
        with self.lock:
            return self._search_locked(query, top_k=top_k, source=source, mode=mode,
                                       candidates=candidates)

    def _search_locked(
        self,
        query: str,
        top_k: int = 8,
        source: str | None = None,
        mode: str = "hybrid",
        candidates: int = CANDIDATES,
    ) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []

        lexical: list[tuple[int, float]] = []
        dense: list[tuple[int, float]] = []
        # 带 source 过滤时把候选池放大：两条臂各自只回 candidates 条，如果这些条目大多来自
        # 别的来源，过滤之后就会"少于 top_k"甚至一条都没有（实测混库里按项目过滤 0/18）。
        # 正确的做法是让过滤发生在**排名之前**，而不是在成组阶段把结果丢掉。
        pool = candidates
        if source:
            pool = max(candidates * 4, 200)
        if mode in ("hybrid", "bm25"):
            lexical = self._bm25(query, pool)
        if mode in ("hybrid", "vector"):
            dense = self._knn(query, pool)
        if not lexical and not dense:
            return []

        # best rank per file in each arm (batched lookup, no per-id queries)
        best_lex: dict[str, int] = {}
        best_dense: dict[str, int] = {}
        chunk_rank: dict[int, tuple[int, int]] = {}

        ids = {cid for cid, _ in lexical} | {cid for cid, _ in dense}
        id_to_path: dict[int, str] = {}
        if ids:
            id_list = list(ids)
            for start in range(0, len(id_list), 500):
                part = id_list[start:start + 500]
                qm = ",".join("?" * len(part))
                for r in self.conn.execute(
                        f"select id, path, source from chunks where id in ({qm})", part):
                    if source and r["source"] != source:
                        continue          # 过滤在排名之前：RRF 只在匹配的来源内部排名
                    id_to_path[r["id"]] = r["path"]

        for rank, (cid, _) in enumerate(lexical):
            p = id_to_path.get(cid)
            if p is None:
                continue
            best_lex.setdefault(p, rank)
            r0, r1 = chunk_rank.get(cid, (10 ** 6, 10 ** 6))
            chunk_rank[cid] = (rank, r1)
        for rank, (cid, _) in enumerate(dense):
            p = id_to_path.get(cid)
            if p is None:
                continue
            best_dense.setdefault(p, rank)
            r0, r1 = chunk_rank.get(cid, (10 ** 6, 10 ** 6))
            chunk_rank[cid] = (r0, rank)

        file_score: dict[str, float] = defaultdict(float)
        for path, rank in best_lex.items():
            file_score[path] += 1.0 / (RRF_K + rank + 1)
        for path, rank in best_dense.items():
            file_score[path] += 1.0 / (RRF_K + rank + 1)

        if not file_score:
            return []

        paths = list(file_score)
        qmarks = ",".join("?" * len(paths))
        all_rows = self.conn.execute(
            f"select * from chunks where path in ({qmarks})", paths).fetchall()

        by_file: dict[str, dict] = {}
        for r in all_rows:
            if source and r["source"] != source:
                continue
            entry = by_file.setdefault(r["path"], {
                "title": self._title_for(r),
                "label": r["source"],
                "path": r["path"],
                "rel": r["rel"],
                "snippets": [],
                "score": 0.0,
                "_chunks": [],
            })
            entry["_chunks"].append(r)

        results: list[dict] = []
        # by_file 的键来自 `path in paths`，而 paths 就是 file_score 的键，所以这里不可能
        # 出现"文件没分数"的情况（原来有一句 `if path not in file_score: continue`，
        # 永远走不到）。
        for path, entry in by_file.items():
            entry["score"] = file_score[path]
            # Order this file's chunks by how well each arm ranked them.
            entry["_chunks"].sort(key=lambda r: chunk_rank.get(r["id"], (10 ** 6, 10 ** 6)))
            top = entry["_chunks"][:3]
            entry["snippets"] = [self._clean(r["text"]) for r in top]
            entry["chunks"] = [
                {"heading": r["heading"], "text": r["text"]}
                for r in top
            ]
            entry["heading"] = top[0]["heading"] if top else ""
            del entry["_chunks"]
            results.append(entry)

        results.sort(key=lambda e: -e["score"])
        results = results[:top_k]

        # Normalize scores to 0..1 for the frontend (which only sorts by them).
        if results:
            top = results[0]["score"] or 1.0
            for e in results:
                e["score"] = round(e["score"] / top, 4)
        return results

    def warm(self) -> float:
        """把嵌入模型加载起来并跑一次，返回毫秒数。供服务启动时预热。

        为什么不能只靠 ``search("预热")``：`backend_mismatch` 时 `_knn` 会在调用嵌入器
        **之前**就返回空，于是"预热"根本没碰到 ONNX 会话，`warm_ms` 只反映一次纯词法查询
        ——用户第一次提问仍然要等模型加载。这里显式 touch。
        """
        t0 = time.time()
        try:
            self.get_embedder().encode_one("预热", is_query=True)
        except Exception:  # noqa: BLE001 - 预热失败不该影响服务启动
            pass
        return (time.time() - t0) * 1000

    def _title_for(self, row: sqlite3.Row) -> str:
        import re

        m = re.search(r"^#{1,6}\s+(.+)$", row["text"], re.MULTILINE)
        if m:
            return m.group(1).strip()
        stem = Path(row["rel"]).stem.replace("-", " ")
        if row["heading"]:
            return f"{stem} — {row['heading'].split(' > ')[-1]}"
        return stem

    @staticmethod
    def _clean(text: str) -> str:
        import re

        s = re.sub(r"[*#`|>]", " ", text)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    # ── introspection ────────────────────────────────────────────────────

    def stats(self) -> dict:
        c = self.conn
        n_chunks = c.execute("select count(*) from chunks").fetchone()[0]
        n_files = c.execute("select count(*) from files").fetchone()[0]
        n_vec = 0
        if self.vec_ready:
            try:
                n_vec = c.execute("select count(*) from chunk_vec").fetchone()[0]
            except sqlite3.OperationalError:
                n_vec = 0
        by_source = {r["source"]: r["n"] for r in c.execute(
            "select source, count(*) n from chunks group by source")}
        return {
            "db": str(self.db_path),
            "kb_dir": str(self.kb_dir),
            "schema_version": SCHEMA_VERSION,
            "dim": self.dim,
            "backend": self.backend,
            "embedder": self.get_embedder().identity,
            "embedder_stored": self._stored_backend(),
            "backend_mismatch": self.backend_mismatch,
            "tokenizer": self._live_fts_tokenizer() or "(none)",
            "tokenizer_config": self.tokenizer,
            "vectors_enabled": self.vec_ready,
            "files": n_files,
            "chunks": n_chunks,
            "vectors": n_vec,
            "chunks_by_source": by_source,
            "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# Module-level convenience (lazy singleton, mirrors old search_kb signature)
# ══════════════════════════════════════════════════════════════════════

_default_index: RagIndex | None = None


def get_index(**kwargs) -> RagIndex:
    global _default_index
    if _default_index is None:
        _default_index = RagIndex(**kwargs)
    return _default_index


def search(query: str, top_k: int = 8, **kwargs) -> list[dict]:
    """Drop-in replacement for ``wiki_server.search_kb``."""
    return get_index().search(query, top_k=top_k, **kwargs)


def index_file(source: str, path) -> dict:
    return get_index().index_file(source, path)


def rebuild() -> dict:
    return get_index().rebuild()
