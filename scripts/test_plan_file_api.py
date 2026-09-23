"""Tests for the speaking plan living in <项目>/发言计划.md.

Three things are being verified, and the third is the one that would bite in real use:

1. A mutation over the API reaches the file (not just the session).
2. The file wins on reload -- that is the point of keeping it as a file.
3. A reload does **not** throw away what the file cannot express: retrieved refs carry
   knowledge-base metadata the Markdown has nowhere to put, and a hand-written item has no
   ``x``/``y``, so a naive "file wins" would strip the evidence off every note and scatter
   the arrangement back to the corner.

Run:  python scripts/test_plan_file_api.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from meeting import planfile                       # noqa: E402
from meeting.server import Handler, MeetingService  # noqa: E402
from meeting.session import MeetingSession          # noqa: E402

FAILS: list[str] = []
PORT = 8533
_SAVED_SERVICE = getattr(Handler, "service", None)


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def eq(a, b, name: str) -> None:
    check(name, a == b, f"\n      得到 {a!r}\n      期望 {b!r}")


def req(path: str, payload=None, method=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def main() -> int:
    from http.server import ThreadingHTTPServer
    import threading
    import time

    print("发言计划文件（<项目>/发言计划.md）测试")

    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj-demo"
        proj.mkdir(parents=True, exist_ok=True)
        plan_p = planfile.plan_path(proj)

        session = MeetingSession(title="项目周会", path=Path(td) / "s.json")
        svc = MeetingService(session=session, assistant=None, asr_root=Path(td),
                             receiver=None, classify_with_llm=False,
                             project_dir=proj, project_db=Path(td) / "rag.db",
                             project_name="proj-demo")
        Handler.service = svc

        # ── 0. startup with an empty plan creates nothing ───────────────
        r0 = svc.ensure_plan()
        check("空计划不凭空造文件", r0.get("ok") and not r0.get("created") and
              not plan_p.exists(), f"{r0}")

        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.4)

        try:
            # ── 1. a mutation over the API reaches the file ─────────────
            st, d = req("/api/prep", {"action": "add", "topic": "汇报联调联试进展",
                                      "kind": "report", "detail": "进度与卡点",
                                      "nx": 120, "ny": 340})
            eq(st, 200, "新增发言计划项 200")
            check("响应里带上了文件信息", "plan_file" in d and d["plan_file"].get("ok"),
                  f"{d.get('plan_file')}")
            check("文件真的出现了", plan_p.is_file(), str(plan_p))
            text = plan_p.read_text(encoding="utf-8")
            check("文件是给人看的 Markdown", "# 我的发言计划" in text and
                  "## 1. 汇报联调联试进展" in text)
            check("kind 写进了文件", "kind=report" in text)
            eq(planfile.load(plan_p)[1].get("project"), "proj-demo", "frontmatter 项目名")

            st, d = req("/api/prep", {"action": "add", "topic": "请甲方确认测评机构",
                                      "kind": "confirm"})
            eq(st, 200, "新增第二项 200")
            eq(len(planfile.load(plan_p)[0]), 2, "文件里有两项")

            # ── 2. a ref carries metadata the file cannot hold ──────────
            st, d = req("/api/prep", {"action": "attach_ref",
                                      "id": d["item"]["id"],
                                      "ref": {"title": "合同 5.2 交付节点",
                                              "snippet": "联调联试应在 9 月 20 日前完成",
                                              "kb": "公共", "score": 0.83}})
            eq(st, 200, "挂参考 200")
            back, _ = planfile.load(plan_p)
            second = [it for it in back if it["id"] == d["item"]["id"]][0]
            eq(len(second["refs"]), 1, "参考写进文件")
            eq(second["refs"][0]["source"], "合同 5.2 交付节点", "参考来源")
            check("参考的可读形式在文件里", "- 参考：合同 5.2" in
                  plan_p.read_text(encoding="utf-8"))

            meta_before = [r for r in session.list_plan("prep")
                           if r.id == d["item"]["id"]][0].refs
            eq(meta_before[0].get("kb"), "公共", "会话里的参考带着知识库来源")

            # ── 3. hand-edit the file, then reload ─────────────────────
            edited = plan_p.read_text(encoding="utf-8")
            edited = edited.replace("kind=confirm", "kind=risk")
            edited = edited.replace("## 1. 汇报联调联试进展", "## 1. 汇报联调联试进展（改过）")
            # 手写一条：没有 id、没有 x/y、没有 kind —— 就像用户在 Obsidian 里随手加的
            edited += "\n## 临时加的：解释这个按钮为什么这么设计\n"
            plan_p.write_text(edited, encoding="utf-8")

            st, d = req("/api/plan", {"action": "reload"})
            eq(st, 200, "重载 200")
            check("重载成功", d.get("ok"), f"{d}")
            eq(d["items"], 3, "重载后三项（含手写那条）")

            got = {a.topic: a for a in session.list_plan("prep")}
            check("手改标题生效", "汇报联调联试进展（改过）" in got,
                  " · ".join(got))
            check("手改 kind 生效",
                  got.get("请甲方确认测评机构") and
                  got["请甲方确认测评机构"].kind == "risk",
                  str(got.get("请甲方确认测评机构")))
            check("手写的新条目被收下",
                  "临时加的：解释这个按钮为什么这么设计" in got, " · ".join(got))

            # ── 4. the merges: refs survive, layout survives ────────────
            # 这一节是"文件说了算"这句话的边界：文件里没有的东西不能被读成"没有了"。
            survivor = got.get("请甲方确认测评机构")
            eq(len(survivor.refs), 1, "重载后参考还在（文件里没有 kb/score 字段）")
            eq(survivor.refs[0].get("kb"), "公共",
               "重载没有抹掉参考的知识库来源")
            first = got.get("汇报联调联试进展（改过）")
            eq(first.nx, 120.0, "重载保留了便签位置 x")
            eq(first.ny, 340.0, "重载保留了便签位置 y")
            hand = got.get("临时加的：解释这个按钮为什么这么设计")
            eq(hand.nx, None, "手写条目没有位置（交给摆放逻辑）")
            eq(hand.kind, "topic", "手写条目默认 kind")

            # ── 5. remove reaches the file ─────────────────────────────
            st, d = req("/api/prep", {"action": "remove", "id": hand.id})
            eq(st, 200, "删除 200")
            eq(len(planfile.load(plan_p)[0]), 2, "文件里只剩两项")

            # ── 6. layout is persisted into the file ───────────────────
            st, d = req("/api/prep/layout",
                        {"items": [{"id": first.id, "nx": 500, "ny": 60, "nz": 3}]})
            eq(st, 200, "布局保存 200")
            lb, _ = planfile.load(plan_p)
            lfirst = [it for it in lb if it["id"] == first.id][0]
            eq(lfirst["nx"], 500.0, "新位置写进了文件")
            eq(lfirst["nz"], 3.0, "层级写进了文件")

            # ── 7. GET reports the file, with text on demand ────────────
            st, d = req("/api/plan?text=1")
            eq(st, 200, "GET /api/plan 200")
            check("报告了文件路径", d["path"].endswith("发言计划.md"), d["path"])
            check("exists 为真", d["exists"] is True)
            check("带回了文件原文", "# 我的发言计划" in d.get("text", ""))
            eq(len(d["items"]), 2, "同时报告当前计划项")

            # ── 8. no project folder = no silent fallback ───────────────
            bare = MeetingService(session=MeetingSession(title="x", path=Path(td) / "b.json"),
                                  assistant=None, asr_root=Path(td), receiver=None,
                                  classify_with_llm=False, project_dir=None)
            eq(bare.plan_path(), None, "没有项目文件夹时没有计划文件路径")
            r = bare.save_plan()
            check("写入被明确拒绝而不是写到某处", r.get("ok") is False and "项目" in
                  r.get("error", ""), f"{r}")

        finally:
            srv.shutdown()
            srv.server_close()
            Handler.service = _SAVED_SERVICE

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
