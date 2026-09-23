"""回归：改这一批修掉的东西（sync 的 prune、StreamASR.reset、voice_names 两种取值、
denied_spk/history_rev 落盘、emb_weak 过滤）。

跑法（不需要模型、不需要起服务）：
    cd meeting-assistant/scripts
    ..\\venv\\Scripts\\python.exe test_fix_batch.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

fails: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  {'✓' if ok else '✗'} {what}", flush=True)
    if not ok:
        fails.append(what)


# ── 1. rag/sync.py：prune=False 不再炸 ───────────────────────────────────────

def test_sync_prune() -> None:
    print("1) sync_folder(prune=False/True)")
    from rag.rag_core import RagIndex
    from rag.sync import sync_folder

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        (root / "a.md").write_text("# 甲\n\n" + "内容甲。" * 40, encoding="utf-8")
        db = Path(td) / "rag.db"

        rep = sync_folder(root, db, name="公共", verbose=False, prune=False)
        check(rep["added"] == 1, f"prune=False 正常返回（added={rep['added']}, "
                                 f"orphans_cleaned={rep['orphans_cleaned']}）")
        check(rep["orphans_cleaned"] == 0, "prune=False 时 orphans_cleaned 是 0 而不是异常")

        (root / "b.md").write_text("# 乙\n\n" + "内容乙。" * 40, encoding="utf-8")
        rep2 = sync_folder(root, db, name="公共", verbose=False, prune=False)
        check(rep2["added"] == 1 and rep2["unchanged"] == 1,
              f"增量仍然生效：新增 {rep2['added']} 未变 {rep2['unchanged']}")

        (root / "a.md").unlink()
        rep3 = sync_folder(root, db, name="公共", verbose=False, prune=True)
        check(rep3["removed"] == 1, f"prune=True 仍会清理被删文件（removed={rep3['removed']}）")

        idx = RagIndex(db_path=str(db), kb_dir=str(root))
        names = {r["path"] for r in idx.conn.execute("select distinct path from chunks")}
        idx.close()
        check(all("a.md" not in n for n in names), f"被删文件确实不在索引里：{sorted(names)}")


# ── 2. phone_mic/stream_asr.py：reset() 真的存在且清状态 ─────────────────────

def test_asr_reset() -> None:
    print("2) StreamASR.reset()")
    import numpy as np

    from phone_mic.stream_asr import StreamASR

    asr = StreamASR(want_spk=False)
    check(hasattr(asr, "reset"), "有 reset()（mic_source.start() 的 hasattr 判断不再落空）")
    # 造一点"上一轮录音"的残留状态
    asr.push(np.zeros(1600, dtype=np.float32))
    asr._cur_text = "上一轮的话"
    asr._cur_start = 3.0
    asr._utt = [np.zeros(1600, dtype=np.float32)]
    asr._stream_s = 12.5
    asr._spk.assign(0, [1.0, 0.0])
    before_idx = asr._next_idx
    asr.reset()
    check(asr._cur_text == "" and asr._cur_start is None, "当前句文本/起点已清")
    check(asr._buf.size == 0 and asr._stream_s == 0.0, "缓冲与流内秒数已清")
    check(asr._utt == [] and asr._rows == [], "整句音频与已发布行已清")
    check(asr._spk.centroids == [] and asr._spk.emb == {}, "声纹簇已清（换一段录音重新算）")
    check(asr._next_idx == before_idx, "行号计数器保持单调（不倒退，避免覆盖旧行）")
    check(asr.tick() == [] and asr.flush() == [], "未加载模型时 tick/flush 安全返回空")


# ── 3. session.py：voice_names 的两种取值、落盘、emb_weak ────────────────────

class FakeStore:
    """只记 enroll 调用，够验"哪条向量进了库"。"""

    def __init__(self) -> None:
        self.enrolled: list[tuple] = []
        self.people: dict = {}

    def enroll(self, pid, name, emb, note="", meeting="", org="", role="") -> bool:
        self.enrolled.append((pid, tuple(round(float(x), 4) for x in emb)))
        return True

    def save(self) -> None:
        pass

    def match(self, emb) -> dict:
        """这一批只验"哪些向量被 enroll"，所以匹配一律给低分（不产生建议）。"""
        return {"ok": False, "person_id": None, "name": "", "score": 0.0,
                "margin": 0.0, "reason": "测试桩"}

    def stats(self) -> dict:
        return {"people": 0, "vectors": 0, "path": "", "threshold": 0.6,
                "margin": 0.1, "load_error": "", "names": {}}


def test_voice_names_and_state() -> None:
    print("3) 会话：voice_names 两种取值 / 落盘 / emb_weak")
    from meeting.session import MeetingSession

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "s.json"
        s = MeetingSession(title="测试", path=path)
        s.add_participant("林浩然", "甲方", "甲方")
        p = s.participants[0]
        s.add_segment("第一句", 0.0, 2.0, idx=0, spk="匿名-1", emb=[1.0, 0.0])
        s.bind_voice(s.segments[0]["id"], p.id)
        check(s.voice_names.get("匿名-1") == p.id, "bind_voice 存的是 participant id")

        s.rename_speaker("匿名-1", "张工")
        check(s.voice_names.get("匿名-1") == "张工", "rename_speaker 存的是显示名")

        # 这一声纹再说一句：改名之后也必须能署名（老代码只按 id 找，会退回未署名）
        seg2 = s.add_segment("第二句", 2.0, 4.0, idx=1, spk="匿名-1")
        check(seg2["speaker"] == "张工", f"改名后新发言仍署名（得到 {seg2['speaker']!r}）")

        # 改归属/删除等历史性改动 + 否定声纹 → 都要落盘
        s.deny_suggestion(s.segments[0]["id"])
        s.remove_segment(s.segments[1]["id"])
        saved = s.history_rev
        check(saved > 0, f"history_rev 已增长（{saved}）")
        check(bool(s.denied_spk), f"denied_spk 记忆了否定：{sorted(s.denied_spk)}")

        again = MeetingSession.load(path)
        check(again.history_rev == saved, f"history_rev 落盘并恢复（{again.history_rev}）")
        check(again.denied_spk == s.denied_spk, f"denied_spk 落盘并恢复（{sorted(again.denied_spk)}）")

        # emb_weak：不可信的向量不该进声纹库
        s.voice_store = FakeStore()
        s.add_segment("弱向量句", 10.0, 11.0, idx=5, spk="匿名-9",
                      emb=[0.0, 1.0], emb_weak=True)
        s.add_segment("可信向量句", 11.0, 14.0, idx=6, spk="匿名-9",
                      emb=[0.5, 0.5], emb_weak=False)
        s.add_participant("孙磊", "我方", "我方")
        p2 = s.participant_by_name("孙磊")
        r = s.bind_voice(s.segment_by_id(
            next(x["id"] for x in s.segments if x["idx"] == 6))["id"], p2.id)
        got = [e[1] for e in s.voice_store.enrolled]
        check(r.get("ok") is True, f"绑定成功：{r.get('ok')}")
        check((0.0, 1.0) not in got, f"emb_weak 的向量没有进库（已入库 {got}）")
        check((0.5, 0.5) in got, "可信向量进了库")


# ── 第二批：ingest 跳过规则 / source 过滤位置 / 回放并发 / 定稿重试 / 短行说话人 / flush 尾巴 ──

def test_ingest_skip_rules() -> None:
    print("4) rag.ingest 与 sync 用同一套跳过规则")
    from rag.ingest import build

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "corpus"
        root.mkdir()
        (root / "纪要-2026.md").write_text("# 决策\n\n" + "本期决定把接口冻结。" * 20,
                                           encoding="utf-8")
        (root / "x-transcript.md").write_text("# 逐字转写\n\n" + "嗯那我们就先这样。" * 30,
                                              encoding="utf-8")
        (root / "session.json").write_text('{"title": "会话"}', encoding="utf-8")
        db = Path(td) / "rag.db"
        idx = build(root, db, name="公共", verbose=False)
        paths = [r["path"] for r in idx.conn.execute("select distinct path from chunks")]
        idx.close()
        names = [Path(p).name for p in paths]
        check(names == ["纪要-2026.md"], f"只索引了纪要，逐字转写/会话文件被跳过：{names}")


def test_source_filter_before_ranking() -> None:
    print("5) source 过滤发生在排名之前（混库按来源过滤不再空手）")
    from rag.rag_core import RagIndex

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        a = root / "a.md"
        b = root / "b.md"
        a.write_text("# 甲\n\n" + "第三方测评安排确认。 " * 40, encoding="utf-8")
        b.write_text("# 乙\n\n" + "第三方测评安排确认。 " * 40, encoding="utf-8")
        idx = RagIndex(db_path=root / "rag.db", kb_dir=root,
                       sources=[("公共", "公共"), ("项目", "项目")])
        idx.index_file("公共", a)
        idx.index_file("项目", b)
        q = "第三方测评安排确认"
        all_hits = idx.search(q, top_k=5)
        only_proj = idx.search(q, top_k=5, source="项目", candidates=1)
        idx.close()
        check(len(all_hits) >= 2, f"不加过滤时两本库都能查到（{len(all_hits)} 条）")
        check(bool(only_proj) and all(h["label"] == "项目" for h in only_proj),
              f"候选池只有 1 条时按来源过滤仍能拿到项目库（{[(h['label'], h['title']) for h in only_proj]}）")


def test_replay_guard() -> None:
    print("6) replay_wav 并发保护")
    import threading
    import numpy as np

    from phone_mic import audio as A
    from phone_mic.mic_source import LocalMicReceiver
    from phone_mic.streaming import FeedSentence

    class StubASR:
        want_spk = False
        spk_ready = False
        spk_error = ""
        loaded = True

        def load(self):
            return 0.0

        def reset(self):
            pass

        def push(self, chunk, sr=A.TARGET_SR):
            pass

        def tick(self):
            return []

        def flush(self):
            return []

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "x.wav"
        A.write_wav(wav, np.zeros(A.TARGET_SR * 3, dtype=np.float32), A.TARGET_SR)
        rx = LocalMicReceiver(device=None, engine="funasr")
        rx.asr = StubASR()
        rx.on_segment = lambda r: None
        errors: list[str] = []

        def second():
            try:
                rx.replay_wav(wav, speed=1.0)
            except RuntimeError as e:
                errors.append(str(e))

        t = threading.Thread(target=rx.replay_wav, args=(wav,), kwargs={"speed": 0.02},
                             daemon=True)
        t.start()
        time.sleep(0.4)
        t2 = threading.Thread(target=second, daemon=True)
        t2.start()
        t2.join(timeout=5)
        t.join(timeout=10)
        check(any("已经在回放" in e for e in errors),
              f"第二路回放被拒绝：{errors}")
        check(rx._replaying is False, "回放结束后标志已复位（下次还能放）")


def test_settle_retry() -> None:
    print("7) 定稿分析失败会重试，不再静默丢 LLM 通道")
    from meeting.server import LLM_TRIES, MeetingService
    from meeting.session import MeetingSession

    with tempfile.TemporaryDirectory() as td:
        s = MeetingSession(title="重试", path=Path(td) / "s.json")
        s.add_segment("甲方要求九月五号之前完成联调。", 0.0, 3.0, idx=0)
        seg = s.segments[0]
        seg["analysed"] = True
        seg["_analysed_text"] = seg["text"]
        seg["_changed_at"] = time.time() - 999        # 早就定稿了
        seg["_llm_done"] = False
        svc = MeetingService(session=s, assistant=None, receiver=None,
                             classify_with_llm=True)

        calls = {"n": 0}

        def boom(*_a, **_k):
            calls["n"] += 1
            raise RuntimeError("模拟检索/写盘失败")

        svc._analyse = boom                    # type: ignore[assignment]
        svc._settle_pass()
        check(seg["_llm_done"] is False and seg["_llm_tries"] == 1,
              f"第一次失败后仍待重试（tries={seg['_llm_tries']}, done={seg['_llm_done']}）")
        for _ in range(LLM_TRIES):
            svc._settle_pass()
        check(seg["_llm_done"] is True and calls["n"] >= LLM_TRIES,
              f"试满 {LLM_TRIES} 次才放弃（实际调用 {calls['n']} 次）")
        check(any("LLM 分类失败" in d for d in s.dropped), "失败现场留在 dropped 里")


def test_short_row_inherits_speaker() -> None:
    print("8) 太短算不出向量的行沿用当前说话人")
    from phone_mic.stream_asr import StreamASR

    asr = StreamASR(want_spk=True)
    asr.loaded = True
    asr._embed = lambda *_a: None          # 模拟"这一行太短，算不出可信向量"
    first = asr._spk.assign(0, [1.0, 0.0])
    row = asr._row("短句", 0.0, 0.8, open_=False)
    check(row.spk == first and row.spk != "",
          f"短行继承了当前说话人（{row.spk!r}），而不是留空")


def test_flush_pads_tail() -> None:
    print("9) flush 不再丢掉不足一块的尾巴")
    import numpy as np

    from phone_mic.stream_asr import CHUNK, StreamASR

    asr = StreamASR(want_spk=False)
    seen: list[int] = []
    asr._model = object()                  # 只为让"有模型"分支成立（generate 会被异常吞掉）

    def fake_decode() -> None:
        seen.append(int(asr._buf.size))
        asr._buf = np.zeros(0, dtype=np.float32)

    asr._decode_ready = fake_decode          # type: ignore[assignment]
    asr.push(np.zeros(CHUNK + 123, dtype=np.float32))
    asr.flush()
    check(seen and seen[0] == CHUNK * 2,
          f"尾巴被补静音凑满一块后一起解码（缓冲 {CHUNK + 123} → 解码 {seen}）")


def test_ui_static() -> None:
    print("10) 界面：详情面板默认隐藏 + S 字段已初始化")
    text = (HERE / "meeting" / "ui.html").read_text(encoding="utf-8")
    check('id="detail" style="display:none"' in text,
          "右栏详情面板默认 display:none（空面板不再挂在底部、第一次 Esc 不再被吃掉）")
    check("managed: false, notesHidden: false" in text,
          "S 里 managed / notesHidden 显式初始化")


def test_guide_marker() -> None:
    """使用引导的"看过了"标记：必须记在服务端。

    这一条是这次唯一"看起来能用、其实每次都弹"的坑：启动器用 pywebview，默认
    private_mode=True（WebView2 用户数据目录在内存里、退出即删），localStorage 一律
    不保留。标记写在那儿的话，用户每次开应用都会被引导盖一遍——比没有引导更烦。
    所以这里同时验服务端行为和界面的接线（界面那半是静态检查，真实点击由
    scripts/probe_guide_ui.js 在无头浏览器里走）。
    """
    print("11) 使用引导：标记在服务端（不是 localStorage），界面接线正确")
    from meeting.server import MeetingService
    from meeting.session import MeetingSession

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        marker = tmp / "guide-seen"
        svc = MeetingService(session=MeetingSession(title="测试", path=tmp / "s.json"),
                            settings_path=tmp / "settings.json",
                            guide_seen_path=marker)
        check(svc.guide_seen is False, "没有标记时 guide_seen=False（第一次打开会弹）")
        svc.mark_guide_seen()
        check(marker.exists() and svc.guide_seen is True,
              "mark_guide_seen() 落盘后 guide_seen=True（这台机器不再弹）")
        check(marker.read_text(encoding="utf-8").strip() != "",
              "标记文件里写了时间戳，不是空文件（空文件也能用，但排查时看不出什么时候看的）")

    text = (HERE / "meeting" / "ui.html").read_text(encoding="utf-8")
    check("#settings,#imp,#guide{" in text,
          "引导与其余覆盖层共用同一条 fixed / display:none 规则"
          "（漏了自己的选择器就会变成文档流里的 div，把三栏挤塌）")
    check('id="guide"' in text and "guideSeen: false" in text,
          "界面里有 #guide，且 S.guideSeen 显式初始化")
    check("S.guideSeen = !!d.service.guide_seen" in text,
          "每轮 /api/state 都把服务端标记带回来")
    check('fetch("/api/guide-seen"' in text,
          "关闭引导时向服务端登记（localStorage 在启动器里留不住）")
    check('new URLSearchParams(location.search).get("guide")' in text,
          "自动化可以用 ?guide=off 压住它（其余探针/审计/截图都靠这个开关）")
    check("localStorage.setItem(GUIDE_KEY" not in text,
          "引导标记不再写 localStorage（WebView2 private_mode 每次启动都清空）")

    srv = (HERE / "meeting" / "server.py").read_text(encoding="utf-8")
    check('u.path == "/api/guide-seen"' in srv and "def mark_guide_seen" in srv,
          "服务端有 /api/guide-seen 与 mark_guide_seen()")


def main() -> int:
    test_sync_prune()
    test_asr_reset()
    test_voice_names_and_state()
    test_ingest_skip_rules()
    test_source_filter_before_ranking()
    test_replay_guard()
    test_settle_retry()
    test_short_row_inherits_speaker()
    test_flush_pads_tail()
    test_ui_static()
    test_guide_marker()
    print(f"\n{'全部通过' if not fails else '失败 ' + str(len(fails)) + ' 项'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
