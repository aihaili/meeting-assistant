"""Test the prep API directly, one request at a time, printing raw responses.

Why this spins up its own server instead of pointing at a running one: it used to default to
``--port 8510``, which meant running the test suite **wrote into the developer's live
session** -- four "测试要点甲/乙/丙/丁" notes appeared in the real meeting every time, and
they showed up as stray sticky notes in the UI. A test that mutates whatever happens to be
running is not a test, it is an accident; and the person running it cannot tell "the code is
broken" from "a previous run left junk behind".

So: own HTTP server, own temp session file, own port. Nothing outside the temp directory is
touched, and the test can be run at any time, repeatedly, with the same result.

Usage:
    python scripts/test_prep_api.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from meeting.server import Handler, MeetingService  # noqa: E402
from meeting.session import MeetingSession  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def post(port: int, path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


LIVE = HERE.parent / "data" / "sessions" / "meeting.json"


def live_junk() -> list[str]:
    """Test topics currently sitting in the real session file, if it exists."""
    if not LIVE.is_file():
        return []
    try:
        raw = json.loads(LIVE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [a.get("topic", "") for a in (raw.get("agenda") or [])
            if a.get("list") == "prep" and str(a.get("topic", "")).startswith("测试要点")]


def main() -> int:
    print("发言计划接口测试（自带服务器，不碰正在运行的那个）")
    saved = getattr(Handler, "service", None)
    # 前后对比，而不是"看真实会话里有没有测试条目"：后者会把**以前**跑测试留下的残留
    # 算成这次失败。要断言的是"这一趟没有写进去"。
    junk_before = live_junk()
    if junk_before:
        print(f"  注意：真实会话里本来就有 {len(junk_before)} 条历史测试残留 —— "
              f"{junk_before[:4]}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        session = MeetingSession(title="接口测试", path=tmp / "s.json")
        svc = MeetingService(session=session, assistant=None, asr_root=tmp,
                             receiver=None, classify_with_llm=False,
                             project_dir=tmp / "proj", project_db=tmp / "proj" / "rag.db",
                             project_name="itest", settings_path=tmp / "settings.json")
        Handler.service = svc
        port = free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.4)

        try:
            state = get(port, "/api/state?since=0")
            start = len(state.get("prep") or [])
            check("全新会话里没有便签", start == 0, f"{start} 条")

            tops = ["测试要点甲", "测试要点乙", "测试要点丙", "测试要点丁"]
            print("  逐个新建：")
            seen = []
            for t in tops:
                code, d = post(port, "/api/prep", {"action": "add", "topic": t})
                item = d.get("item") or {}
                seen.append(item.get("id"))
                print(f"      [{code}] {t} -> id={item.get('id')} kind={item.get('kind')}")
                check(f"新建「{t}」成功", code == 200 and item.get("id"), str(d)[:120])

            check("四个 id 互不相同", len(set(seen)) == len(seen), str(seen))

            # 去重是刻意行为：同一句话拖两次不该出现两条。
            code, d = post(port, "/api/prep", {"action": "add", "topic": tops[0]})
            check("重复主题不会新建第二条",
                  (d.get("item") or {}).get("id") == seen[0], str(d)[:140])

            prep = (get(port, "/api/state?since=0").get("prep") or [])
            check("服务端恰好四条", len(prep) == 4, f"{len(prep)} 条")

            # 空主题要被明确拒绝，而不是悄悄创建一个没有名字的便签
            code, d = post(port, "/api/prep", {"action": "add", "topic": "   "})
            check("空主题被拒（400）", code == 400, f"[{code}] {str(d)[:100]}")

            # 规范化后为空的主题也要拒：否则它们的去重键都是空串，会全部并成第一条
            code, d = post(port, "/api/prep", {"action": "add", "topic": "！？"})
            check("无有效字符的主题被拒（400）", code == 400, f"[{code}] {str(d)[:100]}")

            # 布局写盘
            code, d = post(port, "/api/prep/layout",
                           {"items": [{"id": seen[0], "nx": 321, "ny": 123, "nz": 7}]})
            check("布局保存 200", code == 200, str(d)[:100])
            one = [p for p in (get(port, "/api/state?since=0").get("prep") or [])
                   if p["id"] == seen[0]]
            check("位置落库", one and one[0]["nx"] == 321 and one[0]["ny"] == 123,
                  str(one)[:140])

            # 计划文件必须跟着每一次改动走
            pf = tmp / "proj" / "发言计划.md"
            check("每次改动都重写了发言计划.md", pf.is_file(), str(pf))
            if pf.is_file():
                txt = pf.read_text(encoding="utf-8")
                check("四条都写进了文件",
                      all(f"## {i}. {t}" in txt for i, t in enumerate(tops, 1)))
                check("位置也写进了文件", "x=321" in txt, txt[:400])

            # 删除
            code, d = post(port, "/api/prep", {"action": "remove", "id": seen[3]})
            check("删除 200", code == 200 and d.get("removed"), str(d)[:100])
            left = (get(port, "/api/state?since=0").get("prep") or [])
            check("删除后剩三条", len(left) == 3, f"{len(left)} 条")

            # 最后确认：这一趟没有往真实会话里写任何东西
            junk_after = live_junk()
            check("这一趟没有污染真实会话文件", len(junk_after) <= len(junk_before),
                  f"真实会话里的测试条目从 {len(junk_before)} 条变成 {len(junk_after)} 条")
        finally:
            srv.shutdown()
            srv.server_close()
            Handler.service = saved

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
