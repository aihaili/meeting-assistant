r"""Verify the extraction pipeline + event table against exact ground truth.

Inputs
  * synthetic meeting corpus (rule path)   : data/synth-corpus/*.md  <- EXACT metadata
  * software-generated notes (rule path)   : ai_notes_merged/*.md
  * a real human-written minute (.docx)    : <公共资料库>\项目会议纪要（20260831）.docx (LLM path)

Checks
  1. rule extraction: does it recover the generator's known event counts by type?
  2. llm extraction : does it produce grounded events for the schema-less .docx?
  3. query shapes   : aggregation exact-match, traversal completeness, event lookup
"""
from __future__ import annotations

import html
import json
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"E:\markdown\meeting-assistant\scripts")

from rag.extract import extract_rules, extract_llm, Meeting  # noqa: E402
from rag.event_store import EventStore  # noqa: E402

SYNTH = Path(r"E:\markdown\meeting-assistant\data\synth-corpus")
NOTES = Path.home() / "plaud-knowledge-base" / "ai_notes_merged"
DOCX = Path(r"<公共资料库>\项目会议纪要（20260831）.docx")
DB = r"E:\markdown\meeting-assistant\data\events.db"


def read_docx_text(p: Path) -> str:
    """Full text including table cells (the real minute lives in a cell)."""
    xml = zipfile.ZipFile(p).read("word/document.xml").decode("utf-8", "replace")

    def tx(x):
        return html.unescape("".join(
            re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", x, re.S))).strip()

    paras = [x for x in (tx(q) for q in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)) if x]
    return "\n".join(paras)


store = EventStore(DB)

# ── 1. rule extraction on the synthetic corpus (exact ground truth) ──────────
print("=" * 92)
print("1. 规则抽取（固定 5 段结构）—— 与生成器元数据对照")
print("=" * 92)
total_ok = total_n = 0
for p in sorted(SYNTH.glob("*.md")):
    md = p.read_text(encoding="utf-8")
    mtg, evs, matched = extract_rules(md, p.stem, str(p))
    m = re.search(r"## 结构化事件.*?```json\s*([\s\S]*?)```", md, re.S)
    truth = json.loads(m.group(1)) if m else []
    tcount = Counter(e["type"] for e in truth)
    gcount = Counter(e.type for e in evs)
    n = store.upsert_meeting(mtg, evs)
    same = sum(tcount.values()) == len(evs)
    total_ok += same
    total_n += 1
    print(f"  {p.name[:32]:34} matched={str(matched):5} 生成器={sum(tcount.values()):2} "
          f"抽出={len(evs):2} {'✓' if same else '△'}")
    if not same:
        print(f"      生成器 {dict(tcount)}")
        print(f"      抽出   {dict(gcount)}")
        for e in evs:
            print(f"        - [{e.type:6}] owner={e.owner!r:8} dl={e.deadline!r:10} {e.content[:52]}")
print(f"\n  {total_ok}/{total_n} 场会议的条目数与生成器一致")

# ── 2. rule extraction on the software's own notes ──────────────────────────
print()
print("=" * 92)
print("2. 规则抽取（软件自产纪要 ai_notes_merged/）")
print("=" * 92)
for p in sorted(NOTES.glob("*.md")):
    md = p.read_text(encoding="utf-8")
    mtg, evs, matched = extract_rules(md, p.stem, str(p))
    store.upsert_meeting(mtg, evs)
    print(f"  {p.name[:38]:40} schema={matched} date={mtg.date or '(无)':10} "
          f"events={len(evs)}")
    for e in evs:
        print(f"     [{e.type:6}] owner={e.owner or '-':10} {e.content[:58]}")

# ── 3. LLM extraction on the real .docx ─────────────────────────────────────
print()
print("=" * 92)
print("3. LLM 抽取（非规整文档：真实人写的 .docx）")
print("=" * 92)
if DOCX.exists():
    from rag.docread import docx_minutes_body, docx_meeting_metadata, normalize_date_text

    body = docx_minutes_body(DOCX)
    meta = docx_meeting_metadata(DOCX)
    mdate = normalize_date_text(meta.get("date", "")) or meta.get("date", "")
    print(f"  metadata: date={mdate} kind={meta.get('kind','')} "
          f"place={meta.get('place','')} attendees={len(meta.get('attendees',[]))}")
    print(f"  body: {len(body)} chars (longest paragraph = the minutes cell)")
    mtg = Meeting(meeting_id="docx-20260831", date=mdate,
                  title="项目会议纪要（20260831）", source_path=str(DOCX),
                  source_kind="docx", attendees=meta.get("attendees", []),
                  extractor="llm")
    evs = extract_llm(body, mtg.meeting_id, str(DOCX)) if body else []
    store.upsert_meeting(mtg, evs)
    print(f"  -> {len(evs)} events")
    flat = body.replace(" ", "")
    grounded = sum(1 for e in evs if e.content[:10] and e.content[:10] in flat)
    for e in evs:
        ok = "✓" if e.content[:10] in flat else "?"
        print(f"   {ok} [{e.type:6}] dl={e.deadline or '-':10} {e.content[:60]}")
    print(f"\n  可回溯: {grounded}/{len(evs)}")
else:
    print("  (docx not found)")

# ── 4. the three query shapes ────────────────────────────────────────────────
print()
print("=" * 92)
print("4. 事件表查询 —— 三种形态")
print("=" * 92)
st = store.stats()
print(f"  store: {st['meetings']} meetings, {st['events']} events, "
      f"range={st['date_range']}")
print(f"  by_type: {st['by_type']}")

print("\n  【聚合】按类型精确计数（相似度检索做不到，实测 top-8 只召回 3/13）")
for t in ("甲方要求", "己方承诺", "风险", "决策", "行动项"):
    a = store.aggregate(type=t)
    print(f"    {t:8} -> {a['total']} 条")

print("\n  【聚合】带期限的要求")
a = store.aggregate(has_deadline=True)
print(f"    有期限的事件 -> {a['total']} 条")

print("\n  【遍历】某实体跨会议的时间线（top-1 只能给一场，这里给全部）")
for kw in ("感应器", "验收大纲", "VR"):
    tl = store.timeline(kw)
    print(f"    {kw:8} {tl['count']} 条  span={tl['span']}")
    for grp in tl["timeline"]:
        for e in grp["events"]:
            print(f"        {grp['date']} [{e['type']}] {e['content'][:52]}")

print("\n  【找条目】按文本查具体事件")
for q in ("验收报告编号", "缺陷整改", "几个一级条目"):
    hits = store.find(q, limit=3)
    print(f"    {q!r} -> {len(hits)} 条")
    for h in hits[:2]:
        print(f"        {h['date']} [{h['type']}] {h['content'][:56]}")

store.close()
