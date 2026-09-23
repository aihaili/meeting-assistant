"""声纹绑定：点一次，同一声纹的**所有**发言一起署名。

用户的原话就是验收标准：

    相同声纹的人，点击一次绑定之后，其他相同声纹的就自动把名字改过来。

这件事此前完全没有实现：``bindSpeaker`` 只改本地那一段，然后在参会人上写一个假的
``voice_id``（``"voice-" + id``）。于是左栏显示"已绑定声纹"，而言语流里同一段声音的
其他句子还是"未署名"——**状态是假的**，界面和事实对不上。这个测试盯的正是这一点。

自带服务器与临时会话，不碰正在运行的那个。

Usage:  python scripts/test_voice_bind.py
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


def eq(a, b, name: str) -> None:
    check(name, a == b, f"\n      得到 {a!r}\n      期望 {b!r}")


def req(port: int, path: str, payload: dict | None = None):
    url = f"http://127.0.0.1:{port}{path}"
    body = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=body, method="POST" if body else "GET",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    print("声纹绑定：一次点击覆盖整段声音")
    saved = getattr(Handler, "service", None)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        session = MeetingSession(title="声纹测试", path=tmp / "s.json")
        svc = MeetingService(session=session, assistant=None, asr_root=tmp, receiver=None,
                             classify_with_llm=False, project_dir=tmp / "proj",
                             project_db=tmp / "proj" / "rag.db", project_name="vtest",
                             settings_path=tmp / "settings.json")
        Handler.service = svc
        port = free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.4)

        try:
            # 两个人的声音，各说两句：甲-乙-甲-乙
            convo = [("甲的第一句", 1.0, "spkA"), ("乙的第一句", 3.0, "spkB"),
                     ("甲的第二句", 5.0, "spkA"), ("乙的第二句", 7.0, "spkB")]
            for text, t, spk in convo:
                code, d = req(port, "/api/segment",
                              {"text": text, "start": t, "end": t + 1.5, "spk": spk})
                check(f"灌入「{text}」", code == 200 and d.get("segment"), str(d)[:120])

            st = req(port, "/api/state?since=0")[1]
            segs = sorted(st["segments"], key=lambda s: s["start"])
            eq(len(segs), 4, "四句都在")
            eq([s.get("spk") for s in segs], ["spkA", "spkB", "spkA", "spkB"],
               "声纹标在了每一句上")
            eq([s.get("speaker") for s in segs], ["", "", "", ""], "一开始都没署名")
            eq(st["service"].get("spk_counts"), {"spkA": 2, "spkB": 2},
               "服务端报了每个声纹各有几句")

            people = []
            for nm, org, role in (("甲工", "甲方", "甲方"), ("乙工", "我方", "汇报")):
                code, d = req(port, "/api/participants",
                              {"action": "add", "name": nm, "org": org, "role": role})
                check(f"加人「{nm}」", code == 200 and d.get("participant"), str(d)[:120])
                people.append(d.get("participant") or {})
            people = req(port, "/api/state?since=0")[1]["participants"]
            check("测试会话里有人可绑", len(people) >= 2, f"{len(people)} 人")
            jia, yi = people[0], people[1]

            # ── 核心：绑一句，同声纹的两句一起改名 ──────────────────────
            code, d = req(port, "/api/participants",
                          {"action": "bind_voice", "id": jia["id"], "seg_id": segs[0]["id"]})
            eq(code, 200, "绑定请求 200")
            eq(d.get("changed"), 2, "一次绑定改了两句（甲的两句）")
            eq(d.get("bound_segments"), 2, "报告覆盖 2 句")
            eq(d.get("spk"), "spkA", "报告绑定的是哪个声纹")

            segs = sorted(req(port, "/api/state?since=0")[1]["segments"], key=lambda s: s["start"])
            eq([s.get("speaker") for s in segs], [jia["name"], "", jia["name"], ""],
               "同声纹的两句都署名了，别的声纹没动")
            check("参会人记下了绑定的声纹",
                  [p for p in req(port, "/api/state?since=0")[1]["participants"]
                   if p["id"] == jia["id"]][0]["voice_id"] == "spkA")
            check("参会人写清了覆盖几句",
                  "2" in ([p for p in req(port, "/api/state?since=0")[1]["participants"]
                           if p["id"] == jia["id"]][0]["voice_note"] or ""))

            # ── 绑好之后**新来**的同声纹发言要自动署名 ──────────────────
            code, d = req(port, "/api/segment",
                          {"text": "甲的第三句", "start": 9.0, "end": 10.5, "spk": "spkA"})
            eq(code, 200, "新发言灌入 200")
            eq((d.get("segment") or {}).get("speaker"), jia["name"],
               "新来的同声纹发言自动署名（不用再点一次）")

            # ── 第二个声纹独立绑定，互不影响 ────────────────────────────
            segs = sorted(req(port, "/api/state?since=0")[1]["segments"], key=lambda s: s["start"])
            segB = [s for s in segs if s.get("spk") == "spkB"][0]
            code, d = req(port, "/api/participants",
                          {"action": "bind_voice", "id": yi["id"], "seg_id": segB["id"]})
            eq(d.get("changed"), 2, "乙的两句也一起改名")
            segs = sorted(req(port, "/api/state?since=0")[1]["segments"], key=lambda s: s["start"])
            names = [s.get("speaker") for s in segs]
            eq(names, [jia["name"], yi["name"], jia["name"], yi["name"], jia["name"]],
               "两种声音各自署名，没有串")

            # ── 一个声音只能绑一个人：改绑要解掉前一个 ──────────────────
            code, d = req(port, "/api/participants",
                          {"action": "bind_voice", "id": yi["id"], "seg_id": segs[0]["id"]})
            eq(d.get("changed"), 3, "改绑把甲的 3 句一起改到乙名下")
            after = req(port, "/api/state?since=0")[1]
            eq([p["voice_id"] for p in after["participants"] if p["id"] == jia["id"]], [None],
               "甲被解绑（一个声音只能是一个人）")

            # ── 改名要推到这一声纹的所有发言 ────────────────────────────
            code, d = req(port, "/api/participants",
                          {"action": "update", "id": yi["id"], "name": "孙磊（改）"})
            eq(code, 200, "改名 200")
            names = [s.get("speaker") for s in
                     sorted(req(port, "/api/state?since=0")[1]["segments"],
                            key=lambda s: s["start"])]
            check("改名之后该声纹的发言全部跟着改",
                  all(n in ("孙磊（改）", "") for n in names) and "孙磊（改）" in names,
                  str(names))

            # ── 解绑要能把名字撤掉 ──────────────────────────────────────
            code, d = req(port, "/api/participants",
                          {"action": "unbind_voice", "id": yi["id"]})
            check("解绑 200", code == 200 and d.get("ok"), str(d)[:100])
            names = [s.get("speaker") for s in
                     sorted(req(port, "/api/state?since=0")[1]["segments"],
                            key=lambda s: s["start"])]
            eq(set(names), {""}, "解绑后该声纹的发言回到未署名")

            # ── 没有声纹时不能假装成功 ──────────────────────────────────
            # ASR 没开说话人模型时 spk 是空的。这时只给这一段落名，
            # 并且**不能**报"改了 N 句"——否则界面会说"3 句都记好了"，而其实只改了 1 句。
            code, d = req(port, "/api/segment",
                          {"text": "没有声纹的一句", "start": 20.0, "end": 21.0})
            noSpk = (d.get("segment") or {}).get("id")
            code, d = req(port, "/api/participants",
                          {"action": "bind_voice", "id": jia["id"], "seg_id": noSpk})
            eq(code, 200, "无声纹也能绑（这一下仍然是用户点的那一下）")
            eq(d.get("spk"), "", "如实报告没有声纹")
            eq(d.get("changed"), 1, "只改了一句")
            eq((sorted(req(port, "/api/state?since=0")[1]["segments"],
                       key=lambda s: s["start"])[-1].get("speaker")), jia["name"],
               "无声纹的那一句确实署上了名")

            # ── 落库：会话文件里留下绑定关系 ────────────────────────────
            # 先重新绑一个：上面为了验解绑，把绑定都撤掉了（voice_names 本该是空的）。
            segA = [s for s in req(port, "/api/state?since=0")[1]["segments"]
                    if s.get("spk") == "spkA"][0]
            req(port, "/api/participants",
                {"action": "bind_voice", "id": jia["id"], "seg_id": segA["id"]})
            svc.session._save()
            raw = json.loads((tmp / "s.json").read_text(encoding="utf-8"))
            check("会话文件里存了 voice_names", "voice_names" in raw, str(list(raw.keys())))
            # 读回来必须还在：绑定是跨会议要留的东西，不能只活在内存里
            back = MeetingSession.load(tmp / "s.json")
            check("重新载入后绑定关系还在",
                  any(k.startswith("spk") for k in (back.voice_names or {})),
                  str(getattr(back, "voice_names", None)))
            check("重新载入后发言上的名字还在",
                  any(s.get("speaker") for s in back.segments),
                  str([s.get("speaker") for s in back.segments]))
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
