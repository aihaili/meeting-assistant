"""Markdown-aware chunking for the plaud RAG module.

Splits a Markdown document into retrieval units of ~``target_size`` characters,
preserving the heading path each chunk lives under. Overlap between adjacent
chunks is achieved by *budgeting*: a chunk's own content is capped at
``target_size - overlap`` so that after the previous chunk's tail is prepended
the final text still fits within ``target_size``.

Tables are kept atomic (a split table is worse than a slightly oversized chunk),
as are fenced code blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_TARGET = 450
DEFAULT_MIN = 120
DEFAULT_OVERLAP = 50

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# Generator banners such as <!-- Generated: 2026-09-12T08:37:57 --> carry no
# retrieval value and would otherwise be chunked as their own fragment.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# Chinese sentence terminators followed by an optional closing quote/bracket.
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])(?=[^”’）」』】]|$)")


@dataclass
class Chunk:
    """One retrieval unit.

    ``start`` 是段首在本文档里的字符偏移。它只有一个用处：把过短的尾段并回上一段时
    要保住上一段的起点。原来还有个 ``end`` 字段，写进去之后**没有任何地方读**，
    已删（本来也只在这两个构造点上算过一次）。
    """

    text: str
    heading: str
    start: int


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and len(s) > 1


# Lines that are pure Markdown decoration and carry no retrievable content.
_NOISE_RE = re.compile(r"^(?:[-*_=]{3,}|[-*_]{2,}\s*)$")


def _is_noise(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if _NOISE_RE.match(s):
        return True
    # Italic-only footnote lines like "*共 0 人有待办*" / "*按时间排列*"
    if len(s) <= 24 and re.fullmatch(r"[*_][^*_]{0,20}[*_]", s):
        return True
    return False


def _parse_blocks(lines: list[str]) -> list[tuple[str, str]]:
    """Split lines into ordered (kind, text) blocks.

    kind is one of: ``heading``, ``table``, ``code``, ``text``.
    """
    blocks: list[tuple[str, str]] = []
    buf: list[str] = []
    i = 0
    n = len(lines)

    def flush_text() -> None:
        nonlocal buf
        if buf:
            kept = [ln for ln in buf if not _is_noise(ln)]
            joined = "\n".join(kept).strip()
            if joined:
                blocks.append(("text", joined))
            buf = []

    while i < n:
        line = lines[i]

        if _FENCE_RE.match(line):
            flush_text()
            fence = line.strip()[:3]
            code = [line]
            i += 1
            while i < n:
                code.append(lines[i])
                if lines[i].strip().startswith(fence):
                    i += 1
                    break
                i += 1
            blocks.append(("code", "\n".join(code)))
            continue

        m = _HEADING_RE.match(line)
        if m:
            flush_text()
            blocks.append(("heading", line.strip()))
            i += 1
            continue

        if _is_table_row(line):
            flush_text()
            table = []
            while i < n and _is_table_row(lines[i]):
                table.append(lines[i].strip())
                i += 1
            blocks.append(("table", "\n".join(table)))
            continue

        buf.append(line)
        i += 1

    flush_text()
    return blocks


def _split_sentences(text: str) -> list[str]:
    """Split text into sentence-ish units, keeping terminators attached."""
    parts = [p for p in _SENT_SPLIT_RE.split(text) if p and p.strip()]
    return parts or [text]


def _split_oversized(text: str) -> list[str]:
    """Hard-split a single unit that exceeds any reasonable chunk size."""
    out: list[str] = []
    for idx in range(0, len(text), 200):
        out.append(text[idx:idx + 200])
    return out


def _pack_units(units: list[str], budget: int) -> list[str]:
    """Greedily pack units into chunks no larger than ``budget``."""
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0

    for unit in units:
        for piece in ([unit] if len(unit) <= budget else _split_oversized(unit)):
            plen = len(piece)
            if cur and cur_len + plen > budget:
                chunks.append("".join(cur).strip())
                cur, cur_len = [], 0
            cur.append(piece)
            cur_len += plen
    if cur:
        chunks.append("".join(cur).strip())
    return [c for c in chunks if c]


def _unit_text(kind: str, text: str) -> list[str]:
    """Convert one block into a list of packable units."""
    if kind == "text":
        # Re-attach a newline to sentence pieces so paragraphs stay readable.
        return [p if p.endswith("\n") else p + "\n" for p in _split_sentences(text)]
    return [text + "\n"]


def chunk_markdown(
    text: str,
    target_size: int = DEFAULT_TARGET,
    min_size: int = DEFAULT_MIN,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Chunk Markdown ``text`` into overlapping heading-aware pieces."""
    if not text or not text.strip():
        return []

    # Drop generator banners / comments before any splitting.
    text = _COMMENT_RE.sub("", text)
    if not text.strip():
        return []

    budget = max(80, target_size - max(0, overlap))
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = _parse_blocks(lines)

    # Heading path stack: level -> heading text
    stack: dict[int, str] = {}
    chunks: list[Chunk] = []

    # Group blocks into sections: a section starts at a heading, or at the
    # document start when there is leading content.
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    cur_heading_path = ""
    cur_blocks: list[tuple[str, str]] = []

    for kind, btext in blocks:
        if kind == "heading":
            m = _HEADING_RE.match(btext)
            level = len(m.group(1))
            title = m.group(2).strip()
            # pop deeper or equal levels
            for lv in [lv for lv in stack if lv >= level]:
                stack.pop(lv, None)
            stack[level] = title
            if cur_blocks:
                sections.append((cur_heading_path, cur_blocks))
                cur_blocks = []
            cur_heading_path = " > ".join(stack[lv] for lv in sorted(stack))
            # The heading line itself opens the section body.
            cur_blocks.append(("heading", btext))
            continue
        cur_blocks.append((kind, btext))

    if cur_blocks:
        sections.append((cur_heading_path, cur_blocks))

    cursor = 0
    for heading_path, sec_blocks in sections:
        units: list[str] = []
        for kind, btext in sec_blocks:
            units.extend(_unit_text(kind, btext))
        packed = _pack_units(units, budget)
        prev_tail = ""
        for piece in packed:
            body = piece
            if prev_tail:
                body = prev_tail + "\n" + piece
            body = body.strip()
            if not body:
                continue
            # Merge a too-small trailing fragment into the previous chunk.
            if len(body) < min_size and chunks and chunks[-1].heading == heading_path:
                merged = chunks[-1].text + "\n" + body
                start = chunks[-1].start
                chunks[-1] = Chunk(text=merged, heading=heading_path, start=start)
                prev_tail = body[-overlap:] if overlap else ""
                cursor += len(body)
                continue
            chunks.append(Chunk(text=body, heading=heading_path, start=cursor))
            prev_tail = body[-overlap:] if overlap else ""
            cursor += len(body)

    return chunks


def chunk_text(
    text: str,
    target_size: int = DEFAULT_TARGET,
    min_size: int = DEFAULT_MIN,
    overlap: int = DEFAULT_OVERLAP,
) -> list[tuple[str, str]]:
    """Convenience wrapper returning ``(heading, text)`` pairs."""
    return [(c.heading, c.text) for c in
            chunk_markdown(text, target_size=target_size, min_size=min_size, overlap=overlap)]
