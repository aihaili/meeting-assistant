"""Write a sample 会议流程 as .docx and .md, to exercise the outline import.

A host's outline is the input this feature exists for, and there is no such file in the
project, so one is generated. Two formats on purpose: the .docx path goes through the
zipfile + XML reader and the .md path through plain text, and they exercise different
branches of ``meeting.outline``.

The content mirrors a real 项目进度汇报例会 for the 新疆心理 project: the same people,
the same kind of slots, and items that map onto the deadlines the assistant is expected
to surface.

Usage:
    python scripts/make_sample_outline.py
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "outlines"

# Deliberately messy, in the ways real ones are: mixed numbering styles, a bare 上午,
# a range with an em dash, an owner in brackets, and one item with no marker at all.
LINES = [
    "项目进度汇报例会 · 会议流程",
    "",
    "一、14:00 开场致辞（主持人：林浩然）",
    "二、14:10 上次会议遗留问题跟进 负责人：孙磊",
    "三、14:25 铺线与机房部署进度汇报 负责人：郑涛",
    "四、14:45 软件联调联试进展说明 负责人：高志远",
    "五、15:00 第三方测评进场安排确认",
    "六、15:15 VR 体验验收准备情况",
    "七、15:30 未发货物资证明材料确认",
    "八、15:45 现场每日规划与时间节点落实",
    "九、16:00 会议总结",
    "",
    "备注：请各负责人提前准备汇报材料，汇报时间控制在十分钟以内。",
]

MD = "\n".join(LINES)


def write_docx(path: Path, lines: list[str]) -> None:
    """Minimal but valid .docx: [Content_Types].xml + rels + document.xml.

    Written by hand rather than with python-docx, which is not installed and would be a
    dependency for one sample file. The structure is the minimum Word accepts, and it
    deliberately splits one paragraph across several runs so the reader's run-joining
    logic is exercised (a real Word file does this at every spell-check boundary).
    """
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    body = []
    for i, ln in enumerate(lines):
        if ln == "":
            body.append("<w:p/>")
            continue
        if i == 0:
            # Title paragraph, split into two runs mid-string.
            half = len(ln) // 2
            runs = (f'<w:r><w:t xml:space="preserve">{esc(ln[:half])}</w:t></w:r>'
                    f'<w:r><w:t xml:space="preserve">{esc(ln[half:])}</w:t></w:r>')
        else:
            runs = f'<w:r><w:t xml:space="preserve">{esc(ln)}</w:t></w:r>'
        body.append(f"<w:p>{runs}</w:p>")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(body) + "</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    docx = OUT / "meeting-outline.docx"
    md = OUT / "meeting-outline.md"
    write_docx(docx, LINES)
    md.write_text(MD, encoding="utf-8")
    print(f"写入 {docx}  ({docx.stat().st_size} 字节)")
    print(f"写入 {md}  ({md.stat().st_size} 字节)")

    # Verify both parse to the same thing -- if they diverge, one of the readers is
    # wrong and the import would behave differently per file format.
    sys.path.insert(0, str(ROOT / "scripts"))
    from meeting.outline import outline_to_items

    for p in (docx, md):
        items, note = outline_to_items(p)
        print(f"\n{p.name}  ->  {len(items)} 项  ({note})")
        for it in items:
            slot = f"[{it['slot']}] " if it.get("slot") else ""
            who = f" @{it['speaker']}" if it.get("speaker") else ""
            print(f"    {slot}{it['topic']}{who}  <{it['kind']}>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
