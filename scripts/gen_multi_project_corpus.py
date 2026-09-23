"""Generate several projects' worth of meeting minutes that look alike on purpose.

The risk this exists to measure: when several projects run in parallel, one project's
progress can be retrieved as another's and the assistant then asserts something false —
"you promised this in September" about a deadline that belongs to a different customer.

The danger is not topic overlap, it is **structural identity with different nouns**.
Minutes for two projects are written the same way:

    甲方要求  现场施工要加快速度，该加人加班需安排
    期限      两三天内
    进度      VR 和指挥控制室要加快进度

so a query about project A ("A 项目的验收大纲什么时候做") differs from the project B
documents only in the entity names. That is exactly the case where similarity search is
weakest and where an index keyed only on text will happily cross projects.

Three projects are generated with *disjoint* entity vocabularies sharing *identical*
sentence frames, so any cross-project hit is unambiguously a contamination rather than a
plausible near-miss. Each project gets:
  * its own project name and place (新疆 / 西安 / 上海)
  * its own customer and vendor staff names
  * its own deadlines and quantities

Usage:
    python scripts/gen_multi_project_corpus.py [--out DIR] [--per-project 6]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Three projects. Entity vocabularies are disjoint on purpose: no place name, person or
# customer appears in two projects, so a hit from the wrong project cannot be explained as
# a legitimate reference to another project's work.
PROJECTS = [
    {
        "key": "xinjiang",
        "name": "新疆心理",
        "place": "乌鲁木齐",
        "customer": "甲方单位",
        "customer_people": ["林浩然", "徐博文"],
        "our_people": ["孙磊", "高志远", "郑涛"],
        "asset": "VR 体验区",
        "wiring": "机房铺线",
        "equipment": "生理信号采集设备",
        "milestones": ["九月五号", "九月十号", "九月二十号"],
        "reviewer": "第三方测评机构",
        "quantity": "六台",
    },
    {
        "key": "xian",
        "name": "西安 VR",
        "place": "西安高新区",
        "customer": "使用单位",
        "customer_people": ["赵德海", "孙立群"],
        "our_people": ["罗建国", "孙倩", "周宏"],
        "asset": "全息沙盘",
        "wiring": "展厅综合布线",
        "equipment": "六自由度平台",
        "milestones": ["十月八号", "十月十五号", "十月三十号"],
        "reviewer": "军检验收组",
        "quantity": "四台",
    },
    {
        "key": "shanghai",
        "name": "上海实训",
        "place": "上海浦东",
        "customer": "建设单位",
        "customer_people": ["顾长明", "钱慧敏"],
        "our_people": ["黄振宇", "毕晓东", "娄天成"],
        "asset": "虚拟仿真考核系统",
        "wiring": "实训楼弱电",
        "equipment": "体感交互装置",
        "milestones": ["十二月一号", "十二月十号", "十二月二十五号"],
        "reviewer": "市教委评估组",
        "quantity": "八套",
    },
]

# Sentence frames shared verbatim across projects; only the substituted values differ.
FRAMES = [
    ("甲方要求", "{customer_people0}强调，{wiring}必须加快进度，该加人加班要安排，"
                "{milestones0}之前必须完成。"),
    ("甲方要求", "关于{asset}，如果效果不行要抓紧时间调试，{milestones1}之前必须完整。"),
    ("甲方要求", "公司要增派人员配合{reviewer}，什么时候来、谁来，要尽快确定，"
                "{milestones0}前完成部署。"),
    ("进度", "{our_people0}汇报，{wiring}已经展开，{quantity}{equipment}明天到货后立即对接。"),
    ("进度", "{reviewer}预计{milestones1}左右进场，{our_people1}负责配合。"),
    ("风险", "薪资和人员稳定性方面存在隐患，该加人要加人，否则影响{milestones2}的节点。"),
    ("甲方要求", "验收前的准备工作要先完成，验收大纲不能再等了，否则耽误{milestones2}。"),
    ("进度", "{our_people2}已经把{quantity}{equipment}的检测报告、合格证、说明书收集齐。"),
]


def build(out_dir: Path, per_project: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.md"):
        old.unlink()

    manifest = {"projects": [], "files": []}
    for proj in PROJECTS:
        pdir = out_dir / proj["key"]
        pdir.mkdir(parents=True, exist_ok=True)
        for n in range(per_project):
            # Each meeting advances the same milestones slightly, so a query about a
            # deadline has plausible candidates in *every* project.
            day = 1 + n * 7
            month = 9 if proj["key"] == "xinjiang" else (
                10 if proj["key"] == "xian" else 12)
            lines = [f"# {proj['name']} 项目会议纪要（{month:02d}{day:02d}）", "",
                     f"- 项目：{proj['name']}（{proj['place']}）",
                     f"- 客户：{proj['customer']}　我方：史塔克",
                     f"- 与会：{'、'.join(proj['customer_people'])}、"
                     f"{'、'.join(proj['our_people'])}", ""]
            for ftype, tmpl in FRAMES[:per_project]:
                text = tmpl.format(
                    customer_people0=proj["customer_people"][0],
                    our_people0=proj["our_people"][0],
                    our_people1=proj["our_people"][1],
                    our_people2=proj["our_people"][2],
                    wiring=proj["wiring"], asset=proj["asset"],
                    equipment=proj["equipment"], reviewer=proj["reviewer"],
                    milestones0=proj["milestones"][0],
                    milestones1=proj["milestones"][1],
                    milestones2=proj["milestones"][2],
                    quantity=proj["quantity"],
                )
                # Nudge the milestone day so consecutive meetings differ slightly.
                if n:
                    text = text.replace(proj["milestones"][0],
                                        proj["milestones"][0] + f"（第 {n + 1} 次催办）")
                lines.append(f"- **[{ftype}]** {text}")
            path = pdir / f"项目会议纪要（{month:02d}{day:02d}）.md"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            manifest["files"].append({"project": proj["name"], "key": proj["key"],
                                      "path": str(path.relative_to(out_dir))})
        manifest["projects"].append(proj)

    # Ground truth: one question per project, answerable only from that project's files.
    queries = []
    for proj in PROJECTS:
        queries += [
            {"q": f"{proj['name']}项目 {proj['wiring']} 什么时候完成",
             "expect_project": proj["name"],
             "expect_any": [proj["milestones"][0], proj["wiring"]]},
            {"q": f"{proj['name']}项目的 {proj['asset']} 什么时候之前必须完整",
             "expect_project": proj["name"],
             "expect_any": [proj["milestones"][1], proj["asset"]]},
            {"q": f"{proj['name']} 的 {proj['reviewer']} 什么时候进场",
             "expect_project": proj["name"],
             "expect_any": [proj["reviewer"], proj["milestones"][1]]},
            {"q": f"{proj['name']}项目 {proj['equipment']} 到货后要做什么",
             "expect_project": proj["name"],
             "expect_any": [proj["equipment"]]},
        ]
    manifest["queries"] = queries

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "multi-project"))
    ap.add_argument("--per-project", type=int, default=6)
    args = ap.parse_args()

    m = build(Path(args.out), args.per_project)
    by = {}
    for f in m["files"]:
        by[f["project"]] = by.get(f["project"], 0) + 1
    print(f"写入 {args.out}")
    for p in m["projects"]:
        print(f"  {p['name']:<10} {by.get(p['name'], 0)} 份纪要   "
              f"{p['place']}  {p['wiring']}  {p['milestones'][0]}")
    print(f"  共 {len(m['files'])} 份 / {len(m['queries'])} 道真值问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
