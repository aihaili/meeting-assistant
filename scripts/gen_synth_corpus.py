r"""Generate a synthetic multi-meeting corpus for architecture validation.

WHY: only ONE real meeting minute exists (项目会议纪要（20260831）.docx). With one
meeting you cannot measure cross-meeting retrieval, and my earlier throwaway attempt
failed for a methodological reason -- the synthetic meetings kept reusing the same
domain nouns (VR / 物资 / 验收), so the "correct" answer was genuinely ambiguous and the
test measured my own bias instead of the architecture.

RULES THIS GENERATOR FOLLOWS
1. Reuse the REAL project's cast and framing (新疆心理 project; 史塔克 as implementer;
   甲方单位 reviewer; 第三方测评; the real names from the 20260831 minute).
2. Every meeting gets DISTINCT, checkable facts (its own dates, deliverables,
   numbers) so a question has exactly one right answer.
3. Every produced file is EXPLICITLY MARKED synthetic, with a machine-readable front
   matter block, so it can never be mistaken for a real record.
4. Same document shape as the real minute (header metadata + a minutes body), so the
   same parsing and extraction code paths are exercised.

Output: data/synth-corpus/*.md  (markdown is what rag.ingest consumes directly)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(r"E:\markdown\meeting-assistant\data\synth-corpus")

# The real 20260831 minute (paraphrased faithfully) becomes meeting #1 so the corpus
# has a real anchor; it is labelled "real" and the rest "synthetic".
REAL_SEED = {
    "date": "2026-08-31",
    "weekday": "周一",
    "time": "17:30-18:00",
    "kind": "项目进度汇报例会",
    "place": "线上腾讯会议",
    "attendees": [
        ("林浩然", "甲方单位"), ("徐博文", "甲方单位"),
        ("孙磊", "史塔克"), ("高志远", "史塔克"), ("郑涛", "史塔克"),
        ("何嘉伟", "史塔克"), ("王毅", "史塔克"), ("罗建国", "史塔克"),
    ],
    "items": [
        ("甲方要求", "现场施工要加快速度，该加人加班需安排，机关布设设备明天到货后需立马对接好，让感应器部分设备可调通。", ""),
        ("甲方要求", "铺线以机房为重点展开，各房间网络和电源调通，为部署联调联试打好基础，争取在两三天内完成铺线。", "两三天内"),
        ("甲方要求", "公司要增派软件开发人员负责软件联调联试和配合评估，什么时候来、谁来，公司要尽快确定好，9月5日前软件部署调通完成。", "9月5日前"),
        ("甲方要求", "9月5日左右第三方测评人员进场。", "9月5日左右"),
        ("甲方要求", "会后如果VR体验不行，需抓紧时间调试，在9月10日前完整。", "9月10日前"),
        ("甲方要求", "在9月10日到不了的未发货的物资需准备好合同、采购计划等，以证明在9月20日或15日之前可弄好。", "9月20日前"),
        ("甲方要求", "已发货的物资要收集准备第三方检测报告、合格证、产品说明书。", ""),
        ("甲方要求", "现场的每日都要做一个规划，按照时间节点完工。", ""),
        ("风险", "薪资方面等隐患问题要提前解决，推动力度要大，该加人要加人。", ""),
        ("进度", "VR和指挥控制室要加快进度，9月3日左右黄平工程师把软件能提的问题都提下，以防后续整改一大堆问题。", "9月3日左右"),
        ("进度", "验收前的软件准备工作要先完成，验收大纲等工作不能再等，以免等硬件到位后耽误合同验收整体时间。", ""),
    ],
    "real": True,
}

# ── synthetic follow-ups ────────────────────────────────────────────────────
# Each meeting introduces facts that exist in NO other meeting: a distinct
# deliverable, a distinct date, and a distinct numeric detail.
SYNTH = [
    {
        "date": "2026-09-07", "weekday": "周一", "time": "17:30-18:05",
        "kind": "项目进度汇报例会", "place": "线上腾讯会议",
        "attendees": [("林浩然", "甲方单位"), ("徐博文", "甲方单位"), ("孙磊", "史塔克"),
                      ("郑涛", "史塔克"), ("罗建国", "史塔克"), ("周敏", "第三方测评")],
        "items": [
            ("进度", "软件部署已于9月5日调通，第三方测评人员9月5日进场，测评组由周敏带队，共4人。", "9月5日"),
            ("甲方要求", "第三方测评发现的软件缺陷需在9月12日前完成整改并复测。", "9月12日前"),
            ("风险", "VR体验调试未在9月10日前完成，甲方要求书面说明原因并给出新的完成时间。", ""),
            ("己方承诺", "本周五前提交第三方测评缺陷整改清单，共登记17项缺陷。", "本周五前"),
            ("进度", "机房铺线已完成，各房间网络与电源全部调通，累计敷设网线3120米。", ""),
        ],
    },
    {
        "date": "2026-09-14", "weekday": "周一", "time": "17:30-18:00",
        "kind": "项目进度汇报例会", "place": "线上腾讯会议",
        "attendees": [("林浩然", "甲方单位"), ("孙磊", "史塔克"), ("高志远", "史塔克"),
                      ("何嘉伟", "史塔克"), ("王毅", "史塔克")],
        "items": [
            ("进度", "17项软件缺陷已全部整改完成，第三方于9月12日复测通过。", "9月12日"),
            ("风险", "未发货物资中的6套感应器仍未发货，供应商承诺9月18日发出。", "9月18日"),
            ("甲方要求", "未发货部分的合同与采购计划已收齐，需在9月20日前完成到货签收。", "9月20日前"),
            ("己方承诺", "验收大纲初稿由史塔克起草，9月16日前提交甲方审阅。", "9月16日前"),
            ("进度", "沙盘模型完成第二次打印，尺寸调整为1.8米×1.2米。", ""),
        ],
    },
    {
        "date": "2026-09-21", "weekday": "周一", "time": "17:30-18:10",
        "kind": "项目进度汇报例会", "place": "线上腾讯会议",
        "attendees": [("林浩然", "甲方单位"), ("徐博文", "甲方单位"), ("孙磊", "史塔克"),
                      ("郑涛", "史塔克"), ("黄平", "史塔克")],
        "items": [
            ("甲方确认", "甲方认可VR体验整改结果，同意进入验收准备阶段，VR模块不再列入整改范围。", ""),
            ("甲方要求", "指挥控制室的联调联试需在9月28日前完成。", "9月28日前"),
            ("风险", "6套感应器到货延迟至9月25日，可能压缩联调联试的调试窗口。", "9月25日"),
            ("进度", "验收大纲经甲方审阅后已定稿，共分5个一级条目、23个二级条目。", ""),
            ("己方承诺", "联调联试期间安排2名工程师现场值守。", ""),
        ],
    },
    {
        "date": "2026-09-28", "weekday": "周一", "time": "17:30-18:00",
        "kind": "项目进度汇报例会", "place": "线上腾讯会议",
        "attendees": [("林浩然", "甲方单位"), ("孙磊", "史塔克"), ("罗建国", "史塔克"),
                      ("黄平", "史塔克"), ("王毅", "史塔克")],
        "items": [
            ("进度", "指挥控制室联调联试已于9月28日完成，连续运行测试48小时无故障。", "9月28日"),
            ("甲方要求", "专家验收前需准备完整的验收材料清单，包含检测报告、合格证、说明书三类。", ""),
            ("己方承诺", "节前提交验收材料清单初稿，预计11月1日前完成全部归档。", "11月1日前"),
            ("风险", "全息模块的备件仅有1套，若验收演示期间损坏将无法立即更换。", ""),
            ("进度", "现场已按验收大纲完成第一轮自查，发现待改进项3处。", ""),
        ],
    },
    {
        "date": "2026-10-12", "weekday": "周一", "time": "17:30-18:05",
        "kind": "项目进度汇报例会", "place": "线上腾讯会议",
        "attendees": [("林浩然", "甲方单位"), ("徐博文", "甲方单位"), ("孙磊", "史塔克"),
                      ("高志远", "史塔克"), ("郑涛", "史塔克")],
        "items": [
            ("甲方确认", "专家验收会定于10月20日举行，验收大纲按现稿执行，不再修改。", "10月20日"),
            ("甲方要求", "验收会前完成沙盘打印件与全息模块的现场布置，并完成一次全流程彩排。", ""),
            ("进度", "沙盘打印件已交付并完成现场安装，占用场地约12平方米。", ""),
            ("进度", "全息模块安装完成，上午已完成第一次联调彩排。", ""),
            ("己方承诺", "验收会当天安排5人现场保障，其中2人负责设备应急。", ""),
        ],
    },
    {
        "date": "2026-10-20", "weekday": "周二", "time": "14:00-16:30",
        "kind": "项目验收会", "place": "现场会议室",
        "attendees": [("林浩然", "甲方单位"), ("徐博文", "甲方单位"), ("孙磊", "史塔克"),
                      ("高志远", "史塔克"), ("郑涛", "史塔克"), ("何嘉伟", "史塔克"),
                      ("王毅", "史塔克"), ("罗建国", "史塔克"), ("黄平", "史塔克")],
        "items": [
            ("甲方确认", "专家验收通过，AR模块验收结论为合格，无遗留整改项。", ""),
            ("甲方确认", "验收会上专家认可沙盘与全息模块的联动演示效果，未提出异议。", ""),
            ("己方承诺", "两周内提交项目结案报告与全套交付文档，含光盘介质2份。", "两周内"),
            ("进度", "AR模块验收通过并完成签字，验收报告编号 YS-2026-1020。", ""),
            ("风险", "结案材料涉及第三方检测报告原件，需向供应商索回，存在时间风险。", ""),
        ],
    },
]


def render(m: dict) -> str:
    tag = "REAL" if m.get("real") else "SYNTHETIC"
    lines: list[str] = []
    lines.append("---")
    lines.append(f"doc_type: meeting_minutes")
    lines.append(f"data_provenance: {tag.lower()}")
    if tag == "SYNTHETIC":
        lines.append("synthetic: true")
        lines.append("synthetic_note: 本文件为架构验证用合成数据，不是真实会议记录")
    lines.append(f"meeting_date: {m['date']}")
    lines.append(f"project: 新疆心理")
    lines.append("---")
    lines.append("")
    lines.append(f"# 项目会议纪要（{m['date'].replace('-', '')}）")
    lines.append("")
    lines.append(f"> 数据来源：{'真实纪要（脱敏整理）' if tag == 'REAL' else '**合成数据，非真实记录**'}")
    lines.append("")
    lines.append("## 会议信息")
    lines.append("")
    lines.append("| 项目 | 内容 |")
    lines.append("|---|---|")
    lines.append(f"| 会议类型 | 项目会议 |")
    lines.append(f"| 会议内容 | {m['kind']} |")
    lines.append(f"| 会议地点 | {m['place']} |")
    lines.append(f"| 日期 | {m['date'].replace('-', '.')}（{m['weekday']}） |")
    lines.append(f"| 时间 | {m['time']} |")
    lines.append(f"| 记录单位 | 史塔克 |")
    lines.append("")
    lines.append("## 与会人员")
    lines.append("")
    lines.append("| 序号 | 与会人员 | 单位 |")
    lines.append("|---|---|---|")
    for i, (name, org) in enumerate(m["attendees"], 1):
        lines.append(f"| {i} | {name} | {org} |")
    lines.append("")
    lines.append("## 会议纪要")
    lines.append("")
    for typ, content, deadline in m["items"]:
        suffix = f"（期限：{deadline}）" if deadline else ""
        lines.append(f"- **[{typ}]** {content}{suffix}")
    lines.append("")
    lines.append("## 结构化事件（供检索使用）")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(
        [{"type": t, "content": c, "deadline": d, "meeting_date": m["date"]}
         for t, c, d in m["items"]],
        ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    allm = [REAL_SEED] + SYNTH
    for m in allm:
        p = OUT / f"项目会议纪要（{m['date'].replace('-','')}）.md"
        p.write_text(render(m), encoding="utf-8")
        print(f"  {p.name:44} {len(m['items']):2} items  "
              f"{'REAL' if m.get('real') else 'SYNTHETIC'}")
    print(f"\n{len(allm)} meeting(s) -> {OUT}")
    n_items = sum(len(m["items"]) for m in allm)
    print(f"total items: {n_items}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
