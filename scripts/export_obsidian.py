"""Normalise entity names so Obsidian's graph does not fragment.

The problem this solves
-----------------------
Obsidian builds its graph from ``[[wikilinks]]``: a link creates an edge only when its
target matches a note (or an alias declared in that note's frontmatter). So if the corpus
calls the same person 孙磊, 余工 and 强工, and the same place 乌鲁木齐 / 乌市 / 新疆,
then a graph built from raw text produces **one node per spelling**. A fragmented graph is
worse than no graph: it looks busy and shows nothing, and the fragmentation is invisible
unless you specifically check link resolution.

The user's requirement was "同一实体只应是一个节点", so this module is the prerequisite for
the whole Obsidian feature, not a refinement of it.

Design
------
* Aliases are declared on the canonical note as ``aliases:`` in frontmatter, which is the
  mechanism Obsidian itself resolves. That keeps the graph correct in the app **and** in
  any other tool that reads frontmatter, with no plugin.
* Matching is longest-first, so 乌鲁木齐市 resolves to 乌鲁木齐 before 乌鲁木齐 does, and
  a short name that is a substring of a longer distinct name does not fire by accident
  (林浩然 vs 陈建 — the latter must not match inside the former).
* Ambiguity is refused rather than guessed: if one alias maps to two entities, it is
  reported as a conflict and left alone, because silently picking one would put a wrong
  edge in the graph that the user cannot see.
* Nothing is invented. Only names that appear in the corpus are linked, and a link is only
  emitted for a name that has a note (or an alias on one), so no dangling links are
  produced.

Usage:
    python scripts/export_obsidian.py --corpus data/multi-project --out data/obsidian-vault
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


@dataclass
class Entity:
    """One thing in the graph, with every spelling it appears under."""

    canonical: str
    kind: str = "term"                 # person | org | place | project | term
    aliases: list[str] = field(default_factory=list)
    note_id: str = ""                  # wikilink target

    def all_names(self) -> list[str]:
        # Longest first: 乌鲁木齐市 must resolve before 乌鲁木齐, otherwise the link would
        # be emitted for the shorter spelling and leave 市 dangling in the text.
        return sorted({self.canonical, *self.aliases}, key=len, reverse=True)


def build_alias_index(entities: list[Entity]) -> tuple[dict[str, Entity], list[str]]:
    """Map every spelling to its entity. Returns (index, conflicts).

    A spelling claimed by two entities is a conflict and is dropped from the index: an edge
    to the wrong node is worse than no edge, and it is invisible to the user.
    """
    idx: dict[str, Entity] = {}
    conflicts: list[str] = []
    for e in entities:
        for name in e.all_names():
            if name in idx and idx[name] is not e:
                conflicts.append(f"{name}: {idx[name].canonical} 与 {e.canonical}")
                idx.pop(name, None)
                continue
            if name not in [c.split(":")[0] for c in conflicts]:
                idx[name] = e
    return idx, conflicts


def linkify(text: str, alias_idx: dict[str, Entity], self_note: str = "",
            already: set[str] | None = None) -> tuple[str, set[str]]:
    """Wrap known entity names in ``[[...]]``. Returns (text, linked note ids).

    Two things are deliberately skipped:

    * a name that resolves to **this** note — a page linking to itself adds a self-loop to
      the graph, which Obsidian renders as a circle attached to nothing;
    * a name inside an existing ``[[...]]`` or inside a code span, because linking there
      would either nest brackets or corrupt a code sample.

    Only the first occurrence of each entity is linked. Linking every mention turns a page
    into a wall of blue and makes the graph's edge weights meaningless (they would count
    mentions, not relationships).
    """
    if not text or not alias_idx:
        return text, set()
    already = already if already is not None else set()
    linked: set[str] = set()

    # Protect regions that must not be touched, then restore them at the end.
    protected: list[str] = []

    def stash(m: re.Match) -> str:
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = re.sub(r"\[\[[^\]]*\]\]", stash, text)
    text = re.sub(r"`[^`]*`", stash, text)

    # One pass, longest names first, replacing only the first hit per entity.
    for name in sorted(alias_idx, key=len, reverse=True):
        ent = alias_idx[name]
        if ent.note_id in already or ent.note_id == self_note:
            continue
        if name not in text:
            continue
        # Chinese text has no word boundaries, so no \b here; the longest-first ordering is
        # what prevents a short alias from eating a longer name's prefix.
        text = text.replace(name, f"[[{ent.canonical}]]", 1)
        linked.add(ent.note_id)

    for i, original in enumerate(protected):
        text = text.replace(f"\x00{i}\x00", original)
    return text, linked


def note_frontmatter(title: str, ent: Entity | None, extra: dict | None = None) -> str:
    """YAML frontmatter, with ``aliases`` when the entity has other spellings."""
    lines = ["---", f"title: {title}"]
    if ent is not None:
        lines.append(f"type: {ent.kind}")
        if ent.aliases:
            lines.append("aliases:")
            for a in sorted(ent.aliases):
                lines.append(f"  - {a}")
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(Path(__file__).resolve().parents[1]
                                            / "data" / "multi-project"))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1]
                                         / "data" / "obsidian-vault"))
    ap.add_argument("--entities", default=None,
                    help="entities JSON；缺省时用 --corpus/manifest.json 里的项目信息")
    args = ap.parse_args()

    corpus = Path(args.corpus)
    out = Path(args.out)
    manifest_path = Path(args.entities) if args.entities else corpus / "manifest.json"
    if not manifest_path.exists():
        print(f"找不到实体定义 {manifest_path}")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Build entities from the corpus definition: every project's people, places, customers
    # and equipment become a node. This is the "same thing, one node" set.
    entities: list[Entity] = []
    for p in manifest.get("projects", []):
        entities.append(Entity(canonical=p["name"], kind="project",
                               aliases=[p["key"]], note_id=p["name"]))
        entities.append(Entity(canonical=p["place"], kind="place", note_id=p["place"]))
        entities.append(Entity(canonical=p["customer"], kind="org",
                               note_id=p["customer"]))
        for who in p["customer_people"] + p["our_people"]:
            entities.append(Entity(canonical=who, kind="person", note_id=who))
        for thing in (p["asset"], p["wiring"], p["equipment"], p["reviewer"]):
            entities.append(Entity(canonical=thing, kind="term", note_id=thing))
        # An alias with a real-world second spelling, so the normalisation is exercised.
        if p["key"] == "xinjiang":
            entities.append(Entity(canonical=p["place"], kind="place",
                                   aliases=["乌市", "乌鲁木齐市"], note_id=p["place"]))

    # Merge duplicates (the same person can appear in several projects).
    merged: dict[str, Entity] = {}
    for e in entities:
        if e.canonical in merged:
            for a in e.aliases:
                if a not in merged[e.canonical].aliases:
                    merged[e.canonical].aliases.append(a)
        else:
            merged[e.canonical] = e
    entities = list(merged.values())
    alias_idx, conflicts = build_alias_index(entities)

    out.mkdir(parents=True, exist_ok=True)
    written = 0
    total_links = 0
    for md in sorted(corpus.rglob("*.md")):
        rel = md.relative_to(corpus)
        body = md.read_text(encoding="utf-8", errors="replace")
        # The first heading names the note; it doubles as the wikilink target.
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = m.group(1).strip() if m else md.stem
        ent = next((e for e in entities if e.canonical == title), None)

        linked_body, linked = linkify(body, alias_idx, self_note=title)
        total_links += len(linked)
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(note_frontmatter(title, ent, {"source": str(rel)}) + "\n\n"
                        + linked_body, encoding="utf-8")
        written += 1

    # One note per entity, so every wikilink has a target. Without these the links dangle and
    # Obsidian draws nothing -- the graph would be empty while the pages looked linked.
    for e in entities:
        dest = out / f"{e.canonical}.md"
        if dest.exists():
            continue
        dest.write_text(note_frontmatter(e.canonical, e) + "\n\n"
                        + f"# {e.canonical}\n\n"
                        + f"类型：{e.kind}\n", encoding="utf-8")
        written += 1

    print("=" * 74)
    print(f"导出 Obsidian 库：{out}")
    print(f"  笔记 {written} 个（含 {len(entities)} 个实体节点）")
    print(f"  实体 {len(entities)} 个，可解析写法 {len(alias_idx)} 种")
    print(f"  生成的 wikilink 边 {total_links} 条")
    if conflicts:
        print(f"  冲突写法 {len(conflicts)} 处（已丢弃，宁缺勿错）：")
        for c in conflicts[:8]:
            print(f"    - {c}")
    else:
        print("  无别名冲突")
    return 0


if __name__ == "__main__":
    sys.exit(main())
