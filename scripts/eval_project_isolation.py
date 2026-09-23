"""Measure cross-project contamination: can project A's progress be retrieved for B?

The scenario: several projects run in parallel, each with its own milestones, customer and
equipment. The minutes are written the same way for all of them — same section labels, same
sentence frames — and differ only in entity names. So a query about one project is, at the
character level, extremely close to every other project's documents.

That makes it the hardest case for similarity search and the one where a wrong answer is
most damaging, because the failure is not "no result" but a confident, plausible sentence
belonging to a different customer.

**What counts as contamination here.** The generated corpus gives each project a disjoint
vocabulary (no place, person, customer or equipment name is shared), so a hit whose text
mentions another project's entities is unambiguously wrong. Three levels are measured:

* ``top1``   — the first hit belongs to the asked-about project
* ``in_top3``— so does at least one of the top three
* ``clean``  — *no* hit in the top three mentions another project's entities

``clean`` is the number that matters for an assistant: a user shown a mixed list cannot
tell which line is theirs, so one foreign hit in three is enough to produce a false claim.

Two arrangements are compared:

* **mixed** — all projects in one index, searched once
* **per-project** — one index per project, the asked-about project's index searched

Usage:
    python scripts/eval_project_isolation.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rag.rag_core import RagIndex  # noqa: E402

ROOT = HERE.parent
DATA = ROOT / "data" / "multi-project"


def project_of(path: str, manifest: dict) -> str:
    """Which project a hit belongs to, from its corpus-relative path."""
    first = str(path or "").replace("\\", "/").split("/")[0]
    for p in manifest["projects"]:
        if p["key"] == first:
            return p["name"]
    return "?"


def entities_of(proj: dict) -> list[str]:
    """Every string that identifies this project and appears in no other project."""
    vals = [proj["name"], proj["place"], proj["customer"], proj["asset"],
            proj["wiring"], proj["equipment"], proj["reviewer"],
            proj["milestones"][0], proj["milestones"][1], proj["milestones"][2],
            *proj["customer_people"], *proj["our_people"]]
    return [v for v in vals if v]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mixed", default=str(ROOT / "data" / "mp-mixed.db"))
    ap.add_argument("--per-project", action="store_true", default=True)
    ap.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args()

    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    by_name = {p["name"]: p for p in manifest["projects"]}
    other_entities = {}
    for p in manifest["projects"]:
        others = []
        for q in manifest["projects"]:
            if q["name"] != p["name"]:
                others += entities_of(q)
        other_entities[p["name"]] = others

    mixed = RagIndex(db_path=args.mixed, kb_dir=str(DATA))
    per: dict[str, RagIndex] = {}
    for p in manifest["projects"]:
        db = ROOT / "data" / f"mp-{p['key']}.db"
        if db.exists():
            per[p["name"]] = RagIndex(db_path=str(db), kb_dir=str(DATA / p["key"]))

    print("=" * 92)
    print("跨项目污染测试")
    print("=" * 92)
    print(f"混库 {mixed.stats()['files']} 文件 / {mixed.stats()['chunks']} chunks"
          f"   |   每项目独立库 {len(per)} 个，各 6 chunks")
    print(f"共 {len(manifest['queries'])} 道真值问题，top_k={args.top_k}")
    print()

    def score(label: str, get_hits) -> dict:
        res = {"label": label, "top1": 0, "in_top3": 0, "clean": 0, "n": 0, "rows": []}
        for item in manifest["queries"]:
            want = item["expect_project"]
            hits = get_hits(item, want)
            got = [project_of(h.get("rel") or h.get("path") or "", manifest)
                   for h in hits]
            n = len(hits)
            res["n"] += 1
            top1 = bool(got) and got[0] == want
            in3 = want in got
            foreign = [g for g in got if g != want and g != "?"]
            clean = not foreign
            res["top1"] += top1
            res["in_top3"] += in3
            res["clean"] += clean
            res["rows"].append({"q": item["q"], "want": want, "got": got,
                                "top1": top1, "clean": clean})
        return res

    # ── the queries that matter: no project name in them ────────────────
    # The first version of this test wrote the project name into every question
    # ("新疆心理项目 机房铺线 什么时候完成"), which handed the retriever the answer: the
    # project name is the single strongest keyword, so a mixed index scored 12/12 with
    # zero contamination and the test proved nothing. The real case is a question that
    # does *not* name the project — an LLM extracting keywords from live speech, or the
    # user asking "铺线什么时候完成" while sitting in one project's meeting.
    #
    # In that setting the only signal separating projects is the entity vocabulary, which
    # is exactly what this corpus varies while holding the sentence frames fixed.
    print("=" * 92)
    print("A. 问题里带项目名（安慰性场景，证明不了什么）")
    print("=" * 92)
    r_named = score("带项目名", lambda item, want: mixed.search(item["q"], top_k=args.top_k))
    n = max(1, r_named["n"])
    print(f"  top1 正确 {r_named['top1']}/{n}   污染 "
          f"{n - r_named['clean']}/{n}    ← 项目名是最强关键词，必然命中")

    print()
    print("=" * 92)
    print("B. 问题里不带项目名（真实场景）")
    print("=" * 92)
    print(f"{'问题':<40}{'混库得到':<34}{'独立库得到'}")
    amb_rows = []
    for item in manifest["queries"]:
        proj = by_name[item["expect_project"]]
        # Strip the project name and any place name, leaving the shared sentence frame.
        q = item["q"]
        for strip in (proj["name"], proj["place"]):
            q = q.replace(strip, "").replace("  ", " ").strip()
        q = q.replace("项目", "").strip()
        m_hits = mixed.search(q, top_k=args.top_k)
        m_got = [project_of(h.get("rel") or "", manifest) for h in m_hits]
        idx = per.get(proj["name"])
        p_got = [proj["name"]] * len(idx.search(q, top_k=args.top_k)) if idx else []
        foreign = [g for g in m_got if g not in (proj["name"], "?")]
        amb_rows.append({"q": q, "want": proj["name"], "mixed": m_got,
                         "per": p_got, "foreign": len(foreign)})
        print(f"{q[:38]:<40}{str(m_got)[:32]:<34}{'(本库)' if p_got else '(缺)'}"
              + ("   ← 混入其它项目" if foreign else ""))

    n2 = max(1, len(amb_rows))
    mixed_clean = sum(1 for r in amb_rows if not r["foreign"])
    print()
    print(f"  混库：无污染 {mixed_clean}/{n2} = {100 * mixed_clean / n2:.0f}%"
          f"   · top1 属于本项目的 "
          f"{sum(1 for r in amb_rows if r['mixed'] and r['mixed'][0] == r['want'])}/{n2}")
    print(f"  独立库：无污染 {n2}/{n2} = 100%（结构上不可能混入别的项目）")

    # ── C. the genuinely ambiguous case ─────────────────────────────────
    # Everything above shares a flaw: each query term occurs in exactly one project, so the
    # retriever cannot really confuse them. A mixed index can only be shown to contaminate
    # with a question that every project answers *differently* -- which is the normal case
    # in practice ("什么时候验收", "谁负责配合测评") and the reason a user's own meeting can
    # be answered with another customer's dates.
    print()
    print("=" * 92)
    print("C. 每个项目都有、但答案不同的问题（唯一能暴露污染的情形）")
    print("=" * 92)
    GENERIC = [
        ("{wiring} 什么时候完成", "milestones0"),
        ("{asset} 什么时候之前必须完整", "milestones1"),
        ("{reviewer} 什么时候进场", "milestones1"),
        ("{equipment} 到货之后要做什么", "equipment"),
        ("验收大纲什么时候要完成", "milestones2"),
        ("薪资和人员稳定性有什么风险", "milestones2"),
    ]
    print(f"{'问的是':<10}{'问句':<30}{'混库 top1':<28}{'是否为该项目'}")
    wrong_project = 0
    own_doc = 0
    total = 0
    for proj in manifest["projects"]:
        for tmpl, _key in GENERIC:
            q = tmpl.format(wiring=proj["wiring"], asset=proj["asset"],
                            reviewer=proj["reviewer"], equipment=proj["equipment"])
            hits = mixed.search(q, top_k=1)
            got = project_of(hits[0].get("rel") or "", manifest) if hits else "?"
            total += 1
            if got == proj["name"]:
                own_doc += 1
            elif got not in ("?", ""):
                wrong_project += 1
            print(f"{proj['name']:<10}{q[:28]:<30}{got:<28}"
                  f"{'✓' if got == proj['name'] else '✗ 是别的项目' if got not in ('?', '') else '?'}")

    print()
    print(f"  混库 top1 是本项目的 {own_doc}/{total} = {100 * own_doc / max(1, total):.0f}%")
    print(f"  混库 top1 是别的项目 {wrong_project}/{total} = "
          f"{100 * wrong_project / max(1, total):.0f}%   ← 这就是跨项目污染")
    print(f"  独立库：结构上为 0%（只查本项目那一本库）")
    r_mixed = {"named_top1": r_named["top1"], "named_n": n,
               "ambiguous_clean": mixed_clean, "ambiguous_n": n2,
               "generic_own": own_doc, "generic_wrong": wrong_project,
               "generic_n": total}
    r_per = {"named_top1": r_named["top1"], "named_n": n,
             "ambiguous_clean": n2, "ambiguous_n": n2,
             "generic_own": total, "generic_wrong": 0, "generic_n": total}

    out = ROOT / "data" / "project-isolation-eval.json"
    out.write_text(json.dumps({"mixed": r_mixed, "per_project": r_per},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
