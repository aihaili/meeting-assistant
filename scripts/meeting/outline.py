"""Parse a meeting outline out of whatever the host actually sent.

The host's 会议流程 arrives as a Word file, a pasted message, a photo of a printed
sheet, or a line in a chat. There is no schema, and asking the host to use one has been
tried by everyone who has ever organised a meeting and works for nobody. So the parser
accepts the shapes that actually occur and extracts structure from formatting:

* ``一、二、三`` / ``1. 2. 3.`` / ``(1)`` numbering -> a new item
* ``-`` ``*`` ``•`` bullets -> a new item
* a leading time (``14:00``, ``9:30-10:00``, ``上午``) -> an item's time slot
* ``负责人：`` / ``主持：`` / ``@名字`` -> who leads the slot
* anything else that is short and not punctuation -> a heading-less item

DOCX is read with the same ``xml.etree`` approach already used by ``rag.docread``
(zipfile + XML), because python-docx is not installed and a dependency for reading one
file format is not worth it. Reusing the existing reader also means the nested-paragraph
bug it documents -- text hiding inside ``w:p`` inside tables -- is fixed in one place.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# Ordering markers that begin a new item.
_NUM = re.compile(
    r"^\s*(?:"
    r"[一二三四五六七八九十]+[、.．)）]"
    r"|第[一二三四五六七八九十\d]+[项条点]"
    r"|\d+[、.．)）]"
    r"|[(（]\d+[)）]"
    r"|[（(][一二三四五六七八九十]+[)）]"
    r")\s*"
)
_BULLET = re.compile(r"^\s*[-*•·‣◦]\s+")
# A time at the start of a line: 14:00, 9:30-10:00, 上午, 下午三点, 09:00—10:30.
# A bare 上午/下午 is only a *prefix* -- the real slot usually continues right after it
# ("上午九点 签到"), so the match is extended by _SLOT_MORE rather than stopping at the
# day part. 半/一刻 are part of the clock reading ("九点半") and must be consumed with
# it, or the slot comes out as "上午九点" and leaves a stray "半" in the topic.
_HALF = r"(?:半|一刻|三刻|整)?"
_TIME = re.compile(
    r"^\s*(?:"
    r"[上下]午\s*"
    r"|\d{1,2}\s*[:：]\s*\d{2}\s*(?:[-—~至到]\s*\d{1,2}\s*[:：]\s*\d{2})?"
    r"|[一二三四五六七八九十两]{1,3}\s*[点时时]" + _HALF +
    r"(?:\s*[-—~至到]\s*[一二三四五六七八九十两]{1,3}\s*[点时时]" + _HALF + r")?"
    r")\s*[-—:：、.]?\s*"
)
# Continuation of a date-style slot: the clock part after a bare 上午/下午, or a range.
_SLOT_MORE = re.compile(
    r"^(?:[一二三四五六七八九十两\d]{1,3}\s*[点时时]" + _HALF +
    r"(?:\s*[-—~至到]\s*[一二三四五六七八九十两\d]{1,3}\s*[点时时]" + _HALF + r")?"
    r"|\d{1,2}\s*[:：]\s*\d{2}(?:\s*[-—~至到]\s*\d{1,2}\s*[:：]\s*\d{2})?)"
)

# Document titles. A host's outline usually starts with 「会议流程」 or 「XX会议议程」,
# which is the document's name, not an agenda item. Only skipped before the first real
# item, so a genuine item that happens to end in 安排 is never lost.
_TITLE = re.compile(r"会议(?:流程|议程|安排|日程)$|^(?:流程|议程|日程|安排)表?$|"
                    r"^[\u4e00-\u9fff]{2,12}(?:会议|评审会|例会)(?:议程|流程|安排)?$")
# "负责人：张三" / "主持人：李四" / "（张三）" / "@张三"
# 主持人 must be listed explicitly: a pattern of just "主持" does not match "主持人：",
# because the character in front of the colon is 人, not 持.
_OWNER = re.compile(
    r"(?:负责人|主持人|主持|主讲|汇报人|汇报|召集人|召集)\s*[:：]\s*([\u4e00-\u9fff]{2,4})"
    r"|[@＠]([\u4e00-\u9fff]{2,4})"
    r"|[（(]\s*([\u4e00-\u9fff]{2,4})\s*[)）]\s*$"
)
_NOISE = re.compile(r"^[\s\-—_=*#·。，、；：]+$")
# A trailing note, not an agenda item. Real outlines routinely end with one
# ("备注：请各负责人提前准备汇报材料"), and importing it as the tenth item makes the user
# delete it before the meeting even starts.
_NOTE = re.compile(r"^\s*(?:备注|说明|注意|注|提示)\s*[:：]")
# What is left of "开场致辞（主持人：林浩然）" after the owner is stripped.
_EMPTY_BRACKETS = re.compile(r"[（(]\s*[)）]")

def read_docx_paragraphs(path: str | Path) -> list[str]:
    """Every paragraph of a .docx, in document order, tables included.

    Namespaces are matched by wildcard rather than by the literal ``w:`` prefix: the
    prefix is a document-local alias, and files produced by WPS or by older Word builds
    sometimes bind it differently.
    """
    with zipfile.ZipFile(path) as z:
        with z.open("word/document.xml") as f:
            tree = ET.parse(f)
    out: list[str] = []
    for p in tree.iter():
        if not p.tag.endswith("}p"):
            continue
        # Join the runs of one paragraph; a line is routinely split across several
        # <w:r> elements (spell-check boundaries, formatting changes) and taking only
        # the first run yields truncated headings.
        parts = [t.text or "" for t in p.iter() if t.tag.endswith("}t")]
        line = "".join(parts).strip()
        if line:
            out.append(line)
    return out


def read_outline(path: str | Path) -> tuple[list[dict], str]:
    """Read an outline file. Returns (items, note) where note explains any fallback."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    ext = p.suffix.lower()
    if ext == ".docx":
        lines = read_docx_paragraphs(p)
        note = f"docx 读取 {len(lines)} 段"
    elif ext in (".md", ".txt", ".text", ""):
        raw = p.read_text(encoding="utf-8", errors="replace")
        lines = [ln.strip() for ln in raw.splitlines()]
        note = f"文本读取 {len(lines)} 行"
    elif ext == ".csv":
        raw = p.read_text(encoding="utf-8", errors="replace")
        lines = [ln.split(",")[0].strip() for ln in raw.splitlines()]
        note = "csv 取第一列"
    else:
        # Last resort: read it as text. A .doc or a renamed file usually still has the
        # headings visible in the byte stream, and showing *something* the user can edit
        # beats an error dialog.
        raw = p.read_text(encoding="utf-8", errors="replace")
        lines = [ln.strip() for ln in raw.splitlines()]
        note = f"未知扩展名 {ext}，按文本读取"
    return parse_outline(lines), note


# Words that begin a wrapped continuation rather than a new item.
_WRAP_START = re.compile(r"^(?:接下来|然后|另外|此外|其次|最后|同时|并且|而且|以及|"
                         r"其中|针对|关于|为了|由于|因此|所以|但|而|并|或|和|与|及|等)")


def _looks_like_its_own_item(current: dict, line: str) -> bool:
    """Whether an unmarked line starts a new item instead of wrapping the previous one.

    An unmarked short line is genuinely ambiguous: "软件联调联试进展" and a wrap fragment
    of the same length are indistinguishable without semantics. So the default follows
    the convention that a plain-text outline is **one item per line**, and a line is
    treated as a wrap only on positive evidence:

    * it begins with a connective (接下来/另外/以及 …), or
    * it continues an item that is already long enough to have wrapped (>=40 chars), or
    * the previous line trails a comma, i.e. an obviously unfinished clause.

    That default was chosen because the two mistakes are not symmetric. Splitting a
    wrapped sentence into two items is visible at a glance and takes one click to merge;
    silently collapsing a plain list into a single item hides the host's whole schedule
    behind one row, which is how the first version of this parser behaved.
    """
    t = (line or "").strip()
    if not t:
        return False
    if len(t) >= 40:
        return False                       # certainly prose, not a heading
    if _WRAP_START.match(t):
        return False
    prev_topic = (current or {}).get("topic", "")
    if len(prev_topic) >= 40:
        return False                       # a long item plausibly continues
    if prev_topic.rstrip().endswith(("，", ",", "、", "：", ":")):
        return False                       # previous line was mid-clause
    return True


def parse_outline(lines: list[str]) -> list[dict]:
    """Turn loose lines into agenda items, preserving the host's ordering and times.

    The item/continuation decision is what makes this non-trivial. An *unmarked* line is
    ambiguous: it is either a new item (plain lists are common) or a wrap of the one
    above (Word paragraphs wrap). The rule used here is **a line starts a new item iff
    it carries a marker -- numbering, a bullet, or a time**. Without that rule a plain
    three-line list collapses into a single item with the rest as its detail, which is
    exactly what the first version did.
    """
    items: list[dict] = []
    current: dict | None = None
    seen_item = False

    def flush() -> None:
        nonlocal current
        if current is not None and current["topic"]:
            current["order"] = len(items)
            items.append(current)
        current = None

    for raw in lines:
        if not (raw or "").strip():
            continue
        # Indentation is a continuation signal and must be read before stripping: a
        # wrapped line under a bullet is conventionally indented, and by the time the
        # whitespace is gone there is nothing left to tell it apart from an item.
        indented = bool(raw) and raw[0] in " \t\u3000"
        line = raw.strip()
        if _NOISE.match(line):
            continue
        # Markdown headings are section markers, not items.
        if line.startswith("#"):
            flush()
            continue
        if not seen_item and _TITLE.search(line):
            # The document's own name, not an item.
            continue
        if _NOTE.match(line):
            # Attach the note to the last item as detail rather than dropping it: it
            # often carries a real constraint ("汇报时间控制在十分钟以内").
            if items:
                note = _NOTE.sub("", line).strip()
                if note:
                    prev = items[-1]
                    prev["detail"] = ((prev.get("detail") or "") + " " + note).strip()
            continue

        body = line
        slot = ""
        # Numbering is stripped *before* the time is matched. The other order fails on
        # the most common real format -- "一、14:00 开场致辞" -- because the time pattern
        # is anchored at the start of the line and the marker is still in front of it.
        numbered = bool(_NUM.match(body))
        bulleted = bool(_BULLET.match(body))
        if numbered or bulleted:
            body = _BULLET.sub("", _NUM.sub("", body)).strip()

        m = _TIME.match(body)
        if m:
            slot = m.group(0).strip(" -—:：、.")
            body = body[m.end():].strip()
            # "上午" alone is not the slot; the clock right after it is.
            m2 = _SLOT_MORE.match(body)
            if m2:
                slot = (slot + m2.group(0)).strip()
                body = body[m2.end():].strip()

        if numbered or bulleted or slot:
            flush()
            is_item = True
        elif current is None:
            # First line of a list that uses no markers at all.
            is_item = True
            flush()
        elif not indented and _looks_like_its_own_item(current, line):
            # Unmarked text almost never wraps at a comma onto a line that reads as a
            # heading of its own. A plain list ("铺线进度 / 软件联调联试进展 / VR 体验准备")
            # has no markers at all, so waiting for one collapses it into a single item.
            flush()
            is_item = True
        else:
            is_item = False

        if is_item:
            seen_item = True
            owner = ""
            om = _OWNER.search(body)
            if om:
                owner = next(g for g in om.groups() if g)
                body = _OWNER.sub("", body).strip(" 　·-—")
                # Removing "主持人：林浩然" from "开场致辞（主持人：林浩然）" leaves empty
                # brackets behind; they read as a parsing failure to the user.
                body = _EMPTY_BRACKETS.sub("", body).strip()
            # The note branch appends to items[-1], which is the item *before* this one
            # being opened -- correct, because a trailing 备注 belongs to the outline it
            # follows. Nothing to do here for it.
            current = {"topic": body, "slot": slot, "speaker": owner, "detail": ""}
        else:
            # Continuation line: belongs to the item above.
            if current is None:
                continue
            detail = current.get("detail") or ""
            current["detail"] = (detail + " " + body).strip() if detail else body

    flush()
    # Kind is inferred here rather than in a separate pass, so any caller gets the same
    # classification. The type comes from the host's own wording -- the user's guess at
    # someone else's schedule would be worse, and a wrong label is editable.
    for it in items:
        t = it["topic"]
        if re.search(r"确认|审定|签字|拍板", t):
            it["kind"] = "confirm"
        elif re.search(r"汇报|介绍|说明|通报", t):
            it["kind"] = "report"
        elif re.search(r"上次|遗留|跟进|落实", t):
            it["kind"] = "followup"
        else:
            it["kind"] = "topic"
    return [it for it in items if it["topic"]]


def outline_to_items(path: str | Path) -> tuple[list[dict], str]:
    """Read a file and return (items, note). Items already carry an inferred ``kind``."""
    return read_outline(path)
