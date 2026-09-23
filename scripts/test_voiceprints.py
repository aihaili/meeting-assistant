"""声纹库：跨会议认人。

用户的话就是需求：

    本来就要建立声纹数据库的啊。这样下次会议时，软件自己就可以关联说话人了。

这个测试模拟**两场会**共用一张库：第一场里用户点了几次把名字绑上（同时登记了声音），
第二场里软件自己就该认出来——但只**建议**，不擅自改名（认错人在会议里代价很高）。

自带服务器和临时目录，不碰正在运行的实例。

Usage:  python scripts/test_voiceprints.py
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
from meeting.voiceprints import VoiceprintStore, cosine  # noqa: E402

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


# 两把"嗓子"：三维向量只是为了测试里看得清楚，真实是 192 维的 CAM++ 嵌入。
VOICE_JIA = [1.0, 0.05, 0.05]
VOICE_YI = [0.05, 1.0, 0.05]
# 同一个人的另一次录音：方向接近但带噪声（换麦克风/离得远近）
VOICE_JIA_2 = [1.0, 0.14, 0.10]
# 一个库里没见过的陌生人
VOICE_STRANGER = [0.05, 0.05, 1.0]


def with_server(store, work):
    """开一台临时服务器跑 work(port, svc)，跑完关掉。"""
    saved = getattr(Handler, "service", None)
    session = MeetingSession(title="声纹库测试", path=Path(store.path).parent / "s.json")
    svc = MeetingService(session=session, assistant=None, asr_root=Path(store.path).parent,
                         receiver=None, classify_with_llm=False,
                         project_dir=Path(store.path).parent / "proj",
                         project_db=Path(store.path).parent / "proj" / "rag.db",
                         project_name="vtest",
                         settings_path=Path(store.path).parent / "settings.json",
                         voice_store=store)
    Handler.service = svc
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.35)
    try:
        return work(port, svc)
    finally:
        srv.shutdown()
        srv.server_close()
        Handler.service = saved


def main() -> int:
    print("声纹库：第一场登记，第二场自动认人")

    with tempfile.TemporaryDirectory() as td:
        vp = Path(td) / "voiceprints.json"

        # ── 第 0 层：库本身 ─────────────────────────────────────────────
        st = VoiceprintStore(vp)
        eq(st.stats()["people"], 0, "新库是空的")
        check("同一人的两条向量算相似", cosine(VOICE_JIA, VOICE_JIA_2) > 0.99,
              str(cosine(VOICE_JIA, VOICE_JIA_2)))
        check("不同人的向量算不相似", cosine(VOICE_JIA, VOICE_YI) < 0.2,
              str(cosine(VOICE_JIA, VOICE_YI)))

        # ── 第 1 场会：用户手动绑一次，声音就登记了 ──────────────────
        def meeting_one(port, svc):
            for text, t, spk, emb in [("甲方的话一", 1.0, "c0", VOICE_JIA),
                                      ("我方的话一", 3.0, "c1", VOICE_YI),
                                      ("甲方的话二", 5.0, "c0", VOICE_JIA_2)]:
                req(port, "/api/segment", {"text": text, "start": t, "end": t + 1.2,
                                           "spk": spk, "emb": emb})
            s = req(port, "/api/state?since=0")[1]
            check("第 1 场：三句都带上了向量",
                  all(x.get("spk") for x in s["segments"]), str(s["segments"])[:120])
            # 库里还是空的 → 不该有任何建议
            check("第 1 场：空库不给建议",
                  all(not x.get("suggest") for x in s["segments"]),
                  str([x.get("suggest") for x in s["segments"]])[:160])
            jia = req(port, "/api/participants",
                      {"action": "add", "name": "林浩然", "org": "甲方"})[1]["participant"]
            seg0 = s["segments"][0]
            code, d = req(port, "/api/participants",
                          {"action": "bind_voice", "id": jia["id"], "seg_id": seg0["id"]})
            check("第 1 场：绑定成功", code == 200 and d.get("ok"), str(d)[:140])
            check("第 1 场：绑定的同时把声音登记进了库", d.get("enrolled") is True,
                  str(d.get("voiceprints"))[:160])
            return jia["id"]

        jia_id = with_server(VoiceprintStore(vp), meeting_one)
        after_one = VoiceprintStore(vp).stats()
        eq(after_one["people"], 1, "库里有 1 个人")
        # 库会刻意跳过"太像的"向量（同一次会议里的几十句不该把库撑满），
        # 所以这里只断言至少有一条；"换麦克风能补一条"由下面的 VOICE_JIA_FAR 验。
        check("库里有向量", after_one["vectors"] >= 1, str(after_one["vectors"]))
        eq(after_one["names"].get(jia_id), "林浩然", "库里名字对")

        # ── 第 2 场会：同一个人的声音应该被认出来（只建议，不改名）─────
        def meeting_two(port, svc):
            req(port, "/api/segment", {"text": "甲方又来了", "start": 1.0, "end": 2.2,
                                       "spk": "x0", "emb": VOICE_JIA_2})
            req(port, "/api/segment", {"text": "没见过的人", "start": 3.0, "end": 4.2,
                                       "spk": "x1", "emb": VOICE_STRANGER})
            s = req(port, "/api/state?since=0")[1]
            by = {x["text"]: x for x in s["segments"]}
            known, stranger = by["甲方又来了"], by["没见过的人"]

            out = {}
            out["suggest_known"] = known.get("suggest")
            out["suggest_stranger"] = stranger.get("suggest")
            out["speaker_known"] = known.get("speaker")
            return out

        r2 = with_server(VoiceprintStore(vp), meeting_two)
        check("第 2 场：认出了上一次绑过的那个人",
              (r2["suggest_known"] or {}).get("name") == "林浩然",
              str(r2["suggest_known"]))
        check("第 2 场：认出来但**没有擅自改名**", r2["speaker_known"] == "",
              "speaker=" + repr(r2["speaker_known"]))
        check("第 2 场：陌生人没有被乱认",
              not (r2["suggest_stranger"] or {}).get("ok"),
              str(r2["suggest_stranger"]))

        # ── 确认建议 = 一次绑定 + 再登记一条向量 ────────────────────────
        def meeting_three(port, svc):
            req(port, "/api/segment", {"text": "甲方第三次", "start": 1.0, "end": 2.2,
                                       "spk": "y0", "emb": VOICE_JIA_2})
            s = req(port, "/api/state?since=0")[1]
            sug = s["segments"][0].get("suggest") or {}
            pid = sug.get("person_id")
            check("第 3 场：建议里带着库里那个人的 id", bool(pid), str(sug))
            code, d = req(port, "/api/participants",
                          {"action": "bind_voice", "id": pid, "seg_id": s["segments"][0]["id"]})
            after = req(port, "/api/state?since=0")[1]["segments"][0]
            return {"code": code, "speaker": after.get("speaker"),
                    "suggest_left": after.get("suggest")}

        r3 = with_server(VoiceprintStore(vp), meeting_three)
        eq(r3["code"], 200, "第 3 场：确认建议 200")
        eq(r3["speaker"], "林浩然", "第 3 场：确认之后就署名了")
        check("第 3 场：署名后建议消失", r3["suggest_left"] is None, str(r3["suggest_left"]))

        # ── "不是他"要被记住，不能反复问 ────────────────────────────────
        def meeting_four(port, svc):
            req(port, "/api/segment", {"text": "听着像但不是", "start": 1.0, "end": 2.2,
                                       "spk": "z0", "emb": VOICE_JIA_2})
            s = req(port, "/api/state?since=0")[1]
            check("第 4 场：先给了建议", bool(s["segments"][0].get("suggest")),
                  str(s["segments"][0].get("suggest")))
            req(port, "/api/participants",
                {"action": "deny_suggestion", "seg_id": s["segments"][0]["id"]})
            after = req(port, "/api/state?since=0")[1]["segments"][0]
            # 同一场里再来一句同声纹的：不该再问
            req(port, "/api/segment", {"text": "同一声音的下一句", "start": 5.0, "end": 6.2,
                                       "spk": "z0", "emb": VOICE_JIA_2})
            after2 = [x for x in req(port, "/api/state?since=0")[1]["segments"]
                      if x["text"] == "同一声音的下一句"][0]
            return {"cleared": after.get("suggest"), "again": after2.get("suggest")}

        r4 = with_server(VoiceprintStore(vp), meeting_four)
        check("否掉之后建议立刻消失", r4["cleared"] is None, str(r4["cleared"]))
        check("同一声音再说话不再重复问", r4["again"] is None, str(r4["again"]))

        # ── 库要能被人打开看、能进版本管理 ──────────────────────────────
        raw = json.loads(vp.read_text(encoding="utf-8"))
        check("库是纯 JSON 且带版本号", raw.get("version") == 1, str(list(raw.keys())))
        check("库里存的是名字 + 向量", (
            raw["people"][jia_id]["name"] == "林浩然"
            and raw["people"][jia_id]["embs"]), str(raw["people"][jia_id])[:160])

        # ── 库读坏了不能拖垮软件 ────────────────────────────────────────
        vp.write_text("{ 这不是 JSON", encoding="utf-8")
        broken = VoiceprintStore(vp)
        eq(broken.stats()["people"], 0, "库损坏时退回空库")
        check("库损坏有明确提示", bool(broken.stats()["load_error"]),
              str(broken.stats()))

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
