"""Verify that an import goes to the corpus it was told to, and not the other one.

The conflict this guards against: "imported content goes to the global knowledge base" and
"the host's meeting outline gets imported" are both reasonable rules, and together they put a
per-meeting agenda into the shared corpus -- where it answers questions in every other
project, with dates that were stale the moment the meeting ended.

The check is empirical: import the same file twice with different ``scope`` values and see
where it actually lands on disk and in which index it becomes retrievable. Reading the code
is not enough because the routing has two destinations and one of them is reached through a
different code path (copy-to-folder vs copy-to-shared-corpus).

Usage:
    python scripts/test_import_scope.py [--port 8510]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ROOT = HERE.parent


def post(port: int, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8510)
    ap.add_argument("--outline", default=str(ROOT / "data" / "outlines" / "meeting-outline.md"))
    ap.add_argument("--project", default=str(ROOT / "data" / "proj-demo"))
    args = ap.parse_args()

    outline = Path(args.outline)
    project = Path(args.project)
    if not outline.exists():
        print(f"缺少测试用会议流程 {outline}")
        return 1

    proj_import_dir = project / "导入"
    proj_db = project / ".plaud" / "rag.db"
    proj_files_before = sorted(p.name for p in proj_import_dir.glob("*")) \
        if proj_import_dir.is_dir() else []

    print("=" * 74)
    print("导入路由：scope=project 应当只进项目库")
    print("=" * 74)

    code, d = post(args.port, "/api/agenda/import",
                   {"path": str(outline), "scope": "project"})
    print(f"  HTTP {code}  scope={d.get('scope')}  解析 {d.get('count')} 项")
    print(f"  indexed={json.dumps(d.get('indexed'), ensure_ascii=False)}")

    ok = True
    if code != 200:
        print(f"  ✗ 导入失败: {d}")
        return 1
    if d.get("scope") != "project":
        print(f"  ✗ scope 未回显为 project")
        ok = False

    idx = d.get("indexed") or {}
    copied = Path(idx.get("copied_to", "")) if idx.get("copied_to") else None
    print(f"  复制到: {copied}")

    # ① must land inside the project folder, never in the shared corpus
    if copied is None:
        print("  ✗ 没有报告落盘位置")
        ok = False
    elif not str(copied).startswith(str(project)):
        print(f"  ✗ 落在了项目文件夹之外: {copied}")
        ok = False
    else:
        print("  ✓ 落在项目文件夹内")

    # ② must be retrievable from the project index
    if idx.get("indexed"):
        print(f"  ✓ 已入项目库（本库现有 {idx.get('chunks')} chunks）")
    else:
        print(f"  ! 未入库: {idx.get('note') or idx.get('error')}")

    # ③ and must NOT be in the shared corpus
    from rag.rag_core import RagIndex
    public_db = ROOT / "data" / "ar.db"
    if public_db.exists():
        pub = RagIndex(db_path=str(public_db), kb_dir=r"<公共资料库>")
        hits = pub.search("开场致辞 主持人 林浩然 第三方测评进场安排确认", top_k=5)
        leaked = [h for h in hits if "会议流程" in (h.get("title") or "")]
        print(f"\n  公共库检索会议流程内容：{'✗ 泄漏了！' if leaked else '✓ 检索不到'}")
        for h in leaked:
            print(f"      {h['title']}")
        if leaked:
            ok = False

    # ④ the host's original file must still exist (copy, never move)
    print(f"\n  原始文件仍在: {'✓' if outline.exists() else '✗ 被移动了'}")
    if not outline.exists():
        ok = False

    # ⑤ scope must be validated, not silently coerced
    code, d = post(args.port, "/api/agenda/import",
                   {"path": str(outline), "scope": "全局"})
    print(f"  非法 scope 被拒: {'✓' if code == 400 else '✗ 返回 ' + str(code)}  "
          f"{d.get('error', '')[:40]}")
    if code != 400:
        ok = False

    print(f"\n{'全部通过' if ok else '有失败项'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
