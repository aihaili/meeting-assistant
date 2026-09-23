"""Round-trip tests for meeting/planfile.py.

The property under test is not "render produces pretty Markdown" but **stability**: the
file is rewritten on every plan change and can be hand-edited in between, so
read(write(x)) == x and write(read(write(x))) == write(x). A format that drifts by one
field on each save would corrupt the user's own document while looking fine in a diff of
any single save.

Run:  python scripts/test_planfile.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meeting import planfile  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


def eq(a, b, name: str) -> None:
    check(name, a == b, f"\n      得到 {a!r}\n      期望 {b!r}")


ITEMS = [
    {"id": "p_1", "topic": "汇报联调联试进展", "kind": "report", "done": False,
     "detail": "进度、卡点、需要谁配合。", "nx": 120.0, "ny": 340.0, "nw": 232.0, "nz": 1,
     "refs": [{"source": "合同 5.2 交付节点", "detail": "联调联试应在 9 月 20 日前完成"},
              {"source": "设备到货单", "detail": ""}]},
    {"id": "p_2", "topic": "请甲方确认测评机构", "kind": "confirm", "done": True,
     "detail": "", "nx": 380.0, "ny": 120.0, "nw": 232.0, "nz": 2,
     "refs": [{"source": "第三方测评机构候选名录", "detail": ""}]},
    {"id": "p_3", "topic": "临时加的：解释这个按钮为什么这么设计", "kind": "topic",
     "done": False, "detail": "领导问到就讲设计取舍。", "nx": None, "ny": None,
     "nw": 232.0, "nz": 0, "refs": []},
]


def main() -> int:
    print("planfile 往返测试")

    with tempfile.TemporaryDirectory() as td:
        p = planfile.plan_path(td)

        # ── 1. write then read: every field survives ────────────────────
        planfile.write(p, ITEMS, project="新疆心理AR", meeting="项目周会",
                       now=__import__("datetime").datetime(2026, 9, 13, 9, 12, 0))
        check("文件写出来了", p.is_file())
        text = p.read_text(encoding="utf-8")
        check("标题是 H1", "# 我的发言计划" in text)
        check("每一项是 H2 且带序号", "## 1. 汇报联调联试进展" in text)
        check("机器字段在注释里", "<!-- plan " in text)
        check("参考写成可读的中文条目", "- 参考：合同 5.2 交付节点 — 联调联试" in text)

        back, meta = planfile.load(p)
        eq(len(back), 3, "条目数")
        eq(back[0]["id"], "p_1", "id")
        eq(back[0]["kind"], "report", "kind")
        eq(back[0]["topic"], "汇报联调联试进展", "topic")
        eq(back[0]["detail"], "进度、卡点、需要谁配合。", "detail")
        eq(back[0]["nx"], 120.0, "nx")
        eq(back[0]["nz"], 1.0, "nz")
        eq(len(back[0]["refs"]), 2, "参考条数")
        eq(back[0]["refs"][0]["source"], "合同 5.2 交付节点", "参考来源")
        eq(back[0]["refs"][0]["detail"], "联调联试应在 9 月 20 日前完成", "参考说明")
        eq(back[1]["done"], True, "done=true 读回来")
        eq(back[2]["nx"], None, "未摆放的便签仍是 None")
        eq(meta.get("type"), "speaking-plan", "frontmatter type")
        eq(meta.get("items"), "3", "frontmatter 条目数")

        # ── 2. second write is byte-identical ───────────────────────────
        # 这是最关键的一条：每次改动都重写整份文件，如果 read→write 会漂移，
        # 用户自己的文档就会被助理一次一次改坏，而单看某一次保存的 diff 是正常的。
        planfile.write(p, back, project="新疆心理AR", meeting="项目周会",
                       now=__import__("datetime").datetime(2026, 9, 13, 9, 12, 0))
        again = p.read_text(encoding="utf-8")
        check("read→write 逐字节稳定", again == text,
              "第二次写出的内容与第一次不同")

        # ── 3. a hand-written file with no meta still loads ─────────────
        hand = ("# 我的发言计划\n\n## 汇报进展\n\n- 参考：进度表\n\n"
                "## 请甲方确认测评机构\n")
        p2 = Path(td) / "手写.md"
        p2.write_text(hand, encoding="utf-8")
        hb, _ = planfile.load(p2)
        eq(len(hb), 2, "手写文件条目数")
        eq(hb[0]["topic"], "汇报进展", "手写标题（无序号）")
        eq(hb[0]["kind"], "topic", "没有 kind 时用默认值而不是报错")
        eq(hb[0]["refs"][0]["source"], "进度表", "手写参考")
        check("手写条目没有 id（由会话补）", "id" not in hb[0])

        # ── 4. edits a human would make survive ─────────────────────────
        edited = text.replace("kind=confirm", "kind=risk").replace(
            "## 1. 汇报联调联试进展", "## 1. 汇报联调联试进展（改过标题）")
        p.write_text(edited, encoding="utf-8")
        eb, _ = planfile.load(p)
        eq(eb[1]["kind"], "risk", "手改 kind 生效")
        eq(eb[0]["topic"], "汇报联调联试进展（改过标题）", "手改标题生效")

        # ── 5. a real session-shaped ref is stable and keeps its corpus ──
        # 会话里的参考用的是 title/snippet/kb，文件里用的是 source/detail；
        # 语料标记写成行尾的 ［X库］，parse 必须把它取回来——否则每次保存都会
        # 再追加一个标记（"［公共库］［公共库］"），而单看一次保存是正常的。
        sess_ref = [{"id": "p_5", "topic": "汇报进度", "kind": "report", "done": False,
                     "detail": "", "nx": None, "ny": None, "nw": 232.0, "nz": 0,
                     "refs": [{"title": "合同 5.2 交付节点",
                               "snippet": "联调联试应在 9 月 20 日前完成",
                               "kb": "公共", "score": 0.83, "heading": "5.2"}]}]
        now = __import__("datetime").datetime(2026, 9, 13, 10, 0, 0)
        planfile.write(p, sess_ref, now=now)
        t1 = p.read_text(encoding="utf-8")
        check("参考资料本身写进了文件（不只是标题）",
              "联调联试应在 9 月 20 日前完成" in t1, t1)
        check("语料来源标在行尾", "［公共库］" in t1, t1)
        rb, _ = planfile.load(p)
        eq(rb[0]["refs"][0]["kb"], "公共", "语料被解析回字段")
        eq(rb[0]["refs"][0]["detail"], "联调联试应在 9 月 20 日前完成",
           "说明里不含语料标记")
        planfile.write(p, rb, now=now)
        check("带语料的参考也逐字节稳定", p.read_text(encoding="utf-8") == t1,
              "第二次写出与第一次不同（语料标记被重复追加？）")
        check("没有重复追加语料标记",
              p.read_text(encoding="utf-8").count("［公共库］") == 1,
              str(p.read_text(encoding="utf-8").count("［公共库］")))

        # ── 6. hostile content does not break the format ────────────────
        nasty = [{"id": "p_9", "topic": "含 <!-- 注释 --> 和 | 竖线",
                  "kind": "不存在的类型", "done": False,
                  "detail": "第一行\n第二行", "refs": [{"source": '带"引号"的来源'}]}]
        planfile.write(p, nasty, now=__import__("datetime").datetime(2026, 9, 13))
        nb, _ = planfile.load(p)
        eq(len(nb), 1, "恶意内容仍是一个条目")
        eq(nb[0]["topic"], "含 <!-- 注释 --> 和 | 竖线", "主题里的注释标记不被吃掉")
        eq(nb[0]["kind"], "topic", "非法 kind 归到默认，而不是产生一个没有颜色的便签")
        eq(nb[0]["detail"], "第一行\n第二行", "多行说明")
        eq(nb[0]["refs"][0]["source"], '带"引号"的来源', "引号被正确转义")

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
