r"""Event extraction: meeting minutes -> flat event table.

Design decisions, each driven by something measured earlier:

1. **Markdown is the primary input, .docx is a compatibility branch.**
   The software's own pipeline emits Markdown (ai_notes_merged/*.md), so producing a
   .docx only to parse it back would be self-inflicted. Human-written .docx still
   needs support, but that is an import path, not the main path.

2. **Rule-first, LLM-fallback.**
   The software's notes have a FIXED five-section shape
   (1.会议摘要 / 2.关键决策 / 3.行动项 / 4.后续跟进 / 5.议题要点),
   so decisions and action items can be lifted deterministically -- no model call, no
   hallucination, no cost. Only documents that do NOT match the schema (external
   .docx, free-form minutes) go through the LLM, which was measured to work when the
   input is split into small batches (one 418-char shot produced corrupt JSON).

3. **A flat event table beats a knowledge graph here.**
   Measured on the 7-meeting corpus: aggregation questions ("甲方一共提出多少项要求")
   cannot be answered by similarity at all -- top-8 recall of the target type was 3/13.
   The same questions are answered EXACTLY by a type filter over a flat table.
   Entity-relation extraction would add a whole layer for queries this corpus does not
   produce.

4. **Provenance is kept, but it is not the point.**
   Every event carries its source file + heading so a result can be traced back. The
   product is an assistant, not an audit tool; provenance exists so the user can check
   a claim quickly, not to build a chain of custody.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ── event model ──────────────────────────────────────────────────────────

# Type vocabulary. Kept small and closed so filters stay predictable.
# 甲方确认 / 甲方要求 are separated because in practice they carry different weight:
# one is an instruction to act, the other records that something was agreed.
TYPES = ("决策", "行动项", "甲方要求", "甲方确认", "己方承诺", "风险", "进度", "议题", "其他")

SCHEMA_VERSION = 1


@dataclass
class Meeting:
    """Metadata for one meeting."""

    meeting_id: str          # stable id, derived from the source path
    date: str = ""           # ISO-ish date if known
    title: str = ""
    source_path: str = ""
    source_kind: str = "md"  # md | docx | other
    attendees: list[dict] = field(default_factory=list)
    extractor: str = ""      # rule | llm | mixed

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Event:
    """One checkable item from a meeting."""

    meeting_id: str
    type: str                # one of TYPES
    content: str
    deadline: str = ""       # verbatim deadline phrase, if any
    owner: str = ""          # responsible party, if stated
    section: str = ""        # which heading it came from
    source_ref: str = ""     # traceable pointer: path#section
    seq: int = 0             # order within the meeting

    def to_dict(self) -> dict:
        return asdict(self)


# ── rule-based extraction (software's own notes) ─────────────────────────

# The pipeline's note schema, matched loosely so minor wording changes still work.
SECTION_PATTERNS = {
    "纪要": re.compile(r"^#{2,4}\s*\d*[.、]?\s*会议纪要\s*$"),
    "摘要": re.compile(r"^#{2,4}\s*\d*[.、]?\s*会议摘要"),
    "决策": re.compile(r"^#{2,4}\s*\d*[.、]?\s*(关键决策|决策)"),
    "行动项": re.compile(r"^#{2,4}\s*\d*[.、]?\s*(行动项|待办)"),
    "跟进": re.compile(r"^#{2,4}\s*\d*[.、]?\s*(后续跟进|后续)"),
    "议题": re.compile(r"^#{2,4}\s*\d*[.、]?\s*(议题要点|议题)"),
}

# Second note format: bullets that ANNOTATE their own type, e.g.
#   - **[甲方要求]** 9月5日前软件部署调通完成。（期限：9月5日前）
# Human-written and exported minutes often look like this, so both shapes are parsed.
INLINE_TYPE = re.compile(r"^\s*[-*•]\s*\*\*\[\s*([^\]]{1,10})\s*\]\*\*\s*(.*)$")
DEADLINE_SUFFIX = re.compile(r"[（(]\s*(?:期限|截止|deadline)\s*[:：]\s*([^）)]+)[）)]\s*$")

# Headings/metadata that must never become events.
NOT_EVENT = re.compile(
    r"^(?:提交版本|提交日期|会议信息|会议纪要|会议类型|会议内容|会议地点|日期|时间|"
    r"记录单位|序号|与会人员|结构化事件|数据来源|项目)\b")

DATE_PATTERNS = [
    re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})"),
    re.compile(r"(\d{4})(\d{2})(\d{2})"),
]

# deadline phrases: absolute dates and relative ones that participants really use
DEADLINE_PATTERNS = [
    re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日\s*(?:前|之前|以前|左右)?"),
    re.compile(r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?\s*(?:前|之前)?"),
    re.compile(r"(?:本周|下周|本月|下月|节前|月底|年底)[一二三四五六日天]?\s*(?:前|之前)?"),
    re.compile(r"\d+\s*(?:个)?(?:工作日|天|周|月)(?:内|以内|之内)"),
]

# Action-item owner: "- [ ] **张伟** 任务" or "- [ ] 张三：任务" or trailing "(张三)"
OWNER_BOLD = re.compile(r"^\s*[-*]\s*\[[ xX]?\]?\s*\*\*(.+?)\*\*\s*[:：]?\s*(.*)$")
OWNER_COLON = re.compile(r"^\s*[-*]\s*\[[ xX]?\]?\s*([^：:]{1,12})\s*[:：]\s*(.+)$")
OWNER_TRAIL = re.compile(r"^\s*[-*]\s*\[[ xX]?\]?\s*(.+?)\s*[（(]([^（()）]{1,12})[)）]\s*$")


def normalize_date(s: str) -> str:
    """Best-effort date normalisation to YYYY-MM-DD; '' when nothing parses."""
    for pat in DATE_PATTERNS:
        m = pat.search(s or "")
        if not m:
            continue
        y, mo, d = (int(g) for g in m.groups())
        if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def find_deadline(text: str) -> str:
    for pat in DEADLINE_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(0).strip()
    return ""


def split_sections(md: str) -> dict[str, list[str]]:
    """Split a markdown note into {section_label: [lines]} using the known headings."""
    out: dict[str, list[str]] = {}
    current = ""
    for line in md.splitlines():
        hit = None
        for label, pat in SECTION_PATTERNS.items():
            if pat.match(line.strip()):
                hit = label
                break
        if hit:
            current = hit
            out.setdefault(current, [])
            continue
        if current:
            out.setdefault(current, []).append(line)
    return out


def parse_frontmatter(md: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body). Tolerates its absence."""
    if not md.startswith("---"):
        return {}, md
    end = md.find("\n---", 3)
    if end < 0:
        return {}, md
    raw = md[3:end].strip()
    fm: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"\'')
    return fm, md[end + 4:]


def extract_rules(md: str, meeting_id: str, source_path: str) -> tuple[Meeting, list[Event], bool]:
    """Rule-based extraction. Returns (meeting, events, schema_matched)."""
    fm, body = parse_frontmatter(md)

    title = ""
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if m:
        title = m.group(1).strip()

    # date: frontmatter > any date in the header area > filename
    date = normalize_date(fm.get("meeting_date", "")) or \
        normalize_date(fm.get("date", "")) or \
        normalize_date("\n".join(body.splitlines()[:12])) or \
        normalize_date(Path(source_path).stem)

    attendees: list[dict] = []
    # markdown table with a 单位/职务 column, or "参会人：A、B"
    m = re.search(r"参会[人方员][^\n]*[:：]\s*(.+)", body)
    if m:
        for nm in re.split(r"[、,，\s]+", m.group(1)):
            nm = nm.strip("（）() ")
            if 1 < len(nm) <= 12 and not nm.startswith("未"):
                attendees.append({"name": nm, "org": ""})
    for row in re.findall(r"^\|\s*(\d+)\s*\|\s*([^|]{1,12})\s*\|\s*([^|]{0,16})\s*\|",
                          body, re.MULTILINE):
        nm, org = row[1].strip(), row[2].strip()
        if nm and not any(a["name"] == nm for a in attendees):
            attendees.append({"name": nm, "org": org})

    sections = split_sections(body)
    matched = bool(sections)

    meeting = Meeting(
        meeting_id=meeting_id, date=date, title=title or Path(source_path).stem,
        source_path=source_path,
        source_kind="md",
        attendees=attendees,
        extractor="rule",
    )

    events: list[Event] = []
    seq = 0

    def add(etype: str, content: str, section: str) -> None:
        nonlocal seq
        content = re.sub(r"\s+", " ", content).strip(" -–—·")
        if len(content) < 4:
            return
        if NOT_EVENT.match(content):
            return
        owner = ""
        # a trailing （期限：X） belongs in the deadline field, not in the text
        dm = DEADLINE_SUFFIX.search(content)
        explicit_deadline = ""
        if dm:
            explicit_deadline = dm.group(1).strip()
            content = DEADLINE_SUFFIX.sub("", content).strip()
        if etype not in TYPES:
            etype = "其他"
        seq += 1
        ev = Event(
            meeting_id=meeting_id, type=etype, content=content,
            deadline=explicit_deadline or find_deadline(content), owner=owner,
            section=section,
            source_ref=f"{source_path}#{section}" if section else source_path,
            seq=seq,
        )
        events.append(ev)

    # ---- format B: bullets annotating their own type ----------------------
    # Handles "- **[甲方要求]** ... （期限：9月5日前）" anywhere in the document.
    inline_handled = 0
    for line in body.splitlines():
        m2 = INLINE_TYPE.match(line)
        if not m2:
            continue
        raw_type, raw_content = m2.group(1).strip(), m2.group(2).strip()
        # normalise a few synonyms that appear in human-written minutes
        alias = {"客户要求": "甲方要求", "业主确认": "甲方确认", "确认": "甲方确认",
                 "承诺": "己方承诺", "待办": "行动项", "决定": "决策",
                 "客户确认": "甲方确认", "要求": "甲方要求"}
        add(alias.get(raw_type, raw_type), raw_content, "标注")
        inline_handled += 1
    if inline_handled:
        return meeting, events, True

    # ---- 关键决策 -> 决策 ----
    for line in sections.get("决策", []):
        s = line.strip()
        if not s or s.startswith(">") or s.startswith("|"):
            continue
        if re.match(r"^[-*]\s*\[", s):     # a checkbox in the decisions section
            nm = re.sub(r"^[-*]\s*\[[ xX]?\]?\s*", "", s)
            add("决策", nm, "决策")
            continue
        if s.startswith(("-", "*", "•")):
            body_txt = re.sub(r"^[-*•]\s*", "", s)
            owner = ""
            mb = re.match(r"^\*\*(.+?)\*\*\s*[:：]?\s*(.*)$", body_txt)
            if mb:
                owner, body_txt = mb.group(1).strip(), mb.group(2).strip()
            if body_txt:
                add("决策", body_txt, "决策")
                if owner:
                    events[-1].owner = owner
            continue
        add("决策", s, "决策")

    # ---- 行动项 -> 行动项 ----
    for line in sections.get("行动项", []):
        s = line.strip()
        if not s or s.startswith(">") or s.startswith("|"):
            continue
        if not re.match(r"^[-*]\s*(\[[ xX]?\]\s*)?", s):
            continue
        # "本次会议未产生明确行动项。" is a statement, not an item
        if re.search(r"未(?:产生|形成|提及|记录|明确)|无(?:明确|具体)?(?:行动项|待办)", s):
            continue
        body_txt = re.sub(r"^[-*]\s*(\[[ xX]?\]\s*)?", "", s)
        owner = ""
        for pat, gi, gt in ((OWNER_BOLD, 1, 2), (OWNER_TRAIL, 2, 1)):
            m2 = pat.match(s)
            if m2:
                owner = m2.group(gi).strip()
                body_txt = m2.group(gt).strip()
                break
        add("行动项", body_txt, "行动项")
        if owner:
            events[-1].owner = owner

    # ---- 议题要点 -> 议题 ----
    for line in sections.get("议题", []):
        s = line.strip()
        if not s or s.startswith(">") or s.startswith("|"):
            continue
        if s.startswith(("-", "*", "•")):
            add("议题", re.sub(r"^[-*•]\s*", "", s), "议题")

    # ---- 后续跟进 -> 进度 (follow-ups are forward-looking commitments) ----
    for line in sections.get("跟进", []):
        s = line.strip()
        if not s or s.startswith(">") or s.startswith("|"):
            continue
        if s.startswith(("-", "*", "•")):
            add("己方承诺", re.sub(r"^[-*•]\s*", "", s), "跟进")

    return meeting, events, matched


# ── dispatcher: pick rule vs LLM automatically ───────────────────────────

def meeting_id_for(path: str | Path) -> str:
    """Stable per-FILE id.

    Using the bare filename stem collided in practice: a real minute existed both as
    ``项目会议纪要（20260831）.docx`` and as a Markdown copy with the same stem, and the
    second import silently replaced the first (17 events overwrote 11). Hashing the
    resolved path keeps distinct files distinct while staying stable across re-imports.
    """
    p = Path(path)
    try:
        key = str(p.resolve())
    except OSError:
        key = str(p)
    return f"{p.stem[:40]}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}"


def extract_document(path: str | Path, use_llm: bool = True) -> tuple[Meeting, list[Event], str]:
    """Extract events from one document, choosing the cheap path when possible.

    Rule extraction runs first because it is deterministic, free and hallucination-free;
    it only works on notes that follow the known section schema. Anything else (a
    human-written .docx, a free-form note) falls through to the LLM, which is batched
    by clause. Returns (meeting, events, path_used) where path_used is
    "rule" | "llm" | "none".
    """
    p = Path(path)
    suffix = p.suffix.lower()
    mid = meeting_id_for(p)

    if suffix in (".md", ".markdown", ".txt"):
        md = p.read_text(encoding="utf-8", errors="replace")
        mtg, evs, matched = extract_rules(md, mid, str(p))
        if matched and evs:
            return mtg, evs, "rule"
        if not use_llm:
            return mtg, evs, "rule" if evs else "none"
        evs2 = extract_llm(md, mid, str(p))
        mtg.extractor = "llm"
        return mtg, evs2, "llm" if evs2 else "none"

    if suffix == ".docx":
        from rag.docread import docx_meeting_metadata, docx_minutes_body, normalize_date_text

        meta = docx_meeting_metadata(p)
        body = docx_minutes_body(p)
        date = normalize_date_text(meta.get("date", "")) or normalize_date_text(mid) or ""
        mtg = Meeting(
            meeting_id=mid, date=date,
            title=meta.get("topic") or p.stem, source_path=str(p), source_kind="docx",
            attendees=meta.get("attendees", []), extractor="llm",
        )
        # A .docx whose body happens to carry the section schema can still go the
        # cheap route; check before paying for the model.
        _, r_events, matched = extract_rules(body, mid, str(p))
        if matched and r_events:
            mtg.extractor = "rule"
            return mtg, r_events, "rule"
        if not use_llm or not body:
            return mtg, [], "none"
        return mtg, extract_llm(body, mid, str(p)), "llm"

    raise ValueError(f"unsupported document type: {suffix}")


# ── LLM extraction (schema-less documents: external .docx, free-form minutes) ──

LLM_PROMPT = """把下面 {n} 条会议纪要分句转成事件条目，输出 JSON 数组。

字段：
- "i": 分句编号
- "type": "决策" | "行动项" | "甲方要求" | "甲方确认" | "己方承诺" | "风险" | "进度" | "议题"
- "content": 内容，用原文措辞
- "deadline": 原文出现的期限（如 "9月5日前"），没有则 ""
- "owner": 涉及方，没有则 ""

规则：
1. **只抽取有实际内容的条目**。会议元数据（版本号、日期、时间、地点、与会人员名单、
   表格表头、章节标题）一律不要输出。
2. 不许编造原文没有的日期或单位。
3. **一条分句原则上就是一个条目**。只有当一个分句里包含明显互不相关的多项事务时
   才拆开；不要把一个句子切成若干碎片（实测会把 9 句切成 14 条，读起来很碎）。
4. 只输出 JSON 数组。

分句：
{clauses}"""


def _parse_json_array(raw: str) -> list | None:
    if not raw:
        return None
    cands = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", raw)]
    cands += [m.group(0) for m in re.finditer(r"\[[\s\S]*\]", raw)]
    for cand in cands:
        for attempt in (cand,
                        re.sub(r",\s*([\]}])", r"\1", cand)
                        .replace("“", '"').replace("”", '"').replace("，", ",")):
            try:
                got = json.loads(attempt)
                if isinstance(got, list):
                    return got
            except Exception:
                pass
        objs = []
        for om in re.finditer(r"\{[^{}]*\}", cand):
            try:
                o = json.loads(re.sub(r",\s*}", "}", om.group(0)))
                if isinstance(o, dict) and o.get("content"):
                    objs.append(o)
            except Exception:
                continue
        if objs:
            return objs
    return None


def extract_llm(text: str, meeting_id: str, source_path: str,
                batch: int = 6) -> list[Event]:
    """LLM extraction, batched by clause.

    Batching is not a nicety: feeding the whole 418-char minute in one shot made the
    local 4B model emit 1894 chars of corrupt JSON (doubled quotes, repeated keys).
    Six clauses per call produced clean, fully-grounded output.
    """
    from dotenv import load_dotenv

    load_dotenv(Path.home() / ".hermes" / ".env")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from llm_client import llm_chat

    clauses = [c.strip() for c in re.split(r"(?<=[。；！？])", text) if c.strip()]
    events: list[Event] = []
    seq = 0
    for start in range(0, len(clauses), batch):
        chunk = clauses[start:start + batch]
        numbered = "\n".join(f"{start + i + 1}. {c}" for i, c in enumerate(chunk))
        raw = llm_chat(LLM_PROMPT.format(n=len(chunk), clauses=numbered), timeout=180)
        items = _parse_json_array(raw or "") or []
        for it in items:
            if not isinstance(it, dict):
                continue
            content = str(it.get("content", "")).strip()
            # Metadata and section headings are not events. The model emits them
            # anyway when a document's tables are in the input (measured: 17 of 30
            # items were "提交版本 V1.0" / "会议地点 线上腾讯会议" / name rosters),
            # so filter rather than trust the prompt.
            if len(content) < 8 or NOT_EVENT.match(content):
                continue
            if re.match(r"^\d+\s+\S+\s+\S+$", content):      # "1 林浩然 甲方单位"
                continue
            if re.match(r"^(?:职务|序号|单位|备注)\b", content):
                continue
            etype = str(it.get("type", "")).strip() or "其他"
            if etype not in TYPES:
                etype = "其他"
            deadline = str(it.get("deadline", "")).strip()
            # ground the deadline: a fabricated date quoted back is worse than none
            if deadline and deadline.replace(" ", "") not in text.replace(" ", ""):
                deadline = ""
            if not deadline:
                deadline = find_deadline(content)
            seq += 1
            events.append(Event(
                meeting_id=meeting_id, type=etype, content=content,
                deadline=deadline, owner=str(it.get("owner", "")).strip(),
                section="llm", source_ref=source_path, seq=seq,
            ))
    return events
