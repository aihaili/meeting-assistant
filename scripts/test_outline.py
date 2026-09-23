"""Test the meeting-outline parser against the shapes hosts actually send.

A host's 会议流程 arrives as whatever they had: a Word file with 一、二、三 numbering, a
pasted chat message with bullets, a plain list of times with no markers at all. There is
no schema and no way to require one, so the parser is tested against each of these
shapes separately -- a parser that only handles the tidy case is a parser that fails on
the first real file.

Usage:
    python scripts/test_outline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from meeting.outline import parse_outline  # noqa: E402

CASES: list[tuple[str, list[str], list[dict]]] = [
    (
        "中文数字编号 + 时间 + 负责人",
        [
            "会议流程",
            "一、14:00 开场致辞（主持人：林浩然）",
            "二、14:10 项目进度汇报 负责人：孙磊",
            "三、14:40 第三方测评安排确认",
            "四、15:00 遗留问题跟进",
        ],
        [
            {"topic": "开场致辞", "slot": "14:00", "speaker": "林浩然", "kind": "topic"},
            {"topic": "项目进度汇报", "slot": "14:10", "speaker": "孙磊", "kind": "report"},
            {"topic": "第三方测评安排确认", "slot": "14:40", "speaker": "", "kind": "confirm"},
            {"topic": "遗留问题跟进", "slot": "15:00", "speaker": "", "kind": "followup"},
        ],
    ),
    (
        "阿拉伯数字编号 + 区间时间",
        [
            "1. 09:30-10:00 铺线进度汇报",
            "2. 10:00—10:30 机房调通情况说明",
            "3. 10:30-11:00 物资到货确认",
        ],
        [
            {"topic": "铺线进度汇报", "slot": "09:30-10:00", "kind": "report"},
            {"topic": "机房调通情况说明", "slot": "10:00—10:30", "kind": "report"},
            {"topic": "物资到货确认", "slot": "10:30-11:00", "kind": "confirm"},
        ],
    ),
    (
        "无编号的纯行列表",
        ["铺线进度", "软件联调联试进展", "VR 体验验收准备"],
        [
            {"topic": "铺线进度"},
            {"topic": "软件联调联试进展"},
            {"topic": "VR 体验验收准备"},
        ],
    ),
    (
        "项目符号 + 续行归入上一项",
        [
            "- 现场施工进度",
            "  甲方要求加快速度，增派人手",
            "- 感应器设备调通",
            "- 验收大纲准备",
        ],
        [
            {"topic": "现场施工进度", "detail": "甲方要求加快速度，增派人手"},
            {"topic": "感应器设备调通"},
            {"topic": "验收大纲准备"},
        ],
    ),
    (
        "上午/下午 + 中文点",
        ["上午九点 签到", "上午九点半 领导讲话", "下午两点 现场观摩"],
        [
            {"topic": "签到", "slot": "上午九点"},
            {"topic": "领导讲话", "slot": "上午九点半"},
            {"topic": "现场观摩", "slot": "下午两点"},
        ],
    ),
    (
        "夹杂空行与分隔线",
        ["## 会议议程", "", "一、开场", "-——-", "二、汇报", "   ", "三、总结"],
        [{"topic": "开场"}, {"topic": "汇报"}, {"topic": "总结"}],
    ),
]


def main() -> int:
    fails = 0
    for name, lines, expect in CASES:
        got = parse_outline(lines)
        ok = len(got) == len(expect)
        detail = ""
        if ok:
            for g, e in zip(got, expect):
                for k, v in e.items():
                    if k == "topic":
                        # Topics may keep or drop surrounding punctuation; compare the
                        # core string.
                        if v not in g["topic"] and g["topic"] not in v:
                            ok, detail = False, f"topic {g['topic']!r} != {v!r}"
                            break
                    elif g.get(k, "") != v:
                        ok, detail = False, f"{k}: {g.get(k, '')!r} != {v!r}"
                        break
                if not ok:
                    break
        else:
            detail = f"条数 {len(got)} != {len(expect)}"
        if not ok:
            fails += 1
        print(f"\n  {'PASS' if ok else 'FAIL'}  {name}")
        if detail:
            print(f"        {detail}")
        for g in got:
            slot = f"[{g['slot']}] " if g.get("slot") else ""
            who = f" @{g['speaker']}" if g.get("speaker") else ""
            det = f"  ({g['detail'][:20]})" if g.get("detail") else ""
            print(f"        · {slot}{g['topic']}{who}  <{g.get('kind')}>{det}")

    print(f"\n{'全部通过' if fails == 0 else f'{fails} 处失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
