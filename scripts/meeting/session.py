"""Meeting session state: participants, utterances, clues, agenda.

What this layer is for
----------------------
The assistant's job during a meeting is to keep three things in sync and correct at
all times:

* **who is in the room** -- a fixed, small list the user maintains, because binding a
  voice to a name is the one thing the machine cannot infer (an expert's name is
  known in advance; which body belongs to it is not);
* **what was said, in order** -- appended to as the audio arrives, never rewritten
  into a different order;
* **what matters in it** -- clues, each pointing back at the utterance it came from,
  because a clue with no traceable source is an assertion the user cannot check.

The model is deliberately flat and serialisable. It is written to JSON after every
change so a crash mid-meeting costs at most the last utterance, and so the file can be
re-read by the batch pipeline or exported to Obsidian without a database.

Clue types are a **closed set**. Free-form labels were rejected: an LLM asked for
"categories" invents a new one per utterance, and a list where every row has a unique
type is a list nobody can scan. Adding a type is a deliberate edit to ``CLUE_TYPES``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


# ── 声纹登记与建议的门槛 ────────────────────────────────────────────────
#
# 数值来自真实通话的跨会话实测：
#     同一个人（跨 5 天、跨 2116~6725 Hz 三种信道）余弦 0.72 ~ 0.99
#     不同的人                                    最高 0.37
#
# 所以"登记够不够"和"要不要弹建议"都该围着这两个数定，而不是拍脑袋的 0.4。
# 三秒是"这条向量值不值得进库"的下限：实测 1 秒级的段同人/异人分布几乎重叠。
MIN_ENROLL_S = 3.0
MAX_ENROLL_S = 24.0          # 一个人最多登记多少秒语音，够用就停
MAX_ENROLL_VECTORS = 8       # 与 VoiceprintStore.MAX_EMB_PER_PERSON 对齐
# 弹"听起来像某人"的建议门槛。取 0.55：比"不同人"的实测上限 0.37 高得多，
# 又低于"同一个人"的下限 0.72，所以建议基本不会认错人。
# （老代码是 0.4 —— 落在"不同人"的分布里，建议会刷屏。）
SUGGEST_MIN_SCORE = 0.55


# ── clue taxonomy ───────────────────────────────────────────────────────
#
# Ordered by how much attention each deserves in a meeting: obligations and risks
# above reference material. The order drives the default sort in the right column,
# so it encodes priority rather than being alphabetical noise.
CLUE_TYPES: dict[str, dict] = {
    "requirement": {"label": "甲方要求", "short": "要求", "tone": "req",
                    "hint": "对方提出的、需要我方满足的事项"},
    "commitment": {"label": "我方承诺", "short": "承诺", "tone": "com",
                   "hint": "我方答应下来的事，含负责人与期限"},
    "deadline": {"label": "时间节点", "short": "节点", "tone": "date",
                 "hint": "明确的日期或期限"},
    "risk": {"label": "风险提示", "short": "风险", "tone": "risk",
             "hint": "可能导致返工、延期或验收不通过的问题"},
    "agreement": {"label": "历史约定", "short": "约定", "tone": "agr",
                  "hint": "此前会议上已经定下、现在被引用的结论"},
    "clause": {"label": "条款依据", "short": "条款", "tone": "clause",
               "hint": "合同、规范、大纲、标准中的条文"},
    "decision": {"label": "当场决定", "short": "决定", "tone": "dec",
                 "hint": "本次会议当场拍板的事项"},
    "term": {"label": "技术术语", "short": "术语", "tone": "term",
             "hint": "专业名词、系统名、指标名"},
    "person": {"label": "人名", "short": "人名", "tone": "who",
               "hint": "被点到的人"},
    "org": {"label": "机构", "short": "机构", "tone": "org",
            "hint": "公司、单位、部门、测评机构"},
}

# Types that represent something the user must act on or remember. Used for the
# "只看要点" filter and for the agenda's "待跟进" section.
ACTIONABLE = {"requirement", "commitment", "deadline", "risk", "decision"}


def type_meta(t: str) -> dict:
    return CLUE_TYPES.get(t) or {"label": t or "其他", "short": t or "其他",
                                 "tone": "other", "hint": ""}


# ── entities ────────────────────────────────────────────────────────────

@dataclass
class Participant:
    """One person in the room, as the user maintains them.

    ``voice`` is the binding between a name and a voice. It stays ``None`` until the
    user binds it, and the UI never guesses: the whole reason this list exists is that
    the machine knows the names in advance and the voices only after the fact.
    """

    id: str
    name: str
    org: str = ""
    role: str = ""                 # 主持人 / 甲方 / 我方 / 记录 …
    voice_id: str | None = None    # set once a voice is bound
    voice_note: str = ""           # e.g. "已绑定 3 段" or "待绑定"
    speaking: bool = False         # live flag while they are talking
    color: str = ""                # stable accent, assigned on creation

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Clue:
    """Something in the conversation worth surfacing, with its source attached.

    ``seg_id`` is not decoration. The right-hand panel exists to show *evidence* for
    the current utterance, so every clue must be traceable to the utterance that
    produced it; a clue whose source cannot be located is dropped rather than shown.
    """

    id: str
    kind: str                      # a key of CLUE_TYPES
    text: str                      # the clue, phrased as a short statement
    seg_id: int                    # source utterance
    t: float                       # meeting time of the source utterance
    anchor: str = ""               # exact substring of the utterance, for highlighting
    confidence: float = 0.6
    actor: str = ""                # who it belongs to, when known
    due: str = ""                  # deadline text, when known
    refs: list[dict] = field(default_factory=list)   # retrieved supporting material
    pinned: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["meta"] = type_meta(self.kind)
        return d


@dataclass
class PlanItem:
    """One line of either the meeting agenda or the user's own speaking plan.

    Two documents that look alike and mean opposite things. Getting them into one list
    with one behaviour was wrong:

    * **agenda (会议流程)** -- written by the *host*. It is the schedule of the meeting,
      arriving from outside, usually as a file. It flows one way: the meeting tells the
      user what is coming. A speaker and a time slot belong to it.
    * **prep (发言计划)** -- written by the *user*. It is what they intend to say. It
      does not arrive from anywhere; it accumulates *during* the meeting, because
      hearing someone else's item is exactly what tells you that you have something to
      add. A source (which document, which contract clause) belongs to it, not a time.

    Keeping them separate is what makes the middle column's affordances honest: dropping
    an utterance onto the agenda would be meaningless (the user cannot reschedule the
    host), whereas dropping it into the speaking plan is the main way the plan gets
    built mid-meeting.
    """

    id: str
    topic: str
    kind: str = "topic"            # topic | confirm | report | followup
    # Which list this belongs to: "agenda" (host's schedule) or "prep" (my plan).
    #
    # Deliberately NOT named ``list``. A dataclass field named ``list`` lands in the
    # class namespace and shadows the builtin for every method in the class body, so
    # ``list(x)`` anywhere in PlanItem raised "'str' object is not callable" -- a
    # failure that only appears when the code runs, not at import. The wire format keeps
    # the name "list" (see to_dict) because the UI and session files already use it.
    plan_list: str = "agenda"
    detail: str = ""
    done: bool = False
    seg_ids: list[int] = field(default_factory=list)   # utterances that fed this item
    seg_id: str = ""               # for prep: the utterance it was dragged from
    source: str = ""               # for prep: 依据的文件/条款/发言
    speaker: str = ""              # for agenda: who leads this slot
    slot: str = ""                 # for agenda: time or order as printed
    order: int = 0
    # Reference material pinned to this item -- contract clauses, minutes, manual
    # passages. Kept as "参考", not as content: when something comes up mid-meeting
    # ("why is this button designed that way?") the point is to have the *evidence*
    # ready, and the user still decides how to phrase their answer. A reference is
    # therefore stored alongside the topic rather than becoming a topic of its own --
    # turning it into a plan item would bury the user's own agenda under retrieved text.
    refs: list[dict] = field(default_factory=list)
    # Layout for the sticky-note view, in pixels relative to the notes layer.
    #
    # Stored per item and persisted, not kept in the browser: the user arranges the notes
    # to mirror how they intend to speak, and losing that arrangement on a refresh (or
    # when the laptop sleeps and the tab reloads) would make the arrangement worthless.
    # ``None`` means "not placed yet", which lets the layout pass find a free spot instead
    # of stacking every new note at the origin.
    nx: float | None = None
    ny: float | None = None
    # 0 means "auto": the UI picks the width from the notes lane it currently has room for.
    # It used to default to 232.0, which made "the user resized this" indistinguishable from
    # "nobody ever touched it" -- so the layout code inherited 232 and ended up mixing two
    # different widths in one lane until notes overlapped.
    nw: float = 0.0
    nh: float = 0.0                 # 0 = auto height
    nz: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        # The wire format and the session files use "list"; keep that stable while the
        # attribute is called plan_list for the reason above.
        d["list"] = d.pop("plan_list")
        return d


def new_plan_item(topic: str, plan_list: str = "agenda", **kw) -> PlanItem:
    return PlanItem(id=new_id("a"), topic=(topic or "").strip(),
                    plan_list=plan_list if plan_list in ("agenda", "prep") else "agenda",
                    **kw)


# ── id factory ──────────────────────────────────────────────────────────

_ID_LOCK = threading.Lock()
_ID_COUNTER = [0]


def new_id(prefix: str) -> str:
    """Short, unique, sortable-ish id.

    Counter-based rather than derived from the clock. The first version used
    ``int(time.time() * 1000) % 100000000`` with a counter appended, and two ids created
    inside the same millisecond differed only in that counter -- which was taken modulo
    1000 and therefore *collided* on rapid creation. The visible symptom was bizarre
    rather than obvious: adding three sticky notes in one request each produced the same
    id, so ``add_prep``'s deduplication saw "already exists" and merged them into one
    note. Ids must be unique without depending on timing, so the counter is now the
    whole story and the lock makes it safe across the receiver and HTTP threads.
    """
    with _ID_LOCK:
        _ID_COUNTER[0] += 1
        n = _ID_COUNTER[0]
    return f"{prefix}{int(time.time()) % 100000:05d}{n:05d}"


# ── the session ─────────────────────────────────────────────────────────

class MeetingSession:
    """Everything the UI renders, in one serialisable object.

    Thread-safety: the audio receiver thread appends utterances while HTTP threads
    read and mutate clues and participants. A single re-entrant lock guards all
    mutation, and reads return ``to_dict`` snapshots rather than live objects, so a
    request can never observe a half-applied change.
    """

    def __init__(self, title: str = "", path: str | Path | None = None,
                 persist: bool = True) -> None:
        self.title = title or time.strftime("会议 %Y-%m-%d %H:%M")
        self.started = time.time()
        self.path = Path(path) if path else None
        self.persist = persist and self.path is not None
        self.lock = threading.RLock()

        self.participants: list[Participant] = []
        # 声纹 → 人名。一次绑定就是一条记录；以后同声纹的发言自动署名。
        self.voice_names: dict[str, str] = {}
        # "历史被改过"的版本号：声纹署名、改人名、删发言都会 +1。
        # 客户端的轮询是增量的（since=最后一句的 idx），这些改动落在**已经送出去**的行上，
        # 增量永远看不见。有了这个号，客户端发现它变了就整份重拉一次。
        self.history_rev: int = 0
        # 跨会议的声纹库（由服务端注入）。None = 没启用，一切照旧。
        self.voice_store = None
        # 用户点过"不是他"的声纹：别再问了。用户的否定和肯定一样是信息。
        self.denied_spk: set = set()
        self.segments: list[dict] = []          # utterance dicts, appended in order
        self.clues: list[Clue] = []
        self.agenda: list[PlanItem] = []        # both lists; `list` field separates them
        self.imported_from: str = ""            # where the agenda outline came from

        # Rolling counters so the header can show whether the feed is healthy without
        # the UI having to infer it from the text.
        self.stats: dict = {
            "asr_segments": 0,
            "asr_seconds": 0.0,
            "clue_calls": 0,
            "clue_ms": 0.0,
            "retrieval_ms": 0.0,
        }
        self.dropped: list[str] = []            # classification failures, surfaced in UI

    # ── participants ────────────────────────────────────────────────────

    # A stable palette so a person keeps the same accent across reloads and meetings.
    _PALETTE = ["#2f6fdb", "#8b5cf6", "#0d9488", "#d97706", "#dc2626",
                "#0369a1", "#7c3aed", "#15803d"]

    def add_participant(self, name: str, org: str = "", role: str = "") -> Participant:
        name = (name or "").strip()
        if not name:
            raise ValueError("name required")
        with self.lock:
            for p in self.participants:
                if p.name == name:
                    if org:
                        p.org = org
                    if role:
                        p.role = role
                    self._save()
                    return p
            p = Participant(id=new_id("p"), name=name, org=org, role=role,
                            color=self._PALETTE[len(self.participants) % len(self._PALETTE)])
            self.participants.append(p)
            self._save()
            return p

    def update_participant(self, pid: str, **fields) -> Participant | None:
        with self.lock:
            for p in self.participants:
                if p.id != pid:
                    continue
                changed_name = False
                for k in ("name", "org", "role", "voice_id", "voice_note", "speaking"):
                    if k in fields and fields[k] is not None:
                        if k == "name" and fields[k] != p.name:
                            changed_name = True
                        setattr(p, k, fields[k])
                # 改了名字就要把新名字推给他名下的所有发言。
                # 不推的话，界面上会出现"左栏写着张工、发言流里还是老名字"——
                # 而发言上的名字是在绑定时写死的字符串，不会自己跟着变。
                if changed_name:
                    self._reapply_names()
                self._save()
                return p
        return None

    def remove_participant(self, pid: str) -> bool:
        with self.lock:
            before = len(self.participants)
            # 删人之前先解掉他的声纹绑定：不解的话 voice_names 里会留一条指向不存在的人，
            # 以后同声纹的新发言会去查一个查不到的人——名字永远署不上，也查不出原因。
            self.unbind_voice(pid)
            self.participants = [p for p in self.participants if p.id != pid]
            if len(self.participants) != before:
                self._save()
                return True
        return False

    def participant_by_name(self, name: str) -> Participant | None:
        with self.lock:
            for p in self.participants:
                if p.name == name or (name and name in p.name):
                    return p
        return None

    def participant_by_id(self, pid: str) -> Participant | None:
        for p in self.participants:
            if p.id == pid:
                return p
        return None

    # ── utterances ──────────────────────────────────────────────────────

    def add_segment(self, text: str, start: float, end: float, idx: int = -1,
                    speaker: str = "", revised: bool = False, spk: str = "",
                    emb: list | None = None, emb_weak: bool = False) -> dict:
        """Append or update one utterance, keyed by the ASR row index.

        ``idx`` comes from the streaming layer and is stable across the corrections
        that sliding-window ASR produces, so a row that gains its last characters is
        *updated* rather than appended again. Without that key the timeline would grow
        a duplicate every time the recogniser improved a sentence.

        ``spk`` is the **voiceprint cluster** this utterance belongs to. That is the whole
        reason the participant list exists: the machine knows the names in advance and the
        voices only after the fact, so one binding has to cover every utterance from that
        voice. When the cluster is already bound, a new utterance from it is named on
        arrival -- see ``bind_voice``.

        When there is no row index (``idx < 0`` -- the manual/demo path), the same text at
        the same start time is treated as the same utterance. That path used to append
        unconditionally, which made the demo seed script non-idempotent: running it twice
        doubled the entire transcript, and the user saw every line twice. Text alone is not
        enough to key on -- someone can genuinely repeat a sentence -- but the same text at
        the same timestamp is a re-submission, not a repetition.
        """
        with self.lock:
            seg = None
            if idx >= 0:
                for s in self.segments:
                    if s["idx"] == idx:
                        seg = s
                        break
            else:
                for s in self.segments:
                    if (s.get("text") == text
                            and abs(float(s.get("start") or 0.0) - float(start)) < 0.05):
                        seg = s
                        break
            # 这一声纹已经绑过名字：直接署名，不用等用户再点一次。
            #
            # `voice_names[spk]` 里可能是**两种东西**：`bind_voice` 存的是 participant id，
            # 而 `rename_speaker` 存的是**显示名**（那个说话人可能压根没有 Participant）。
            # 所以先按 id 找，找不到就把它当成名字本身。老代码只按 id 找，于是"改过名的
            # 声纹再说话"会取不到人、署名退回未署名——用户看到的正是"改名只对当时那几句生效"。
            if spk and spk in self.voice_names:
                v = self.voice_names[spk]
                who = self.participant_by_id(v) or self.participant_by_name(v)
                speaker = who.name if who is not None else str(v or "")
            # 没绑过、但这次带着声音向量：问一声**跨会议的声纹库**。
            # 注意这里**只给建议、不改名字**：认错人在会议里代价很高
            # （把甲方的话记到我方名下，比"未署名"糟得多），所以要人点一下。
            suggest = None
            if spk and spk not in self.voice_names and emb and self.voice_store is not None \
                    and spk not in self.denied_spk:
                m = self.voice_store.match(emb)
                if m.get("score", 0) >= SUGGEST_MIN_SCORE:
                    suggest = {"person_id": m["person_id"], "name": m["name"],
                               "score": m["score"], "ok": bool(m.get("ok")),
                               "reason": m.get("reason", "")}
            if seg is not None:
                # 文本真的变了就抬 history_rev——**这是"跟不上"的根因**。
                #
                # 客户端的轮询是增量的（since = 已经见过的最大 idx + 1），它**只收新行**，
                # 永远不会重送一条"已经被改过"的行。而流式引擎的核心行为恰恰是
                # **原地更新同一行**（同一个 idx，文本越来越长）。
                # 结果：界面上每一行只显示它的**第一个版本**——也就是第一块 600ms 解出的
                # 那几个字，此后它长成什么样都看不到，直到用户按 F5。
                # 实测就是这么表现的（"微信基本跟得上语音，你的软件基本跟不上"）。
                # 抬 rev 之后客户端发现它变了就整份重拉一次，正在长的那一行就能实时刷新。
                changed_text = seg.get("text") != text
                seg.update({"text": text, "start": start, "end": end,
                            "revised": revised, "updated_at": time.time()})
                if changed_text:
                    self.history_rev += 1
                if speaker:
                    seg["speaker"] = speaker
                if spk:
                    seg["spk"] = spk
                if emb:
                    seg["_emb"] = [float(x) for x in emb]
                    # emb_weak：这一段音频不足/借了太多静音，向量不可信 → 别拿它开新簇、
                    # 也别拿它去登记声纹。老代码只写了 `_emb` 从不写这个标志，而
                    # `bind_voice` 里有一道 `if s.get("emb_weak"): continue` —— 于是那道
                    # 过滤永远是死代码（读的是一个谁都没写的键）。
                    seg["emb_weak"] = bool(emb_weak)
                if suggest:
                    seg["suggest"] = suggest
            else:
                seg = {
                    "id": new_id("s"),
                    "idx": idx if idx >= 0 else len(self.segments),
                    "text": text,
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "speaker": speaker,
                    "spk": spk,
                    "suggest": suggest,
                    # 下划线开头 = 服务端内部字段，to_dict 会剥掉（向量很大，不该进 API）
                    "_emb": [float(x) for x in emb] if emb else None,
                    "emb_weak": bool(emb_weak),
                    "revised": revised,
                    "at": time.time(),
                }
                self.segments.append(seg)
                self.stats["asr_segments"] = len(self.segments)
                self.stats["asr_seconds"] = round(end, 1)
            self._save()
            return seg

    def remove_segment(self, sid: str) -> bool:
        """Drop one utterance. The transcript is a record, but a mis-transcribed line is
        not a record of anything -- and a test that can only add rows leaves junk in the
        user's meeting."""
        with self.lock:
            before = len(self.segments)
            self.segments = [s for s in self.segments if s.get("id") != sid]
            if len(self.segments) != before:
                self.history_rev += 1
                self._save()
                return True
        return False

    def segment_by_id(self, sid: str) -> dict | None:
        with self.lock:
            for s in self.segments:
                if s["id"] == sid:
                    return s
        return None

    def assign_speaker(self, seg_idx: int, speaker: str) -> bool:
        with self.lock:
            for s in self.segments:
                if s["idx"] == seg_idx:
                    s["speaker"] = speaker
                    self._save()
                    return True
        return False

    # ── clues ───────────────────────────────────────────────────────────

    def add_clue(self, kind: str, text: str, seg_id: str, t: float,
                 anchor: str = "", confidence: float = 0.6, actor: str = "",
                 due: str = "", refs: list[dict] | None = None) -> Clue | None:
        """Add a clue, de-duplicating against ones already on the board.

        Deduplication is by anchor plus kind, not by full text: the LLM paraphrases
        the same fact differently each time it is asked, and a board that shows
        "9月5日前完成软件部署" three times with three phrasings is worse than useless.
        """
        text = (text or "").strip()
        if not text or kind not in CLUE_TYPES:
            return None
        anchor = (anchor or "").strip()
        with self.lock:
            key_new = _norm(anchor or text)
            for c in self.clues:
                key_old = _norm(c.anchor or c.text)
                if c.kind == kind and key_old and key_new and (
                        key_old == key_new or key_old in key_new or key_new in key_old):
                    # Same fact seen again: keep the longer phrasing and the better
                    # anchoring, refresh the source if the newer utterance is clearer.
                    if len(text) > len(c.text):
                        c.text = text
                    if anchor and not c.anchor:
                        c.anchor = anchor
                    c.confidence = max(c.confidence, confidence)
                    if actor and not c.actor:
                        c.actor = actor
                    if due and not c.due:
                        c.due = due
                    if refs and not c.refs:
                        c.refs = refs
                    self._save()
                    return c
            c = Clue(id=new_id("c"), kind=kind, text=text, seg_id=seg_id, t=round(t, 2),
                     anchor=anchor, confidence=confidence, actor=actor, due=due,
                     refs=refs or [])
            self.clues.append(c)
            self._save()
            return c

    def update_clue(self, cid: str, **fields) -> Clue | None:
        with self.lock:
            for c in self.clues:
                if c.id == cid:
                    for k in ("kind", "text", "pinned", "actor", "due", "confidence"):
                        if k in fields and fields[k] is not None:
                            setattr(c, k, fields[k])
                    if "kind" in fields and fields["kind"] not in CLUE_TYPES:
                        c.kind = "term"
                    self._save()
                    return c
        return None

    def remove_clue(self, cid: str) -> bool:
        with self.lock:
            before = len(self.clues)
            self.clues = [c for c in self.clues if c.id != cid]
            if len(self.clues) != before:
                self._save()
                return True
        return False

    def clues_for_segment(self, seg_id: str) -> list[Clue]:
        with self.lock:
            return [c for c in self.clues if c.seg_id == seg_id]

    # ── agenda (host's schedule) and prep (my speaking plan) ────────────

    def set_agenda(self, items: list[dict], list_name: str = "agenda") -> list[PlanItem]:
        """Replace one of the two lists wholesale.

        Whole-list replacement is only safe for the agenda, which arrives as a document.
        The speaking plan is edited item by item during the meeting (see ``add_prep`` /
        ``update_plan_item``) because a client that had to send the full list back would
        overwrite whatever else had been added in the meantime.
        """
        list_name = list_name if list_name in ("agenda", "prep") else "agenda"
        with self.lock:
            keep = [a for a in self.agenda if a.plan_list != list_name]
            fresh = []
            for i, it in enumerate(items):
                topic = (it.get("topic") or "").strip()
                if not topic:
                    continue
                fresh.append(PlanItem(
                    id=it.get("id") or new_id("a"), topic=topic,
                    kind=it.get("kind") or "topic", plan_list=list_name,
                    detail=it.get("detail") or "", done=bool(it.get("done")),
                    seg_ids=list(it.get("seg_ids") or []),
                    seg_id=it.get("seg_id") or "", source=it.get("source") or "",
                    speaker=it.get("speaker") or "", slot=it.get("slot") or "",
                    order=it.get("order", i)))
            self.agenda = keep + fresh
            self._save()
            return [a for a in self.agenda if a.plan_list == list_name]

    def add_prep(self, topic: str, source: str = "", seg_id: str = "",
                 detail: str = "", kind: str = "topic",
                 nx: float | None = None, ny: float | None = None) -> PlanItem | None:
        """Add one item to the speaking plan; deduplicates by normalised topic.

        Mid-meeting additions come from dragging an utterance across, so the same
        sentence can easily be dropped twice while reaching for the mouse.
        """
        topic = (topic or "").strip()
        if not topic:
            return None
        # A topic whose normalised form is empty cannot be compared to anything, so the
        # dedup key would be the empty string and *every* such topic would collapse into
        # the first one. That is not hypothetical: a client sending mangled encoding
        # produced four distinct-looking notes that all reduced to "" and merged into a
        # single row. Rejecting them turns a silently wrong result into a visible error.
        key = _norm(topic)
        if len(key) < 2:
            return None
        with self.lock:
            for a in self.agenda:
                if a.plan_list == "prep" and _norm(a.topic) == key:
                    # Already there: enrich rather than duplicate, and remember the
                    # extra source so both utterances are reachable later.
                    if source and source not in a.source:
                        a.source = (a.source + " · " + source).strip(" ·")
                    if seg_id and seg_id != a.seg_id:
                        a.seg_ids.append(seg_id)
                    self._save()
                    return a
            item = PlanItem(id=new_id("a"), topic=topic, plan_list="prep", source=source,
                            seg_id=seg_id, detail=detail, kind=kind,
                            order=len([x for x in self.agenda if x.plan_list == "prep"]),
                            nx=nx, ny=ny)
            self.agenda.append(item)
            self._save()
            return item

    def replace_plan(self, items: list[dict]) -> list[PlanItem]:
        """Replace the speaking plan from the plan file, which is its source of truth.

        Used when the user edits ``发言计划.md`` in Obsidian and asks the assistant to
        reload, and at startup so a restart does not lose the plan.

        Two merges, both deliberate, because "the file wins" taken literally loses work:

        * **Refs are enriched, not replaced.** A ref attached mid-meeting carries retrieval
          metadata -- which knowledge base it came from, which passage, which document --
          that the Markdown has nowhere to put. Writing the file drops it, so a plain reload
          would strip the provenance off every note. A ref whose source still appears in the
          file keeps the session's richer record and only takes the file's edited text.
          A ref the user *deleted* from the file stays deleted: the file decides which
          references exist, because that is what editing a file means.
        * **Layout falls back to the session.** A hand-written item has no ``x``/``y``.
          Reading that as ``None`` and re-running the placement pass would scatter notes the
          user had arranged back to the corner, so an item matched by id keeps its position
          when the file does not specify one.
        """
        with self.lock:
            old = {a.id: a for a in self.agenda if a.plan_list == "prep"}
            keep = [a for a in self.agenda if a.plan_list != "prep"]
            fresh: list[PlanItem] = []
            for i, it in enumerate(items):
                topic = (it.get("topic") or "").strip()
                if not topic:
                    continue
                prev = old.get(it.get("id") or "")
                refs: list[dict] = []
                by_source: dict[str, dict] = {}
                if prev is not None:
                    for r in prev.refs:
                        by_source.setdefault(r.get("source") or r.get("title") or "", r)
                for r in (it.get("refs") or []):
                    src = r.get("source") or r.get("title") or ""
                    was = by_source.pop(src, None)
                    if was is None:
                        refs.append(r)
                        continue
                    merged = dict(was)
                    # The file carries the text the user can see and edit; the session
                    # carries the provenance the file cannot hold. Take the first, keep
                    # the second.
                    if r.get("detail"):
                        merged["snippet"] = r["detail"]
                    refs.append(merged)
                nw = it.get("nw")
                nz = it.get("nz")
                fresh.append(PlanItem(
                    id=it.get("id") or new_id("a"), topic=topic,
                    kind=it.get("kind") or "topic", plan_list="prep",
                    detail=it.get("detail") or "", done=bool(it.get("done")),
                    seg_id=it.get("seg_id") or (prev.seg_id if prev else ""),
                    source=it.get("source") or (prev.source if prev else ""),
                    order=i, refs=refs,
                    nx=it.get("nx") if it.get("nx") is not None
                       else (prev.nx if prev else None),
                    ny=it.get("ny") if it.get("ny") is not None
                       else (prev.ny if prev else None),
                    nw=float(nw) if nw else (prev.nw if prev else 0.0),
                    nz=int(nz) if nz else (prev.nz if prev else 0),
                ))
            self.agenda = keep + fresh
            self._save()
            return fresh

    def bind_voice(self, seg_id: str, participant_id: str) -> dict:
        """Bind one participant to a **voice**, and name every utterance from that voice.

        这就是用户要的那件事：

            相同声纹的人，点击一次绑定之后，其他相同声纹的就自动把名字改过来。

        点一次要改的不只是这一段，而是**同一段声音说过的所有话**。机器先知道名字、
        后知道声音，所以"这是谁"只能由人来定一次；定完之后同一声纹的其余发言不该再问。

        实现上分三层：

        * ``voice_names`` 是长期映射（声纹 → 人）。以后新来的同声纹发言在 ``add_segment``
          里就被直接署名，不需要再点。
        * 已有的同声纹发言**当场改名**，并把改了几段报回给界面——"点了没反应"和
          "改了 3 段"在用户眼里是两回事。
        * ``Participant.voice_id`` 记下绑定的声纹；``voice_note`` 写清"这一段声音共 N 句"，
          于是左栏一眼能看出谁绑了、绑了几段，谁还没绑。

        没有声纹时（ASR 没开说话人模型、或手工灌进来的演示数据）只给这一段落名——
        这仍然是用户点的那一下的直接结果，不该因为缺声纹就什么也不做。
        """
        with self.lock:
            seg = None
            for s in self.segments:
                if s.get("id") == seg_id:
                    seg = s
                    break
            p = self.participant_by_id(participant_id)
            if p is None:
                # 这一场会议里还没有这个人，但声纹库认识他——那就把他建出来。
                #
                # 这正是"下次会议软件自己关联说话人"的落点：第二场会议开始时，
                # 参会人列表是空的（或者只有主持人），而库里记着"这个声音是林浩然"。
                # 用户在建议上点一下，会议里就该自动多出这个人，而不是报"查无此人"。
                # 用库里那个 id 当新参会人的 id，这样库的键在跨会时保持稳定。
                rec = None
                if self.voice_store is not None:
                    rec = (self.voice_store.people or {}).get(participant_id)
                if rec is None:
                    return {"ok": False, "error": "participant not found"}
                p = Participant(id=participant_id,
                                name=rec.get("name") or "未命名",
                                org=rec.get("org") or "", role=rec.get("role") or "")
                self.participants.append(p)
            if seg is None:
                return {"ok": False, "error": "segment not found"}

            spk = seg.get("spk") or ""
            changed = 0
            # 登记到跨会声纹库：用户点这一下的同时也在"教会"软件这个人是什么声音。
            # 这是整张库的**唯一**来源——没有用户确认就没有标签，绝不自己长出来。
            #
            # **不能用 `vecs[:3]`**（老代码就是这样）：那是最早出现的三条向量，
            # 而"最早出现"和"质量好"毫无关系——实测生产链路的段长中位数只有 1.1 秒，
            # 于是库里存的都是最差的一秒级向量，跨会认人自然认不准。
            # 改成：按该声纹**各行时长**从长到短挑，凑够 MIN_ENROLL_S 秒为止。
            enrolled = False
            enrolled_s = 0.0
            if spk and self.voice_store is not None:
                cand = []
                for s in self.segments:
                    if s.get("spk") != spk or not s.get("_emb"):
                        continue
                    dur = float(s.get("end") or 0.0) - float(s.get("start") or 0.0)
                    if s.get("emb_weak"):
                        continue
                    cand.append((dur, s["_emb"]))
                cand.sort(key=lambda kv: -kv[0])
                for dur, v in cand[:MAX_ENROLL_VECTORS]:
                    if self.voice_store.enroll(p.id, p.name, v, note="点选确认",
                                               meeting=self.title,
                                               org=p.org, role=p.role):
                        enrolled = True
                        enrolled_s += max(0.0, dur)
                    if enrolled_s >= MAX_ENROLL_S:
                        break
                if enrolled:
                    self.voice_store.save()
            if spk:
                self.voice_names[spk] = p.id
                for s in self.segments:
                    if s.get("spk") == spk and s.get("speaker") != p.name:
                        s["speaker"] = p.name
                        changed += 1
                # 已经署名的那一段也算在内（它本来就是这个名字时不重复计）
                for s in self.segments:
                    if s.get("spk") == spk:
                        s["speaker"] = p.name
                        s.pop("suggest", None)
                total = sum(1 for s in self.segments if s.get("spk") == spk)
                p.voice_id = spk
                # 把登记到的秒数说清楚：用户才能判断"这个声音教够了没有"。
                warn = "" if enrolled_s >= MIN_ENROLL_S else "（语音偏少，建议再多说几句）"
                p.voice_note = (f"这一段声音共 {total} 句，登记 {enrolled_s:.1f} 秒{warn}"
                                if enrolled else f"这一段声音共 {total} 句")
            else:
                # 没有声纹：只给这一段落名
                if seg.get("speaker") != p.name:
                    seg["speaker"] = p.name
                    changed = 1
                p.voice_note = "已标注 1 段（这段没有声纹信息）"

            # 同一个声纹以前绑过别人？把那个人解绑——一个声音只能是一个人。
            for other in self.participants:
                if other.id != p.id and spk and other.voice_id == spk:
                    other.voice_id = None
                    other.voice_note = "已被改绑到 " + p.name

            self.history_rev += 1
            self._save()
            return {"ok": True, "spk": spk, "participant": p.to_dict(),
                    "changed": changed, "enrolled": enrolled,
                    "voiceprints": (self.voice_store.stats() if self.voice_store else None),
                    "bound_segments": sum(1 for s in self.segments
                                          if s.get("spk") == spk) if spk else changed}

    def deny_suggestion(self, seg_id: str) -> bool:
        """用户说"不是他"：把这个声纹记下来，别再问。

        否定和肯定一样是信息。不记的话，下一句同声纹的发言又会弹同一个错误的建议——
        用户会开始无视所有建议，那这个功能就白做了。
        """
        with self.lock:
            for s in self.segments:
                if s.get("id") == seg_id:
                    spk = s.get("spk") or ""
                    if spk:
                        self.denied_spk.add(spk)
                    for x in self.segments:
                        if spk and x.get("spk") == spk:
                            x.pop("suggest", None)
                    self._save()
                    return True
        return False

    def unbind_voice(self, participant_id: str) -> bool:
        """Undo a binding: clear the name off **every** utterance of **every** voice of
        that person.

        一个人的名下可能挂着不止一个声纹（同一台机器换过麦克风、或者用户手动绑了两段）。
        "解除绑定"要撤掉全部，不能只撤最近绑的那一个——只撤一个的话，另一个声纹的发言
        仍然写着他的名字，而左栏已经显示"未绑定"：界面和事实又对不上了。
        （这个 bug 是 test_voice_bind.py 抓到的。）
        """
        with self.lock:
            p = self.participant_by_id(participant_id)
            if p is None:
                return False
            spks = [k for k, v in self.voice_names.items() if v == p.id]
            for k in spks:
                self.voice_names.pop(k, None)
            for s in self.segments:
                if s.get("spk") and s["spk"] in spks:
                    s["speaker"] = ""
            p.voice_id = None
            p.voice_note = "待绑定"
            self.history_rev += 1
            self._save()
            return True

    def spk_counts(self) -> dict:
        """声纹 → 这个声纹有多少句。界面用它告诉用户"点一下会改几句"。"""
        out: dict = {}
        for s in self.segments:
            k = s.get("spk") or ""
            if k:
                out[k] = out.get(k, 0) + 1
        return out

    def rename_speaker(self, spk: str, new_name: str) -> dict:
        """把某个匿名声纹（匿名-N）重命名为自定义名字。

        不创建 Participant（那是 bind_voice 的事），只改显示名：
        - 所有 spk==old_spk 的段 speaker 字段改为 new_name
        - voice_names[spk] = new_name（持久化映射）
        - 如果 new_name 已对应某个 Participant，则复用其 id

        返回 {"ok": True, "renamed": N, "spk": spk, "name": new_name}
        """
        with self.lock:
            new_name = (new_name or "").strip()
            if not new_name:
                return {"ok": False, "error": "名字不能为空"}
            if not spk:
                return {"ok": False, "error": "spk 不能为空"}
            n = 0
            for s in self.segments:
                if s.get("spk") == spk:
                    s["speaker"] = new_name
                    n += 1
            # 记录映射：voice_names 存 spk → 显示名（这里存名字本身而非 participant_id，
            # 因为可能没有对应 Participant；_reapply_names 只认 participant_id，
            # 所以这里直接写 speaker 字段即可，voice_names 留空或存名字做标记）
            self.voice_names[spk] = new_name
            self.history_rev += 1
            self._save()
            return {"ok": True, "renamed": n, "spk": spk, "name": new_name}

    def reassign_segment(self, seg_id: str, target_spk: str) -> dict:
        """把某段发言从当前 spk 移到另一个 spk（改归属）。

        - 段 spk 改为 target_spk
        - speaker 字段跟随 target_spk 的 voice_names 映射（若有的话）
        - 若 target_spk 为空串，则清除署名

        返回 {"ok": True, "seg_id": seg_id, "from_spk": old, "to_spk": target_spk}
        """
        with self.lock:
            if not seg_id:
                return {"ok": False, "error": "seg_id 不能为空"}
            seg = None
            for s in self.segments:
                if s.get("id") == seg_id:
                    seg = s
                    break
            if seg is None:
                return {"ok": False, "error": "未找到该段发言"}
            old_spk = seg.get("spk") or ""
            seg["spk"] = target_spk
            # 更新 speaker 显示名
            if target_spk:
                name = self.voice_names.get(target_spk, "")
                if name:
                    seg["speaker"] = name
                else:
                    seg["speaker"] = ""
            else:
                seg["speaker"] = ""
            self.history_rev += 1
            self._save()
            return {"ok": True, "seg_id": seg_id,
                    "from_spk": old_spk, "to_spk": target_spk}

    def _reapply_names(self) -> None:
        """After a rename, push the new name onto that voice's utterances.

        `voice_names[spk]` 可能是 participant id（bind_voice 写的）也可能是显示名
        （rename_speaker 写的），所以两种都认——否则"改了名的声纹"在这里又会被跳过。
        """
        for s in self.segments:
            spk = s.get("spk") or ""
            v = self.voice_names.get(spk) if spk else None
            if not v:
                continue
            p = self.participant_by_id(v) or self.participant_by_name(v)
            s["speaker"] = p.name if p is not None else str(v)
        self.history_rev += 1

    def update_layout(self, aid: str, **fields) -> PlanItem | None:
        """Move/resize/raise one note. Separate from ``update_plan_item`` because layout
        changes arrive several times per second while dragging, and each one would
        otherwise rewrite the whole session file on every mousemove."""
        with self.lock:
            for a in self.agenda:
                if a.id != aid:
                    continue
                for k in ("nx", "ny", "nw", "nh", "nz"):
                    if k in fields and fields[k] is not None:
                        setattr(a, k, float(fields[k]))
                self._save()
                return a
        return None

    def reset_layout(self, ids: list[str] | None = None) -> int:
        """Return notes to "never placed", so the UI lays them out again.

        Two callers, and both need it for the same reason -- there was no way back to the
        automatic layout:

        * the user, after a window resize has pushed their arrangement into a corner;
        * the rendering audit, which drags a note to prove dragging works. Without a reset it
          could only drag it back *approximately*, and every run left the note a little
          further from where it started. Worse, dragging a note that had never been placed
          gave it a stored position -- so a read-only diagnostic quietly turned auto-laid-out
          notes into pinned ones, and the user's layout drifted a bit more each time the
          audit ran.
        """
        with self.lock:
            n = 0
            for a in self.agenda:
                if a.plan_list != "prep":
                    continue
                if ids and a.id not in ids:
                    continue
                if a.nx is not None or a.ny is not None or a.nw or a.nz:
                    a.nx = None
                    a.ny = None
                    a.nw = 0.0
                    a.nh = 0.0
                    a.nz = 0
                    n += 1
            if n:
                self._save()
            return n

    def attach_ref(self, aid: str, ref: dict) -> PlanItem | None:
        """Pin reference material onto a plan item.

        Deduplicated by title plus snippet, because the same contract clause is easy to
        drag twice while reaching for the mouse, and a plan item with the same passage
        listed three times is harder to read than one with it listed once.
        """
        title = (ref.get("title") or "").strip()
        snippet = (ref.get("snippet") or "").strip()
        if not title and not snippet:
            return None
        with self.lock:
            for a in self.agenda:
                if a.id != aid:
                    continue
                for existing in a.refs:
                    if (existing.get("title") == title
                            and existing.get("snippet") == snippet):
                        return a
                a.refs.append({
                    "title": title, "snippet": snippet,
                    "label": ref.get("label") or "", "rel": ref.get("rel") or "",
                    "score": ref.get("score"), "heading": ref.get("heading") or "",
                    # Which corpus it came from. Without this a reference is anonymous:
                    # with a project corpus and a shared corpus both in play, "合同 5.2 交付
                    # 节点" is a very different claim depending on whether it came out of
                    # this project's own records or the company-wide contract set, and the
                    # user is the one who has to judge which one is authoritative here.
                    "kb": ref.get("kb") or "", "path": ref.get("path") or "",
                })
                self._save()
                return a
        return None

    def detach_ref(self, aid: str, index: int) -> PlanItem | None:
        with self.lock:
            for a in self.agenda:
                if a.id == aid and 0 <= index < len(a.refs):
                    a.refs.pop(index)
                    self._save()
                    return a
        return None

    def update_plan_item(self, aid: str, **fields) -> PlanItem | None:
        with self.lock:
            for a in self.agenda:
                if a.id != aid:
                    continue
                for k in ("topic", "detail", "done", "kind", "source", "speaker",
                          "slot", "order", "seg_id"):
                    if k in fields and fields[k] is not None:
                        setattr(a, k, fields[k])
                if "seg_ids" in fields and fields["seg_ids"] is not None:
                    a.seg_ids = list(fields["seg_ids"])
                if "refs" in fields and fields["refs"] is not None:
                    a.refs = list(fields["refs"])
                self._save()
                return a
        return None

    def remove_plan_item(self, aid: str) -> bool:
        with self.lock:
            before = len(self.agenda)
            self.agenda = [a for a in self.agenda if a.id != aid]
            if len(self.agenda) != before:
                self._save()
                return True
        return False

    def list_plan(self, list_name: str, include_done: bool = True) -> list[PlanItem]:
        with self.lock:
            return [a for a in self.agenda
                    if a.plan_list == list_name and (include_done or not a.done)]

    def mark_agenda(self, aid: str, done: bool, seg_id: str = "") -> bool:
        with self.lock:
            for a in self.agenda:
                if a.id == aid:
                    a.done = done
                    if seg_id and seg_id not in a.seg_ids:
                        a.seg_ids.append(seg_id)
                    self._save()
                    return True
        return False

    # ── snapshot ────────────────────────────────────────────────────────

    def to_dict(self, since_seg: int = 0) -> dict:
        """A consistent snapshot for the UI.

        ``since_seg`` lets a polling client ask only for what it has not seen. Note what
        that means: this is **not** a change feed. A row that was already delivered and is
        then edited (renamed speaker, reassigned, deleted) will never come back through
        here — which is exactly why ``history_rev`` exists and why ``/api/stream`` pushes
        the row that is still growing.
        """
        with self.lock:
            segs = [s for s in self.segments if s["idx"] >= since_seg]
            return {
                "title": self.title,
                "started": self.started,
                "elapsed": round(time.time() - self.started, 1),
                "participants": [p.to_dict() for p in self.participants],
                "voice_names": dict(self.voice_names),
                "history_rev": self.history_rev,
                # Keys starting with "_" are the service's bookkeeping (which text was
                # last analysed). Stripped here so the wire format stays a clean view of
                # the meeting rather than leaking an implementation detail into the API.
                "segments": [{k: v for k, v in s.items() if not k.startswith("_")}
                             for s in segs],
                "total_segments": len(self.segments),
                "clues": [c.to_dict() for c in self._sorted_clues()],
                # The two lists are delivered separately because the UI renders them as
                # different things with different affordances (see PlanItem).
                "agenda": [a.to_dict() for a in self.agenda if a.plan_list == "agenda"],
                "prep": [a.to_dict() for a in self.agenda if a.plan_list == "prep"],
                "agenda_source": self.imported_from,
                "stats": dict(self.stats, dropped=len(self.dropped)),
                "dropped": self.dropped[-5:],
                "types": {k: v for k, v in CLUE_TYPES.items()},
            }

    def _sorted_clues(self) -> list[Clue]:
        order = {k: i for i, k in enumerate(CLUE_TYPES)}
        return sorted(self.clues, key=lambda c: (not c.pinned, order.get(c.kind, 99), c.t))

    # ── persistence ─────────────────────────────────────────────────────

    def _save(self) -> None:
        """Write the whole session after every change.

        Whole-file rather than incremental: the state is a few hundred kilobytes at
        the very worst, and an atomic rewrite cannot leave a half-written file the way
        an append can. Called with the lock already held.
        """
        if not self.persist or self.path is None:
            return
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "title": self.title,
                "started": self.started,
                "saved_at": time.time(),
                "stats": self.stats,
                "dropped": self.dropped,
                "imported_from": self.imported_from,
                # 这两样以前不落盘，代价是恢复会话后行为变了样：
                #   * denied_spk（用户点过"不是他"的声纹）丢失 → 同一个错建议反复弹，
                #     而"否定也是信息"正是 deny_suggestion 存在的理由；
                #   * history_rev 归零 → 与前端"首个值只当基线、不重拉"的约定靠巧合成立。
                "history_rev": int(self.history_rev),
                "denied_spk": sorted(self.denied_spk),
                "participants": [p.to_dict() for p in self.participants],
                "voice_names": dict(self.voice_names),
                "segments": self.segments,
                "clues": [c.to_dict() for c in self.clues],
                "agenda": [a.to_dict() for a in self.agenda],
            }
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:  # noqa: BLE001 - never let persistence kill a meeting
            self.dropped.append(f"保存失败: {type(e).__name__}: {e}")

    @classmethod
    def load(cls, path: str | Path) -> "MeetingSession":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        s = cls(title=data.get("title") or "", path=path)
        s.started = data.get("started") or time.time()
        s.participants = [Participant(**p) for p in data.get("participants", [])]
        s.voice_names = dict(data.get("voice_names") or {})
        # 与 _save 成对：老会话文件里没有这两个键，缺省就是 0 / 空集。
        s.history_rev = int(data.get("history_rev") or 0)
        s.denied_spk = set(data.get("denied_spk") or [])
        s.segments = data.get("segments", [])
        s.clues = [Clue(**{k: v for k, v in c.items() if k != "meta"})
                   for c in data.get("clues", [])]
        # Older session files stored one flat list. Anything without a `list` field came
        # from that version and was the user's own plan (it was created by hand in the
        # first UI), so it is migrated to "prep" rather than guessed at. Unknown keys are
        # dropped so a file written by an older or newer build still loads.
        raw_plan = list(data.get("agenda") or []) + list(data.get("prep") or [])
        items = []
        known = {f for f in PlanItem.__dataclass_fields__}
        for a in raw_plan:
            a = dict(a)
            a.pop("meta", None)
            if "list" in a and "plan_list" not in a:
                a["plan_list"] = a.pop("list")
            a.setdefault("plan_list", "prep" if "prep" in data else "agenda")
            # 232.0 是"便签道"出现之前的固定宽度默认值。在它还是默认值的那段时间里，
            # "用户缩过这张"和"没人碰过这张"在数据上无法区分；于是存量的 232 会被
            # 当成用户设的宽度，在只需要 172 宽的两列布局里撑出便签道、压住线索栏。
            # 凡是正好等于旧默认值的，按"自动"处理（0 = 自动）。
            if a.get("nw") == 232.0:
                a["nw"] = 0.0
            items.append(PlanItem(**{k: v for k, v in a.items() if k in known}))
        s.agenda = items
        s.stats.update(data.get("stats") or {})
        s.dropped = list(data.get("dropped") or [])
        s.imported_from = data.get("imported_from") or ""
        return s


def _norm(s: str) -> str:
    return re.sub(r"[\s，。、；：？！,.;:?!\"'“”‘’()（）\[\]【】—…·-]+", "", s or "")
