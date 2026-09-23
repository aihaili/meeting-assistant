r"""Document readers: turn .md / .docx / .txt into plain text for extraction.

Why this exists: parsing .docx with regexes broke twice on the same real file --
the minutes body lives in a TABLE CELL, and cells can nest ``<w:p>`` elements, which a
greedy/non-greedy regex handles inconsistently (one attempt read 18 chars of headings
and the model "extracted" those; another read 0). ``xml.etree`` handles nesting
correctly, so use it.

Markdown stays the primary input: this software's own pipeline emits Markdown, so
producing a .docx only to parse it back would be self-inflicted. The .docx path is a
compatibility branch for minutes written by people or by other tools.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Paragraphs that are clearly metadata rather than minutes content.
META_LINE = re.compile(
    r"^(?:提交版本|提交日期|会议信息|会议类型|会议内容|会议地点|日期|时间|记录单位|"
    r"序号|与会人员|职务|单位|备注|数据来源)\b")


def docx_paragraphs(path: str | Path) -> list[str]:
    """All paragraphs in document order, table cells included, nesting handled."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    out: list[str] = []

    def walk(node) -> None:
        for child in node:
            tag = child.tag
            if tag == f"{W}p":
                text = "".join(t.text or "" for t in child.iter(f"{W}t"))
                text = text.strip()
                if text:
                    out.append(text)
            else:
                walk(child)

    walk(root)
    return out


def docx_tables(path: str | Path) -> list[list[str]]:
    """Table rows as lists of cell strings (used for metadata + attendee rosters)."""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    rows: list[list[str]] = []
    for tbl in root.iter(f"{W}tbl"):
        for tr in tbl.findall(f"{W}tr"):
            cells: list[str] = []
            for tc in tr.findall(f"{W}tc"):
                txt = "".join(t.text or "" for t in tc.iter(f"{W}t")).strip()
                if txt:
                    cells.append(txt)
            if cells:
                rows.append(cells)
    return rows


def docx_meeting_metadata(path: str | Path) -> dict:
    """Pull date / type / place / attendees out of a minutes table.

    Metadata handled here rather than by the extractor: asking the model for it made
    it emit "提交版本 V1.0" and the attendee roster as *events* (17 of 30 items).
    """
    rows = docx_tables(path)
    meta: dict = {"attendees": []}
    KEYMAP = {"日期": "date", "时间": "time", "会议类型": "kind",
              "会议内容": "topic", "会议地点": "place", "提交版本": "version"}
    for r in rows:
        # Metadata rows come in two shapes: 2-cell (| 日期 | 2026.8.31 |) and
        # 4-cell (| 日期 | 2026.8.31 | 时间 | 17:30-18:00 |). Handle both by
        # scanning key/value pairs positionally.
        i = 0
        while i + 1 < len(r):
            key = r[i].strip()
            if key in KEYMAP:
                if not meta.get(KEYMAP[key]):
                    meta[KEYMAP[key]] = r[i + 1].strip()
                i += 2
            else:
                i += 1
        if len(r) >= 3 and re.match(r"^\d+$", r[0]):
            meta["attendees"].append({"name": r[1], "org": r[-1]})
    return meta


def docx_minutes_body(path: str | Path, min_chars: int = 120) -> str:
    """The substantive minutes text: the longest paragraph-ish blob in the file.

    The real minute keeps its entire body inside ONE table cell, so the body is a
    single long paragraph rather than a list of clauses.
    """
    paras = docx_paragraphs(path)
    if not paras:
        return ""
    body = max(paras, key=len)
    if len(body) >= min_chars:
        return body
    # fall back: join the non-metadata paragraphs
    keep = [p for p in paras if not META_LINE.match(p) and len(p) > 12]
    return "\n".join(keep)


def normalize_date_text(s: str) -> str:
    """'2026.8.31' / '2026-08-31' / '2026年8月31日' -> '2026-08-31'. '' if unparseable."""
    m = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", s or "")
    if not m:
        return ""
    y, mo, d = (int(g) for g in m.groups())
    if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def read_text_document(path: str | Path) -> tuple[str, dict]:
    """Return (text, metadata) for a supported document."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".docx":
        meta = docx_meeting_metadata(p)
        # Normalise here so dates from every reader sort identically. Mixed formats
        # ('2026-08-31' vs '2026-8-31') silently broke timeline ordering.
        if meta.get("date"):
            meta["date"] = normalize_date_text(meta["date"]) or meta["date"]
        return docx_minutes_body(p), meta
    if suffix in (".md", ".markdown", ".txt"):
        return p.read_text(encoding="utf-8", errors="replace"), {}
    raise ValueError(f"unsupported document type: {suffix}")
