"""The meeting assistant HTTP service: the UI, the realtime audio, and retrieval.

One process, one port
---------------------
The audio path and the UI used to be separate programs, which meant the UI could only
be driven by pasted text while the live microphone wrote somewhere else. This module
puts them together: the same server reads the machine's microphone and serves the page,
so what the microphone hears is what the page shows.

**8510** is the HTTP port by default — the same one the launcher, ``run_meeting.ps1`` and
the headless UI probes use. They used to disagree (this server defaulted to 8500 while
everything else dialled 8510), so a bare ``python -m meeting.server`` looked "broken" to
every probe that tried to talk to it. ``asst/server.py`` is the older standalone assistant
and still defaults to 8500, which is why the two must not run at the same time.

Transport: incremental polling **and** a push channel
-----------------------------------------------------
The page polls ``/api/state?since=<n>`` for the whole structure (participants, clues,
agenda, plan, service state) and subscribes to ``/api/stream`` (Server-Sent Events) for
the utterance that is currently growing. Both are needed: polling is incremental by
``idx`` and therefore **never re-sends a row it already delivered**, while sliding-window
ASR rewrites the same row in place — that is exactly the "界面跟不上" symptom, and the
push channel is what fixes it. ``history_rev`` covers the other half: when a row that was
already delivered gets edited (rename, reassign, delete), the client sees the counter move
and re-fetches everything. SSE rather than WebSocket because the traffic is one-way and
needs no framing; no reconnection logic of our own because EventSource reconnects itself.

Endpoints
---------
    GET  /                        the page
    GET  /api/state?since=N       session snapshot, utterances with idx >= N, + service
    GET  /api/stats               index and service statistics
    GET  /api/transcript          the running transcript as plain text
    GET  /api/refine?term=        per-keyword drill-down (the hover path)
    GET  /api/clue?id=            one clue with its evidence bundle (the click path)
    GET  /api/search?q=           full-text search across the meeting and the corpus
    GET  /api/stream              SSE: one event per changed utterance
    GET  /api/mic                 microphone availability / recording state
    GET  /api/audio               the last recorded WAV
    GET  /api/plan?text=1         the speaking-plan file (optionally its raw text)
    GET  /api/voiceprints         voiceprint store stats (diagnostic; unused by the page)
    GET  /api/settings            settings + health + restart keys + corpora
    POST /api/participants        add/update/remove a person, bind or rename a voice
    POST /api/segment             add, remove or reassign an utterance
    POST /api/clue                pin, retype or delete a clue
    POST /api/agenda              replace the agenda; /api/agenda/mark ticks one item
    POST /api/agenda/import       parse a docx/md/txt outline; scope picks the corpus
    POST /api/prep                plan items: add/update/remove/reorder, attach refs
    POST /api/prep/layout         sticky-note positions (dragging writes these)
    POST /api/plan                reload the plan from 发言计划.md, or write it back
    POST /api/mic                 start / stop recording
    POST /api/audio/test          replay a WAV through the microphone path
    POST /api/settings            write settings (a blank secret means "leave it alone")
    POST /api/restart             ask the launcher to restart this service (managed mode)
    POST /api/reset               clear the utterance stream (test self-cleanup)
    POST /api/title               rename the meeting
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
for _p in (str(_HERE), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from meeting.classify import classify_llm, classify_rules, merge  # noqa: E402
from meeting.planfile import plan_path  # noqa: E402
from meeting.session import ACTIONABLE, CLUE_TYPES, MeetingSession, type_meta  # noqa: E402
from meeting.settings import RESTART_KEYS, SECRET_KEYS, Settings  # noqa: E402

ASR_DEFAULT = Path(r"E:\models\gguf-asr")
# 使用引导"是否已经看过"的标记文件。**为什么记在服务端而不是浏览器**：启动器用
# pywebview 打开界面，而 pywebview 默认是 private_mode=True —— WebView2 的用户数据
# 目录建在内存里、退出即删，localStorage 一律不保留。标记要是写在 localStorage，
# 用户每次开都会再被引导盖一遍（那比没有引导更烦）。记在这个文件里就是"这台机器看过
# 了"，同时浏览器直接打开（http://127.0.0.1:8510）也一样有效。
GUIDE_SEEN = _SCRIPTS.parent / "data" / "guide-seen"
# 定稿后的 LLM 分析失败后重试几次（见 _settle_pass）：失败不再当成功收尾，但也不能无限重试。
LLM_TRIES = 3


class MeetingService:
    """Holds the session, the retrieval assistant, and the audio receiver."""

    def __init__(self, session: MeetingSession, assistant=None,
                 asr_root: Path = ASR_DEFAULT, receiver=None,
                 classify_with_llm: bool = True,
                 settle_after_s: float = 5.0,
                 project_dir: Path | None = None,
                 project_db: Path | None = None,
                 project_name: str = "",
                 global_kb_dir: Path | None = None,
                 global_db: Path | None = None,
                 settings_path: str | Path | None = None,
                 managed: bool = False,
                 guide_seen_path: str | Path | None = None,
                 voice_store=None) -> None:
        self.session = session
        self.assistant = assistant
        self.asr_root = Path(asr_root)
        self.receiver = receiver
        self.classify_with_llm = classify_with_llm
        self.settle_after_s = settle_after_s
        self.llm_max_per_pass = 4      # 每轮最多分类几条（见下面 pending 的说明）
        # 实时推送的订阅者队列（每开一个页面一个）。
        # 换掉轮询的理由：轮询是"拉"，天生有间隔；而且增量的 since=最大idx+1
        # **只送新行、永远不重送被更新的行**，而流式识别恰恰是原地把一行越改越长——
        # 于是界面上每行只显示第一个版本，看起来就是"跟不上"。推送没有这个问题：
        # 服务端一改就送，送的就是那一行的当前内容。
        self._subs: list = []
        self._subs_lock = threading.Lock()
        self.lock = threading.RLock()      # guards retrieval + classification calls
        self._stop = threading.Event()
        # 最近一次落盘的录音（界面「试听回放」/ 转录用）。以前是个只有 "latest"
        # 一个键的 dict，名字暗示多路，实际单路 —— 改成一个路径更诚实。
        self.audio_path: Path | None = None
        self.last_error: str | None = None
        # Where each corpus lives, so an import can be routed to the right one instead of
        # always landing in the same place. A meeting outline must not end up in the shared
        # corpus, and a contract must not end up inside one project's folder.
        self.project_dir = Path(project_dir) if project_dir else None
        self.project_db = Path(project_db) if project_db else None
        self.project_name = project_name or (self.project_dir.name if self.project_dir else "")
        self.global_kb_dir = Path(global_kb_dir) if global_kb_dir else None
        self.global_db = Path(global_db) if global_db else None
        # 由启动器托管（--managed）：界面上的「重启服务」按钮就是冲着它来的——
        # 界面写一个标记文件，启动器把子进程杀掉再拉起来（引擎/知识库才会重新加载）。
        # 不是托管启动时这个按钮不显示：没有人会把进程拉回来。
        self.managed = bool(managed)
        # 使用引导的标记（见模块顶部 GUIDE_SEEN 的说明）。路径可注入，便于测试指向临时目录。
        self.guide_seen_path = Path(guide_seen_path) if guide_seen_path else GUIDE_SEEN
        # Back-end settings live here, not in the session: they describe this installation,
        # not this meeting, and they must survive across meetings.
        self.settings = Settings(settings_path)
        # 把（settings.json + 环境变量）里最终定下来的 LLM 设置推回环境变量：llm_client 只认
        # 环境变量，而 apply_to_env 原来只在"界面上点了保存"时才调用。于是重启之后、没点过
        # 保存的话，settings.json 里的值其实不生效（例如 timeout 一直退回 llm_client 的 1800）。
        self.settings.apply_to_env()
        # 跨会声纹库：这次会议里"点一次绑定"会往库里登记，下次会议就靠它认人。
        self.session.voice_store = voice_store
        # 热词表（P1 识别质量）：参会名单 + 领域术语 → 文本级 ASR 纠错。开关和
        # 显式错词表目录都在 settings.json 的 asr 块（asr.hotwords / asr.hotwords_dir）。
        # 建好表立刻塞给 ASR（若它支持 set_hotwords），并按当前会话填一次；之后每次
        # on_segment 新增参会人/线索时再刷新（见 _refresh_hotwords）。
        from phone_mic.hotwords import HotwordTable
        self.hotwords = HotwordTable(
            enabled=bool(self.settings.get("asr.hotwords", True)),
            explicit_dir=str(self.settings.get("asr.hotwords_dir") or ""))
        self._attach_hotwords()
        self.hotwords.sync_from_session(self.session)

    def _attach_hotwords(self) -> None:
        """把热词表塞给 ASR 引擎（若它支持）。旧版 StreamingASR 没有 set_hotwords，跳过。"""
        asr = getattr(self.receiver, "asr", None) if self.receiver is not None else None
        if asr is not None and hasattr(asr, "set_hotwords"):
            asr.set_hotwords(self.hotwords)

    def _refresh_hotwords(self) -> None:
        """on_segment 后调用：会话的参会人/线索变了就重建热词表（没变则内部跳过）。"""
        try:
            self.hotwords.sync_from_session(self.session)
        except Exception as e:  # noqa: BLE001
            # 纠错是增强不是必需：重建失败绝不影响识别主链路
            print(f"[hotwords] 刷新失败（忽略）：{type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)

    # ── the speaking plan is a file, not a field ────────────────────────

    def plan_path(self) -> Path | None:
        """Where the speaking plan lives: a real Markdown file in the project folder.

        ``None`` when no project folder was mounted -- there is nowhere honest to put it
        then, and inventing a location would mean the plan silently stops travelling with
        the project it belongs to.
        """
        if self.project_dir is None:
            return None
        return plan_path(self.project_dir)

    def save_plan(self) -> dict:
        """Write the speaking plan out. Called after every plan mutation."""
        from meeting import planfile

        p = self.plan_path()
        if p is None:
            return {"ok": False, "error": "没有项目文件夹，发言计划无处存放"}
        items = [a.to_dict() for a in self.session.agenda if a.plan_list == "prep"]
        try:
            planfile.write(p, items, project=self.project_name,
                           meeting=self.session.title or "")
        except OSError as e:
            return {"ok": False, "error": f"写入失败: {e}", "path": str(p)}
        return {"ok": True, "path": str(p), "items": len(items),
                "bytes": p.stat().st_size}

    def load_plan(self) -> dict:
        """Read the plan file back into the session.

        This is the reload path: the user edits ``发言计划.md`` in Obsidian (or any
        editor), then asks the assistant to pick the changes up. The file wins for text,
        kind, done and refs; see ``MeetingSession.replace_plan`` for the two merges that
        stop a reload from throwing away work the file cannot represent.
        """
        from meeting import planfile

        p = self.plan_path()
        if p is None:
            return {"ok": False, "error": "没有项目文件夹"}
        if not p.is_file():
            return {"ok": False, "error": f"文件不存在: {p}", "path": str(p)}
        try:
            items, meta = planfile.load(p)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"解析失败: {type(e).__name__}: {e}",
                    "path": str(p)}
        fresh = self.session.replace_plan(items)
        return {"ok": True, "path": str(p), "items": len(fresh),
                "topics": [a.topic for a in fresh], "meta": meta}

    def ensure_plan(self) -> dict:
        """At startup: adopt an existing plan file, or create one.

        Order matters. If the file exists it is the truth (the user may have edited it
        between meetings, which is the whole point of keeping it as a file). Only when it
        does not exist is one written from the session.
        """
        p = self.plan_path()
        if p is None:
            return {"ok": False, "error": "没有项目文件夹"}
        if p.is_file():
            return self.load_plan()
        if not [a for a in self.session.agenda if a.plan_list == "prep"]:
            # Nothing to write and nothing to read. Creating an empty plan file on every
            # startup would litter the project folder with a document nobody asked for.
            return {"ok": True, "path": str(p), "items": 0, "created": False}
        r = self.save_plan()
        r["created"] = True
        return r

    # ── import routing ──────────────────────────────────────────────────

    def index_imported(self, path: str, scope: str) -> dict:
        """Put an imported file into the project or the global corpus it belongs to.

        The two targets are genuinely different and the choice is the user's:

        * **project** — the file is copied next to the project's other documents so it
          travels with the folder and is re-indexed by the ordinary folder sync. A meeting
          outline belongs here: it is a per-meeting working document.
        * **global** — the file is copied into the shared corpus directory and indexed
          there. Contracts, certificates and regulations belong here.

        The file is *copied*, never moved: the host's original stays where it was, because
        the assistant should not be the reason someone's document disappeared.
        """
        src = Path(path)
        if not src.is_file():
            return {"ok": False, "error": f"文件不存在: {path}"}
        if scope == "global":
            dest_dir = self.global_kb_dir
            db = self.global_db
            name = "公共"
        else:
            dest_dir = self.project_dir / "导入"
            db = self.project_db
            name = self.project_name
        if dest_dir is None or db is None:
            return {"ok": False, "error": f"{name}知识库未配置，跳过入库"}

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        try:
            shutil.copy2(src, dest)
        except OSError as e:
            return {"ok": False, "error": f"复制失败: {e}"}

        from rag.sync import sync_folder

        # Only .md can be chunked today; the parser reads docx but the indexer does not, so
        # a .docx outline is stored for the record and reported as not-indexed rather than
        # silently producing an empty result.
        if dest.suffix.lower() != ".md":
            return {"ok": True, "indexed": False, "copied_to": str(dest), "kb": name,
                    "note": f"{dest.suffix} 暂不支持自动索引，已存放于 {dest_dir.name}/"}
        try:
            root = dest_dir if scope != "global" else self.global_kb_dir
            rep = sync_folder(root, db, name=name, verbose=False)
            return {"ok": True, "indexed": True, "copied_to": str(dest), "kb": name,
                    "added": rep["added"], "updated": rep["updated"],
                    "chunks": rep["chunks"]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"入库失败: {type(e).__name__}: {e}"}

    def start(self) -> None:
        """Start the background settle loop. Idempotent."""
        if getattr(self, "_settle_thread", None) is not None:
            return
        self._settle_thread = threading.Thread(target=self._settle_loop,
                                               name="settle", daemon=True)
        self._settle_thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── audio intake ────────────────────────────────────────────────────

    def _broadcast(self, seg: dict) -> None:
        """把一条发言的当前内容推给所有订阅者。慢的订阅者直接丢，不拖累采集。

        这是"推送"策略的落点：轮询是拉，天生有间隔，而且增量的 since=最大idx+1
        只送新行、永远不重送被更新的行；而流式识别正是原地把一行越改越长。
        """
        payload = {"type": "seg", "seg": {k: v for k, v in seg.items()
                                          if not k.startswith("_")},
                   "history_rev": self.session.history_rev}
        with self._subs_lock:
            for q in list(self._subs):
                try:
                    q.put_nowait(payload)
                except Exception:  # noqa: BLE001  队列满：丢这一条，不阻塞
                    pass

    def on_segment(self, sent) -> None:
        """Record an utterance and run the cheap analysis pass on it.

        Called from the receiver's publish thread. The utterance is recorded first and
        unconditionally: the transcript is the product's core, and a classification
        failure must never cost the user a line of what was said.

        Two-stage analysis is the whole point of this method. Sliding-window ASR
        republishes a row every time it improves it -- the opening "好，" of a test
        meeting was revised 21 times -- and running the LLM on each revision cost 50
        calls for 22 utterances in measurement. So:

        * **now**: retrieval plus the rule pass. Milliseconds, deterministic, and it is
          what produces deadlines, obligations and organisations -- the clue types that
          matter most.
        * **when the row settles** (see ``_settle_loop``): the LLM pass, once.

        The user never waits for the model to see a clue appear, and the model is asked
        about the finished sentence instead of five successive prefixes of it.
        """
        new_spk = getattr(sent, "spk", "") or ""
        # 重发前先记下这一行的旧标签：定稿重聚（refit）可能只改说话人、文本不变，
        # 这种"只换标签"的行也得广播，否则界面永远停在第一个版本。
        prev_spk = ""
        _idx = getattr(sent, "idx", -1)
        if _idx >= 0:
            for s in self.session.segments:
                if s.get("idx") == _idx:
                    prev_spk = s.get("spk", "") or ""
                    break
        seg = self.session.add_segment(sent.text, sent.start, sent.end,
                                       idx=sent.idx, revised=sent.revisions > 0,
                                       spk=new_spk,
                                       emb=getattr(sent, "emb", None),
                                       # emb_weak 要一路带到会话里：绑定声纹时会跳过不可信的
                                       # 向量（短段/借静音算出来的），否则库里存的就是最差的那些。
                                       emb_weak=bool(getattr(sent, "emb_weak", False)))
        text = sent.text or ""
        if seg.get("_analysed_text") == text:
            if new_spk and new_spk != prev_spk:
                self._broadcast(seg)      # 文本没变，但说话人变了 → 补一次刷新
            return
        seg["_analysed_text"] = text
        seg["_changed_at"] = time.time()
        seg["_llm_done"] = False      # it changed, so the LLM verdict is stale again
        self._broadcast(seg)

        try:
            self._analyse(seg, text, llm=False)
        except Exception as e:  # noqa: BLE001
            self.session.dropped.append(f"分析失败: {type(e).__name__}: {e}")
            self.session._save()
        # 这一句可能新增了参会人/领域术语线索 → 刷新热词表，下一句就纳入纠错范围。
        # 目标集没变时内部直接跳过，所以每句都调也不贵。
        self._refresh_hotwords()

    def _settle_loop(self) -> None:
        """Run the LLM pass on utterances that have stopped changing.

        A row is considered finished when it has not been republished for
        ``settle_after_s``. Polling rather than hooking the ASR tick keeps this
        independent of the recognition layer: the service only needs to know "has this
        text stopped moving", which is observable from the session alone.
        """
        while not self._stop.is_set():
            self._stop.wait(1.0)
            if self._stop.is_set():
                break
            try:
                self._settle_pass()
            except Exception as e:  # noqa: BLE001
                self.session.dropped.append(f"定稿分析失败: {type(e).__name__}: {e}")

    def _settle_pass(self) -> None:
        if not self.classify_with_llm:
            return
        now = time.time()
        with self.lock:
            pending = [
                s for s in self.session.segments
                if not s.get("_llm_done")
                and s.get("analysed")
                and (now - s.get("_changed_at", 0)) >= self.settle_after_s
            ]
        # 单轮上限：一段静音之后可能有一批行同时"够 5 秒没变"，不设上限就会
        # 一次打出去一串 LLM 调用（本地模型上就是几十秒的卡顿）。分轮做完，总量不变。
        pending = pending[:self.llm_max_per_pass]
        for seg in pending:
            # 失败**不当成功收尾**：以前这里先把 `_llm_done=True` 再分析，一旦 `_analyse`
            # 抛异常（检索/写盘出错），这一行的 LLM 通道就永久消失了，现场只在 dropped
            # 里留一行字。现在记尝试次数，失败就留着下轮重试，试满 LLM_TRIES 次才放弃。
            tries = int(seg.get("_llm_tries") or 0) + 1
            seg["_llm_tries"] = tries
            try:
                self._analyse(seg, seg["text"], llm=True)
                seg["_llm_done"] = True
            except Exception as e:  # noqa: BLE001
                seg["_llm_done"] = tries >= LLM_TRIES
                self.session.dropped.append(
                    f"LLM 分类失败（第 {tries} 次"
                    f"{'，已放弃' if seg['_llm_done'] else '，下轮重试'}）: "
                    f"{type(e).__name__}: {e}")
                self.session._save()

    def on_audio_file(self, path: Path) -> None:
        with self.lock:
            self.audio_path = path

    # ── 使用引导的"看过了"标记 ─────────────────────────────────────────
    # 为什么由服务端记（而不是 localStorage）：见 GUIDE_SEEN 的注释——启动器的
    # WebView2 是 private_mode，localStorage 每次启动都清空。
    @property
    def guide_seen(self) -> bool:
        try:
            return self.guide_seen_path.exists()
        except OSError:
            # 盘符没了/权限不对：当成"没看过"会让引导每次弹，当成"看过"则相反。
            # 这里选后者——一个读不出来的标记文件不值得让用户每次被盖一层。
            return True

    def mark_guide_seen(self) -> None:
        self.guide_seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.guide_seen_path.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")

    def _analyse(self, seg: dict, text: str, llm: bool = False) -> None:
        """Attach keywords, retrieval evidence and typed clues to one utterance.

        ``llm=False`` is the cheap pass: retrieval plus rules only. ``llm=True`` adds the
        model's judgement, and is called once a row has settled rather than on every
        revision of it.
        """
        t0 = time.time()
        full = text
        result: dict = {"keywords": [], "results": []}

        if self.assistant is not None:
            with self.lock:
                result = self.assistant.process(full)
            seg["keywords"] = [k["term"] for k in result.get("keywords", [])]
            seg["results"] = [
                {"title": r["title"], "rel": r["rel"], "label": r["label"],
                 "score": r["score"], "heading": r.get("heading", ""),
                 "snippet": r["snippet"]}
                for r in result.get("results", [])
            ]
            self.session.stats["retrieval_ms"] = round(
                (time.time() - t0) * 1000, 1)

        people = [p.name for p in self.session.participants]
        clue_specs = classify_rules(full, known_people=people)
        if llm:
            tt = time.time()
            try:
                llm_clues, _ = classify_llm(full)
                clue_specs = merge(clue_specs, llm_clues)
                self.session.stats["clue_calls"] += 1
                self.session.stats["clue_ms"] = round((time.time() - tt) * 1000, 1)
                self.session.stats["llm_calls"] = self.session.stats.get("llm_calls", 0) + 1
            except Exception as e:  # noqa: BLE001 - rules alone are a usable board
                self.session.dropped.append(f"线索分类回退规则: {type(e).__name__}")

        for spec in clue_specs:
            # An anchor must exist in the utterance: it is what the UI highlights and
            # what the evidence panel quotes. Anything unanchored is dropped rather
            # than shown as an assertion the user cannot check.
            if spec.get("anchor") and spec["anchor"] not in full:
                continue
            refs = self._refs_for(spec, seg)
            self.session.add_clue(
                kind=spec["type"], text=spec["text"], seg_id=seg["id"], t=seg["start"],
                anchor=spec.get("anchor", ""), confidence=spec.get("confidence", 0.6),
                actor=spec.get("actor", ""), due=spec.get("due", ""), refs=refs)

        seg["analysed"] = True
        self.session._save()

    def _refs_for(self, spec: dict, seg: dict) -> list[dict]:
        """Retrieve supporting material for a clue, preferring its own anchor.

        The anchor is a better query than the whole clause for reference material --
        "九月五号之前" finds the deadline in the minutes, while the full sentence mostly
        matches itself. Falls back to the segment's results when the anchor is too
        short to be a useful query.
        """
        anchor = spec.get("anchor", "")
        if self.assistant is not None and len(anchor) >= 3:
            with self.lock:
                try:
                    out = self.assistant._search(anchor)
                    if out:
                        return out[:2]
                except Exception:  # noqa: BLE001
                    pass
        return (seg.get("results") or [])[:2]

    def evidence_for(self, clue) -> dict:
        """The click-through bundle: source utterance, corpus hits, related clues."""
        seg = self.session.segment_by_id(clue.seg_id) or {}
        related = [c.to_dict() for c in self.session.clues
                   if c.id != clue.id and (c.kind == clue.kind or c.actor == clue.actor)]
        return {
            "clue": clue.to_dict(),
            "segment": seg,
            "refs": clue.refs,
            "related": related[:6],
            "meta": type_meta(clue.kind),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "plaud-meeting/0.2"
    service: MeetingService = None       # injected by main()
    ui_path: Path = _HERE / "ui.html"

    # ── plumbing ────────────────────────────────────────────────────────

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, s: str, ctype: str = "text/plain; charset=utf-8",
              code: int = 200) -> None:
        body = s.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args) -> None:
        # Requests arrive once a second per open page; logging them would bury the
        # transcript the operator actually wants to watch.
        if "/api/state" not in (args[0] if args else ""):
            sys.stderr.write("[web] %s\n" % (fmt % args))

    # ── GET ─────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        svc = self.service

        if u.path in ("/", "/index.html"):
            try:
                html = self.ui_path.read_bytes()
            except OSError as e:
                self._json({"error": f"ui.html 缺失: {e}"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            return

        if u.path == "/api/state":
            since = int((q.get("since") or ["-1"])[0])
            st = svc.session.to_dict(since_seg=since if since >= 0 else 0)
            st["service"] = self._service_state()
            self._json(st)
            return

        if u.path == "/api/stats":
            a = svc.assistant
            self._json({
                "index": a.stats if a else None,
                "load_ms": round(a.load_ms, 1) if a else None,
                "warm_ms": round(a.warm_ms, 1) if a else None,
                "asr_root": str(svc.asr_root),
                "asr_available": (svc.asr_root / "runtime").is_dir(),
                "ui": str(self.ui_path),
                "clue_types": CLUE_TYPES,
                "actionable": sorted(ACTIONABLE),
            })
            return

        if u.path == "/api/transcript":
            rows = []
            for s in svc.session.segments:
                who = f"{s['speaker']}：" if s.get("speaker") else ""
                rows.append(f"[{s['start']:7.2f}] {who}{s['text']}")
            self._text("\n".join(rows))
            return

        if u.path == "/api/refine":
            term = (q.get("term") or [""])[0].strip()
            if not term:
                self._json({"error": "term required"}, 400)
                return
            with svc.lock:
                if svc.assistant is None:
                    self._json({"term": term, "results": [], "elapsed_ms": 0})
                    return
                self._json(svc.assistant.refine(term))
            return

        if u.path == "/api/clue":
            cid = (q.get("id") or [""])[0]
            for c in svc.session.clues:
                if c.id == cid:
                    self._json(svc.evidence_for(c))
                    return
            self._json({"error": "clue not found"}, 404)
            return

        if u.path == "/api/search":
            text = (q.get("q") or [""])[0].strip()
            if not text:
                self._json({"error": "q required"}, 400)
                return
            with svc.lock:
                corpus = svc.assistant._search(text) if svc.assistant else []
            hits = [s for s in svc.session.segments if text in s.get("text", "")]
            self._json({"q": text, "corpus": corpus,
                        "segments": hits[:20], "segment_hits": len(hits)})
            return

        if u.path == "/api/audio":
            p = svc.audio_path
            if not p or not Path(p).exists():
                self._json({"error": "no audio yet"}, 404)
                return
            data = Path(p).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            self.wfile.write(data)
            return

        if u.path == "/api/stream":
            # Server-Sent Events：一条长连接，服务端一有变化就推。
            # 不用 WebSocket 是因为只需要单向推送，SSE 是浏览器原生的、不需要任何依赖。
            import queue as _q
            sub: "_q.Queue" = _q.Queue(maxsize=200)
            with svc._subs_lock:
                svc._subs.append(sub)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                # 先送一次"现在就绪"，让前端知道连上了
                self.wfile.write(b"event: hello\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        item = sub.get(timeout=15.0)
                    except _q.Empty:
                        self.wfile.write(b": ping\n\n")     # 心跳，防代理断链
                        self.wfile.flush()
                        continue
                    self.wfile.write(("data: " + json.dumps(item, ensure_ascii=False)
                                      + "\n\n").encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass                                  # 页面关了，正常
            finally:
                with svc._subs_lock:
                    if sub in svc._subs:
                        svc._subs.remove(sub)
            return

        if u.path == "/api/mic":
            mic = svc.receiver if hasattr(svc.receiver, "running") else None
            self._json({"available": mic is not None,
                        "running": bool(mic and mic.running),
                        "take": mic.last_take if mic else None,
                        "seconds": (round(time.time() - mic.started_at, 1)
                                    if mic and mic.running and mic.started_at else 0)})
            return

        if u.path == "/api/voiceprints":
            st = svc.session.voice_store
            self._json({"voiceprints": st.stats() if st else None,
                        "denied": sorted(svc.session.denied_spk)})
            return

        if u.path == "/api/plan":
            # The plan file's identity and (optionally) its raw text. The text is what the
            # UI shows in the editor tab: the file is the user's document, so showing the
            # actual file -- not a re-rendered approximation of it -- is the only honest
            # thing to put in an editor.
            p = svc.plan_path()
            out = {"path": str(p) if p else "", "exists": bool(p and p.is_file()),
                   "in_project": p is not None,
                   "items": [a.to_dict() for a in svc.session.list_plan("prep")]}
            if q.get("text", ["0"])[0] in ("1", "true", "yes"):
                try:
                    out["text"] = p.read_text(encoding="utf-8") if out["exists"] else ""
                except OSError as e:
                    out["text"] = ""
                    out["error"] = f"读取失败: {e}"
            self._json(out)
            return

        if u.path == "/api/settings":
            # Never returns a secret. `describe()` reports a key only as set/unset, so a
            # live credential cannot land in the browser, in the page's memory, or in any
            # proxy log between here and there.
            self._json({"settings": svc.settings.describe(),
                        "health": svc.settings.check(),
                        "restart_keys": sorted(RESTART_KEYS),
                        "managed": svc.managed,
                        "corpora": {
                            "project_dir": str(svc.project_dir or ""),
                            "project_db": str(svc.project_db or ""),
                            "project_name": svc.project_name,
                            "global_db": str(svc.global_db or ""),
                            "global_kb": str(svc.global_kb_dir or ""),
                        }})
            return

        self._json({"error": "not found"}, 404)

    # ── POST ────────────────────────────────────────────────────────────

    def do_POST(self) -> None:
        u = urlparse(self.path)
        svc = self.service
        data = self._body()

        if u.path == "/api/participants":
            action = data.get("action") or "add"
            if action == "remove":
                self._json({"removed": svc.session.remove_participant(data.get("id", ""))})
                return
            if action == "deny_suggestion":
                ok = svc.session.deny_suggestion(data.get("seg_id", ""))
                self._json({"ok": ok})
                return
            if action == "bind_voice":
                # 用户点一次"这段是某人说的"：服务端把同一声纹的**所有**发言一起署名。
                # 用户要的就是这个——"相同声纹的人，点击一次绑定之后，其他相同声纹的就
                # 自动把名字改过来"。返回改了几段，界面才能说"已改 3 段"而不是"点了没反应"。
                r = svc.session.bind_voice(data.get("seg_id", ""), data.get("id", ""))
                if not r.get("ok"):
                    self._json({"error": r.get("error", "绑定失败")}, 404)
                    return
                self._json(r)
                return
            if action == "unbind_voice":
                ok = svc.session.unbind_voice(data.get("id", ""))
                self._json({"ok": ok})
                return
            if action == "rename_speaker":
                r = svc.session.rename_speaker(
                    data.get("spk", ""), data.get("name", ""))
                if not r.get("ok"):
                    self._json({"error": r.get("error", "改名失败")}, 400)
                    return
                self._json(r)
                return
            if action == "update":
                p = svc.session.update_participant(
                    data.get("id", ""),
                    name=data.get("name"), org=data.get("org"), role=data.get("role"),
                    voice_id=data.get("voice_id"), voice_note=data.get("voice_note"),
                    speaking=data.get("speaking"))
                if p is None:
                    self._json({"error": "participant not found"}, 404)
                    return
                svc._refresh_hotwords()   # 姓名/单位变了 → 纠错目标跟着变
                self._json({"participant": p.to_dict()})
                return
            try:
                p = svc.session.add_participant(data.get("name", ""),
                                                data.get("org", ""), data.get("role", ""))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            svc._refresh_hotwords()       # 新参会人 → 他的姓名进纠错范围
            self._json({"participant": p.to_dict()})
            return

        if u.path == "/api/reset":
            # 清空这一次会议的内容（发言/线索）。测试和"重新开始"都要用，
            # 而且必须走服务端——直接改文件的话内存里那份还在。
            keep_people = bool(data.get("keep_people"))
            with svc.lock:
                n_seg = len(svc.session.segments)
                svc.session.segments = []
                svc.session.clues = []
                svc.session.dropped = []
                svc.session.history_rev += 1
                if not keep_people:
                    for p in svc.session.participants:
                        p.voice_id = None
                        p.voice_note = "待绑定"
                svc.session._save()
            print(f"[web] 已清空发言流（{n_seg} 条发言、线索全清）", flush=True)
            self._json({"ok": True, "cleared": n_seg,
                        "history_rev": svc.session.history_rev})
            return

        if u.path == "/api/mic":
            # 界面上的录音按钮走这里。开始/停止都由用户点，不由服务器猜。
            mic = svc.receiver if hasattr(svc.receiver, "running") else None
            if mic is None:
                self._json({"error": "这次启动没有麦克风输入（--mic）"}, 400)
                return
            action = (data.get("action") or "start").strip()
            if action == "start":
                # 告诉采集器：会话里最大的 idx 是几，你从下一个开始编。
                # 不告诉她的话，新录的话会覆盖已有发言（见 mic_source 里的说明）。
                base = max([s.get("idx", -1) for s in svc.session.segments] + [-1]) + 1
                if hasattr(mic, "prepare"):
                    # elapsed = 会议已经进行了多久；不平移时间戳的话新发言会跑到时间轴最前面
                    # 基准取"已有内容在时间轴上的末尾"，**不是会话寿命**。
                    # 用 now - session.started 撞过大坑：会话文件是 22 小时前建的、
                    # 中间反复重启，于是新发言被放到 79990 秒（22 小时后），
                    # 数据全在、位置离谱，看起来就像"没更新"。
                    tail = max([float(x.get("end") or 0) for x in svc.session.segments] + [0.0])
                    mic.prepare(base, tail)
                mic.start(load_models=True)
                self._json({"ok": True, "running": True, "take": mic.last_take})
                return
            if action == "stop":
                take = mic.stop()
                self._json({"ok": True, "running": False, "take": take})
                return
            self._json({"error": f"unknown action {action}"}, 400)
            return

        if u.path == "/api/segment":
            # 删掉一段发言。除了"转错了要能删"，它还是测试自清理的唯一办法——
            # 一个只能加不能删的接口，会让每个测试都在用户的会议里留垃圾。
            if (data.get("action") or "") == "remove":
                self._json({"removed": svc.session.remove_segment(data.get("id", ""))})
                return
            if (data.get("action") or "") == "reassign":
                r = svc.session.reassign_segment(
                    data.get("id", ""), data.get("target_spk", ""))
                if not r.get("ok"):
                    self._json({"error": r.get("error", "改归属失败")}, 400)
                    return
                self._json(r)
                return
            text = (data.get("text") or "").strip()
            if not text:
                self._json({"error": "text required"}, 400)
                return
            # Manual path kept alive on purpose: it is how the page is demonstrated and
            # how a passage from a document gets analysed without any audio at all.
            seg = svc.session.add_segment(text, data.get("start", 0.0),
                                          data.get("end", 0.0), idx=-1,
                                          spk=(data.get("spk") or ""),
                                          emb=(data.get("emb") or None))
            svc._broadcast(seg)   # 手工路径也要推送，否则它和实时路径行为不一致
            try:
                svc._analyse(seg, text)
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            self._json({"segment": seg})
            return

        if u.path == "/api/clue":
            cid = data.get("id", "")
            if data.get("action") == "remove":
                self._json({"removed": svc.session.remove_clue(cid)})
                return
            c = svc.session.update_clue(
                cid, kind=data.get("kind"), text=data.get("text"),
                pinned=data.get("pinned"), actor=data.get("actor"),
                due=data.get("due"), confidence=data.get("confidence"))
            if c is None:
                self._json({"error": "clue not found"}, 404)
                return
            self._json({"clue": c.to_dict()})
            return

        if u.path == "/api/agenda":
            items = data.get("items")
            if items is None:
                self._json({"error": "items required"}, 400)
                return
            self._json({"agenda": [a.to_dict() for a in svc.session.set_agenda(items)]})
            return

        if u.path == "/api/settings":
            items = data.get("set") or {}
            if not isinstance(items, dict) or not items:
                self._json({"error": "set 需要是 {键: 值} 的对象"}, 400)
                return
            changed, errors = [], []
            for k, v in items.items():
                try:
                    # An empty string for a secret means "leave it alone", not "clear it":
                    # the field is rendered blank on purpose, so submitting the form without
                    # touching it must not wipe a working key.
                    if k in SECRET_KEYS and str(v) == "":
                        continue
                    old = svc.settings.get(k)
                    svc.settings.set(k, v)
                    changed.append({"key": k, "old": "" if k in SECRET_KEYS else old,
                                    "new": "" if k in SECRET_KEYS else svc.settings.get(k)})
                except KeyError as e:
                    errors.append(str(e))
            if changed:
                svc.settings.save()
                # The LLM settings go straight into the environment, which is how
                # llm_client reads them -- so a change takes effect on the next call with no
                # restart, and the batch pipeline picks up the same values.
                svc.settings.apply_to_env()
                if svc.assistant is not None and svc.assistant.unused_llm:
                    svc.assistant.unused_llm = False
            self._json({"changed": changed, "errors": errors,
                        "settings": svc.settings.describe(),
                        "health": svc.settings.check(),
                        "restart_keys": sorted(RESTART_KEYS),
                        "managed": svc.managed})
            return

        if u.path == "/api/guide-seen":
            # 界面上那份使用引导被关掉时调一次：从此这台机器不再自动弹。
            # 写失败（盘满/只读）要返回 500 并在 stderr 留一行，否则"前端到底发没发"
            # 只能靠猜；但界面那边刻意不弹错误——引导下次再弹一遍不值得打断用户。
            try:
                svc.mark_guide_seen()
            except OSError as e:
                print(f"[web] 写使用引导标记失败: {e}", file=sys.stderr, flush=True)
                self._json({"ok": False, "error": str(e)}, 500)
                return
            self._json({"ok": True, "guide_seen": True})
            return

        if u.path == "/api/restart":
            # 「重启服务」：进程没法重启自己（ASR 模型、知识库、嵌入后端都是启动那一刻
            # 加载好的），所以把这件事交给启动器——写一个标记文件，它负责把子进程杀掉
            # 再拉起来。界面在断连期间显示"服务重启中"，恢复后自己接上（见 ui.html）。
            # 标记文件路径与 launcher/app.py 里的 Backend.flag 必须一致。
            if not svc.managed:
                self._json({"error": "本次服务不是由启动器启动的，无法自动重启："
                                     "请手动关掉再重开"}, 400)
                return
            flag = _SCRIPTS.parent / "data" / "launcher" / "restart.flag"
            try:
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            except OSError as e:
                self._json({"error": f"写重启标记失败: {e}"}, 500)
                return
            print("[web] 已请求启动器重启服务", flush=True)
            self._json({"ok": True, "flag": str(flag)})
            return

        if u.path == "/api/agenda/import":
            # Import the host's outline. Reads docx / md / txt / plain text; the parser
            # lives in meeting.outline and is tested against the shapes hosts actually
            # send (see scripts/test_outline.py).
            #
            # ``scope`` decides which knowledge base the file's *content* goes to, and it is
            # required rather than defaulted silently, because the two答案 are both plausible
            # and one of them is wrong:
            #
            #   "project" (default) — the file stays in the project's own corpus. A meeting
            #       outline is a per-meeting working document: who leads which slot, in what
            #       order. Indexing it globally would let "15:00 第三方测评安排确认" answer
            #       questions in every other project, and it is stale the moment the meeting
            #       ends -- exactly the "会议内容污染" the user objected to.
            #   "global" — for documents that really are shared reference material
            #       (contracts, qualification certificates, regulations). A host's agenda is
            #       not that.
            #
            # Today this endpoint only parses the outline into the session (no indexing at
            # all), so the default changes nothing yet. It exists now so that when the
            # automatic folder indexing lands, the outline cannot be swept into the global
            # corpus by a rule nobody chose.
            path = (data.get("path") or "").strip()
            scope = (data.get("scope") or "project").strip().lower()
            if scope not in ("project", "global", "session"):
                self._json({"error": f"scope 只能是 project / global / session，收到 {scope!r}"}, 400)
                return
            if not path:
                self._json({"error": "path required"}, 400)
                return
            try:
                from meeting.outline import outline_to_items

                items, note = outline_to_items(path)
            except FileNotFoundError:
                self._json({"error": f"文件不存在: {path}"}, 404)
                return
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
            if not items:
                self._json({"error": "没有解析出任何条目", "note": note}, 422)
                return
            saved = svc.session.set_agenda(items, list_name="agenda")
            svc.session.imported_from = str(path)
            indexed = None
            if scope in ("project", "global") and svc.project_dir is not None:
                # Only indexed when the assistant actually has a corpus to put it in. The
                # response says what happened either way, so the UI never has to guess.
                indexed = svc.index_imported(path, scope)
            self._json({
                "agenda": [a.to_dict() for a in saved],
                "note": note, "path": path, "count": len(saved),
                "scope": scope, "indexed": indexed,
                "preview": items[:40],
            })
            return

        if u.path == "/api/prep":
            # The speaking plan is edited item by item, never replaced wholesale: it grows
            # during the meeting, and a client sending the full list back would clobber
            # whatever was added in the meantime.
            #
            # Every successful mutation also rewrites <项目>/发言计划.md, because that file
            # -- not the session -- is the plan's real home. Wrapping it in one closure
            # rather than repeating the call at each of the seven success paths is not just
            # brevity: a forgotten call at one branch would mean "this particular edit
            # silently does not reach the file", which is invisible until the user opens it
            # in Obsidian and finds it stale.
            def done(payload: dict, code: int = 200) -> None:
                r = svc.save_plan()
                if payload is not None and r.get("ok"):
                    payload["plan_file"] = r
                self._json(payload, code)

            action = data.get("action") or "add"
            if action == "add":
                # ``kind`` is passed through, not ignored. It drives the note's colour
                # band, so dropping it would silently make every sticky note look
                # generic (the payload field existed and was accepted by the client, and
                # the notes still all came out as "topic" -- a silent field loss).
                item = svc.session.add_prep(
                    topic=data.get("topic", ""), source=data.get("source", ""),
                    seg_id=data.get("seg_id", ""), detail=data.get("detail", ""),
                    kind=data.get("kind") or "topic",
                    nx=data.get("nx"), ny=data.get("ny"))
                if item is None:
                    # Distinguish "no topic" from "topic cannot be deduplicated" so a
                    # client with an encoding problem sees why nothing was created,
                    # instead of silently getting back the same item every time.
                    raw = (data.get("topic") or "").strip()
                    why = ("topic required" if not raw
                           else f"主题无效（规范化后为空）: {raw[:20]!r}")
                    self._json({"error": why}, 400)
                    return
                done({"item": item.to_dict()})
                return
            if action == "attach_ref":
                # Pin retrieved material onto a plan item as *reference*. Three shapes,
                # because the UI offers three: attach to a named item, attach to the
                # item the user currently has selected, or create an item to hold it.
                ref = data.get("ref") or {}
                aid = (data.get("id") or "").strip()
                if not aid:
                    # The fallback topic comes from the reference's own title, so it is
                    # almost always non-empty; if it still fails the reason is worth
                    # surfacing rather than returning a bare 500.
                    item = svc.session.add_prep(
                        topic=(data.get("topic") or ref.get("title") or ""),
                        detail="作为发言参考", kind="topic")
                    if item is not None:
                        svc.session.attach_ref(item.id, ref)
                        done({"item": item.to_dict(), "created": True})
                        return
                    self._json({"error": "参考没有可用标题，无法新建计划项"}, 400)
                    return
                item = svc.session.attach_ref(aid, ref)
                if item is None:
                    self._json({"error": "计划项不存在或参考为空"}, 404)
                    return
                done({"item": item.to_dict()})
                return
            if action == "detach_ref":
                item = svc.session.detach_ref(data.get("id", ""), int(data.get("index", -1)))
                if item is None:
                    self._json({"error": "参考不存在"}, 404)
                    return
                done({"item": item.to_dict()})
                return
            if action == "update":
                item = svc.session.update_plan_item(
                    data.get("id", ""), topic=data.get("topic"), detail=data.get("detail"),
                    done=data.get("done"), kind=data.get("kind"), source=data.get("source"),
                    order=data.get("order"))
                if item is None:
                    self._json({"error": "item not found"}, 404)
                    return
                done({"item": item.to_dict()})
                return
            if action == "remove":
                done({"removed": svc.session.remove_plan_item(data.get("id", ""))})
                return
            if action == "reorder":
                ids = data.get("ids") or []
                for i, aid in enumerate(ids):
                    svc.session.update_plan_item(aid, order=i)
                done({"prep": [a.to_dict() for a in svc.session.list_plan("prep")]})
                return
            self._json({"error": f"unknown action {action}"}, 400)
            return

        if u.path == "/api/plan":
            # The plan file itself: where it is, what is in it, and two actions.
            # "reload" is the point of keeping it as a file -- the user edits it in
            # Obsidian and asks the assistant to pick the changes up.
            action = (data.get("action") or "reload").strip()
            if action == "reload":
                self._json(svc.load_plan())
                return
            if action == "write":
                self._json(svc.save_plan())
                return
            self._json({"error": f"unknown action {action}"}, 400)
            return

        if u.path == "/api/prep/layout":
            # Batch, and called when a drag *ends* rather than on every mousemove: each
            # call rewrites the session file, and a drag emits dozens of positions per
            # second. The browser keeps the live position; the server keeps the result.
            if (data.get("action") or "") == "reset":
                # 回到"没摆过"，让界面重新自动排布。窗口改大小之后、或者想把界面收回
                # 干净状态时用得上；也是审计器"拖完不留痕"的唯一办法。
                n = svc.session.reset_layout(data.get("ids") or None)
                plan = svc.save_plan()
                self._json({"reset": n, "plan_file": plan,
                            "items": [a.to_dict() for a in svc.session.list_plan("prep")]})
                return
            items = data.get("items") or []
            out = []
            for it in items:
                a = svc.session.update_layout(
                    it.get("id", ""), nx=it.get("nx"), ny=it.get("ny"),
                    nw=it.get("nw"), nh=it.get("nh"), nz=it.get("nz"))
                if a is not None:
                    out.append(a.to_dict())
            # Positions live in the plan file too, so a drag has to reach it -- otherwise
            # the arrangement the user just made is gone the next time the file wins.
            plan = svc.save_plan()
            self._json({"saved": len(out), "items": out, "plan_file": plan})
            return

        if u.path == "/api/agenda/mark":
            # Separate from /api/agenda because ticking a box must not round-trip the
            # whole list: the client would have to send back a snapshot that may be
            # stale by the time it arrives, silently reverting another device's edit.
            ok = svc.session.mark_agenda(data.get("id", ""), bool(data.get("done")))
            if not ok:
                self._json({"error": "agenda item not found"}, 404)
                return
            self._json({"agenda": [a.to_dict() for a in svc.session.agenda]})
            return

        if u.path == "/api/title":
            title = (data.get("title") or "").strip()
            if title:
                svc.session.title = title
                svc.session._save()
            self._json({"title": svc.session.title})
            return

        if u.path == "/api/audio/test":
            # Replay a WAV through the microphone path, so the whole page can be
            # exercised on a machine with no working microphone.
            path = (data.get("path") or "").strip()
            if not path or not Path(path).is_file():
                self._json({"error": f"文件不存在: {path!r}"}, 400)
                return
            mic = svc.receiver if hasattr(svc.receiver, "replay_wav") else None
            if mic is None:
                self._json({"error": "这次启动没有音频输入（--no-asr 起的服务不能回放）"}, 400)
                return
            if getattr(mic, "running", False):
                self._json({"error": "正在录音：先按停止，再回放文件"}, 400)
                return
            speed = float(data.get("speed") or 1.0)

            def pump():
                time.sleep(0.3)
                try:
                    # 和录音按钮同一套编号/时间戳规则。不 prepare 的话，回放出来的
                    # 发言会从 idx 0 开始，把会话里已有的行逐条覆盖掉。
                    base = max([s.get("idx", -1) for s in svc.session.segments] + [-1]) + 1
                    tail = max([float(x.get("end") or 0) for x in svc.session.segments] + [0.0])
                    mic.prepare(base, tail)
                    info = mic.replay_wav(path, speed=speed)
                    print(f"[web] 回放 {Path(path).name}: {info}", flush=True)
                except Exception as e:  # noqa: BLE001
                    svc.last_error = f"回放失败: {type(e).__name__}: {e}"

            threading.Thread(target=pump, daemon=True).start()
            self._json({"started": True, "path": path, "speed": speed})
            return

        self._json({"error": "not found"}, 404)

    # ── service state for the page header ───────────────────────────────

    def _service_state(self) -> dict:
        svc = self.service
        r = svc.receiver
        st = {
            "audio": None,
            "resampler": None,
            "asr": None,
            "last_error": svc.last_error,
            "classify_llm": svc.classify_with_llm,
            # 由启动器托管时，界面才显示「重启服务」（改 ASR 引擎/知识库后一键生效）。
            "managed": svc.managed,
            # 使用引导是否已经看过（标记文件在服务端，见 GUIDE_SEEN）。
            "guide_seen": svc.guide_seen,
            # 声纹 → 句数。界面用它显示"同一声音还有 2 句"，让用户知道点一下会改几句。
            "spk_counts": svc.session.spk_counts(),
            # 声纹 → 名字映射。界面"改归属"对话框用它显示目标说话人名字。
            "voice_names": svc.session.voice_names,
            # The corpora the *running* service actually has, not what settings.json
            # says. Those can differ until a restart, and the import dialog has to show
            # the real target: offering "项目库" when no project folder was mounted would
            # send the file nowhere.
            "corpora": {
                "project_name": svc.project_name,
                "project_dir": str(svc.project_dir or ""),
                "project_db": str(svc.project_db or ""),
                "global_db": str(svc.global_db or ""),
                "global_kb": str(svc.global_kb_dir or ""),
            },
        }
        if r is not None:
            try:
                live = r.status()
                st["audio"] = live.get("session")
                st["asr"] = live.get("asr")
                st["resampler"] = live.get("resampler")
                st["tick_error"] = live.get("tick_error")
            except Exception as e:  # noqa: BLE001
                st["audio_error"] = f"{type(e).__name__}: {e}"
        return st


# ── entry point ─────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    root = _SCRIPTS.parent
    ap = argparse.ArgumentParser(prog="meeting.server")
    ap.add_argument("--mic-auto", action="store_true",
                    help="启动就开始录（默认等界面上的录音按钮）")
    ap.add_argument("--mic-save", default=None, metavar="WAV",
                    help="把麦克风采集到的音频存成 WAV，便于离线排查识别问题")
    ap.add_argument("--mic", nargs="?", const=-1, type=int, default=-1,
                    metavar="DEV",
                    help="本机麦克风设备号（不传设备号=用系统默认）。"
                         "设备号见 python scripts/mic_list.py")
    ap.add_argument("--db", default=None,
                    help="公共库索引路径（缺省取 settings.json 的 corpora.public_db；"
                         "文件不存在就新建，让嵌入模型启动即就绪）")
    ap.add_argument("--kb", default=None,
                    help="公共库语料目录（缺省取 settings.json 的 corpora.public_kb）")
    ap.add_argument("--kb-spec", action="append", default=None, metavar="NAME=DB|KB",
                    help="额外知识库，可重复；给了它就走多库模式，--db/--kb 被忽略。"
                         "格式：名称=索引.db|语料目录 或 名称=索引.db。"
                         "例：--kb-spec \"公共=data/public.db|D:/公司文档\" "
                         "--kb-spec \"项目=data/ar.db|D:/公共资料库\"" )
    ap.add_argument("--no-rag", action="store_true",
                    help="不加载检索：既不开知识库索引，也不预热嵌入模型")
    ap.add_argument("--managed", action="store_true",
                    help="由启动器托管：允许界面请求重启服务（启动器收到标记后把子进程拉回来）")
    ap.add_argument("--project", default=None, metavar="DIR",
                    help="项目文件夹：启动时自动增量索引它，并作为「项目库」加载。"
                         "配合 --public-db 得到「公共库 + 当前项目库」两本。")
    ap.add_argument("--public-db", default=None, metavar="DB",
                    help="公共库索引（合同/资质/规范）。与 --project 一起用时作为共享库。")
    ap.add_argument("--public-kb", default=None, metavar="DIR",
                    help="公共库语料目录（默认取 --public-db 所在目录）。导入到全局库的文件复制到这里。")
    ap.add_argument("--project-name", default=None,
                    help="项目名（缺省取文件夹名），用于在结果里标注来源。")
    ap.add_argument("--project-db", default=None, metavar="DB",
                    help="项目索引文件位置（缺省 <项目>/.plaud/rag.db，随项目文件夹走）。")
    ap.add_argument("--port", type=int, default=8510, help="HTTP port for the UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--session", default=None, help="session JSON path (autosaved)")
    ap.add_argument("--fresh", action="store_true",
                    help="start a new session even if --session already exists")
    ap.add_argument("--title", default="")
    ap.add_argument("--no-llm", action="store_true",
                    help="rule-only clue classification (offline / fastest)")
    ap.add_argument("--no-asr", action="store_true",
                    help="UI only, no audio receiver (browse a saved session)")
    ap.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args(argv)

    session_path = Path(args.session) if args.session else (
        root / "data" / "sessions" / f"session-{time.strftime('%Y%m%d-%H%M%S')}.json")

    if session_path.exists() and not args.fresh:
        # Resuming is the common case: a meeting that was interrupted, or a demo session
        # built in advance. Loading keeps the utterances, clues and voice bindings, and
        # the session keeps autosaving to the same file.
        session = MeetingSession.load(session_path)
        print(f"  载入已有会话: {session_path}  "
              f"({len(session.segments)} 段发言, {len(session.clues)} 条线索)")
    else:
        session = MeetingSession(title=args.title, path=session_path)
        # A realistic starting roster for this project, so the left column is not empty
        # on first run. Removing or renaming them is one click; typing eight names
        # before the first demo is friction with no benefit.
        for name, org, role in (("林浩然", "甲方单位", "甲方"),
                                ("徐博文", "甲方单位", "甲方"),
                                ("孙磊", "史塔克", "我方"),
                                ("高志远", "史塔克", "我方"),
                                ("郑涛", "史塔克", "我方"),
                                ("何嘉伟", "史塔克", "我方"),
                                ("王毅", "史塔克", "我方"),
                                ("罗建国", "史塔克", "我方")):
            session.add_participant(name, org, role)

    # ── 检索语料：默认挂「公共库」，索引缺了就新建 ─────────────────────
    #
    # 不给任何语料参数时，以前是"检索未启用"——于是**嵌入模型根本没有被加载**，
    # 界面右栏永远空着，导入面板也只会说"未配置知识库"。现在默认挂 settings.json
    # 里的公共库（corpora.public_db / corpora.public_kb）：
    #   · 索引已存在 → 直接加载
    #   · 索引不存在、语料目录里有东西 → 先增量建索引，再加载
    #   · 两者都没有 → 新建空索引并加载：嵌入模型启动即预热（Assistant 里那次
    #     "预热" 检索），之后在界面「导入」里喂资料就能立刻被检索到
    # `--no-rag` 整个关掉；`--project` / `--kb-spec` 仍然优先于这条默认路径。
    _cfg = Settings()
    public_db = Path(args.public_db or args.db or _cfg.get("corpora.public_db")
                     or (root / "data" / "ar.db"))
    public_kb = Path(args.public_kb or args.kb or _cfg.get("corpora.public_kb")
                     or public_db.parent)

    assistant = None
    if args.no_rag:
        print("  检索     : 未启用（--no-rag）")
    elif args.project:
        # The intended flow: point the assistant at a project folder and it indexes itself.
        #
        # Auto-indexing on startup is only acceptable because it is *incremental*. A full
        # rebuild (what `rag.ingest` does by default) would re-embed the whole project every
        # launch, and that cost grows with the project's age -- measured here at 1.07s for
        # 3 files, 0.10s when nothing changed. So the startup path is sync-then-load, and the
        # report is printed so the user can see what changed rather than guessing.
        from rag.sync import sync_folder

        proj_dir = Path(args.project)
        proj_name = args.project_name or proj_dir.name
        # The index lives beside the project as a dot-folder, so moving the project folder
        # moves its knowledge with it and nothing outside the project needs updating.
        proj_db = Path(args.project_db) if args.project_db else (
            proj_dir / ".plaud" / "rag.db")
        try:
            rep = sync_folder(proj_dir, proj_db, name=proj_name, verbose=False)
            print(f"  项目索引 : {proj_name}  "
                  f"新增 {rep['added']} 更新 {rep['updated']} 未变 {rep['unchanged']} "
                  f"删除 {rep['removed']} 嵌入 {rep['embedded']}  "
                  f"({rep['elapsed_s']}s, 现有 {rep['files']} 文件/{rep['chunks']} chunks)")
            for s in rep["skip_reasons"]:
                print(f"             - 跳过 {s}")
        except Exception as e:  # noqa: BLE001 - a broken project folder must not stop the UI
            print(f"  项目索引 : 失败 {type(e).__name__}: {e}")

        specs = []
        if public_db.exists():
            specs.append({"name": "公共", "db": str(public_db), "kb": str(public_kb)})
        specs.append({"name": proj_name, "db": str(proj_db), "kb": str(proj_dir)})
        from asst.core import Assistant
        from rag.multi_index import MultiIndex

        t0 = time.time()
        mi = MultiIndex(specs)
        assistant = Assistant(index=mi, top_k=args.top_k, unused_llm=args.no_llm)
        st = assistant.stats
        print(f"  检索     : {len(mi.entries)} 个知识库 / {st['chunks']} chunks "
              f"({time.time() - t0:.2f}s)")
        for row in st["indexes"]:
            print(f"             · {row['name']:<8} {row['chunks']} chunks  {row['db']}")
        for err in st.get("errors") or []:
            print(f"             ! {err}")
    elif args.kb_spec:
        # 多库模式：公共库（合同/资质/规范）+ 项目库（会议纪要/进度）。
        # 分开的理由不是检索质量 —— 实测六道公共题在混库与分库下都是 3/6、正确答案
        # 都排第一 —— 而是生命周期：项目库每次开会都要重建，混在一起会让每次重建
        # 都重新嵌入整个稳定的公共语料。项目库放在项目文件夹里还让该文件夹自包含。
        specs = []
        for raw in args.kb_spec:
            name, _, rest = raw.partition("=")
            db, _, kb = rest.partition("|")
            if not db:
                print(f"  检索     : --kb-spec 格式错误，忽略: {raw}")
                continue
            specs.append({"name": name.strip() or Path(db).stem,
                          "db": db.strip(), "kb": kb.strip() or str(Path(db).parent)})
        if specs:
            from asst.core import Assistant
            from rag.multi_index import MultiIndex

            t0 = time.time()
            mi = MultiIndex(specs)
            assistant = Assistant(index=mi, top_k=args.top_k, unused_llm=args.no_llm)
            st = assistant.stats
            mode = "按排名融合（不同嵌入模型）" if st.get("mixed_backends") else "分数合并（同一嵌入模型）"
            print(f"  检索     : {len(mi.entries)} 个知识库 / {st['chunks']} chunks "
                  f"({time.time() - t0:.2f}s, {mode})")
            for row in st["indexes"]:
                print(f"             · {row['name']:<8} {row['chunks']} chunks  {row['db']}")
            for err in st.get("errors") or []:
                print(f"             ! {err}")
        else:
            print("  检索     : --kb-spec 里没有可用的索引，未启用")
    else:
        # 默认路径：公共库（settings.json 的 corpora.*），索引缺了就建。
        from asst.core import Assistant
        from rag.rag_core import RagIndex

        t0 = time.time()
        if public_db.exists():
            idx = RagIndex(db_path=str(public_db), kb_dir=str(public_kb))
            print(f"  公共库   : {public_db.name} · {idx.stats()['chunks']} chunks "
                  f"· {public_db}")
        elif public_kb.is_dir() and any(public_kb.rglob("*.md")):
            from rag.sync import sync_folder

            rep = sync_folder(public_kb, public_db, name="公共", verbose=False)
            idx = RagIndex(db_path=str(public_db), kb_dir=str(public_kb))
            print(f"  公共库   : 新建索引 {public_db.name}  新增 {rep['added']} "
                  f"更新 {rep['updated']} 嵌入 {rep['embedded']} "
                  f"({rep['elapsed_s']}s, {rep['chunks']} chunks)")
        else:
            # 空库也要加载：嵌入模型只有在这里被"预热"过一次，用户第一次提问才不会
            # 等模型加载；导入面板也才能把资料直接喂进来。
            idx = RagIndex(db_path=str(public_db), kb_dir=str(public_kb))
            print(f"  公共库   : 空（已新建 {public_db}）——界面「导入」里加资料即可被检索")
        assistant = Assistant(index=idx, top_k=args.top_k, unused_llm=args.no_llm)
        print(f"  检索     : {assistant.stats['chunks']} chunks "
              f"({time.time() - t0:.2f}s, {assistant.stats['embedder']}, "
              f"嵌入预热 {assistant.warm_ms:.0f} ms)")

    receiver = None
    if not args.no_asr:
        # 本机麦克风就是音频输入通道。它和界面走的是**同一套**发布逻辑（on_segment），
        # 所以检索、线索分类、声纹、界面全都自动跟上，不需要第二套代码。
        from phone_mic.mic_source import LocalMicReceiver

        # 识别后端由 settings.json 的 asr.engine 决定（funasr 默认 / firered）。
        # 单独读一遍 Settings 只为选后端；MeetingService 后面还会按自己的
        # settings_path 再读一次，两者读的是同一个文件，互不干扰。
        _cfg = Settings()
        dev = None if args.mic == -1 else args.mic
        receiver = LocalMicReceiver(device=dev, want_spk=True,
                                    engine=str(_cfg.get("asr.engine") or "funasr"),
                                    firered_dir=str(_cfg.get("asr.firered_dir") or ""))
        receiver.save_dir = (Path(args.mic_save).parent if args.mic_save
                             else root / "data" / "mic-test")
        # 刻意**不自动开始**：录不录由用户在界面上按。自动开始的话，
        # 服务器一启动就在录静音，而"什么时候停"没人说得准——
        # 用户的反馈就是这一条（「没有按钮让我告诉你什么时候停止录音」）。
        # **后台预加载模型**：加载要二十多秒，绝不能放在"点录音"的请求里
        # （那样按钮二十秒没反应，用户会以为没按到，于是按两次）。
        def _preload() -> None:
            receiver._loading = True
            t0 = time.time()
            try:
                receiver.asr.load()
                # 记下这次加载花了多久：状态接口（界面上那个"音频"指示）会读它，
                # 不记的话永远是 0.0，"到底自动加载成功了没有"就只能靠猜。
                receiver.load_ms = (time.time() - t0) * 1000
            finally:
                receiver._loading = False
                receiver._load_evt.set()
        threading.Thread(target=_preload, daemon=True).start()
        if args.mic_auto:
            receiver.start(load_models=False)
            print("  麦克风   : 已自动开始（--mic-auto）")
    # The receiver owns ASR; retrieval and clue classification happen here, in one
    # place, so nothing is analysed twice.
    # 跨会声纹库。放在**项目文件夹里**（<项目>/.plaud/voiceprints.json），因为"谁是林浩然"
    # 是这个项目的人际关系，跟着项目走；没挂项目时退回 data/voiceprints.json。
    # 它的价值全在跨会议：这次认出来，下次自动认。
    from meeting.voiceprints import VoiceprintStore

    vp_path = (Path(args.project) / ".plaud" / "voiceprints.json" if args.project
               else root / "data" / "voiceprints.json")
    voice_store = VoiceprintStore(vp_path)
    _vp = voice_store.stats()

    svc = MeetingService(session=session, assistant=assistant, receiver=receiver,
                         classify_with_llm=not args.no_llm,
                         project_dir=(Path(args.project) if args.project else None),
                         project_db=(Path(args.project_db) if args.project_db else (
                             Path(args.project) / ".plaud" / "rag.db" if args.project else None)),
                         project_name=(args.project_name or (
                             Path(args.project).name if args.project else "")),
                         global_db=public_db,
                         global_kb_dir=public_kb,
                         managed=bool(args.managed),
                         voice_store=voice_store)
    svc.start()
    Handler.service = svc

    # The speaking plan is a file in the project folder, so startup has to reconcile the
    # two: adopt the file if the user edited it between meetings, otherwise write one from
    # whatever the session already holds. Either way the announcement is printed rather than
    # assumed -- "the plan is a file now" is only true if this actually ran.
    plan_info = svc.ensure_plan()
    plan_line = ""
    if plan_info.get("ok"):
        if plan_info.get("created"):
            plan_line = f"新建（{plan_info.get('items', 0)} 项）"
        elif plan_info.get("items"):
            plan_line = f"已从文件载入 {plan_info['items']} 项"
        else:
            plan_line = "空（还没有内容）"
        plan_line += f"  {plan_info.get('path', '')}"
    else:
        plan_line = plan_info.get("error", "未启用")

    print("=" * 66)
    print("  plaud 实时会议助理")
    print(f"  会议     : {session.title}")
    print(f"  会话文件 : {session_path}")
    print(f"  页面     : http://{args.host}:{args.port}/")
    print(f"  发言计划 : {plan_line}")
    print(f"  声纹库   : {_vp['people']} 人 / {_vp['vectors']} 条向量  {_vp['path']}")
    if receiver is not None:
        print("  音频     : 本机麦克风（设备 "
              f"{'默认' if args.mic == -1 else args.mic}）——对着麦克风说话，页面会出字")
    else:
        print("  音频     : 未启用（--no-asr）")
    print(f"  线索分类 : {'规则 + LLM' if not args.no_llm else '仅规则'}"
          f"  ({len(CLUE_TYPES)} 类)")
    print("=" * 66, flush=True)

    if receiver is not None:
        receiver.on_segment = svc.on_segment
        receiver.on_audio_file = svc.on_audio_file
        # 不在这里启动采集：录不录由界面上的按钮决定（见上面 --mic-auto 的说明）。

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        if receiver is not None:
            receiver.stop()
        svc.stop()
        session._save()
        print(f"会话已保存: {session_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
