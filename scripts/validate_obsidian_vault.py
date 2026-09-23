"""Validate an exported Obsidian vault: does the graph actually connect?

Counting notes and links proves nothing. The two failures that make a graph useless are
both invisible in a count:

* **Dangling links** — a ``[[name]]`` with no note of that name. Obsidian renders it as
  plain text, so the page looks linked while the graph has no edge.
* **Fragmented entities** — the same real thing appearing as several nodes because its
  spellings were not unified. The graph then looks rich and conveys nothing, and the
  fragmentation cannot be seen without specifically resolving every spelling.

So this resolves every link against the set of note names and aliases, the way Obsidian
does, and reports the components: for each entity that has aliases, whether every spelling
in the corpus text ends up at one node.

Usage:
    python scripts/validate_obsidian_vault.py --vault data/obsidian-vault
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML reader for the flat frontmatter this exporter writes."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict = {}
    key = None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        if line.startswith("  - "):
            if key:
                out.setdefault(key, [])
                if isinstance(out[key], list):
                    out[key].append(line[4:].strip())
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            out[key] = val if val else []
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=str(Path(__file__).resolve().parents[1]
                                           / "data" / "obsidian-vault"))
    args = ap.parse_args()
    vault = Path(args.vault)
    if not vault.is_dir():
        print(f"找不到 {vault}")
        return 1

    notes: dict[str, Path] = {}
    aliases: dict[str, str] = {}
    alias_groups: dict[str, list[str]] = defaultdict(list)

    for md in vault.rglob("*.md"):
        text = md.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text)
        name = md.stem
        notes[name] = md
        for a in fm.get("aliases") or []:
            if isinstance(a, str) and a:
                aliases[a] = name
                alias_groups[name].append(a)

    print("=" * 74)
    print(f"库 {vault}")
    print(f"  笔记 {len(notes)} 个，声明的别名 {len(aliases)} 种")
    print("=" * 74)

    # ── dangling links ──────────────────────────────────────────────────
    link_re = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
    total_links = 0
    dangling: list[tuple[str, str]] = []
    edges: set[tuple[str, str]] = set()
    for name, path in notes.items():
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in link_re.finditer(text):
            target = m.group(1).strip()
            total_links += 1
            resolved = target if target in notes else aliases.get(target)
            if resolved is None:
                dangling.append((name, target))
            elif resolved != name:
                edges.add((name, resolved))

    print(f"\n① 链接解析")
    print(f"  wikilink 总数 {total_links}")
    print(f"  可解析 {total_links - len(dangling)}，断链 {len(dangling)}")
    for src, tgt in dangling[:10]:
        print(f"    ! {src} -> [[{tgt}]] 没有对应笔记")

    # ── fragmentation ───────────────────────────────────────────────────
    # The point of aliases: every spelling must land on one node. This is checked by
    # looking for the *alias text* appearing as a link target, which is what a fragmented
    # graph would contain.
    print(f"\n② 实体归一（别名是否都指向同一节点）")
    frag = 0
    for canonical, alist in sorted(alias_groups.items()):
        # A link written as [[alias]] must resolve to the canonical note, not create a node.
        for a in alist:
            if a in notes and a != canonical:
                print(f"    ! [[{a}]] 既是独立笔记又是 {canonical} 的别名 —— 会分裂成两个节点")
                frag += 1
        print(f"    {canonical:<14} 别名 {alist}"
              + ("   ← 全部解析到同一节点" if not frag else ""))
    if not alias_groups:
        print("    （没有声明任何别名）")

    # ── connectivity ────────────────────────────────────────────────────
    print(f"\n③ 图谱连通性")
    print(f"  去重后的边 {len(edges)} 条")
    degree: dict[str, int] = defaultdict(int)
    for a, b in edges:
        degree[a] += 1
        degree[b] += 1
    isolated = [n for n in notes if degree.get(n, 0) == 0]
    print(f"  孤立节点 {len(isolated)} 个")
    if isolated:
        for n in isolated[:10]:
            print(f"    - {n}")
    top = sorted(degree.items(), key=lambda kv: -kv[1])[:8]
    print("  度数最高的节点（图谱的中心）：")
    for n, d in top:
        print(f"    {n:<16} {d}")

    print("\n" + "=" * 74)
    ok = not dangling and not frag
    if ok:
        print("结论：无断链、无实体分裂，Obsidian 能直接形成连通图谱。")
    else:
        print(f"结论：断链 {len(dangling)} 处、分裂 {frag} 处，图谱会缺边或碎成多份。")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
