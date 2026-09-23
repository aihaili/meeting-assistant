"""Self-test for the plaud RAG module: ``python -m rag.selftest``

Covers the behaviour that was actually hard to get right on this deployment:

* chunking (sizes, heading paths, no decorative noise fragments)
* FTS5 tokenizer matches what is configured (a stale trigram table silently broke
  every 2-character Chinese query once -- see ``_ensure_fts_table``)
* jieba segmentation resolves 2-character names, which FTS5's trigram tokenizer
  structurally cannot
* vector distance -> cosine conversion (vec0 returns squared L2)
* end-to-end ranking on the real KB, with an oracle computed by substring match

Exit code 0 only when every check passes.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import chunking  # noqa: E402
from rag.rag_core import DEFAULT_KB, RagIndex, build_fts_query, segment  # noqa: E402

# (query, substring that a correct chunk must contain)
RANKING_CASES = [
    ("行动项", "行动项"),
    ("孙总", "孙总"),
    ("张伟", "张伟"),
    ("李强", "李强"),
    ("会议录音时长", "本次录音时长极短"),
    ("五出六进", "五出六进"),
    ("多重社会属性", "多重社会属性的动物"),
    ("没有业务结论", "无业务结论"),
    ("X 公司", "X 公司"),
    ("组织架构核心", "组织架构核心"),
    ("会议纪要", "会议纪要"),
    ("未确认", "未确认"),
    ("议题要点", "议题要点"),
    ("谁负责售后", "负责售后"),
    ("会议有没有形成决策", "未形成任何可识别的决策"),
]

_fails: list[str] = []
_passes = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passes
    if cond:
        _passes += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        _fails.append(name)
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def test_chunking() -> None:
    print("\n== chunking ==")
    md = (
        "<!-- Generated: 2026-01-01T00:00:00 -->\n\n"
        "# Doc\n\nintro text here.\n\n"
        "## Section A\n\n" + "句子一。" * 40 + "\n\n"
        "### Sub B\n\nshort body.\n\n"
        "## Table\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    chunks = chunking.chunk_markdown(md)
    check("produces chunks", len(chunks) >= 3, f"{len(chunks)} chunks")
    check("no fragment starts with a stripped comment", all("Generated" not in c.text.strip()[:12] for c in chunks))
    check("heading path recorded", any("Section A" in c.heading for c in chunks))
    check("nested heading path uses ' > '", any(" > " in c.heading for c in chunks))
    check("table kept atomic", any(c.text.count("|") >= 6 for c in chunks))

    # A headline is real content, so the doc is not empty -- what matters is that the
    # decorative lines (---, *footnote*) are stripped rather than becoming chunks of
    # their own (they used to: 47-char chunks holding nothing but "---").
    with_noise = chunking.chunk_markdown("# T\n\n---\n\n*footnote*\n")
    without = chunking.chunk_markdown("# T\n\nreal body text here\n")
    check("decorative lines are stripped, not chunked",
          len(with_noise) == len(without) == 1, f"{len(with_noise)} vs {len(without)}")
    check("only the heading survives from a decoration-only doc",
          bool(with_noise) and "footnote" not in with_noise[0].text
          and "---" not in with_noise[0].text,
          repr(with_noise[0].text) if with_noise else "none")


def test_fts_query_builder() -> None:
    print("\n== fts query builder ==")
    check("2-char name is one term", build_fts_query("孙总") == '"孙总"', build_fts_query("孙总"))
    q = build_fts_query("会议录音时长")
    check("long CJK run becomes a jieba OR expression", " OR " in q and '"' in q, q)
    check("punctuation-only query yields no expression", build_fts_query("，。！") == "")
    check("ascii words survive", "organizational" in build_fts_query("organizational structure"))


def test_segmentation() -> None:
    """jieba is context-dependent, so the index must tolerate boundary mismatch.

    segment("孙总是合作方对接人") == "孙 总是 合作方 对接 人" -- the standalone name
    survives only in isolation. Query "孙总" would then be a phrase (孙 总) that does not
    exist in the index. It still retrieves correctly because segmentation never *merges*
    the query term, and the vector arm covers the rest. This test pins that behaviour so
    a future "improvement" (e.g. a non-overlapping bigram index) does not silently trade
    it away -- measured: bigram indexing scored 12/17 vs jieba's 15/17 on the same set.
    """
    print("\n== segmentation (context-dependent; documented tradeoff) ==")
    seg = segment("孙总是合作方对接人")
    check("segmented text is space-joined", " " in seg, seg)
    check("standalone name stays whole", "孙总" in segment("孙总").split(),
          segment("孙总"))
    check("in-context split is the known tradeoff, not a crash",
          "孙" in seg.split() or "孙总" in seg.split(), seg)


def test_index(no_vector: bool = False) -> RagIndex:
    print(f"\n== index (kb={DEFAULT_KB}) ==")
    idx = RagIndex(kb_dir=DEFAULT_KB, with_vectors=not no_vector)

    live = idx._live_fts_tokenizer()
    check("live FTS tokenizer matches config", live == idx.tokenizer,
          f"live={live} config={idx.tokenizer}")

    if no_vector:
        return idx

    check("vectors enabled", idx.vec_ready)
    idx.index_all(verbose=False)
    n_chunks = idx.conn.execute("select count(*) from chunks").fetchone()[0]
    n_vec = idx.conn.execute("select count(*) from chunk_vec").fetchone()[0]
    check("every chunk has a vector", n_chunks == n_vec, f"{n_vec}/{n_chunks}")
    return idx


def test_distance_conversion(idx: RagIndex) -> None:
    print("\n== vec0 distance -> cosine ==")
    knn = idx._knn("张伟", 5)
    check("knn returns hits", bool(knn), f"{len(knn)} hits")
    check("cosines are in [-1, 1]", all(-1.001 <= s <= 1.001 for _, s in knn))
    check("best cosine is positive for a present term", knn and knn[0][1] > 0,
          f"top={knn[0][1]:.4f}" if knn else "n/a")


def test_ranking(idx: RagIndex) -> None:
    print("\n== ranking on the real KB ==")
    rows = [(r["id"], r["text"]) for r in idx.conn.execute("select id, text from chunks")]
    path_ids: dict[str, set[int]] = {}
    for r in idx.conn.execute("select id, path from chunks"):
        path_ids.setdefault(r["path"], set()).add(r["id"])

    tally = {"bm25": [0, 0], "vector": [0, 0], "hybrid": [0, 0]}
    top1 = {"bm25": [0, 0], "vector": [0, 0], "hybrid": [0, 0]}

    for q, needle in RANKING_CASES:
        want = {cid for cid, t in rows if needle and needle in t}
        want_paths = {p for p, ids in path_ids.items() if ids & want}
        for mode in tally:
            hits = idx.search(q, top_k=3, mode=mode)
            tally[mode][1] += 1
            tally[mode][0] += int(any(path_ids.get(h["path"], set()) & want for h in hits))
            top1[mode][1] += 1
            top1[mode][0] += int(bool(hits) and hits[0]["path"] in want_paths)

    for mode in tally:
        a, b = tally[mode]
        t1a, t1b = top1[mode]
        print(f"    {mode:7} hit@3 {a}/{b}   top-1 {t1a}/{t1b}")
    # hybrid must not be worse than either arm (that is the whole point of fusing)
    check("hybrid hit@3 >= both arms",
          tally["hybrid"][0] >= max(tally["bm25"][0], tally["vector"][0]),
          f"hybrid={tally['hybrid'][0]} bm25={tally['bm25'][0]} vector={tally['vector'][0]}")
    check("hybrid top-1 >= both arms",
          top1["hybrid"][0] >= max(top1["bm25"][0], top1["vector"][0]),
          f"hybrid={top1['hybrid'][0]} bm25={top1['bm25'][0]} vector={top1['vector'][0]}")

    # result-shape contract expected by wiki_server
    hits = idx.search("孙总", top_k=2)
    required = {"title", "label", "path", "rel", "snippets", "score"}
    check("result shape matches wiki_server contract",
          bool(hits) and required <= set(hits[0]), f"{sorted(hits[0]) if hits else 'no hits'}")
    check("results carry chunks for prompt assembly",
          bool(hits) and isinstance(hits[0].get("chunks"), list) and hits[0]["chunks"])
    check("rel is source-relative (no 'wiki/' prefix for wiki)",
          bool(hits) and not hits[0]["rel"].startswith("wiki/"), hits[0]["rel"] if hits else "")
    check("scores normalized to 0..1", bool(hits) and hits[0]["score"] <= 1.0,
          f"{hits[0]['score']}" if hits else "")


def main() -> int:
    no_vector = "--no-vector" in sys.argv
    print("plaud RAG self-test" + (" (no vectors)" if no_vector else ""))
    test_chunking()
    test_fts_query_builder()
    test_segmentation()
    idx = test_index(no_vector=no_vector)
    if not no_vector:
        test_distance_conversion(idx)
    test_ranking(idx)
    idx.close()

    print(f"\n{_passes} passed, {len(_fails)} failed")
    for f in _fails:
        print(f"  FAILED: {f}")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
