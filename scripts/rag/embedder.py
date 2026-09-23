"""Embedding backends for the plaud RAG module — ONNX Runtime, INT8 by default.

Two backends, chosen by corpus language coverage:

  ``bge-small-zh`` (default) — BAAI/bge-small-zh-v1.5, Chinese text-only, 512-dim.
      ONNX INT8 is **23.9 MB**, runs on CPU, has no GPU/VRAM cost and no cold-start
      penalty. This is the real-time path: ASR transcripts are Chinese plain text.

  ``bge-m3`` — BAAI/bge-m3, multilingual 100+, 1024-dim, long-context.
      ONNX INT8 is 568 MB. Use when the corpus mixes languages or needs
      cross-lingual retrieval (a Chinese question against an English contract).

Why ONNX rather than GGUF: an embedding model is not a generative decoder, so
llama.cpp/GGUF (which only runs decoder graphs) cannot host it. ONNX Runtime is the
supported quantisation route, and the INT8 artifacts above are published.

Why not WeMM-Embedding-2B (the multimodal model tried first):
  * separation between present/absent queries  +0.0401  vs  +0.0852 for bge-small-zh
  * 5.09 GB with **no quantised release at all**, vs 23.9 MB for INT8 bge-small-zh
  * its one unique ability (embedding images directly) is unused here, because
    documents are normalised to Markdown upstream before indexing.

Quantisation was verified, not assumed: INT8 vs FP32 on the real corpus gave
per-document cosine ~0.96 and **identical top-1 ranking on 8/8 probes**. The vectors
drift slightly; the *ordering* — which is what retrieval depends on — does not.

**Vector spaces are not interchangeable.** Vectors from different models, or even
different precisions of the same family with different weights, live in unrelated
spaces; mixing them in one vec0 table makes KNN meaningless. The index therefore
records which embedder produced its vectors (``meta.embedder``) and disables the
vector arm on a mismatch instead of returning nonsense. Matryoshka truncation is the
one exception: it is exact *within* one model (measured cos = 1.000000).
"""

from __future__ import annotations

import os
from pathlib import Path

# ── backend registry ────────────────────────────────────────────────────

BACKENDS: dict[str, dict] = {
    "bge-small-zh": {
        "hf_repo": "Xenova/bge-small-zh-v1.5",
        "dim": 512,
        "langs": "zh",
        # bge-zh expects this instruction on the QUERY side only; documents are
        # embedded bare. Omitting it measurably costs recall.
        "query_prefix": "为这个句子生成表示以用于检索相关文章：",
        "approx_mb_int8": 24,
        "note": "中文纯文本；实时路径默认，CPU 上毫秒级",
    },
    "bge-m3": {
        "hf_repo": "Xenova/bge-m3",
        "dim": 1024,
        "langs": "100+",
        "query_prefix": "",
        "approx_mb_int8": 569,
        "note": "多语言 + 跨语言检索；语料含英文时改用这个",
    },
}
# pooling：两个后端都是 CLS 池化（取 hidden[:, 0]），在 encode() 里硬编码。
# 以前注册表里各写了一行 `"pooling": "cls"`，但没有任何代码读它 —— 已删掉这行
# "看起来可配置"的元数据，免得下次有人改了它却发现没生效。

DEFAULT_BACKEND = "bge-small-zh"

# Preference order for the ONNX artifact actually downloaded.
#   int8  — 4x smaller than fp32, top-1 ranking identical on our probes
#   fp32  — fallback when a repo publishes only the full model
PRECISION_ORDER = ("int8", "fp16", "fp32")

_ONNX_FILENAMES = {
    "int8": "onnx/model_int8.onnx",
    "fp16": "onnx/model_fp16.onnx",
    "fp32": "onnx/model.onnx",
}


def _hf_cache_root() -> Path:
    return Path(
        os.environ.get("HF_HUB_CACHE")
        or (Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")
    )


def _resolve_local_snapshot(repo_id: str) -> str | None:
    """Find a snapshot in the HF cache that actually contains ONNX weights.

    ``snapshot_download(local_files_only=True)`` refuses a snapshot missing any
    metadata file (it raised ``IncompleteSnapshotError`` on a repo whose weights were
    fully present), and on this box huggingface.co is DNS-hijacked so an online call
    retries five times before failing. Scanning the cache avoids both.

    The scan is deliberately a *targeted* glob on ``<snapshot>/onnx/*.onnx`` rather
    than ``rglob("*.onnx")``: the repo directory also holds ``blobs/`` with multi-GB
    weights for other models (WeMM 5 GB, bge-m3 2.3 GB), and walking that tree made a
    24 MB model take 8.5 s to "resolve".
    """
    repo_dir = _hf_cache_root() / ("models--" + repo_id.replace("/", "--"))
    snaps = repo_dir / "snapshots"
    if not snaps.is_dir():
        return None
    for cand in sorted(snaps.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if cand.is_dir() and any(cand.glob("onnx/*.onnx")):
            return str(cand)
    return None


class Embedder:
    """Unified text embedder over ONNX Runtime backends.

    Returns L2-normalized vectors of ``spec['dim']`` length.
    """

    def __init__(
        self,
        backend: str = DEFAULT_BACKEND,
        dim: int | None = None,
        device: str | None = None,       # accepted for API compatibility; ONNX runs on CPU
        local_dir: str | None = None,
        batch_size: int = 16,
        max_length: int = 512,
        prefer: str | None = None,       # force a precision, e.g. "fp32"
    ) -> None:
        if backend not in BACKENDS:
            raise ValueError(f"unknown backend {backend!r}; choose from {list(BACKENDS)}")
        spec = BACKENDS[backend]
        self.backend = backend
        self.spec = spec
        self.model_id = spec["hf_repo"]
        self.dim = dim or spec["dim"]
        self.batch_size = batch_size
        self.max_length = max_length
        self._local_dir = local_dir
        self._prefer = prefer
        self._tok = None
        self._sess = None
        self._in_names: set[str] = set()
        self.precision: str | None = None

    # ── identity / lifecycle ─────────────────────────────────────────────

    @property
    def identity(self) -> str:
        """Stable id recorded in the index so a mismatch can be detected."""
        return f"{self.backend}:{self.model_id}@{self.dim}"

    @property
    def loaded(self) -> bool:
        return self._sess is not None

    def _download(self) -> tuple[str, str]:
        """Return (onnx_path, precision), resolving from cache first."""
        order = (self._prefer,) + tuple(p for p in PRECISION_ORDER if p != self._prefer) \
            if self._prefer else PRECISION_ORDER
        names = [(_ONNX_FILENAMES[p], p) for p in order if p in _ONNX_FILENAMES]

        if self._local_dir:
            base = Path(self._local_dir)
            for rel, prec in names:
                p = base / rel
                if p.is_file():
                    return str(p), prec
            raise FileNotFoundError(f"no ONNX model under {base}")

        snap = _resolve_local_snapshot(self.model_id)
        if snap:
            for rel, prec in names:
                p = Path(snap) / rel
                if p.is_file():
                    return str(p), prec

        # Not cached yet: fetch (fresh install). Uses HF_ENDPOINT / mirror if set.
        from huggingface_hub import hf_hub_download

        last = None
        for rel, prec in names:
            try:
                return hf_hub_download(self.model_id, rel), prec
            except Exception as e:      # try the next precision
                last = e
        raise RuntimeError(f"could not obtain an ONNX model for {self.model_id}: {last}")

    def load(self) -> None:
        """Load tokenizer + ONNX session. Idempotent."""
        if self._sess is not None:
            return

        import onnxruntime as ort
        from tokenizers import Tokenizer

        path, prec = self._download()
        self.precision = prec

        snap = self._local_dir or _resolve_local_snapshot(self.model_id)
        if not snap:
            from huggingface_hub import hf_hub_download

            snap = str(Path(hf_hub_download(self.model_id, "tokenizer.json")).parent)

        # `tokenizers` reads tokenizer.json directly (10 ms) and brings truncation,
        # padding and attention masks with it. Going through
        # transformers.AutoTokenizer cost ~4 s of import time -- measured: importing
        # transformers spends 3.9 s inside importlib.metadata.packages_distributions()
        # enumerating every installed distribution, which has nothing to do with the
        # model. The ONNX path needs no torch and no transformers at all.
        tok_path = Path(snap) / "tokenizer.json"
        if not tok_path.is_file():
            raise FileNotFoundError(f"tokenizer.json not found under {snap}")
        self._tok = Tokenizer.from_file(str(tok_path))
        self._tok.enable_truncation(max_length=self.max_length)
        self._tok.enable_padding()          # pads to the longest in the batch

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
        self._sess = ort.InferenceSession(path, sess_options=so,
                                          providers=["CPUExecutionProvider"])
        self._in_names = {i.name for i in self._sess.get_inputs()}

    def unload(self) -> None:
        self._sess = None

    # ── encoding ─────────────────────────────────────────────────────────

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """Embed ``texts`` -> list of ``self.dim``-length unit vectors."""
        if not texts:
            return []
        import numpy as np

        self.load()
        prefix = self.spec.get("query_prefix") or ""
        if is_query and prefix:
            texts = [prefix + t for t in texts]

        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            encs = self._tok.encode_batch(batch)
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            feed = {}
            if "input_ids" in self._in_names:
                feed["input_ids"] = ids
            if "attention_mask" in self._in_names:
                feed["attention_mask"] = mask
            if "token_type_ids" in self._in_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self._sess.run(None, feed)[0]        # (batch, seq, dim)
            v = hidden[:, 0]                              # CLS pooling
            n = np.linalg.norm(v, axis=1, keepdims=True)
            v = v / np.maximum(n, 1e-9)
            if self.dim < v.shape[1]:
                v = v[:, : self.dim]
                v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
            out.extend(v.tolist())
        return out

    def encode_one(self, text: str, is_query: bool = False) -> list[float]:
        vecs = self.encode([text], is_query=is_query)
        return vecs[0] if vecs else []


_cache: dict[str, Embedder] = {}


def get_embedder(backend: str = DEFAULT_BACKEND, dim: int | None = None, **kwargs) -> Embedder:
    """Process-wide cache so a model is loaded at most once per backend."""
    key = f"{backend}:{dim or BACKENDS.get(backend, {}).get('dim')}"
    if key not in _cache:
        _cache[key] = Embedder(backend=backend, dim=dim, **kwargs)
    return _cache[key]
