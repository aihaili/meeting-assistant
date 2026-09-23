"""热词表：参会名单 + 线索 → 文本级 ASR 纠错（P1 识别质量，精度优先）。

## 这是什么

FunASR 的标准 `paraformer-zh` 没有模型级热词（只有 contextual_paraformer /
fun_asr_nano 才有）。本模块做**文本级后处理纠错**：对已解码文本，把"听岔的专名/术语"
拉回热词表里的正确写法。数据源随会议生长（不碰静态 vault）：

* **参会名单** = `session.participants`（姓名 + 单位）；
* **领域术语** = `session.clues` 里 kind 为 person/org/term 的线索（锚点词）；
* 可选 **显式错词表** = `asr.hotwords_dir` 下的 `*.txt`（`错词 => 正词` 行，精确替换）。

## 为什么"同长 + 汉明距离"，而不是照搬 FunASR 的模糊匹配

FunASR 的 `PostprocessHotwordMatcher._apply_fuzzy` 会拿「长度 ±1」的窗口去比对目标，
于是**目标能匹配到比它更长的片段，"纠错"实际是删掉多出来的字**：

    林浩然做 → 林浩然（删了"做"）
    机房铺线…孙磊负 → 机机房铺线…孙磊责（既插"机"又删"负"）

这正是用户抱怨的"一段话里到处是关键词"——真实字符被吃掉、被插。

真实的 ASR 专名错误几乎都是**同长同音**：听成 N 个字，写错其中 1-2 个同音字
（如 虽广→推广、六自由渡→六自由度）。所以本表只用一条高精度规则做模糊纠错，三者叠加：

1. **同长**：片段与目标字数相同——直接排除增字/删字；
2. **汉明距离 ≤ 上限**：逐字比对，差异位不超过上限（短词 1 个、长词 2 个），
   排除"整体错位"的滑窗假匹配（那种片段逐字几乎全不同）；
3. **拼音相似度 ≥ 阈值**（rapidfuzz，默认 0.85）：确保差异位是同音/近音，而非随机字。

三者叠加后，实测把上面那类删字/插字假匹配全部挡掉，同时保留真纠错。
显式错词表走精确子串替换，不受这条规则影响。

## 线程安全

`sync_from_session` 在发布线程（on_segment）里跑，`apply` 在 ASR 线程里跑。
`_buckets`/`_explicit` 每次 sync 整体重建、从不原地改，引用替换在 GIL 下是原子的，
所以读写无需额外锁。

## 性能

模糊纠错按「目标长度分桶 + 等长滑窗」，每个窗口只算一次拼音再比同桶目标，
复杂度约 O(文本长 × 同长目标数)。几十词表 + 百字文本是毫秒级
（stream_asr 还会按文本缓存 apply 结果，见其 `_apply_hotwords`）。
"""

from __future__ import annotations

import re
from pathlib import Path

# 显式错词表分隔符（`错词 => 正词`）。
_EXPLICIT_SEPARATORS = ("=>", "->", "→")
# 术语/人名长度窗口：太短（1 字）易误伤，太长（>24）多半是整句而非专名。
_MIN_TERM = 2
_MAX_TERM = 24
# 线索里算"专名/术语"的 kind（requirement 等动作型不进表）。
_ENTITY_KINDS = ("person", "org", "term")
# 窗口里至少含一个"词元"（汉字/英文串/数字串）才参与匹配，过滤纯标点。
_TOKEN = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z]+|[0-9]+")

# 懒加载缓存（避免每次 sync/apply 重复 import；依赖缺失时降级为"只做显式替换"）。
_lazy_pinyin = None
_pinyin_style = None
_rapidfuzz_fuzz = None


def _require_pypinyin():
    """返回 (lazy_pinyin, Style)；缺失时抛 ImportError 由调用方降级。"""
    global _lazy_pinyin, _pinyin_style
    if _lazy_pinyin is None:
        from pypinyin import Style, lazy_pinyin

        _lazy_pinyin = lazy_pinyin
        _pinyin_style = Style
    return _lazy_pinyin, _pinyin_style


def _require_rapidfuzz():
    """返回 rapidfuzz.fuzz；缺失时抛 ImportError 由调用方降级。"""
    global _rapidfuzz_fuzz
    if _rapidfuzz_fuzz is None:
        from rapidfuzz import fuzz

        _rapidfuzz_fuzz = fuzz
    return _rapidfuzz_fuzz


def _to_pinyin_key(text: str) -> str:
    """整段拼成小写拼音串（NORMAL 无声调），作为相似度比较的键。"""
    lazy_pinyin, style = _require_pypinyin()
    return "".join(lazy_pinyin(text, style=style.NORMAL, errors="ignore")).lower()


class HotwordTable:
    """参会名单 + 线索 → 文本级纠错表。

    用法（见 meeting/server.py 装配、phone_mic/stream_asr.py 应用）::

        table = HotwordTable(enabled=True, explicit_dir="data/hotwords")
        table.sync_from_session(session)   # 每次 on_segment 新增参会人/线索后刷新
        fixed = table.apply(已解码文本)     # 在 _open_utterance / _close_utterance 各跑一次
    """

    def __init__(self, enabled: bool = True, threshold: float = 0.85,
                 explicit_dir: str = "", enable_fuzzy: bool = True) -> None:
        self.enabled = bool(enabled)
        self.threshold = float(threshold)
        self.explicit_dir = str(explicit_dir or "")
        self.enable_fuzzy = bool(enable_fuzzy)
        self._targets: set[str] = set()
        self._explicit: dict[str, str] = {}
        # 长度 -> [(目标, 目标拼音键)]；每次 sync 整体重建，apply 只读。
        self._buckets: dict[int, list[tuple[str, str]]] = {}
        # 指纹：内容没变就跳过重建（避免每次 on_segment 重算拼音）。
        self._key = None

    # -- 构建 ---------------------------------------------------------------

    def sync_from_session(self, session) -> None:
        """从当前会话收集纠错目标（参会人姓名/单位 + person/org/term 线索），重建表。

        内容没变（指纹相同）就跳过重建。发布线程调用。
        """
        if not self.enabled:
            self._targets = set()
            self._explicit = {}
            self._buckets = {}
            self._key = None
            return

        targets = set()
        for p in getattr(session, "participants", []) or []:
            name = (getattr(p, "name", "") or "").strip()
            if _MIN_TERM <= len(name) <= _MAX_TERM:
                targets.add(name)
            org = (getattr(p, "org", "") or "").strip()
            if _MIN_TERM <= len(org) <= _MAX_TERM:
                targets.add(org)
        for c in getattr(session, "clues", []) or []:
            kind = getattr(c, "kind", "") or ""
            if kind not in _ENTITY_KINDS:
                continue
            t = (getattr(c, "anchor", "") or getattr(c, "text", "") or "").strip()
            if _MIN_TERM <= len(t) <= _MAX_TERM:
                targets.add(t)

        explicit = self._load_explicit()
        key = (frozenset(targets), tuple(sorted(explicit.items())),
               self.threshold, self.enable_fuzzy)
        if key == self._key:
            return  # 内容没变，不重建
        self._key = key
        self._targets = targets
        self._explicit = explicit

        buckets: dict[int, list[tuple[str, str]]] = {}
        if self.enable_fuzzy and targets:
            try:
                _require_pypinyin()
                _require_rapidfuzz()
            except ImportError:
                buckets = {}  # 依赖缺失：降级为"只做显式替换"，不让会议起不来
            else:
                for t in sorted(targets):
                    buckets.setdefault(len(t), []).append((t, _to_pinyin_key(t)))
        self._buckets = buckets

    def _load_explicit(self) -> dict[str, str]:
        """读显式错词表目录下的 `*.txt`（`错词 => 正词` 行）。缺失/空目录返回空表。"""
        out: dict[str, str] = {}
        if not self.explicit_dir:
            return out
        d = Path(self.explicit_dir)
        if not d.is_dir():
            return out
        for f in sorted(d.glob("*.txt")):
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    for sep in _EXPLICIT_SEPARATORS:
                        if sep in line:
                            wrong, right = line.split(sep, 1)
                            wrong, right = wrong.strip(), right.strip()
                            if wrong and right and wrong != right:
                                out[wrong] = right
                            break
            except Exception:
                continue
        return out

    # -- 应用 ---------------------------------------------------------------

    def apply(self, text: str) -> str:
        """对已解码文本做纠错；任何异常都原样返回（纠错永远不能弄坏已识别文本）。"""
        if not self.enabled or not text:
            return text
        if not self._explicit and not self._buckets:
            return text
        try:
            updated = self._apply_explicit(text)
            if self._buckets:
                updated = self._apply_fuzzy(updated)
            return updated
        except Exception:
            return text

    def _apply_explicit(self, text: str) -> str:
        """显式错词表：精确子串替换，长词优先（避免短词先吃掉长词的一部分）。"""
        if not self._explicit:
            return text
        updated = text
        for wrong in sorted(self._explicit, key=len, reverse=True):
            right = self._explicit[wrong]
            start = 0
            while True:
                idx = updated.find(wrong, start)
                if idx < 0:
                    break
                end = idx + len(wrong)
                updated = updated[:idx] + right + updated[end:]
                start = idx + len(right)
        return updated

    def _apply_fuzzy(self, text: str) -> str:
        """同长 + 汉明距离 + 拼音相似度的高精度模糊纠错（见模块 docstring）。"""
        buckets = self._buckets
        if not buckets:
            return text
        fuzz = _require_rapidfuzz()
        n = len(text)
        candidates: list[tuple[float, int, int, str]] = []
        for length, items in buckets.items():
            if length < 2 or n < length:
                continue
            max_ham = 1 if length <= 3 else 2  # 短词更严（1 字），长词容忍 2 字
            for start in range(0, n - length + 1):
                seg = text[start:start + length]
                if not _TOKEN.search(seg):
                    continue
                seg_py = _to_pinyin_key(seg)
                for target, target_py in items:
                    if seg == target:
                        continue  # 已经正确
                    # 汉明距离：逐字数差异位，超上限（多为整体错位滑窗）直接跳过。
                    ham = 0
                    for a, b in zip(seg, target):
                        if a != b:
                            ham += 1
                            if ham > max_ham:
                                break
                    if ham > max_ham:
                        continue
                    score = fuzz.ratio(seg_py, target_py) / 100.0
                    if score >= self.threshold:
                        candidates.append((score, start, start + length, target))
        if not candidates:
            return text
        # 非重叠选取：高分优先，同分取长（与 FunASR 一致的取舍）。
        candidates.sort(key=lambda c: (c[0], c[2] - c[1]), reverse=True)
        selected: list[tuple[int, int, str]] = []
        occupied: list[tuple[int, int]] = []
        for score, st, en, target in candidates:
            if any(not (en <= o0 or st >= o1) for o0, o1 in occupied):
                continue
            selected.append((st, en, target))
            occupied.append((st, en))
        updated = text
        for st, en, target in sorted(selected, key=lambda x: x[0], reverse=True):
            updated = updated[:st] + target + updated[en:]
        return updated

    # -- 观测 ---------------------------------------------------------------

    def stats(self) -> dict:
        return {"enabled": self.enabled,
                "targets": len(self._targets),
                "explicit": len(self._explicit),
                "matcher": bool(self._buckets or self._explicit)}
