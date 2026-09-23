"""声纹库：把人名和声音绑定**跨会议**存下来。

为什么必须有它：``spk``（CAM++ 的说话人簇号）只在一次会议内部有效——换个会它重新编号，
所以它永远无法回答"这个声音上次是谁"。要跨会认人，只有一条路：
把**嵌入向量**存下来，下次拿新声音的向量去比余弦相似度。

这个库因此是产品的一部分，而不是一个缓存：

* 第一次开会，人还是陌生人，用户点几次把名字绑上；
* 从第二次开始，软件自己认人，用户只需要**确认**（或者纠正）；
* 每一次确认都是一次登记，库越用越准。

两条设计上的取舍，都是刻意的：

1. **不静默改名。** 相似度过了阈值只给出**建议**（"听起来像 林浩然 0.82"），要人点一下。
   认错人在会议里代价很高——把甲方的话记到我方名下，比"未署名"糟得多。
2. **一个人可以存多条向量。** 同一个人换麦克风、感冒、离得远近，声音都不一样。
   只存一条质心会越用越偏；存最近 N 条、取最大值匹配，鲁棒得多。
   （代价是误配概率略升，所以阈值和"要比第二名高出多少"一起判。）

存储：``data/voiceprints.json``。纯 JSON，因为这张库要能被人打开看、能跟着项目走、
能进版本管理——它记的是"谁的什么声音"，属于用户的资料，不该藏在二进制里。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

# 匹配阈值：余弦相似度。**用真实会议录音实测校准**（data/phone-pull/phone_rec_16k.wav）：
#   同一个人相邻两段 0.72 / 0.81；不同人之间 0.08 / 0.12 / 0.15 / 0.22。
# 0.60 落在中间，两侧余量都很大——比拍脑袋的数字可靠。见 test_voiceprint_real.py。
DEFAULT_THRESHOLD = 0.60
# 还要比第二名高这么多才算"确定"，否则说明两个人听着都像——宁可问用户。
DEFAULT_MARGIN = 0.10
# 每个人最多留几条向量。多了会让"取最大值"偏向噪声，少了不适应声音变化。
MAX_EMB_PER_PERSON = 8


def cosine(a, b) -> float:
    """余弦相似度。两条向量都归一化过就等价于点积，但这里不假设归一化。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class VoiceprintStore:
    """人名 ↔ 声音向量的跨会存储。线程安全（服务端是多线程的）。"""

    def __init__(self, path: str | os.PathLike | None = None,
                 threshold: float = DEFAULT_THRESHOLD,
                 margin: float = DEFAULT_MARGIN):
        self.path = Path(path) if path else None
        self.threshold = threshold
        self.margin = margin
        self.people: dict[str, dict] = {}
        self.load_error = ""
        self.lock = threading.RLock()
        self.load()

    # ── persistence ─────────────────────────────────────────────────────

    def load(self) -> None:
        with self.lock:
            if self.path is None or not self.path.is_file():
                self.people = {}
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # 库损坏不能让软件起不来：宁可从一个空库开始（下面会报出来），
                # 也不能让"声纹库读不了"变成"会议助理打不开"。
                self.people = {}
                self.load_error = "声纹库读取失败，已从空库开始"
                return
            self.people = raw.get("people") or {}
            self.load_error = ""

    def save(self) -> None:
        with self.lock:
            if self.path is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "saved_at": time.time(), "people": self.people}
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)

    # ── enrollment ──────────────────────────────────────────────────────

    def enroll(self, person_id: str, name: str, embedding: list[float],
               note: str = "", meeting: str = "", org: str = "", role: str = "") -> bool:
        """把一条声音登记到某人名下。返回是否真的加了新向量。"""
        if not embedding:
            return False
        with self.lock:
            rec = self.people.setdefault(person_id, {
                "name": name, "embs": [], "count": 0,
                "first_seen": time.time(), "meetings": [],
            })
            rec["name"] = name or rec.get("name") or ""
            if org:
                rec["org"] = org
            if role:
                rec["role"] = role
            rec["count"] = int(rec.get("count") or 0) + 1
            rec["last_seen"] = time.time()
            if note:
                rec["note"] = note
            if meeting and meeting not in (rec.get("meetings") or []):
                rec.setdefault("meetings", []).append(meeting)
                rec["meetings"] = rec["meetings"][-12:]
            embs = rec.setdefault("embs", [])
            # 太像的就不重复存：同一次会议里同一个人几十句，存满了会把库撑爆，
            # 而且"取最大值"也会偏向那一次的音质。
            for e in embs:
                if cosine(e, embedding) > 0.92:
                    return False
            embs.append([float(x) for x in embedding])
            del embs[:-MAX_EMB_PER_PERSON]
            return True

    # ── matching ────────────────────────────────────────────────────────

    def match(self, embedding: list[float]) -> dict:
        """拿一条声音去库里找人。

        返回 ``{"person_id", "name", "score", "margin", "ok"}``；``ok`` 表示"够确定"。
        不够确定时仍然返回最高分，好让界面说"有点像某人，但不确定"——
        静默地什么都不显示，用户会以为这个功能不存在。
        """
        if not embedding or not self.people:
            return {"ok": False, "person_id": None, "name": "", "score": 0.0,
                    "margin": 0.0, "reason": "库是空的" if not self.people else "没有声纹"}
        with self.lock:
            scored = []
            for pid, rec in self.people.items():
                best = 0.0
                for e in (rec.get("embs") or []):
                    c = cosine(e, embedding)
                    if c > best:
                        best = c
                scored.append((best, pid, rec.get("name") or ""))
            scored.sort(reverse=True)
            top = scored[0]
            second = scored[1][0] if len(scored) > 1 else 0.0
            margin = top[0] - second
            ok = top[0] >= self.threshold and margin >= self.margin
            return {"ok": ok, "person_id": top[1], "name": top[2],
                    "score": round(top[0], 4), "margin": round(margin, 4),
                    "reason": "" if ok else (
                        "相似度不够（%.2f < %.2f）" % (top[0], self.threshold)
                        if top[0] < self.threshold else
                        "两个人听着都像（只高出 %.2f）" % margin)}

    def forget(self, person_id: str) -> bool:
        with self.lock:
            if person_id in self.people:
                self.people.pop(person_id, None)
                self.save()
                return True
        return False

    def stats(self) -> dict:
        with self.lock:
            return {
                "people": len(self.people),
                "vectors": sum(len(r.get("embs") or []) for r in self.people.values()),
                "path": str(self.path or ""),
                "threshold": self.threshold,
                "margin": self.margin,
                "load_error": getattr(self, "load_error", ""),
                "names": {pid: r.get("name") for pid, r in self.people.items()},
            }
