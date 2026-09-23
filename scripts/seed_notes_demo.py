"""Populate the running meeting server with a sticky-note demo session.

Created because driving the API from PowerShell silently mangled the Chinese request
bodies -- `ConvertTo-Json` escaped them and the server received unusable text, so four
distinct notes arrived as four empty strings and collapsed into one. That is now
rejected by the server, but the lesson is to drive this API from Python where the
encoding is unambiguous.

Usage:
    python scripts/seed_notes_demo.py [--port 8510]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

NOTES = [
    ("铺线以机房为重点，各房间网络与电源同步调通", "report", [
        {"title": "项目会议纪要（20260831）",
         "snippet": "铺线以机房为重点展开，各房间网络和电源调通，为部署联调联试打好基础，"
                    "争取在两三天内完成铺线。",
         "label": "历史约定", "score": 1.0},
    ]),
    ("软件联调联试：已定两位开发人员，下周一进场", "report", [
        {"title": "项目会议纪要（20260907）",
         "snippet": "公司要增派软件开发人员负责软件联调联试和配合评估，"
                    "9月5日前软件部署调通完成。",
         "label": "条款依据", "score": 0.98},
    ]),
    ("已发货物资：检测报告、合格证、产品说明书", "topic", [
        {"title": "项目会议纪要（20260831）",
         "snippet": "已发货的物资要收集准备第三方检测报告、合格证、产品说明书。",
         "label": "甲方要求", "score": 1.0},
    ]),
    ("未发货物资：合同与采购计划证明 9/20 前到货", "confirm", [
        {"title": "项目会议纪要（20260831）",
         "snippet": "在9月10日到不了的未发货的物资需准备好合同、采购计划等，"
                    "以证明在9月20日或15日之前可弄好。",
         "label": "时间节点", "score": 1.0},
    ]),
]

# A 2x2 arrangement clear of the left column, so the notes are readable without dragging
# first. These are *page* coordinates now, not column-relative: the notes layer covers the
# whole viewport (that is what lets a note sit anywhere), so x must clear the 264px
# participant column or the notes land on top of it.
LAYOUT = [(292, 78), (542, 78), (292, 250), (542, 250)]


# A few utterances so the feed, the highlight layer and the "drag a sentence into a
# note" affordance all have something to work with. Without them the demo shows an
# empty-state page and the drag gesture -- the whole point of the notes layer -- cannot
# be exercised at all.
SEGMENTS = [
    ("林浩然", "第三方测评人员九月五号左右进场，这个时间不能再往后退了。", 5.5, 8.5),
    ("孙磊", "铺线这一块我们以机房为重点展开，各房间的网络和电源同步调通。", 17.1, 21.6),
    ("孙磊", "已发货的物资我们会把第三方检测报告、合格证、产品说明书都收集齐。", 36.1, 41.8),
    ("林浩然", "九月十号之前 VR 体验必须完整，这两个节点是硬指标。", 27.0, 31.4),
]


def req(port: int, path: str, payload: dict | None = None, method: str = "POST"):
    url = f"http://127.0.0.1:{port}{path}"
    if payload is None:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    body = json.dumps(payload).encode("utf-8")
    rq = urllib.request.Request(url, data=body,
                                headers={"Content-Type": "application/json"},
                                method=method)
    with urllib.request.urlopen(rq, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8510)
    ap.add_argument("--outline", default=str(ROOT / "data" / "outlines" / "meeting-outline.docx"))
    args = ap.parse_args()

    # 发言：先灌几段，界面才有内容可看、拖拽才有东西可拖
    for who, text, start, end in SEGMENTS:
        try:
            req(args.port, "/api/segment",
                {"text": text, "start": start, "end": end})
        except Exception as e:  # noqa: BLE001
            print(f"  发言灌入失败: {type(e).__name__}: {e}")
    st0 = req(args.port, "/api/state?since=0")
    print(f"发言: {len(st0['segments'])} 段  线索: {len(st0['clues'])} 条")

    # 会议流程：从 docx 导入，验证导入链路本身
    try:
        imp = req(args.port, "/api/agenda/import", {"path": args.outline})
        print(f"会议流程: 导入 {imp['count']} 项  ({imp['note']})")
    except Exception as e:  # noqa: BLE001
        print(f"会议流程导入失败: {type(e).__name__}: {e}")

    # 发言计划：四张便签
    ids = []
    for topic, kind, _refs in NOTES:
        d = req(args.port, "/api/prep", {"action": "add", "topic": topic, "kind": kind})
        item = d.get("item") or {}
        if not item:
            print(f"  新建失败: {d}")
            continue
        ids.append(item["id"])
        print(f"  便签 + {item['id']}  {topic[:24]}")

    if not ids:
        return 1
    print(f"\n便签数: {len(ids)}   id 唯一: {len(set(ids)) == len(ids)}")

    # 摆位置
    items = [{"id": ids[i], "nx": LAYOUT[i % len(LAYOUT)][0], "ny": LAYOUT[i % len(LAYOUT)][1],
              "nw": 232, "nh": 158, "nz": 10 + i} for i in range(len(ids))]
    lay = req(args.port, "/api/prep/layout", {"items": items})
    print(f"布局: 保存 {lay['saved']} 张")

    # 钉参考
    n_ref = 0
    for i, (_t, _k, refs) in enumerate(NOTES):
        for ref in refs:
            r = req(args.port, "/api/prep",
                    {"action": "attach_ref", "id": ids[i], "ref": ref})
            n_ref += len((r.get("item") or {}).get("refs") or [])
    print(f"参考: 共钉上 {n_ref} 条")

    st = req(args.port, "/api/state?since=0")
    print(f"\n最终 会议流程 {len(st['agenda'])} 项 · 便签 {len(st['prep'])} 张")
    for p in st["prep"]:
        print(f"  ({p['nx']:>4.0f},{p['ny']:>4.0f}) 参考{len(p['refs'])}条  {p['topic'][:30]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
