"""索引范围：**会议纪要要进，原始转写导出不进**。

为什么要专门测这一条：这条规则的注释和实现曾经不一致——文档字符串里写着
"(transcripts, session JSON, **generated minutes**)" 都被 denylist，而实现里
**纪要从来没有被跳过**。这种"说的和做的不一样"最危险：读代码的人（包括写它的 agent）
会照注释去推断行为，然后做出错误的决定（我确实因此说过"检索里只有外部库"）。

所以这里不靠读注释，而是**造两份文件跑一遍真实索引**，断言：
* `会议纪要/xxx.md` **被索引**，而且它的内容**检索得到**；
* `导出/xxx-transcript.md`（原始转写）**不被索引**，内容**检索不到**；
* `session.json` 不被索引。

自建夹具、用临时目录，跑完清掉——不碰用户的任何数据。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"   {detail}"))
    if not cond:
        FAILS.append(name)


# 三段**各不相同**的特征文本，好判断谁进了索引
DOC_TEXT = "本项目的软件环境包含全息投影与动作捕捉两套子系统，验收标准见第五章。"
MIN_TEXT = "上次会议决定：下周一之前完成联调联试的排期确认，由甲方代表一负责跟进。"
TR_TEXT = "这是一段原始转写导出，属于机器对已有内容的复述，不应该被重新导入索引。"


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "proj"
        (root / "会议纪要").mkdir(parents=True)
        (root / "导出").mkdir(parents=True)

        (root / "项目文档.md").write_text(f"# 项目文档\n\n{DOC_TEXT}\n", encoding="utf-8")
        (root / "会议纪要" / "2026-01-01 测试纪要.md").write_text(
            f"# 测试纪要\n\n{MIN_TEXT}\n", encoding="utf-8")
        (root / "导出" / "meeting-transcript.md").write_text(
            f"# 转写\n\n{TR_TEXT}\n", encoding="utf-8")
        (root / "session.json").write_text(
            json.dumps({"segments": [{"text": TR_TEXT}]}, ensure_ascii=False),
            encoding="utf-8")

        from rag.sync import sync_folder

        db = root / ".plaud" / "rag.db"
        rep = sync_folder(root, db, name="proj", verbose=False)
        print(f"  索引报告: 新增 {rep['added']} 未变 {rep['unchanged']} "
              f"嵌入 {rep['embedded']} 文件 {rep['files']} 块 {rep['chunks']}")
        for s in rep.get("skip_reasons", []):
            print(f"    跳过: {s}")

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        files = [r[0].replace("\\", "/") for r in con.execute("select path from files")]
        texts = [r[0] for r in con.execute("select text from chunks")]

        def indexed(rel: str) -> bool:
            return any(f.endswith(rel) for f in files)

        def searchable(snippet: str) -> bool:
            """内容是否真的进了索引（按特征串找块）。"""
            return any(snippet[:18] in t for t in texts)

        print("\n  索引里的文件：")
        for f in files:
            print("    " + f)

        check("项目文档被索引", indexed("项目文档.md"))
        check("**会议纪要被索引**（这条是重点）",
              indexed("会议纪要/2026-01-01 测试纪要.md"),
              "纪要没进索引 —— 就是注释与实现不符的那个坑")
        check("会议纪要的内容检索得到", searchable(MIN_TEXT))
        check("原始转写导出**不**被索引", not indexed("meeting-transcript.md"),
              "转写被索引了 → 索引会被机器复述污染")
        check("转写内容检索不到", not searchable(TR_TEXT))
        check("session.json 不被索引", not indexed("session.json"))

        # 跳过原因要说得出话，不能"静默少文件"
        reasons = " ".join(rep.get("skip_reasons", []))
        check("跳过的文件在报告里说明了原因",
              "transcript" in reasons or "产物" in reasons, reasons[:120])
        con.close()

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
