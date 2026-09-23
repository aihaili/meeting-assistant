r"""会议纪要用 Obsidian 能认的 Markdown 导出。

## 目标只有一条

用户的原话：**"只需要让用户用 Obsidian 打开会议纪要时，有些特定内容自动关联项目内的相关文档"**。
所以这不是"生成一份好看的纪要"，而是**让纪要里的实体和依据能点进去**：

* 人名/单位/术语 → `[[林浩然]]` 这样指向项目库里的笔记（能解析才算数）；
* 每条线索下面挂**依据文档**：`[[示例项目资料设计实施方案#5.2 软件环境]]`
  ——从会上的一句话能跳到它依据的那份文档的那一节。这才是"关联文档"的价值。

## 两条硬规则

1. **纪要跟着项目走**（用户明确要求）：写在 `<项目>/会议纪要/`。
   项目文件夹本身就是 Obsidian 库（里面已有 `.obsidian`），所以链接按**项目内相对路径**解析。
2. **不制造断链**。线索的 refs 带 `label` 区分语料库：项目库的能链接，
   公共库（如 `ar` = `<公共资料库>`）**不在这个库里**，链过去必然断——它们渲染成纯文本 + 来源标注。
   `validate` 会检查"零断链"，因为**断链的图谱比没有图谱更糟**（看起来热闹，其实点不动）。

## 不做的事

* 不为每个人/每条决议单独建笔记（碎片化的图谱没意义——见 KB_HANDBACK 的教训）。
  实体要链接到**已有**的笔记；没有对应笔记的名字就不加链接。
* **纪要本身不回灌索引**（KB_HANDBACK 里已有这条规则：我们自己的产物不能喂回检索，
  否则索引会随着每次会议增长，而里面装的是机器对已有内容的复述）。

用法: python scripts/export_minutes.py --session data/sessions/meeting.json \
          --project data/proj-demo [--title "项目进度汇报例会"]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 线索类型的显示名（和界面上左栏的用词一致）
KIND_LABEL = {
    "requirement": "要求",
    "commitment": "承诺",
    "risk": "风险",
    "deadline": "时间节点",
    "decision": "决定",
    "question": "待确认",
    "term": "术语",
    "person": "人物",
    "org": "单位",
    "topic": "议题",
}
# 纪要里的分节顺序：先"要办的"，再"人和词"，最后是原始记录
SECTION_ORDER = ["decision", "requirement", "commitment", "deadline", "risk",
                 "question", "topic", "person", "org", "term"]
PRIORITY = ["decision", "requirement", "commitment", "deadline", "risk", "question"]

MINUTES_DIR = "会议纪要"
# 人员档案：一份文档，每人一个标题。链接写成 [[参与人员#林浩然|林浩然]]——
# 这样"谁说过什么"在 Obsidian 里能点进去，而且**不会给每个人生成一堆空壳笔记**
# （KB_HANDBACK 的教训：碎片化的图谱比没有图谱更糟），图谱里还得到一个有意义的枢纽。
ROSTER = "参与人员"


def _safe_name(s: str) -> str:
    """文件名里不能有的字符换掉。Obsidian 对 `#[]|` 特别敏感（它们是链接语法）。"""
    return re.sub(r'[\\/:*?"<>|#\[\]]+', "-", s).strip() or "未命名"


def person_link(name: str) -> str:
    """人名 → 指向人员档案里那一节的链接。"""
    return f"[[{ROSTER}#{name}|{name}]]"


def roster_path(project_dir: Path) -> Path:
    return project_dir / f"{ROSTER}.md"


def parse_sections(text: str) -> dict[str, str]:
    """把档案里 `## 名字` 那一节的正文抠出来（合并时用来保住手写内容）。"""
    out: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur:
                out[cur] = "\n".join(buf).strip()
            cur = line[3:].strip()
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur:
        out[cur] = "\n".join(buf).strip()
    return out


def person_clues(clues: list[dict], segs: dict, name: str) -> dict[str, list[str]]:
    """把线索按"是谁说的"归到人头上。

    这就是人员档案"天生有内容"的来源：不需要用户手写，每开一次会就自动攒。
    只收**能指到具体人**的线索（发言人有署名，或线索自己带 actor）。
    """
    out: dict[str, list[str]] = {}
    for c in clues:
        kind = c.get("kind") or ""
        if kind not in PRIORITY:
            continue
        who = (c.get("actor") or "").strip()
        if not who:
            s = segs.get(_seg_idx(c, segs))
            who = (s or {}).get("speaker") or ""
        if who != name:
            continue
        text = (c.get("anchor") or c.get("text") or "").strip()
        if text:
            out.setdefault(kind, []).append(text)
    return out


def write_roster(project_dir: Path, people: list[dict], clues: list[dict] | None = None,
                 segs: dict | None = None, minutes_name: str = "",
                 ) -> tuple[Path, int, int]:
    """生成/更新项目人员档案。返回 (路径, 新增人数, 保留的人数)。

    **合并而不是重写**：用户可能已经在 Obsidian 里给某个人写了备注，
    直接重写会把它们抹掉。所以已有那一节的正文原样保留，只补新人和缺失的单位/角色。
    """
    from export_obsidian import note_frontmatter

    p = roster_path(project_dir)
    existing: dict[str, str] = {}
    if p.is_file():
        existing = parse_sections(p.read_text(encoding="utf-8"))

    lines = [note_frontmatter(ROSTER, None, {
        "type": "项目人员",
        "project": project_dir.name,
        "aliases": ["参与人员", "人员名单", "参会人员"],
        "tags": ["人员", project_dir.name],
    }), f"# {ROSTER}", ""]
    lines.append("> 由会议助理维护：每开一次会就补上出现过的参会人。"
                 "**你自己的备注不会被覆盖。**")
    lines.append("")
    added = kept = 0
    for who in people:
        nm = (who.get("name") or "").strip()
        if not nm:
            continue
        bits = [x for x in (who.get("org"), who.get("role")) if x]
        lines.append(f"## {nm}")
        if bits:
            lines.append(f"- {' · '.join(bits)}")
        # 自动攒的内容：这个人说过哪些要求/承诺/风险。**这是他那一节的实质内容**，
        # 不是"会议里出现过"这种空话——没有内容的图谱不如不做。
        mine = person_clues(clues or [], segs or {}, nm)
        if mine:
            for kind in PRIORITY:
                items = mine.get(kind)
                if not items:
                    continue
                label = KIND_LABEL.get(kind, kind)
                for text in items[:4]:
                    tail = (f"　（{minutes_name}）" if minutes_name else "")
                    lines.append(f"- **{label}**：{text}{tail}")
        body = existing.get(nm)
        if body:
            kept += 1
            lines.append("")
            lines.append(body)
        elif not mine:
            added += 1
            lines.append("- （会议里出现过；这里可以记笔记）")
        else:
            added += 1
        lines.append("")
    # 档案里已有、但这次会议没出现的人**保留**（别把人删了）
    for nm, body in existing.items():
        if nm and nm not in {x.get("name") for x in people}:
            lines.append(f"## {nm}")
            lines.append("")
            lines.append(body)
            lines.append("")
            kept += 1
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(p)
    return p, added, kept


def vault_files(project_dir: Path) -> set[str]:
    """项目库里现有的笔记（按**不含扩展名的相对路径**和**文件名**两种键）。

    Obsidian 的 wikilink 两种写法都能解析：`[[子目录/笔记]]` 和 `[[笔记]]`（同名唯一时）。
    两种都收，校验时才算得准。
    """
    keys: set[str] = set()
    for p in project_dir.rglob("*.md"):
        if any(part.startswith(".") for part in p.relative_to(project_dir).parts):
            continue
        rel = p.relative_to(project_dir).with_suffix("").as_posix()
        keys.add(rel)
        keys.add(p.stem)
    return keys


def link_ref(project_dir: Path, ref: dict, known: set[str]) -> str:
    """把一条"依据文档"渲染成 Markdown。

    只有**指向本项目库**的引用才做成 wikilink；公共库的渲染成纯文本 + 来源标注，
    因为链过去一定断（那是另一棵树）。
    """
    label = (ref.get("label") or "").strip()
    rel = (ref.get("rel") or "").strip()
    title = (ref.get("title") or Path(rel).stem or "文档").strip()
    heading = (ref.get("heading") or "").strip()
    is_project = project_dir.name == label or label in ("", project_dir.name)
    if is_project and rel:
        target = rel[:-3] if rel.endswith(".md") else rel
        # 路径或文件名任一能在库里解析，就做成链接
        if target in known or Path(target).stem in known:
            if heading:
                # heading 可能带父级前缀（"文件 > 章节"），Obsidian 的锚点取最后一段
                anchor = heading.split(">")[-1].strip()
                return f"[[{target}#{anchor}|{title}]]"
            return f"[[{target}|{title}]]"
    # 非本库：纯文本 + 标注，不制造断链
    src = f"（{label}库）" if label else ""
    return f"{title}{src}"


def build(project_dir: Path, session: dict, title: str = "",
          date: str = "") -> tuple[str, dict]:
    """生成纪要的 Markdown 文本。返回 (文本, 统计)。"""
    from export_obsidian import Entity, build_alias_index, linkify, note_frontmatter

    known = vault_files(project_dir)
    segs = {s["idx"]: s for s in session.get("segments", [])}
    clues = session.get("clues", [])
    people = session.get("participants", [])

    # 原标题/日期先算出来，因为人员档案里要回指这一次的纪要（Obsidian 不在乎先后）
    _title = title or session.get("title") or "会议纪要"
    _started = session.get("started") or time.time()
    _day = date or time.strftime("%Y-%m-%d", time.localtime(_started))
    _minutes = f"{_day} {_safe_name(_title)}"
    # 先确保人员档案存在且是最新的——否则 [[参与人员#某人]] 全是断链
    _roster, _added, _kept = write_roster(
        project_dir, people, clues=clues, segs=segs, minutes_name=_minutes)

    # 实体表：参会人 + 线索里被标为 person/org/term 的词。
    # 只把**库里已有对应笔记**的实体做成链接——没有笔记的名字加链接就是断链。
    ents: list[Entity] = []
    title_of = {e["title"]: e for e in _entity_titles(known)}
    for p in people:
        nm = (p.get("name") or "").strip()
        if not nm:
            continue
        aliases = [a for a in (p.get("aliases") or []) if a]
        note = title_of.get(nm)
        ents.append(Entity(canonical=note or nm, aliases=aliases,
                           note_id=(note or "")))
    for c in clues:
        if c.get("kind") in ("person", "org", "term"):
            t = (c.get("anchor") or c.get("text") or "").strip()
            if 1 < len(t) <= 24:
                note = title_of.get(t)
                ents.append(Entity(canonical=note or t, note_id=(note or "")))
    alias_idx, _warn = build_alias_index(ents)

    # 只保留"能在库里解析"的实体用于 linkify（其余留普通文本）
    linkable = {k: v for k, v in alias_idx.items()
                if v.canonical in known or Path(v.canonical).stem in known
                or (v.note_id and (v.note_id in known
                                   or Path(v.note_id).stem in known))}

    pnames = sorted({(p.get("name") or "").strip() for p in people if p.get("name")},
                    key=len, reverse=True)

    def lk(text: str) -> str:
        # 人名先换成 [[参与人员#名字|名字]]（比 linkify 更精确：直接落到那个人的那一节），
        # 再让 linkify 处理其它实体（它会跳过已经成链接的部分）。
        s = text
        for nm in pnames:
            if nm and nm in s:
                s = s.replace(nm, person_link(nm))
        got, _ids = linkify(s, linkable, self_note="")
        return got

    title, started, day = _title, _started, _day
    fm = note_frontmatter(title, None, {
        "date": day,
        "type": "会议纪要",
        "project": project_dir.name,
        "participants": [p.get("name") for p in people if p.get("name")],
        "tags": ["会议纪要", project_dir.name],
    })

    out: list[str] = [fm, f"# {title}", ""]
    out.append(f"> {day} · 项目 `{project_dir.name}` · 发言 {len(segs)} 句 · "
               f"线索 {len(clues)} 条")
    out.append("")
    if people:
        out.append("## 参会人")
        for p in people:
            nm = p.get("name") or "未署名"
            bits = [x for x in (p.get("org"), p.get("role")) if x]
            out.append(f"- {lk(nm)}" + (f"　{' · '.join(bits)}" if bits else ""))
        out.append("")

    stats = {"linked_refs": 0, "plain_refs": 0, "sections": 0,
             "roster_added": _added, "roster_kept": _kept}
    grouped: dict[str, list] = {}
    for c in clues:
        grouped.setdefault(c.get("kind") or "topic", []).append(c)
    for kind in SECTION_ORDER + [k for k in grouped if k not in SECTION_ORDER]:
        items = grouped.get(kind)
        if not items:
            continue
        stats["sections"] += 1
        out.append(f"## {KIND_LABEL.get(kind, kind)}")
        for c in items:
            who = ""
            seg = segs.get(_seg_idx(c, segs))
            if seg and seg.get("speaker"):
                who = seg["speaker"]
            elif c.get("actor"):
                who = c["actor"]
            line = f"- {lk((c.get('anchor') or c.get('text') or '').strip())}"
            meta = []
            if who:
                meta.append(lk(who))
            if c.get("due"):
                meta.append(f"截止 {c['due']}")
            if meta:
                line += "　— " + " · ".join(meta)
            out.append(line)
            for ref in (c.get("refs") or [])[:3]:
                rendered = link_ref(project_dir, ref, known)
                if rendered.startswith("[["):
                    stats["linked_refs"] += 1
                else:
                    stats["plain_refs"] += 1
                out.append(f"    - 依据：{rendered}")
        out.append("")

    out.append("## 完整发言")
    for s in sorted(session.get("segments", []), key=lambda x: (x.get("start") or 0)):
        who = s.get("speaker") or "未署名"
        y = int((s.get("start") or 0) // 60)
        sec = int((s.get("start") or 0) % 60)
        out.append(f"- `{y:02d}:{sec:02d}` {lk(who)}：{lk((s.get('text') or '').strip())}")
    out.append("")
    return "\n".join(out), stats


def _entity_titles(known: set[str]):
    """把库里"可能的实体名"列出来（用于判断某个名字有没有对应笔记）。"""
    out = []
    for k in sorted(known):
        if "/" in k:                       # 只要文件名，不要子目录路径
            continue
        out.append({"title": k})
    return out


def _seg_idx(clue: dict, segs: dict) -> int:
    """线索挂在哪一句上。``seg_id`` 是稳定 id，优先按它找；找不到再按时间兜底。"""
    sid = clue.get("seg_id")
    if sid:
        for idx, s in segs.items():
            if s.get("id") == sid:
                return idx
    t = clue.get("t")
    if t is not None:
        try:
            tf = float(t)
        except (TypeError, ValueError):
            return -1
        best, gap = -1, 1e9
        for idx, s in segs.items():
            d = abs((s.get("start") or 0) - tf)
            if d < gap:
                best, gap = idx, d
        return best
    return -1


def write(project_dir: Path, text: str, day: str, title: str) -> Path:
    d = project_dir / MINUTES_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{day} {_safe_name(title)}.md"
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)                     # 原子写：写一半的纪要比没有纪要更糟
    return p


def validate(path: Path, project_dir: Path) -> dict:
    """检查所有 wikilink 都能在库里解析。**这是本模块的验收标准。**"""
    known = vault_files(project_dir)
    text = path.read_text(encoding="utf-8")
    links = re.findall(r"\[\[([^\]]+)\]\]", text)
    broken, ok = [], 0
    for raw in links:
        target = raw.split("|")[0].split("#")[0].strip()
        if not target:
            continue
        if target in known or Path(target).stem in known:
            ok += 1
        else:
            broken.append(target)
    return {"links": len(links), "resolved": ok, "broken": broken}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="data/sessions/meeting.json")
    ap.add_argument("--project", default="data/proj-demo")
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    session = json.loads(Path(args.session).read_text(encoding="utf-8"))
    project_dir = Path(args.project)
    text, stats = build(project_dir, session, title=args.title)
    day = time.strftime("%Y-%m-%d", time.localtime(session.get("started") or time.time()))
    p = write(project_dir, text, day, args.title or session.get("title") or "会议纪要")
    v = validate(p, project_dir)
    print(f"已写出 {p}")
    print(f"  关联到项目内文档 {stats['linked_refs']} 处 · "
          f"公共库引用（纯文本）{stats['plain_refs']} 处 · 分节 {stats['sections']} 个")
    print(f"  wikilink {v['links']} 条，可解析 {v['resolved']} 条，"
          f"断链 {len(v['broken'])} 条")
    if v["broken"]:
        print("  断链示例：" + ", ".join(v["broken"][:5]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
