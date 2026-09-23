"""Meeting-assistant core: one ASR segment in -> highlights + retrieval out.

The loop this implements (measured earlier on a 590-chunk real corpus):

    ASR segment (a few sentences)
      -> LLM extracts keywords for highlighting        ~0.2 s
      -> segment-level RAG retrieval (hybrid, RRF)      ~16 ms
      -> per-keyword retrieval, computed on demand      ~11 ms each
      -> hotspots + result cards for the UI

Design notes worth keeping:

* **The segment, not the keyword, is the retrieval unit.** Measured: asking for
  "审查讯问时我方人员不得少于几人" returns the wrong chapter when searched as the
  keyword "审查讯问" (generic terms hit other handbooks), but the right chapter when
  the whole segment is used. Keywords are therefore used for *highlighting and
  drill-down*, while the main answer card comes from the segment.
* **Keyword retrieval is deferred.** Segment cards are built eagerly (one search);
  per-keyword searches run only when the caller asks (`refine`), so a segment with six
  hotspots costs one search, not seven.
* Everything is kept warm in one process: the RAG index and the embedder are loaded
  once at startup. A cold process costs ~1 s here (ONNX int8), but repeated process
  spawns would still cost the LLM connection and the index open each time.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PARENT = _HERE.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from rag.rag_core import RagIndex  # noqa: E402
from asst.keywords import extract_keywords  # noqa: E402


@dataclass
class Hotspot:
    """One highlighted keyword plus (optionally) its own retrieval results."""

    term: str
    count: int = 0
    results: list[dict] = field(default_factory=list)
    elapsed_ms: float = 0.0
    refined: bool = False

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "count": self.count,
            "results": self.results,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "refined": self.refined,
        }


class Assistant:
    """Holds the warm resources and turns segments into UI payloads."""

    def __init__(
        self,
        db_path: str | Path = "",
        kb_dir: str | Path = "",
        top_k: int = 3,
        max_keywords: int = 6,
        unused_llm: bool = False,
        index=None,
    ) -> None:
        """``index`` lets the caller supply an already-built index.

        A meeting assistant may search several knowledge bases at once (a shared public
        corpus plus the project's own), and that combination is assembled outside this
        class. Accepting the object rather than a path keeps the assistant from having to
        know how many corpora exist -- it only needs something with ``search`` and
        ``stats``.
        """
        t0 = time.time()
        if index is not None:
            self.index = index
        elif db_path:
            self.index = RagIndex(db_path=db_path, kb_dir=kb_dir)
        else:
            raise ValueError("either index= or db_path= is required")
        self.load_ms = (time.time() - t0) * 1000
        self.top_k = top_k
        self.max_keywords = max_keywords
        self.unused_llm = unused_llm
        # touch the embedder so the first request is not the one paying for it
        t1 = time.time()
        self.index.search("预热", top_k=1)
        # 有些索引在"向量臂被停用"时（backend_mismatch / 纯词法库）不会因为上面那次检索
        # 而加载嵌入模型，所以再显式 warm 一下。MultiIndex 会把这次调用转给每个子索引。
        warm = getattr(self.index, "warm", None)
        if callable(warm):
            warm()
        self.warm_ms = (time.time() - t1) * 1000
        self.stats = self.index.stats()

    # ── keyword extraction ───────────────────────────────────────────────

    def extract_keywords(self, segment: str) -> tuple[list[str], float]:
        """Return (keywords, elapsed_ms). Delegates to the standalone extractor."""
        return extract_keywords(segment, unused_llm=self.unused_llm,
                                max_keywords=self.max_keywords)

    # ── retrieval ────────────────────────────────────────────────────────

    def _search(self, query: str) -> list[dict]:
        """Search, then surface the passage that actually matched.

        A file yields several chunks and only the top few are returned, so the chunk
        whose opening gets displayed is often *not* the one containing the answer:
        for "不得少于两人" the correct chunk ranked #1, yet the card showed the
        neighbouring chunk's opening ("的敌对、恐惧心理…") and the constraint itself
        ("…不得少于 2 人，讯问过程应当录音录像") never appeared. A hotspot whose card
        hides the answer is useless, so pick the passage by term overlap instead.
        """
        hits = self.index.search(query, top_k=self.top_k)
        terms = self._query_terms(query)
        out = []
        for h in hits:
            chunks = h.get("chunks") or []
            best, best_score, best_at = "", -1, -1
            for c in chunks:
                # Normalise first, then locate terms, so the offset we keep is valid
                # for the exact string we later slice (mixing raw and cleaned offsets
                # shifts the window by however many newlines were collapsed).
                clean = re.sub(r"[ \t]+", " ", c.get("text", "")).replace("\n", " ")
                score, at = 0, -1
                for t in terms:
                    i = clean.find(t)
                    if i >= 0:
                        score += 1
                        if at < 0 or i < at:
                            at = i
                if score > best_score:
                    best, best_score, best_at = clean, score, at
            out.append({
                "title": h["title"],
                "label": h["label"],
                "rel": h["rel"],
                "score": h["score"],
                "heading": h.get("heading", ""),
                "snippet": self._passage(best, best_at),
                "matched_terms": max(best_score, 0),
            })
        return out

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        """Content-bearing terms of a query, longest first (for overlap scoring)."""
        import jieba

        stop = {"我们", "他们", "这个", "那个", "就是", "什么", "现在", "可以",
                "应该", "需要", "进行", "如果", "因为", "所以", "但是", "然后",
                "一下", "怎么", "多少", "以及", "对于", "关于"}
        ts = [t.strip() for t in jieba.cut(query)]
        ts = [t for t in ts if len(t) >= 2 and t not in stop]
        return sorted(set(ts), key=len, reverse=True)[:12]

    @staticmethod
    def _passage(clean: str, at: int, width: int = 150) -> str:
        """A readable window around ``at`` in an already-normalised string."""
        if not clean:
            return ""
        if at < 0:
            frag = clean[: width * 2].strip()
            return frag + ("…" if len(clean) > width * 2 else "")
        start = max(0, at - width // 2)
        end = min(len(clean), start + width * 2)
        frag = clean[start:end].strip()
        if start > 0:
            frag = "…" + frag
        if end < len(clean):
            frag = frag + "…"
        return frag

    def process(self, segment: str, refine: bool = False) -> dict:
        """Turn one ASR segment into the payload the UI renders."""
        segment = (segment or "").strip()
        if not segment:
            return {"error": "empty segment"}

        t_start = time.time()
        kws, t_kw = self.extract_keywords(segment)

        t0 = time.time()
        results = self._search(segment)
        t_seg = (time.time() - t0) * 1000

        hotspots: list[Hotspot] = []
        for k in kws:
            hs = Hotspot(term=k, count=segment.count(k))
            if refine:
                t0 = time.time()
                hs.results = self._search(k)
                hs.elapsed_ms = (time.time() - t0) * 1000
                hs.refined = True
            hotspots.append(hs)

        return {
            "segment": segment,
            "keywords": [h.to_dict() for h in hotspots],
            "results": results,
            "timing": {
                "keywords_ms": round(t_kw, 1),
                "segment_search_ms": round(t_seg, 1),
                "total_ms": round((time.time() - t_start) * 1000, 1),
            },
            "index": {
                "db": self.stats["db"],
                "chunks": self.stats["chunks"],
                "embedder": self.stats["embedder"],
            },
        }

    def refine(self, term: str) -> dict:
        """Per-keyword drill-down, called when the user hovers a hotspot."""
        t0 = time.time()
        results = self._search(term)
        return {
            "term": term,
            "results": results,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        }
