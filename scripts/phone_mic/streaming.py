"""Real-time meeting feed: PCM in, committed utterances out.

Why this is a sliding window and not an incremental "growing buffer"
---------------------------------------------------------------
The obvious design -- keep everything uncommitted in a buffer and re-transcribe
the buffer as it grows, committing sentences the VAD has already closed -- was
measured and does not work (``scripts/probe_stream_vad.py``, section C). Feeding
a cumulative buffer of 4 s, 8 s, ... still produced only **one** committed
sentence after 8 s of audio: the VAD does not finalise a span until it has seen
considerable silence after it. A caller polling that interface would sit silent
for tens of seconds while people were talking.

So each tick transcribes a **fixed recent window** instead. Two facts from the
same probe make that safe:

1. VAD and sentence timestamps are measured **from the start of the array we
   pass in**, not from some internal absolute clock (slice @12 s reported
   ``2030``, i.e. 2.03 s into that slice). Offsets must therefore be added by
   the caller -- this module does that, and never reports a raw model timestamp.
2. ``sentence_info[i]["timestamp"]`` is a **per-token** list of ``[start_ms,
   end_ms]`` pairs, so a sentence's span is ``ts[0][0] .. ts[-1][1]``. Using
   ``timestamp[0]`` alone collapses every sentence to ~0.1 s. ``pipeline.py``
   carries a long comment about this exact bug; the helper here is the single
   place it is handled.

Windows overlap heavily and the same utterance is re-transcribed several times.
Committing everything would emit it three or four times, so a sentence is only
released once its end is at least ``guard_s`` behind the window's end. Anything
closer to the edge may still be cut mid-word, and is left for the next tick,
by which time it has moved leftwards into settled territory.

Deduplication is textual, not timestamp-based, because timestamps for the same
utterance genuinely differ between windows (different leading context changes
where the VAD decides the span starts). The key is the normalised text; when the
same key reappears, the row is **updated in place** -- its text may gain a
character, and its start time settles -- and ``changed`` flags it so a UI can
re-render rather than duplicate.
"""

from __future__ import annotations

import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import audio as A

# ── 阈值（全部是**余弦相似度**，不是距离）────────────────────────────────
#
# 数值来自真实通话的跨会话实测：
#
#     同一个人（跨 5 天、跨 2116~6725 Hz 三种信道）  0.72 ~ 0.99
#     不同的人                                      最高 0.37
#
# 所以 0.55 落在中间且两侧余量都很大。**别用距离写阈值**：`refit_cap=0.70` 若按距离理解，
# 等价于"余弦 0.30 以上就合并"——比"不同人"的上限 0.37 还低，等于把两个不同的人强行并
# 在一起（这正是"新说话人被旧簇吞掉"的成因）。
#
# 归簇面对的是**一两秒的短句**，向量噪声比 6 秒段大，所以归簇的合并门槛要比
# 跨会匹配的 0.60 略低一点：0.55。
SPK_MERGE_COS = 0.55
# 低于这个相似度就**确定**不是同一个人 → 开新簇（而不是硬并到已有质心上）。
SPK_NEW_COS = 0.35
# 归簇时要求的最短语音。太短的段算出来的向量不可靠：
# 0.5 秒的段连 CAM++ 自己都没有足够信息，硬拿去归簇只会制造新簇。
MIN_EMB_S = 1.2
# 算向量时往两边各借一点音频：把边界上的半个字包进来，向量更稳。
EMB_PAD_S = 0.35
# 目标窗口长度与滑窗步长。**短句也要凑够 2 秒**：实测 2 秒段的同人/异人可分性
# （d′ 7.6）明显优于 1 秒段，而 2.0s/1.0s 滑窗平均把 d′ 再提到 8.4。
# 这也是 3D-Speaker 自己的一手配置（1.5s 窗 / 0.75s 跳）的同一个思路。
EMB_WIN_S = 2.0
EMB_HOP_S = 1.0
# 短句最多往两边各借多少秒。**只借静音**：邻近有别的行时就停在那里，
# 免得把别人的话算进来。
EMB_EXPAND_S = 1.0
# 一个 row 最多用多少秒算向量（长独白取尾部，避免滚出缓冲后取到别人的音频）。
EMB_MAX_S = 12.0
# 旧名字（test_voiceprint_real.py 还在用）。归簇的合并门槛就是它。
SPK_CLUSTER_T = SPK_MERGE_COS


def slice_span(audio: np.ndarray, t0: float, start: float, end: float,
               *, win: float = EMB_WIN_S, hop: float = EMB_HOP_S,
               pad: float = EMB_PAD_S, min_win: float = EMB_WIN_S,
               expand: float = EMB_EXPAND_S, max_len: float = EMB_MAX_S,
               lo_bound: float | None = None,
               hi_bound: float | None = None) -> tuple[np.ndarray | None, dict]:
    """切出说话人片段要送进声纹模型的音频。返回 ``(样本, meta)``。

    这一步是整条链路最容易出错的地方，所以规则写死在这里，两条路径共用：

    1. **必须先凑够 ``min_win`` 秒**。实测 1 秒段的同人/异人分布几乎重叠，
       2 秒才分得开。句子的 ASR 时间戳中位数只有 1.1 秒（见 docs 的实测表），
       直接拿它去算向量就是在制造噪声。所以不足 2 秒就往两边**借静音**。
    2. **只借静音**：`lo_bound` / `hi_bound` 是左右相邻行给出的硬边界，
       借到那里就停——跨过邻近的发言就会把别人的声音算进这个向量。
    3. **头被滚出缓冲时取尾部**，而不是从缓冲起点切。老代码用 ``max(0, ...)``
       钳位，切片会从缓冲区起点开始，**混进最多 0.3 秒别人的话**（实测 5% 的行命中）。
    4. **长段用滑窗平均**：2.0s 窗 / 1.0s 跳，逐窗算向量再平均。实测 d′ 7.6 → 8.4。

    ``meta`` 里带 ``used_s`` / ``n_win`` / ``head_missing_s`` / ``tail_missing_s`` /
    ``borrowed_s``，调用方据此决定可信度（``used_s < MIN_EMB_S`` 就别拿去开新簇）。
    """
    n = len(audio)
    if n == 0:
        return None, {"used_s": 0.0, "n_win": 0, "reason": "empty"}
    # 1) 带 pad 的目标区间
    a0, a1 = start - pad, end + pad
    # 2) 不足 min_win 就向两边借，但不越过邻近行
    if a1 - a0 < min_win:
        need = min_win - (a1 - a0)
        lo_lim = (lo_bound if lo_bound is not None else a0 - expand)
        hi_lim = (hi_bound if hi_bound is not None else a1 + expand)
        lo_lim = max(lo_lim, a0 - expand, t0)
        hi_lim = min(hi_lim, a1 + expand, t0 + n / float(A.TARGET_SR))
        take_lo = min(need / 2.0, max(0.0, a0 - lo_lim))
        a0 -= take_lo
        take_hi = min(need - take_lo, max(0.0, hi_lim - a1))
        a1 += take_hi
        # 还不够就再向能借的一侧多借
        take_lo2 = min(min_win - (a1 - a0), max(0.0, a0 - lo_lim))
        a0 -= max(0.0, take_lo2)
        take_hi2 = min(min_win - (a1 - a0), max(0.0, hi_lim - a1))
        a1 += max(0.0, take_hi2)
    borrowed = max(0.0, (start - pad) - a0) + max(0.0, a1 - (end + pad))
    # 3) 换算下标，记录被截掉的部分（不再静默钳位）
    i0_raw = int(round((a0 - t0) * A.TARGET_SR))
    i1_raw = int(round((a1 - t0) * A.TARGET_SR))
    head_missing = max(0.0, -i0_raw) / float(A.TARGET_SR)
    tail_missing = max(0.0, i1_raw - n) / float(A.TARGET_SR)
    i0 = max(0, i0_raw)
    i1 = min(n, i1_raw)
    if i1 - i0 < int(MIN_EMB_S * A.TARGET_SR):
        return None, {"used_s": max(0, i1 - i0) / float(A.TARGET_SR), "n_win": 0,
                      "head_missing_s": head_missing, "tail_missing_s": tail_missing,
                      "borrowed_s": borrowed, "reason": "too_short"}
    # 4) 头部确实丢了 → 改用**尾部**（同一行的后半段，比缓冲起点安全）
    if head_missing > 0.05:
        keep = int(min(max_len, (i1 - i0) / float(A.TARGET_SR)) * A.TARGET_SR)
        i0 = max(0, i1 - keep)
    seg = audio[i0:i1]
    meta = {"used_s": len(seg) / float(A.TARGET_SR), "n_win": 0,
            "head_missing_s": head_missing, "tail_missing_s": tail_missing,
            "borrowed_s": borrowed, "reason": ""}
    w, step = int(win * A.TARGET_SR), int(hop * A.TARGET_SR)
    if len(seg) <= w:
        return seg, meta
    return seg, meta | {"w": w, "step": step}


def embed_slice(sv, seg: np.ndarray) -> list | None:
    """一段音频 → L2 归一化向量。失败返回 None（调用方决定怎么降级）。"""
    try:
        res = sv.generate(input=np.asarray(seg, np.float32), cache={},
                          disable_pbar=True)
        r = res[0] if isinstance(res, list) and res else res
        raw = (r or {}).get("spk_embedding") if isinstance(r, dict) else None
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    vec = raw.cpu().numpy().reshape(-1).tolist() if hasattr(raw, "cpu") \
        else [float(x) for x in list(raw)]
    nrm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / nrm for x in vec]


def embed_span(sv, seg: np.ndarray, meta: dict) -> tuple[list | None, dict]:
    """滑窗平均：长段切成 2.0s/1.0s 的窗，逐窗算向量再平均（实测 d′ 7.6→8.4）。"""
    w, step = meta.get("w"), meta.get("step")
    if not w or not step or len(seg) <= w:
        v = embed_slice(sv, seg)
        return v, (meta | {"n_win": 1 if v is not None else 0})
    vs, pos = [], 0
    while pos + w <= len(seg):
        v = embed_slice(sv, seg[pos:pos + w])
        if v is not None:
            vs.append(v)
        pos += step
    tail = seg[pos:]
    if len(tail) >= int(0.8 * A.TARGET_SR) and len(tail) >= step // 2:
        v = embed_slice(sv, tail)
        if v is not None:
            vs.append(v)
    if not vs:
        return None, (meta | {"n_win": 0, "reason": "embed_failed"})
    m = np.mean(np.asarray(vs, dtype=float), axis=0)
    nrm = float(np.linalg.norm(m)) or 1.0
    return (m / nrm).tolist(), (meta | {"n_win": len(vs)})


# ── 说话人归簇 ──────────────────────────────────────────────────────────
#
# 老的"顺序单质心"在线归簇（来一行就和每个簇的质心比，够像就并、否则新开）有个
# 结构性缺陷：它把**先到的簇**当标准，后来的行只能往已有质心上靠。窄带电话音里同一
# 人的向量噪声很大——行 0 先开了一个簇并把它拉向自己，行 2 其实和行 1 更近、却被迫
# 去和"被拉偏的质心"比，于是同一个人被拆成好几簇（实测主说话人被拆成 匿名-1 + 匿名-2）。
#
# 解法分两步：
#   * assign：来一行先给个**临时**标签（和已有质心比，够像就并、否则新开），同时把
#     向量存下来。这一步只为"当场有个标签显示"，不追求正确。
#   * refit：每定稿一批后，对**所有已定稿行的向量**做一次层次聚类（scipy linkage +
#     fcluster，距离 = 1-余弦），再按"多数成员沿用旧标签、且每个旧标签最多被一个新簇
#     继承"的贪心规则把新簇映射回稳定的 匿名-N。这样后到的行可以**把先到的行重新归
#     组**，碎片被合并；已绑过名字的标签不会乱改（大簇优先继承旧标签，拆出的小簇才拿新号）。
class SpeakerClusterer:
    """本场会议的说话人归簇器：在线先分、定稿后聚合重聚，标签保持稳定。

    **阈值全部是余弦相似度**。老版本把 ``online_thr`` 说成余弦、``refit_cap`` 说成距离，
    两个数在一个类里混着用，结果 ``refit_cap=0.70`` 实际是"余弦 0.30 就合并"——
    比"不同人"的实测上限 0.37 还低。现在统一：``merge_cos`` 管合并、``new_cos`` 管开新簇、
    ``split_floor_cos``/``merge_cap_cos`` 管重聚的上下界，单位都是余弦。
    """

    def __init__(self, merge_cos: float = SPK_MERGE_COS,
                 new_cos: float = SPK_NEW_COS,
                 refit_min: int = 3,
                 refit_thr_cos: float = 0.45,
                 split_floor_cos: float = 0.80,
                 merge_cap_cos: float = SPK_MERGE_COS,
                 min_gap: float = 0.10) -> None:
        self.merge_cos = merge_cos          # 在线 assign：≥ 这个相似度就并进已有质心
        self.new_cos = new_cos              # < 这个相似度就**确定**开新簇
        self.refit_min = refit_min          # 至少这么多行才值得重聚（太少没意义）
        self.refit_thr_cos = refit_thr_cos  # 行数过少时的兜底（余弦）
        self.split_floor_cos = split_floor_cos  # 重聚下界：再也不切比这更相似的（防过碎）
        self.merge_cap_cos = merge_cap_cos  # 重聚上界：再也不并比这更不像的（防吞人）
        self.min_gap = min_gap              # 最大间隙需超过此值才视为真实说话人边界
        self.centroids: list = []        # [(标签, 质心向量)]，供在线 assign 用
        self.seq = 0                     # 已用掉的 匿名-N 序号（只增不减，保证稳定）
        self.emb: dict = {}              # 行号 -> 向量
        self.spk: dict = {}              # 行号 -> 当前标签
        self.inherited: dict = {}        # 行号 -> 标签（太短没算向量、沿用上一句的说话人）
        self.last_map: dict = {}         # 上次 refit 的 旧标签 -> 新标签
        self.log: list = []              # 实时轨迹：(事件, ...)，供测试观察

    # 余弦 ↔ 距离（scipy linkage 用距离）
    @staticmethod
    def _d2c(d: float) -> float:
        return 1.0 - d

    @staticmethod
    def _c2d(c: float) -> float:
        return 1.0 - c

    def _new_label(self) -> str:
        self.seq += 1
        return f"匿名-{self.seq}"

    def assign(self, idx: int, vec: list, track: bool = True) -> str:
        """给新定稿的一行先分个临时标签：和已有质心比，够像就并（EMA），否则新开。

        两个门槛分开用：``merge_cos`` 之上合并；``new_cos`` 之下新开；**中间那段是模糊带**，
        仍然合并（免得把同一人切碎），但记进 log 让上层知道这一段不可靠。

        ``track=False`` 用于**向量不可信**的行（音频太短、靠借静音凑出来的）：仍然给它
        一个标签（否则整段会议会出现大片空白），但不写进 ``self.emb``，因此**不参与重聚**——
        不可信的向量拿去重聚会把整个结构带偏。
        """
        best, bi = 0.0, -1
        for k, (_, cen) in enumerate(self.centroids):
            dot = sum(a * b for a, b in zip(cen, vec))
            if dot > best:
                best, bi = dot, k
        merged = bi >= 0 and best >= self.merge_cos
        if merged:
            sid, cen = self.centroids[bi]
            self.centroids[bi] = (sid, [0.8 * a + 0.2 * b for a, b in zip(cen, vec)])
        else:
            sid = self._new_label()
            self.centroids.append((sid, list(vec)))
        if track:
            self.emb[idx] = vec
            self.inherited.pop(idx, None)
        else:
            # 不参与重聚，但标签变化时要跟着走（见 refit 里的 last_map）
            self.inherited[idx] = sid
        self.spk[idx] = sid
        kind = "merge" if merged else "new"
        if merged and best < self.new_cos:
            kind = "merge-ambiguous"
        if not track:
            kind += "-untracked"
        self.log.append(("assign", idx, sid, round(best, 3), kind))
        return sid

    def inherit(self, idx: int, sid: str) -> str:
        """太短、算不出可信向量的行：**沿用上一句的说话人**，不拿它开新簇。

        老代码对每行都算向量，1.1 秒中位数的段噪声很大，于是"新声音"和"旧声音"的
        分数在同一区间里乱飘——这正是"新发言人被误认成旧的"和"一个人被拆成好几簇"
        同时出现的原因。短行不该参与身份判定，只该挂到当前说话人身上。
        """
        if not sid:
            return ""
        self.spk[idx] = sid
        self.inherited[idx] = sid
        self.log.append(("inherit", idx, sid, 0.0, "too_short"))
        return sid

    def active_label(self) -> str:
        """最近一次 assign 的标签，给 inherit() 用。"""
        for e in reversed(self.log):
            if e[0] == "assign":
                return e[2]
        return ""

    def _cluster_ids(self, embs: list) -> list:
        """对一批向量做层次聚类，返回每个向量的簇号（0..K-1）。失败时退化为全 0。"""
        n = len(embs)
        if n <= 1:
            return [0] * n
        E = np.asarray(embs, dtype=float)
        D = 1.0 - E @ E.T
        D = 0.5 * (D + D.T)
        np.fill_diagonal(D, 0.0)
        try:
            from scipy.cluster.hierarchy import linkage, fcluster
            condensed = D[np.triu_indices(n, k=1)]
            Z = linkage(condensed, method="average")
            thr = self._pick_threshold(Z)
            return fcluster(Z, t=thr, criterion="distance").tolist()
        except Exception:  # noqa: BLE001  scipy 不可用/数值异常：保守全归一簇
            return [0] * n

    def _pick_threshold(self, Z) -> float:
        """数据驱动选重聚阈值（**返回距离**，单位是 1-余弦）。

        在层次树最大间隙处切，并约束到
        ``[1 - split_floor_cos, 1 - merge_cap_cos]``：

        * 下限 ``1 - 0.80 = 0.20``：再也不把相似度高于 0.80 的两簇切开（防过碎）
        * 上限 ``1 - 0.55 = 0.45``：再也不把相似度低于 0.55 的两簇并起来（防吞人）

        老代码的上限是 0.70（余弦 0.30），低于"不同人"的实测上限 0.37 —— 会并错人。
        """
        lo = self._c2d(self.split_floor_cos)
        hi = self._c2d(self.merge_cap_cos)
        levels = Z[:, 2]
        if levels.size < 2:
            # 兜底：树太浅（只有 2 个点）时没有"间隙"可挑，用固定余弦阈值，并照样夹在
            # [lo, hi] 里。注意默认值 refit_thr_cos=0.45 正好等于 hi=1-0.55，所以这条
            # 分支和下面"间隙不够大"那条在默认配置下结果一样（都是 0.45）；这**不是**
            # 笔误：两条都是"拿不准时按最保守的边界切"，只是保守的含义不同
            # （这里是固定值，那里是上界）。
            return min(hi, max(lo, self._c2d(self.refit_thr_cos)))
        gaps = np.diff(levels)
        j = int(np.argmax(gaps))
        if gaps[j] < self.min_gap:
            # 最大间隙不够大 → 看不出说话人边界，于是取上界（最容易切开的那一侧），
            # 宁可多切几簇，也不要在这里就并人。
            return hi
        thr = 0.5 * (levels[j] + levels[j + 1])
        return float(min(max(thr, lo), hi))

    def refit(self) -> dict:
        """对全部已算过向量的行重新聚合聚类，返回 {行号: 新标签}（只含标签变了的行）。

        同时按新结构重建质心，让后续在线 assign 更准。``inherit()`` 挂上来的行不参与
        重聚（它们本来就没有可信向量），但会**跟着旧标签一起被映射到新标签**，
        免得重聚后留下一个指向已废弃标签的孤儿行。
        """
        idxs = sorted(self.emb.keys())
        old_spk = dict(self.spk)
        # 重聚前先记下旧标签 -> 新标签的映射基础（按簇大小决定谁继承旧标签）
        if not idxs:
            return {}
        if len(idxs) >= self.refit_min:
            embs = [self.emb[i] for i in idxs]
            labels = self._cluster_ids(embs)
            groups: dict = {}
            for i, lab in zip(idxs, labels):
                groups.setdefault(lab, []).append(i)
            taken: set = set()
            new_spk: dict = {}
            for lab in sorted(groups, key=lambda g: -len(groups[g])):
                members = groups[lab]
                counts: dict = {}
                for i in members:
                    old = old_spk.get(i, "")
                    if old:
                        counts[old] = counts.get(old, 0) + 1
                pick = None
                for cand, _cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                    if cand not in taken:
                        pick = cand
                        break
                if pick is None:
                    pick = self._new_label()
                taken.add(pick)
                for i in members:
                    new_spk[i] = pick
            self.spk = new_spk
            # 旧标签 -> 新标签：多数成员的去向（给 inherited 行用）
            self.last_map = {}
            for i in idxs:
                old, new = old_spk.get(i, ""), new_spk.get(i, "")
                if old and old != new and old not in self.last_map:
                    self.last_map[old] = new
            # 按新结构重建质心（每个簇的向量均值，L2 归一化）
            by_label: dict = {}
            for i, lab in new_spk.items():
                by_label.setdefault(lab, []).append(i)
            self.centroids = []
            for lab in sorted(by_label):
                em = np.mean([self.emb[i] for i in by_label[lab]], axis=0)
                nrm = float(np.linalg.norm(em)) or 1.0
                self.centroids.append((lab, (em / nrm).tolist()))
            self.log.append(("refit", len(idxs), len(by_label), sorted(by_label)))
        # inherited 行跟着映射走（即使这次没重聚也要跑，保证标签不悬空）
        changed: dict = {}
        for i, old in list(self.inherited.items()):
            new = self.last_map.get(old, old)
            if new != self.spk.get(i):
                self.spk[i] = new
                self.inherited[i] = new
            if old_spk.get(i) != self.spk.get(i):
                changed[i] = self.spk[i]
        for i in idxs:
            if old_spk.get(i) != self.spk.get(i) and i not in changed:
                changed[i] = self.spk[i]
        return changed

    def label_map(self) -> dict:
        """给外层的旧->新标签映射（重聚会把某些标签并掉/改名）。"""
        return dict(self.last_map)

    def reset(self) -> None:
        self.centroids = []
        self.seq = 0
        self.emb = {}
        self.spk = {}
        self.inherited = {}
        self.last_map = {}
        self.log = []


# ── text helpers ────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[\s，。、；：？！,.;:?!\"'“”‘’()（）\[\]【】—…·-]+")

_SENT_END = "。！？!?…"
_CLAUSE_END = "，、；：,;:"

# How long a silence still counts as "mid-utterance". Split by what the previous row
# ended with, because the gap alone cannot separate the two cases: a speaker taking a
# breath and a speaker handing over both pause for a few tenths of a second (the
# regression case below pauses 0.11 s between two different speakers).
#
# A trailing comma is decisive -- the punctuation model saw a clause boundary and the
# speaker was mid-thought, so a long-ish pause is still the same utterance. A bare
# character is ambiguous, so only a very short pause counts as a fragment split.
_GAP_CLAUSE_S = 0.85
_GAP_BARE_S = 0.45

# Matching thresholds. Deliberately floors rather than a best-effort pick: the first
# version of this code fell back to "whichever candidate overlaps most" with no
# minimum, and glued unrelated utterances together -- "明白已发货的物资" was merged
# into a preceding VR sentence as "明白已发货的物资好基础还有 VR 体验这块我们会把...".
# A wrong merge is far more damaging than a missed one, because it corrupts a row the
# user has already read.
_MIN_TEXT = 0.45     # character similarity required to consider a text match
# How close a new span's start must be to a row's start to count as the same
# utterance re-heard rather than a later fragment.
_ANCHOR_S = 0.35
# How far past a row's end a span may start and still be an append to it.
_APPEND_S = 0.40
# A replacement must cover at least this fraction of the row it replaces, so a
# truncated re-hearing cannot overwrite a complete row.
_MIN_COVERAGE = 0.60
# Spans this short carry no information and are dropped. The VAD emits them from
# pauses and breaths: a real run produced rows "好的，" (0.28 s) and "陈。" that then
# sat in the timeline as the last revision of an utterance, making latency look like
# 41 s when the text had actually appeared 1.5 s after it was spoken.
_MIN_SPAN_CHARS = 3
# Candidate search bounds: only recent rows, only nearby in time.
_CANDIDATE_ROWS = 12
_CANDIDATE_BACK_S = 30.0

# ── partial-overlap re-hearing (the long-monologue fix) ─────────────────
# Measured on a 116 s / 8-paragraph meeting at window_s=12 (scripts/probe_long_trace.py,
# scripts/span_union_bound.py): the union of *every* span the ASR produced already
# contains **99.8 %** of the ground-truth characters -- nothing was lost in the ring
# buffer or the model -- yet the finished timeline kept only 90.9 %, and 4 of 8
# paragraph heads existed in the raw spans but not in the rows. Of 184 span folds,
# **105 were discarded by the final ``else`` in ``update_row``**. Every one of them was
# a span that began *inside* an existing row:
#
#     row0 [0.25 - 3.23] 关于这个时间节点的问题我要再强调一遍。
#     span [2.15 - 3.27] 我要再强调一遍。      -> start_gap 1.90 > _ANCHOR_S
#     ...                                          end_gap  -0.04 (started before the row
#                                                  ended) -> not an append either -> ignored
#
# and the mirror image, the one that loses text for good:
#
#     row1 [3.53 - 9.89] 第三方测评人员...是上级
#     span [3.53 - 10.26] 第三方测评人员...是上级机关定的
#                          -> coverage 0.52 < _MIN_COVERAGE -> text kept, *but the row's
#                             start/end were overwritten anyway*, moving the anchor the
#                             next window must hit.
#
# Both are the same root cause: a re-hearing from a sliding window does not respect the
# row boundaries the previous window happened to pick. It has to be *clipped* against
# the audio already on the timeline, so that only genuinely uncovered audio becomes a
# new row.
_MIN_COVER_S = 0.20   # a trimmed span with less than this left is not worth folding; it is
                      # the tail of an utterance whose head a settled row already shows.
_COVER_TAIL_MARGIN_S = 2.0   # keep intervals that can still overlap a future window
_MAX_COVER_TAIL = 400        # hard cap so a long meeting cannot grow the mask unbounded
# _MIN_COVERAGE applies only to a span that does not *end* at least this much later than
# the row it would replace: such a span covers the row's tail and extends it, so
# replacing cannot lose the row's head. A span that stops short of the row's end still
# has to pass the coverage bar, because taking it would drop whatever the row holds
# beyond the span's end. (Measured: with a plain coverage bar, a 0.92 s row
# "陈主任我先把我。" was replaced by a span that ended 1.67 s earlier and the row's real
# content was gone -- that is the mechanism behind the "head of a paragraph is missing"
# report.)
_TAIL_EXTEND_S = 0.20


def is_complete(text: str) -> bool:
    """Whether a segment reads as a finished utterance.

    The ASR's punctuation model emits *clauses*, not sentences: a real capture came
    back as ``呀今，`` / ``天活儿太多来，`` / ``不及回宿舍做饭了咱，`` -- three rows for one
    utterance, and ``咱们`` split across two of them. Committing a clause trailing a
    comma as a finished utterance is not just ugly, it hands half a phrase to the
    retrieval layer, which is exactly the input it handles worst.
    """
    t = (text or "").strip()
    if not t:
        return False
    return t[-1] in _SENT_END


def seam_overlap(a: str, b: str, min_chars: int = 2) -> int:
    """Length of the longest suffix of ``a`` that is also a prefix of ``b``.

    Appending two windows' readings of the same speech duplicates whatever both
    contain: a real run produced
    ``...铺线这一块我们以机房为。我们以机房为重点展开...``, where "我们以机房" appears
    twice because the earlier window ended mid-phrase and the later one began at the
    same phrase. Trimming the overlap is the difference between a sentence and a
    stutter.
    """
    limit = min(len(a), len(b))
    for n in range(limit, min_chars - 1, -1):
        if a.endswith(b[:n]):
            return n
    return 0


def join_text(a: str, b: str) -> str:
    """Glue two ASR fragments, fixing the seam.

    When the VAD or the window boundary splits mid-word, the trailing punctuation of
    the first piece and the leading fragment of the second are both artefacts, so the
    mark is dropped and the pieces are welded directly: ``不及回宿舍做饭了咱，`` +
    ``们去伙房吃吧火，`` becomes ``不及回宿舍做饭了咱们去伙房吃吧火，``. Any text the two
    pieces share at the seam is kept once, not twice. Inserting a separator would
    preserve the very break we are trying to remove.

    Overlap has to be measured on text with the trailing punctuation removed:
    ``铺线这一块我们以机房为。`` vs ``我们以机房为重点展开`` only share "我们以机房" once the
    ``。`` is out of the way, and comparing raw strings reported overlap 0 and left
    the phrase duplicated.
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a:
        return b
    if not b:
        return a
    if a[-1] in _CLAUSE_END or a[-1] in _SENT_END:
        a = a[:-1]
    n = seam_overlap(a, b)
    if n:
        b = b[n:]
    return a + b



def norm_key(text: str) -> str:
    """Fuzzy identity for a sentence: punctuation- and space-insensitive.

    The ASR restores punctuation with a separate model, and the punctuation of a
    partial window routinely differs from the final one. Keying on raw text would
    turn "我这边放了。" and "我这边放了，" into two separate utterances even though
    the words are identical.
    """
    return _PUNCT.sub("", text or "").strip()


def _lcs_len(a: str, b: str) -> int:
    """Longest common subsequence length, iterative, O(len(a)*len(b))."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


def ratio(x: str, y: str) -> float:
    """Normalised longest-common-subsequence similarity in [0, 1].

    LCS rather than edit distance: far-field ASR routinely drops or invents a
    character mid-sentence, and LCS tolerates that far better than a positional
    comparison while still refusing to score two unrelated sentences highly.
    """
    if not x or not y:
        return 0.0
    if x in y or y in x:
        return 1.0
    return _lcs_len(x, y) / min(len(x), len(y))


# ── data model ──────────────────────────────────────────────────────────

@dataclass
class FeedSentence:
    """One utterance on the meeting timeline.

    ``open`` marks an utterance that is still inside the guard band, i.e. close
    enough to the end of the current window that a partial transcription could
    still grow. Only open rows may absorb a following clause; once a row has
    settled it is finished, and a span starting after it belongs to whoever spoke
    next. (An earlier version extended any short row, which let one speaker's reply
    get appended to the previous speaker's finished sentence.) An open row is
    rendered normally but updated in place, so growth never produces a second row.
    """

    idx: int                     # stable, monotonic, assigned on first sighting
    text: str
    start: float                 # seconds from the start of the whole meeting
    end: float
    first_seen_at: float         # wall-clock, for latency reporting
    committed_at: float | None = None
    # 声纹簇 id（CAM++ 每个句子给一个）。空字符串 = 这次没做说话人分离。
    # 它的用途只有一个但很关键：用户点一次"这句是某人说的"，同簇的其余句子一起署名。
    spk: str = ""
    revisions: int = 0           # how many times the transcription was corrected
    open: bool = False           # still inside the guard band -> may be extended
    # 声纹向量是从哪一段音频算出来的。行的时间戳在定稿过程中会移动，向量必须跟着重算，
    # 否则一个 1 秒的残句算出来的向量会一直挂在长成 8 秒的同一行上（老代码就是这样）。
    emb_start: float = 0.0
    emb_end: float = 0.0
    emb_weak: bool = False       # 音频不足/借用过多，这个向量不可信 -> 别拿它开新簇
    # 声纹向量本身。老代码没声明这个字段，全靠 getattr/setattr 动态挂，
    # 于是 `not row.emb` 会直接抛 AttributeError。
    emb: list | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {
            "idx": self.idx,
            "text": self.text,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "duration": round(self.duration, 2),
            "committed_at": self.committed_at,
            "revisions": self.revisions,
            "open": self.open,
        }

def parse_span_time(ts, text: str) -> dict:
    """Normalise a FunASR span into {text, start, end} in seconds.

    ``sentence_info[i]["timestamp"]`` is a **per-token** list of ``[start_ms,
    end_ms]`` pairs, so a sentence's span is ``ts[0][0] .. ts[-1][1]``. Taking
    ``timestamp[0]`` alone collapses every sentence to ~0.1 s; ``pipeline.py``
    carries a long comment about that exact bug, and this is the one place the
    correct handling lives.
    """
    if isinstance(ts, list) and ts and isinstance(ts[0], (list, tuple)):
        start_ms, end_ms = ts[0][0], ts[-1][1]
    elif isinstance(ts, list) and len(ts) == 2 and all(
            isinstance(v, (int, float)) for v in ts):
        start_ms, end_ms = ts
    else:
        start_ms = end_ms = 0
    return {"text": text, "start": start_ms / 1000.0, "end": end_ms / 1000.0}


def contains(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """How much of the shorter span lies inside the longer one, in [0, 1].

    Containment rather than intersection-over-union for the *time* signal. Two
    different utterances spoken back-to-back have a high IoU purely by being
    adjacent and long, whereas a re-transcription genuinely sits inside the span it
    re-covers. Measured on the case that broke the first end-to-end run: the VR
    sentence (39.24-45.0) and the unrelated reply (39.35-46.0) score IoU 0.86 --
    which is how they got fused -- but containment only 0.71, and the text signal
    separates them outright.
    """
    inter = min(a_end, b_end) - max(a_start, b_start)
    if inter <= 0:
        return 0.0
    shorter = min(a_end - a_start, b_end - b_start)
    return inter / shorter if shorter > 0 else 0.0


def tail_overlap(row_key: str, span_key: str, min_chars: int = 2) -> str:
    """The longest prefix of ``span_key`` that is also a suffix of ``row_key``.

    Used to decide, in *text* rather than in time, whether a re-heard span continues a
    row from where that row's words stop. Pure substring containment (``span_key in
    row_key``) is not enough: the spans that lose the head of a long utterance start
    *inside* the row, so the span's text is neither contained in the row nor containing
    it. What they do share is a seam --
    ``row "第三方测评人员九月五号左右就要进场这个时间不"`` and
    ``span "进场这个时间不是我们定的"`` share the seam ``"进场这个时间不"`` -- and the
    span is exactly the row's words continued. Anything shorter than ``min_chars`` is
    treated as coincidence, the same guard ``seam_overlap`` uses.
    """
    limit = min(len(row_key), len(span_key))
    for n in range(limit, min_chars - 1, -1):
        if row_key.endswith(span_key[:n]):
            return span_key[:n]
    return ""


def coverage_mask(sentences: list[FeedSentence], upto: float) -> list[tuple[float, float]]:
    """Time ranges already fixed on the timeline by **settled** rows, merged, oldest first.

    Settled only (``not s.open``): a row still inside the guard band is expected to grow
    into the next window's span, so treating it as covered would cut that span in half
    and turn one utterance into two rows. Measured at window_s=20 with open rows included:
    ``[14.31-18.50] ...调通而且要。`` appeared beside ``[17.99-23.18] 而且要明确派谁来...``.
    """
    iv = [(s.start, s.end) for s in sentences
          if not s.open and s.end > upto - _COVER_TAIL_MARGIN_S]
    if not iv:
        return []
    iv.sort()
    out: list[tuple[float, float]] = [iv[0]]
    for a, b in iv[1:]:
        if a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out[-_MAX_COVER_TAIL:]


def clip_span(span: dict, mask: list[tuple[float, float]]) -> list[dict]:
    """Trim the *leading* overlap a span has with audio already on the timeline.

    Returns ``[]`` when the span's words already start inside a settled row, else the one
    piece worth folding: the span's own start, but never later than the start of the
    first settled row that reaches into it.

    **Only the leading edge is trimmed, deliberately.** A re-heard span that begins
    inside a row carries new words *after* that row's end, and those words are found by
    ``update_row``'s seam rule, which needs the span's real start. Cutting such a span
    into two pieces was measured and made things worse: at window_s=20 it manufactured
    ``[14.31-18.50] 你们公司必须在这之前把软件部署调通而且要。`` beside a parallel
    ``[17.99-23.18] 而且要明确派谁来...`` -- the same audio shown twice, the first copy
    cut mid-word at a boundary nothing could repair. Trimming the leading edge keeps the
    span intact for the seam rule while still blocking the failure this exists for: a
    span belonging to the *previous* speaker's utterance reaching into an already
    settled row and being welded onto it.

    The mask must contain only **settled** rows (``tick`` builds it that way): an open
    row is still expected to grow into whatever the span holds, so clipping against it
    would split one utterance in two.
    """
    start, end = span["start"], span["end"]
    if end - start <= 0:
        return []
    limit = end
    for a, _b in mask:
        if a <= start:
            continue
        if a >= end:
            break
        limit = min(limit, a)
    if limit - start <= _MIN_COVER_S:
        # Nothing is left but the tail of an utterance whose head is already on screen;
        # the row that owns it extends itself from this very span.
        return []
    return [{"text": span["text"], "start": start, "end": limit}]


def match_score(s: FeedSentence, text: str, key: str, start: float, end: float) -> float:
    """How likely ``(text, start, end)`` is a re-transcription of ``s`` (0 if not).

    **Text is mandatory unless one string literally contains the other.** Temporal
    overlap cannot be trusted to identify a re-transcription, and this was measured
    rather than assumed: in the first end-to-end run the VR sentence (39.24-45.0)
    and the unrelated reply that followed (39.35-46.0) scored temporal containment
    **0.981** and IoU 0.86 -- they are near-identical spans holding completely
    different words. No purely temporal rule rejects that, which is precisely how
    "明白已发货的物资" ended up glued onto the VR sentence as
    "明白已发货的物资好基础还有 VR 体验这块...".

    Containment of one *string* in the other is still accepted on its own, and that
    is the case that matters in practice: a 20 s window truncates the head of a long
    utterance, so the same speech returns as a literal substring with a later start.
    """
    s_key = norm_key(s.text)
    if not s_key or not key:
        return 0.0
    contain_text = key in s_key or s_key in key
    sim = 1.0 if contain_text else ratio(key, s_key)
    if not contain_text and sim < _MIN_TEXT:
        return 0.0
    cont = contains(start, end, s.start, s.end)
    return 0.6 * cont + 0.4 * sim


def fold_span(
    sentences: list[FeedSentence],
    next_idx: int,
    span: dict,
    offset: float,
    now: float,
    window_end: float,
    guard_s: float = 1.5,
    force: bool = False,
) -> tuple[FeedSentence | None, bool, int]:
    """Fold one transcribed span into the running sentence list.

    Split out of the class so the segmentation rules can be tested without loading a
    model -- they are pure text/timing logic, and they are the part that was wrong.

    Three outcomes, in priority order:

    1. **Re-transcription of an existing utterance** (matched by ``match_score``).
       The text is *replaced* by whichever version is more complete, never
       concatenated: appending is what produced 22-second frankenstein rows.
    2. **Continuation of the previous clause-chain.** The punctuation model emits
       clauses, so a comma-terminated fragment followed closely by another fragment
       is one utterance -- but only while the previous row is still *open*. Once a
       row is in the guard band it has stopped changing, and a span that begins
       after it ends is a new speaker, not an extension.
    3. **A new utterance.**

    Returns ``(row, changed, next_idx)``; ``changed`` says whether the caller should
    re-emit the row (it already exists on screen and must be updated in place).
    """
    text = (span.get("text") or "").strip()
    key = norm_key(text)
    if len(key) < _MIN_SPAN_CHARS:
        return None, False, next_idx

    start = offset + span["start"]
    end = offset + span["end"]
    duration = max(0.0, end - start)
    # A span is *open* -- still changeable -- while it sits inside the guard band,
    # i.e. closer to the end of the audio we just decoded than `guard_s`. Once the
    # window has advanced past it by more than the guard, and it has not been
    # revised, it is final. This expression was inverted when first written, which
    # is not a subtle failure: every row was marked final the moment it appeared, so
    # no utterance could ever absorb a following clause and each one fragmented.
    open_new = force or (end > window_end - guard_s)

    # ── 1. same utterance seen again ────────────────────────────────────
    # Only the recent tail is considered, and not the whole history: an utterance
    # from a minute ago cannot reappear in a 20 s window, and letting it match was
    # how "明白已发货的物资" (at 52 s) got merged into an unrelated row at 40 s.
    window = [s for s in sentences[-_CANDIDATE_ROWS:] if start - s.start <= _CANDIDATE_BACK_S]
    best, best_score = None, 0.0
    for s in window:
        sc = match_score(s, text, key, start, end)
        if sc > best_score:
            best, best_score = s, sc
    if best is not None:
        changed = update_row(best, text, key, start, end, open_new, now)
        return best, changed, next_idx

    # ── 2. continuation of the previous utterance ───────────────────────
    # Two things must hold: the gap is too short to be a turn change, and the
    # previous row reads as unfinished. "Unfinished" is ``is_complete`` on the text,
    # *not* the open flag -- a row can be settled by the guard band and still be a
    # clause the speaker was mid-way through, because the punctuation model emits
    # clauses rather than sentences. Requiring ``open`` here fragmented every
    # utterance that happened to cross a window boundary; requiring nothing but a
    # short gap is what let one speaker's answer attach to the previous speaker's
    # finished question. Terminal punctuation is the discriminator: 。！？ ends a
    # thought, everything else does not.
    if sentences:
        prev = sentences[-1]
        gap = start - prev.end
        prev_text = prev.text.rstrip()
        # A trailing clause mark means the punctuation model cut mid-thought.
        loose_end = bool(prev_text) and prev_text[-1] in _CLAUSE_END
        limit = _GAP_CLAUSE_S if loose_end else _GAP_BARE_S
        # ``0 <= gap`` is not redundant: a span that *starts inside* the previous row
        # is not a continuation of it, and without this bound a negative gap passes
        # ``gap <= limit`` trivially. That is exactly how the unrelated 39.35-46.0
        # reply, overlapping the 39.24-45.0 sentence, was appended to it.
        #
        # A *negative* gap is now a different failure with its own fix: the
        # re-hearings that lose the head of a long utterance start inside the row they
        # came from, and joining them by time is what produced a 29-row timeline out of
        # 8 paragraphs. They are joined by *text seam* in ``update_row`` instead, which
        # is why this branch stays strictly "the span begins after the previous row
        # ended". Measured at window_s=12: span [9.21, 10.79] "是上级机关定的，" arrived
        # while the previous row already ended at 9.89, gap -0.68 passed the old bound,
        # and the two were welded into one row.
        if (
            0.0 <= gap <= limit
            and not is_complete(prev_text)
            and key not in norm_key(prev_text)
        ):
            prev.text = join_text(prev.text, text)
            prev.end = max(prev.end, end)
            prev.revisions += 1
            prev.open = open_new
            # 声纹只在原来是空的时候补上，绝不用空值覆盖已有的——
            # 覆盖会让"已经绑好名字的那一句"退回到未署名。
            if not prev.spk:
                prev.spk = span.get("spk") or ""
            return prev, True, next_idx

    # ── 3. genuinely new utterance ──────────────────────────────────────
    s = FeedSentence(idx=next_idx, text=text, start=start, end=end,
                     first_seen_at=now, committed_at=now, open=open_new,
                     spk=span.get("spk") or "")
    sentences.append(s)
    return s, True, next_idx + 1


def update_row(s: FeedSentence, text: str, key: str, start: float, end: float,
               open_new: bool, now: float) -> bool:
    """Fold a re-transcription into an existing row. Returns whether text changed.

    **Text and times are only ever updated together.** A row must describe one
    coherent stretch of audio, so a span may replace the row wholesale or extend it,
    but never edit the words while leaving the boundaries pointing at different
    audio. Two earlier versions of this function violated that, in opposite
    directions and both visibly:

    * ``s.end = max(s.end, end)`` with independent text appends gave rows spanning
      ``[0.25 - 31.25]`` -- start from one sentence, end from another.
    * an "ignore the times, keep the better text" branch gave
      ``[4.09 - 5.27] 今天的进度汇报例会先说时间节点。``, where 4.09 belonged to a
      different utterance than the words did.

    So there are exactly two outcomes, and a span that fits neither changes nothing:

    * **Replace** -- the span starts where the row starts (anchor within
      ``_ANCHOR_S``) *and* either covers at least ``_MIN_COVERAGE`` of the row's
      duration or runs past the row's end by ``_TAIL_EXTEND_S``. The coverage test is
      what stops a truncated re-hearing from destroying a complete row: if the
      utterance is longer than the window, the new span only holds its tail, and
      taking it would lose the beginning. The tail-extension escape is what lets a
      *growing* utterance be replaced by its own longer reading, even though that
      reading only covers part of the row's duration -- it cannot lose the head,
      because it contains the row's final words (measured: this is the difference
      between a 20 s paragraph ending ``...是上级机关`` and ending ``...是上级机关定的``).
    * **Extend by seam** -- the span's words begin where the row's words stop
      (``tail_overlap``). This is the case the old code had no branch for at all, and
      the one that drops text: the span starts inside the row in *time* while being
      brand-new words in *text*, so an anchor test fails and an append test fails.
      Only the text seam is evidence here, so the seam is the gate.
    * **Append** -- the span begins just after the row ends and brings words the row
      does not already have.

    **Times are never moved without the text moving.** The old code applied
    ``s.start = start`` whenever the coverage test passed, even on the branch that
    explicitly declines to take the new text. Measured consequence at window_s=12: row 0
    was left as ``关于这个时间节点的问题我要再强调一遍。`` while its start walked
    0.25 -> 2.15 s, and row 1's start walked 3.53 -> 9.21 s in one step -- after which
    nothing re-heard from the head of those utterances could meet the ``_ANCHOR_S``
    test again, so their beginnings became separate rows. Anchors may only move
    *earlier*: a re-hearing that starts sooner is the same utterance heard from further
    back, and keeping the earliest start keeps the anchor reachable.
    """
    s_key = norm_key(s.text)
    if not s_key:
        return False
    start_gap = abs(start - s.start)
    end_gap = start - s.end
    coverage = (end - start) / s.duration if s.duration > 0 else 1.0
    seam = tail_overlap(s_key, key) if key not in s_key else ""

    if start_gap <= _ANCHOR_S and (
            coverage >= _MIN_COVERAGE or end > s.end + _TAIL_EXTEND_S):
        # Never let a shorter re-hearing shrink a longer row: the punctuation model
        # drops trailing clauses between windows ("...派谁来？" comes back as
        # "...你们公司"), and taking that would lose words we already reported.
        if len(key) >= len(s_key) or coverage >= 0.95:
            if norm_key(text) != s_key:
                s.text = text
                s.start = min(s.start, start)
            # The text did not change, so the row still describes exactly the audio it
            # described before: leave both boundaries alone. Moving ``s.start`` here
            # (the old behaviour) is what moved the anchor out from under later
            # windows and fragmented the utterance.
            s.end = max(s.end, end)
    elif seam:
        # Same utterance, re-heard from inside the row and continuing past it. Keep the
        # row's head, splice the span at the seam so the shared characters appear once,
        # and extend the end -- but never pull the start forward.
        s.text = join_text(s.text, seam + key[len(seam):])
        s.end = max(s.end, end)
    elif 0.0 <= end_gap <= _APPEND_S and key not in s_key:
        s.text = join_text(s.text, text)
        s.end = end
    else:
        # Neither a replacement nor a continuation. Ignore the span rather than let
        # it half-edit the row; the next window will carry the full utterance and
        # match cleanly.
        #
        # The times are deliberately *not* touched here -- see the function docstring.
        s.open = open_new
        return False

    s.open = open_new
    new_key = norm_key(s.text)
    changed = new_key != s_key
    if changed:
        s.revisions += 1
    if s.committed_at is None:
        s.committed_at = now
    return changed


def _find_sv_model() -> str:
    """找到 CAM++ 声纹模型。

    优先用**本地已经下载好的**那份：ModelScope 的默认缓存是 ``~/.cache/modelscope``，
    而本机把 ASR 模型都放在 ``E:/models/gguf-asr`` 下（含它自己的 .cache）。
    只按名字 ``"cam++"`` 去要的话，会在没设 MODELSCOPE_CACHE 的机器上重新联网下载一遍——
    或者更糟：在没有网的环境里直接失败。所以这里按候选路径找，找到就用**路径**加载，
    找不到才退回按名字要（那时才需要联网）。
    """
    import glob
    cands = []
    env = os.environ.get("MODELSCOPE_CACHE")
    if env:
        cands.append(Path(env))
    cands.append(Path.home() / ".cache" / "modelscope" / "hub")
    # 本机 ASR 模型的集散地：带上它自己的 .cache 一起找
    for root in (r"E:\models\gguf-asr", str(Path.home() / "models")):
        cands.append(Path(root) / ".cache" / "modelscope")
        cands.append(Path(root))
    pats = ["models/iic--speech_campplus_sv_zh-cn_16k-common/snapshots/*",
            "hub/models/iic--speech_campplus_sv_zh-cn_16k-common/snapshots/*",
            "models/iic/speech_campplus_sv_zh-cn_16k-common",
            "**/speech_campplus_sv_zh-cn_16k-common", "**/campplus*"]
    for base in cands:
        if not base.exists():
            continue
        for pat in pats:
            for hit in sorted(glob.glob(str(base / pat), recursive=("**" in pat))):
                h = Path(hit)
                if h.is_dir() and any(h.glob("*.bin")):
                    return str(h)
    return "cam++"


class StreamingASR:
    """Sliding-window paraformer + VAD + punctuation.

    Not thread-safe by design: the caller feeds it audio and takes results. The
    TCP receiver wraps it in a lock, because FunASR model objects are not safe to
    call concurrently.
    """

    def __init__(
        self,
        window_s: float = 20.0,
        guard_s: float = 1.5,
        min_window_s: float = 2.0,
        model: str = "paraformer-zh",
        model_revision: str = "v2.0.4",
        device: str | None = None,
        hotwords: list[str] | None = None,
        want_spk: bool = True,
    ) -> None:
        self.window_s = window_s
        self.guard_s = guard_s
        self.min_window_s = min_window_s
        self.device = device
        self.hotwords = hotwords or []

        self._model = None
        # 说话人分离（CAM++）。想要，但不是必须——见 load()。
        self.want_spk = want_spk
        self._sv = None                      # CAM++ 声纹向量模型
        self._spk = SpeakerClusterer()       # 在线分配 + 凝聚重聚类（与 StreamASR 同一路径）
        self.spk_ready = False
        self.spk_error = ""
        self._lock = threading.RLock()

        # rolling audio, capped at the window length so long meetings do not grow
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start = 0.0          # absolute time of _buf[0]
        self._total = 0.0              # absolute time of the newest sample

        self._sentences: list[FeedSentence] = []
        self._closed: list[FeedSentence] = []   # no longer re-transcribed
        self._next_idx = 0
        self._stats = {
            "ticks": 0,
            "asr_calls": 0,
            "asr_seconds": 0.0,
            "audio_seconds": 0.0,
        }

    # ── model ───────────────────────────────────────────────────────────

    def load(self) -> float:
        """Load the FunASR models; returns seconds spent. Idempotent.

        The speaker model (CAM++) is what makes one binding cover a whole voice -- every
        sentence from the same speaker comes back with the same ``spk`` cluster id, so
        "这段是某人说的" only has to be said once. Without it every sentence is anonymous and
        the user would have to name each one, which is the thing this product exists to avoid.

        It is requested but **not required**: if the model is not on this machine (FunASR
        resolves ``cam++`` through ModelScope, which does not work offline) the load would
        raise and take the whole transcriber down with it. A meeting assistant that refuses
        to transcribe because a *diarisation* model is missing has its priorities backwards,
        so the speaker model failing is reported and the rest carries on.
        """
        with self._lock:
            if self._model is not None:
                return 0.0
            from funasr import AutoModel

            t0 = time.time()
            base: dict = {
                "model": "paraformer-zh",
                "model_revision": "v2.0.4",
                "vad_model": "fsmn-vad",
                "vad_model_revision": "v2.0.4",
                "punc_model": "ct-punc-c",
                "punc_model_revision": "v2.0.4",
                "disable_update": True,
            }
            if self.device:
                base["device"] = self.device
            kw = dict(base)
            if self.want_spk:
                kw["spk_model"] = "cam++"
                kw["spk_model_revision"] = "v2.0.2"
            try:
                self._model = AutoModel(**kw)
                self.spk_ready = bool(self.want_spk)
            except Exception as e:  # noqa: BLE001
                if not self.want_spk:
                    raise
                self.spk_error = f"{type(e).__name__}: {e}"
                print(f"[asr] 说话人模型（cam++）加载失败，改为不分离声纹：{self.spk_error}",
                      file=sys.stderr, flush=True)
                base["disable_update"] = True
                self._model = AutoModel(**base)
                self.spk_ready = False
            if self.want_spk:
                # 向量模型要单独加载：ASR 管线只给"第几句话是谁"的簇号，
                # 而跨会议认人需要**向量**本身（簇号换个会就重编号了）。
                try:
                    self._sv = AutoModel(model=_find_sv_model(), disable_update=True,
                                         device=self.device or "cuda:0")
                    self.spk_ready = True
                except Exception as e:  # noqa: BLE001
                    self.spk_error = f"{type(e).__name__}: {e}"
                    self._sv = None
                    print(f"[asr] 声纹向量模型（cam++）加载失败：{self.spk_error}",
                          file=sys.stderr, flush=True)
            return time.time() - t0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    # ── audio intake ────────────────────────────────────────────────────

    def push(self, chunk: np.ndarray, sr: int = A.TARGET_SR) -> None:
        """Append float32 mono audio at ``sr``."""
        if chunk is None or len(chunk) == 0:
            return
        if chunk.dtype != np.float32:
            chunk = chunk.astype(np.float32)
        if sr != A.TARGET_SR:
            chunk = A.resample(chunk, sr, A.TARGET_SR)
        with self._lock:
            self._buf = np.concatenate([self._buf, chunk])
            self._total += len(chunk) / float(A.TARGET_SR)
            self._stats["audio_seconds"] += len(chunk) / float(A.TARGET_SR)
            keep = A.samples(self.window_s)
            if len(self._buf) > keep:
                drop = len(self._buf) - keep
                self._buf = self._buf[drop:]
                self._buf_start += drop / float(A.TARGET_SR)

    # ── the loop ────────────────────────────────────────────────────────

    def tick(self, force: bool = False) -> list[FeedSentence]:
        """Transcribe the current window and return utterances that just settled.

        ``force`` bypasses the guard band and commits everything in the window --
        used when the stream ends, where waiting for more audio is pointless.
        """
        with self._lock:
            if self._model is None:
                raise RuntimeError("call load() before tick()")
            if len(self._buf) < A.samples(self.min_window_s):
                return []

            audio = self._buf.copy()
            offset = self._buf_start
            window_end = offset + len(audio) / float(A.TARGET_SR)

            t0 = time.time()
            res = self._model.generate(
                input=audio,
                cache={},
                batch_size_s=300,
                sentence_timestamp=True,
                hotword=" ".join(self.hotwords) if self.hotwords else None,
            )
            self._stats["ticks"] += 1
            self._stats["asr_calls"] += 1
            self._stats["asr_seconds"] += time.time() - t0

            now = time.time()
            fresh: list[FeedSentence] = []
            # Every span in this window starts where the *window* starts, not where the
            # sentence starts: after the ring buffer begins sliding, a 14 s monologue
            # inside a 12 s window comes back as a span whose text starts partway into
            # the utterance, over and over, each time from a different point. Clipping
            # each span against the audio already on the timeline turns those overlapping
            # readings into (a) pure re-hearings, which match a row and are dropped, and
            # (b) genuinely new stretches, which become rows. Measured before this:
            # 105 of 184 span folds (57 %) died in ``update_row``'s final ``else``.
            mask = coverage_mask(self._sentences, window_end)
            clipped: list[dict] = []
            for span in self._spans(res):
                if not span["text"]:
                    continue
                for piece in clip_span(span, mask):
                    piece["text"] = span["text"]
                    clipped.append(piece)
            # Sorted by start so the head of an utterance is folded before its tail and
            # the seam logic sees the row in the order the words were spoken.
            clipped.sort(key=lambda p: p["start"])
            for span in clipped:
                # Spans inside the guard band are *not* dropped. Dropping them was
                # the first implementation, and it meant the screen stayed empty for
                # as long as someone kept talking: a 22-second utterance only became
                # visible once the whole thing had settled, which is useless for a
                # live assistant. They are folded in as open rows -- shown
                # immediately, then finalised in place by a later window. Because
                # updates are matched back to the same row, the provisional text is
                # never duplicated.
                row, emit, self._next_idx = fold_span(
                    self._sentences, self._next_idx, span, offset, now,
                    window_end, self.guard_s, force)
                if row is not None and emit:
                    self._label_row(row)
                    fresh.append(row)
            self._refit_spk(fresh)
            return fresh

    def _label_row(self, row) -> None:
        """给一行算声纹。

        老版本在**任何** emit 时都算一次（包括还在 guard band 里、随时会被改写的
        open 行），而且算过就不再重算。实测后果：68% 的向量尾巴还没进缓冲
        （算的是半句话），行的时间戳后来长到几秒也不管。现在的规则：

        * **只在定稿后算**（``not row.open``）——与本地路径 ``StreamASR`` 一致，
          定稿行的尾巴一定已经在缓冲里，尾部截断问题自然消失；
        * 行的 span 移动超过 0.25 s 就**重算**（``emb_start/emb_end`` 记着上次算的是哪段）；
        * 算不出可信向量（太短/借音太多）时**沿用当前说话人**，不拿它开新簇。
        """
        if self._sv is None or row.open:
            return
        moved = (not row.emb) or abs(row.start - row.emb_start) > 0.25 \
            or abs(row.end - row.emb_end) > 0.25
        if not moved:
            return
        vec, sid, meta = self._embed_and_label(row.start, row.end, row.idx, row)
        row.emb_start, row.emb_end = row.start, row.end
        if vec is not None:
            row.emb = vec
        if sid:
            row.spk = sid
            row.emb_weak = False
            return
        # 没拿到可信声纹（音频太短 / 借用过多 / 切不出来）：**每一行都必须有个说话人**。
        # 优先沿用当前说话人；一句都还没定过时先开个头。留下 spk="" 是错的——
        # 实测那样 call1 会有 20/31 行（60 秒语音）是一片没有声纹的空白，
        # 界面上一半的发言归属为空，用户看到的比"认错人"还糟。
        row.emb_weak = True
        cur = self._spk.active_label()
        if cur:
            row.spk = self._spk.inherit(row.idx, cur)
        elif vec is not None:
            row.spk = self._spk.assign(row.idx, vec, track=False)

    def _neighbour_bounds(self, row) -> tuple[float, float]:
        """这一行左右能"借"到哪儿。跨过邻近的发言就会把别人的声音算进这个向量。"""
        lo = hi = None
        for s in self._sentences:
            if s is row or s.idx == row.idx:
                continue
            if s.end <= row.start and (lo is None or s.end > lo):
                lo = s.end
            if s.start >= row.end and (hi is None or s.start < hi):
                hi = s.start
        return lo, hi

    def _embed_and_label(self, start: float, end: float, idx: int,
                         row=None):
        """切音频 → 声纹向量 → 归簇。返回 ``(向量, 声纹id, meta)``。

        三条与老版本的关键差别：

        1. 音频从**滚动缓冲**切，但被滚出去的部分不再静默钳位（老代码的
           ``max(0, ...)`` 会让切片从缓冲区起点开始，混进别人的话）；
           ``slice_span`` 改成"头部丢了就取尾部"，尾部也丢了就放弃。
        2. **不足 2 秒就往两边借静音凑够**（只借到邻近行为止）。老代码直接拿 1.1 秒
           中位数的 ASR 跨度算向量，实测同人/异人分布几乎重叠。
        3. 长段用 2.0s/1.0s 滑窗平均（实测 d′ 7.6 → 8.4）。

        ``meta["reason"]`` 非空表示这次没能给出可信向量，调用方应让该行**沿用**
        当前说话人，而不是拿它去开新簇。
        """
        with self._lock:
            buf = self._buf
            base = self._buf_start
        if self._sv is None or buf is None or len(buf) == 0:
            return None, "", {"reason": "no_model"}
        lo, hi = self._neighbour_bounds(row) if row is not None else (None, None)
        seg, meta = slice_span(buf, base, start, end, lo_bound=lo, hi_bound=hi)
        if seg is None:
            return None, "", meta
        vec, meta = embed_span(self._sv, seg, meta)
        if vec is None:
            return None, "", meta
        # 借得太多（说明这一行本来就短、靠邻居撑起来的）→ 标记不可信
        meta["weak"] = bool(meta.get("borrowed_s", 0.0) > 1.2
                            or meta.get("used_s", 0.0) < 1.8)
        if meta["weak"]:
            # 不可信就让调用方沿用当前说话人，不参与归簇
            return vec, "", meta
        sid = self._spk.assign(idx, vec)
        return vec, sid, meta

    def _refit_spk(self, fresh: list) -> None:
        """重聚类：把在线吸收后混进来的新声纹拆出来，回写到已定稿行并补进 fresh。

        与 StreamASR._refit_spk 同构。refit 只重算被 assign() 过的行，
        返回 {行号: 新声纹id}（仅发生变化的行）；按行号回写 spk，
        并把受影响的行补进 fresh，让上层广播 spk 变更。
        """
        changed = self._spk.refit()
        if not changed:
            return
        by_idx = {s.idx: s for s in self._sentences}
        for idx, new_spk in changed.items():
            s = by_idx.get(idx)
            if s is None:
                continue
            s.spk = new_spk
            if s not in fresh:
                fresh.append(s)

    @staticmethod
    def _spans(res: list) -> list[dict]:
        """Normalise a FunASR result into {text, start, end} with seconds.

        ``sentence_info`` is preferred because it carries the speaker field that
        CAM++ fills in later; ``text``/``timestamp`` is the fallback for model
        configurations that do not emit sentence_info at all.
        """
        out: list[dict] = []
        if not res:
            return out
        r = res[0] or {}

        si = r.get("sentence_info")
        if isinstance(si, list) and si:
            for s in si:
                text = (s.get("text") or "").strip()
                if not text:
                    continue
                span = parse_span_time(s.get("timestamp"), text)
                # CAM++ 把说话人填在 sentence_info 的 spk 里（没开就是 None）。
                # 归一成字符串，免得下游一会儿 None 一会儿 int。
                raw = s.get("spk")
                span["spk"] = "" if raw is None else str(raw)
                out.append(span)
            return out

        text = (r.get("text") or "").strip()
        if text:
            out.append(parse_span_time(r.get("timestamp"), text))
        return out

    # ── accessors ───────────────────────────────────────────────────────

    def flush(self) -> list[FeedSentence]:
        """Finalise everything still inside the guard band (end of stream).

        Open flags are cleared as well: once the audio has stopped there is no more
        context coming, so a row that was waiting to be extended is simply the final
        version of that utterance.
        """
        with self._lock:
            tail = self.tick(force=True)
            for s in self._sentences:
                s.open = False
            # 上面 tick 时这些行还在 guard band 里，_label_row 会跳过它们。
            # 现在 open 清了，补一次定稿标注，否则最后几句永远没有声纹。
            extra = []
            for s in self._sentences:
                self._label_row(s)
                if s not in tail and s not in extra:
                    extra.append(s)
            self._refit_spk(extra)
            return tail + [s for s in extra if s not in tail]

    def finalize(self) -> list[FeedSentence]:
        """Clear every open flag -- call after ``flush`` when the stream ends."""
        with self._lock:
            for s in self._sentences:
                s.open = False
            return self.sentences

    @property
    def sentences(self) -> list[FeedSentence]:
        with self._lock:
            return sorted(self._sentences, key=lambda s: (s.start, s.idx))

    @property
    def elapsed(self) -> float:
        """Seconds of audio ingested -- the meeting clock."""
        return self._total

    def transcript(self) -> str:
        return "\n".join(s.text for s in self.sentences)

    def stats(self) -> dict:
        with self._lock:
            st = dict(self._stats)
        st["sentences"] = len(self._sentences)
        st["elapsed_s"] = round(self._total, 1)
        st["realtime_factor"] = (
            round(st["asr_seconds"] / st["audio_seconds"], 4)
            if st["audio_seconds"] else None
        )
        return st

    # ── persistence ─────────────────────────────────────────────────────

    def save_segments(self, path) -> int:
        """Write the transcript as JSON segments for the existing pipeline."""
        import json
        from pathlib import Path

        segs = [
            {"start_s": round(s.start, 3), "end_s": round(s.end, 3),
             "spk": "?", "text": s.text}
            for s in self.sentences
        ]
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"segments": segs, "source": "phone-mic"},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        return len(segs)
