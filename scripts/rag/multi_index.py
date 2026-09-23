"""Search several knowledge bases as one, without pretending their scores are comparable.

Why more than one index
-----------------------
A project-based assistant needs two kinds of knowledge with opposite lifecycles:

* **public** — contracts, bids, qualification certificates, regulations. These change
  rarely, are shared across projects, and are rebuilt almost never.
* **project** — meeting minutes, progress notes, decisions. These grow with every meeting.

Measured on this machine, putting them in one index does **not** hurt retrieval quality:
the same six public queries scored 3/6 whether the meeting minutes were present or not,
and the correct public document still ranked first. So separation is not bought with
accuracy. It is bought with lifecycle: a minutes index is rebuilt after every meeting, and
with them mixed in, each rebuild re-embeds the entire stable corpus for no reason. Keeping
the project index inside the project folder also makes that folder self-contained.

Scores across indexes are not comparable -- and not only across models
--------------------------------------------------------------------
``RagIndex.search`` normalises its own results before returning them::

    top = results[0]["score"] or 1.0
    e["score"] = round(e["score"] / top, 4)

so **every index's best hit comes back as exactly 1.0**. Merging by score therefore ties
the top hit of every index at 1.0 and falls back to insertion order -- which put the public
corpus ahead of the project corpus on every meeting question, answering "what did we agree
in this meeting" with a regulation. Measured: only **2 of 8** queries had the right
document first, and **7 of 8** tied with an unrelated one.

The lesson generalises: a per-index snapshot normalisation makes scores comparable *within*
an index and meaningless *between* indexes, even when both use the same embedder. So this
class always fuses by **reciprocal rank**, which needs only the ordering from each index to
be meaningful. ``score_kind`` is set to ``"rrf"`` on every hit so no caller can mistake a
fusion weight for a similarity.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from rag.rag_core import RagIndex  # noqa: E402

# No per-corpus prior.
#
# A tie-breaking bump for the project corpus was tried and removed. It fixed the symptom
# (rank fusion gives every index's rank-1 hit an identical weight, so the tops of two
# corpora always tie 0.016393/0.016393 and the order collapses to insertion order -- with
# the public corpus first, 0 of 12 project questions had their answer first) but broke
# something worse: with a prior of 0.0006, **0 of 4** questions answerable only from the
# public corpus still surfaced a public document.
#
# The reason is that the two corpora answer different kinds of question -- "what did we
# promise in this meeting" versus "what does the regulation require" -- and no single scalar
# can rank across them without burying one. So the merge stays pure and neutral, and the
# consumer distinguishes the corpora explicitly: results carry ``kb``, and the assistant
# reports them as separate groups (see ``Assistant.process``). Two honest lists beat one
# list with a hidden preference.
RRF_K = 60


class MultiIndex:
    """A drop-in stand-in for :class:`RagIndex` that queries several indexes.

    The callers in this project only need ``search``, ``stats``, ``db_path`` and
    ``kb_dir``, so the surface here is deliberately that small. Anything richer would
    invite callers to depend on behaviour that differs between the single- and
    multi-index cases.
    """

    def __init__(self, specs: list[dict]) -> None:
        """``specs`` is a list of ``{"db": path, "kb": dir, "name": "公共"}``.

        ``name`` is surfaced on every hit as ``kb`` so the UI can show where an answer
        came from — with two corpora in play, "which库 说的" is part of the answer.

        There is deliberately no notion of a preferred corpus here. A tie-breaking prior
        for the project index was tried and removed (see ``RRF_K`` above): it fixed the
        ordering between corpora but buried the public one. Callers that want the groups
        kept apart should use :meth:`search_by_index`.
        """
        self.specs = list(specs)
        self.entries: list[tuple[dict, RagIndex]] = []
        self.errors: list[str] = []

        for spec in self.specs:
            db = Path(spec["db"])
            if not db.exists():
                self.errors.append(f"{spec.get('name') or db.name}: 索引不存在 {db}")
                continue
            try:
                idx = RagIndex(db_path=str(db), kb_dir=spec.get("kb") or str(db.parent))
            except Exception as e:  # noqa: BLE001 - one bad index must not kill the rest
                self.errors.append(f"{spec.get('name') or db.name}: "
                                   f"{type(e).__name__}: {e}")
                continue
            self.entries.append((spec, idx))

        if not self.entries:
            raise RuntimeError("没有任何可用的索引: " + "; ".join(self.errors))

        # Same backend means one score scale; different dimensions mean fusion is not
        # even meaningful (a 512-d and a 1024-d vector cannot be compared at all).
        self.dims = {idx.dim for _, idx in self.entries}
        self.backends = {idx.backend for _, idx in self.entries}
        self.mixed_backends = len(self.backends) > 1
        self.dim_conflict = len(self.dims) > 1
        for _, idx in self.entries:
            if idx.backend_mismatch:
                # The stored vectors came from a different model than the one loaded now:
                # the index is stale and its scores are meaningless until rebuilt.
                self.errors.append(
                    f"{idx.db_path.name}: 索引是用 {idx.backend_mismatch} 建的，"
                    f"当前加载 {idx.backend} —— 需要重建")

    # ── the one method callers actually need ────────────────────────────

    def warm(self) -> float:
        """把每个子索引的嵌入模型都预热一遍（Assistant 启动时调）。

        单库时 Assistant 直接拿到 RagIndex，多库时拿到 MultiIndex —— 两边都要有 warm()，
        否则"启动即预热"只在单库模式下成立。
        """
        total = 0.0
        for _spec, idx in self.entries:
            fn = getattr(idx, "warm", None)
            if callable(fn):
                total += float(fn() or 0.0)
        return total

    def search(self, query: str, top_k: int = 3, **kw) -> list[dict]:
        if not self.entries:
            return []
        if len(self.entries) == 1:
            # One index: its own scores are already meaningful (normalised within it), so
            # they are passed through untouched.
            return self._search_one(self.entries[0], query, top_k, **kw)
        return self._search_rrf(query, top_k, **kw)

    def search_by_index(self, query: str, top_k: int = 3, **kw) -> dict[str, list[dict]]:
        """Search each index separately and keep the results grouped by corpus.

        This is the honest shape for a multi-corpus assistant. Rank fusion produces one
        list whose order between corpora is arbitrary (fusion weights for two rank-1 hits
        are identical), and a prior that forces an order was measured to bury the public
        corpus entirely. Presenting the groups side by side avoids the choice: each hit is
        ranked against its own corpus, and the caller decides what to show.
        """
        out: dict[str, list[dict]] = {}
        for spec, idx in self.entries:
            name = spec.get("name") or Path(str(spec["db"])).stem
            try:
                out[name] = [self._tag(h, spec) for h in idx.search(query, top_k=top_k, **kw)]
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{name}: 检索失败 {type(e).__name__}")
                out[name] = []
        return out

    def _search_one(self, entry, query: str, top_k: int, **kw) -> list[dict]:
        spec, idx = entry
        hits = idx.search(query, top_k=top_k, **kw)
        return [self._tag(h, spec) for h in hits]

    def _search_rrf(self, query: str, top_k: int, **kw) -> list[dict]:
        """Fuse rankings, not scores.

        Scores cannot be used: ``RagIndex.search`` divides each result by that index's own
        best score, so every index's top hit is exactly 1.0 and a score-based merge ties
        them all and falls back to insertion order. Rankings carry the information that
        survives the normalisation.
        """
        scores: dict[str, float] = {}
        best: dict[str, dict] = {}
        for spec, idx in self.entries:
            try:
                hits = idx.search(query, top_k=max(top_k, 5), **kw)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{spec.get('name')}: 检索失败 {type(e).__name__}")
                continue
            for rank, h in enumerate(hits, 1):
                key = f"{spec.get('name')}::{h.get('title')}::{h.get('label')}"
                scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
                if key not in best:
                    best[key] = self._tag(h, spec)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        out = []
        for key, sc in ranked[:top_k]:
            h = dict(best[key])
            # The number is a fusion weight, not a similarity -- labelled so no caller can
            # present it to a user as "相关度".
            h["score"] = round(sc, 6)
            h["score_kind"] = "rrf"
            out.append(h)
        return out

    @staticmethod
    def _tag(hit: dict, spec: dict) -> dict:
        h = dict(hit)
        h["kb"] = spec.get("name") or ""
        h["kb_backend"] = spec.get("backend") or ""
        return h

    # ── surface the rest of the code depends on ─────────────────────────

    @property
    def db_path(self) -> str:
        return " + ".join(str(s["db"]) for s in self.specs)

    @property
    def kb_dir(self) -> str:
        return " + ".join(str(s.get("kb") or "") for s in self.specs)

    def stats(self) -> dict:
        per = []
        for spec, idx in self.entries:
            st = idx.stats()
            per.append({"name": spec.get("name") or Path(str(spec["db"])).stem,
                        "files": st.get("files"), "chunks": st.get("chunks"),
                        "db": str(spec["db"])})
        return {
            "db": self.db_path,
            "kb_dir": self.kb_dir,
            "files": sum(p["files"] or 0 for p in per),
            "chunks": sum(p["chunks"] or 0 for p in per),
            "embedder": ("混合（按排名融合）" if self.mixed_backends
                         else (self.entries[0][1].stats().get("embedder") if self.entries else "")),
            "mixed_backends": self.mixed_backends,
            "dim_conflict": self.dim_conflict,
            "indexes": per,
            "errors": self.errors,
        }

    def close(self) -> None:
        for _, idx in self.entries:
            try:
                idx.close()
            except Exception:  # noqa: BLE001
                pass
