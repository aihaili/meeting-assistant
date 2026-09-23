"""流式识别引擎：用 FunASR 官方的 paraformer-zh-streaming 做增量解码。

## 为什么重写

原来用的是**离线模型** `paraformer-zh` 套一个 20 秒滑窗，每 1 秒把整个窗口重新解码一遍。
后果在真麦录音上全部暴露：

* 同一段音频在不同窗口解出的**文本不一样**（`铺线以机房为重点展开` / `现以机房为重点单`），
  于是靠文本相似度做的"同一句合并"必然失败 → **碎片 + 重复**；
* 每 1 秒重解码 20 秒音频，**约 20 倍实时算力**，白烧 GPU。

`streaming.py` 里那一大堆 `match_score` / `coverage_mask` / `clip_span` / 覆盖掩码，
本质上都是为了让离线模型在滑窗里装成流式而手写的补丁——**在重新发明 FunASR 已经做好的东西**。

官方方案（[FunASR 长语音识别指南](https://adg.csdn.net/69707018437a6b40336a3eb8.html)）：

    AutoModel(model="paraformer-zh-streaming")
    chunk_size = [0, 10, 5]      # 600ms 粒度
    res = model.generate(input=chunk, cache=cache, is_final=..., chunk_size=chunk_size,
                         encoder_chunk_look_back=4, decoder_chunk_look_back=1)

**增量解码，不回头改已经吐出来的字**——所以不存在"两次转写对不上"这个问题，
碎片化在源头就没了。这也让 fp16 之外的一切都简单了：不需要猜哪两行是同一句。

## 这里的切句方式

* 流式解码持续吐字，累积到"当前这一句"的缓冲里；
* **静音**（按能量，自适应门限）超过 `gap_end_s` 就收句；
* 收句时用 `ct-punc` 加标点，然后发一行（带会议时间轴上的起止时间）；
* 未收句的那部分作为"开放行"发出去（同一个 idx），界面能实时看到字在长；
* 声纹向量由 `_embed` 提取，说话人标签由 `SpeakerClusterer`（在线分配 + 聚合重聚类）给出。

时间戳：由服务端给的 `time_offset`（会议已进行多少秒）平移，见 `mic_source`。
"""

from __future__ import annotations

import math
import re
import sys
import threading
import time
import wave
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from . import audio as A
from .streaming import (MIN_EMB_S, EMB_PAD_S, SpeakerClusterer,
                       _find_sv_model)

# 官方实时粒度：600ms。chunk_size[1] * 960 = 9600 采样 @16k
CHUNK_MS = 600
CHUNK = 9600
ENCODER_LOOK_BACK = 4
DECODER_LOOK_BACK = 1

# 静音判定的自适应门限：低于"语音包络"的这个比例算静音
SILENCE_RATIO = 0.12
# 静音多久收句。0.7 秒是"说话人换气"的量级；2.5 秒以上基本是换人/停顿
# 断句规则。原来只有"静音 0.9 秒"，**没有"说太久强制断"**——实测撞过：
# 一段 39.5 秒的说话里静音始终没到门限，于是**永远不断句、第二遍永远不跑**，
# 用户自始至终只看到第一遍的粗糙文本。sherpa-onnx 官方 two-pass 示例用的是三条规则
# （rule1 静音2.4s / rule2 静音1.2s且像说完了 / rule3 超过20秒强制断），
# 我们照它的思路补上 rule2 与 rule3。
GAP_END_S = 1.2        # 静音这么久，且文本像是说完了 → 断句（rule2）
GAP_HARD_S = 2.6       # 静音这么久，无论如何断句（rule1）
MAX_UTT_S = 26.0       # 一段超过这么久就收尾（rule3）。比 20 秒放宽：
                       # 因为收尾后会**按句末标点切段落**，不再从中间硬砍，
                       # 所以放宽只会让段落更长一点，不会伤到句子完整。
# 段落切分：一段最多这么多字（约 20 秒语音），太短的尾巴并进上一段。
# 用户的要求是"保留单句完整的前提下分段输出，这样 LLM 解析效率才高"——
# 所以切点**只能落在句末标点**上，句子本身一个都不切。
PARA_MAX_CHARS = 130
PARA_MIN_CHARS = 24
# 增量标点的节流：至少 1.2 秒、且至少多了 4 个字才重算一次
PUNC_EVERY_S = 1.2
PUNC_MIN_CHARS = 4
# 开放行也跑第二遍的节流：至少 1.5 秒、且至少多了 6 个字才重听一次。
# 为什么必须做：实测一段 39.5 秒的说话，静音始终没到收句门限，于是整段停在
# "开放行"里，第二遍**一次都没跑**——用户看到的自始至终是第一遍流式的文本
# （24% 分歧、`虽广` 那一档），好结果要等停止录音才出现。
SP_EVERY_S = 1.5
SP_MIN_CHARS = 6


@dataclass
class Row:
    """一行发言。字段和 phone_mic.streaming.FeedSentence 对齐，便于两边共用发布逻辑。"""

    idx: int
    text: str
    start: float
    end: float
    spk: str = ""
    emb: list | None = None
    open: bool = True
    first_seen_at: float = field(default_factory=time.time)
    revisions: int = 0
    final_at: float | None = None

    def to_dict(self) -> dict:
        return {"idx": self.idx, "text": self.text, "start": self.start, "end": self.end,
                "spk": self.spk, "open": self.open, "revisions": self.revisions}


class StreamASR:
    """流式识别。公开接口刻意和老的 StreamingASR 一致：load/push/tick/flush。"""

    def __init__(self, want_spk: bool = True, punc: bool = True,
                 device: str = "cuda:0", two_pass: bool = True) -> None:
        self.want_spk = want_spk
        self.use_punc = punc
        self.two_pass = two_pass
        self.device = device
        self._model = None
        self._punc = None
        self._sv = None
        self.spk_ready = False
        # 热词表（HotwordTable）：参会名单 + 领域术语 → 文本级纠错。由 server 注入，
        # 离线遍定稿与开放行实时都过它。None = 未启用（行为与原来完全一致）。
        self.hotwords = None
        # 开放行纠错缓存：文本没变就复用上次结果，免得每个 tick 都重跑拼音滑窗
        self._hot_cache: tuple[str, str] | None = None
        self.spk_error = ""
        self.loaded = False
        self.load_s = 0.0

        self._lock = threading.RLock()
        self._buf = np.zeros(0, dtype=np.float32)   # 待喂的音频（不足一块就攒着）
        self._cache: dict = {}
        self._tail = np.zeros(0, dtype=np.float32)  # 最近几秒，用于声纹切片
        self._tail_start = 0.0                      # _tail[0] 对应流内第几秒
        self._stream_s = 0.0                        # 已喂进模型的秒数
        self._cur_text = ""                         # 当前这一句已解码的文本
        self._cur_start: float | None = None        # 当前句的起点（流内秒）
        self._cur_end = 0.0
        self._silence_s = 0.0                       # 当前已连续静音多少秒
        self._rows: list[Row] = []
        self._open_row: Row | None = None   # 当前未收句的那一行（原地更新）
        # 增量标点：上一次给开放行加标点的时刻 / 当时的字数。
        # 只在收句时加标点是不够的——收句靠静音间隔，用户连续说话时整段都停在
        # 一个"开放行"里，于是**标点要等到停止录音才一次性补上**，中间那一大段
        # 是没有标点的长串（用户对比微信时一眼就看出来了）。
        # **两遍法的第一半**：留着"当前这一句"的音频，句尾拿它给离线模型重听一遍。
        # 这是实测出来的差距来源：同一段真麦录音，流式 29.0% 分歧、整句离线 20.8%，
        # 而且离线把 `虽广` 修成了 `推广`（微信也是 推广）。不是块大小的问题——
        # 块放大到 960ms 反而更差（30.8%），所以只能靠整句上下文。
        self._utt: list[np.ndarray] = []
        self._utt_t0 = 0.0         # _utt[0] 对应流内第几秒（裁边时要按它换算）
        self._vad_first = None     # 这一句第一次被判为语音的时刻（流内秒）
        self._vad_last = None      # 最后一次
        self._sp_at = 0.0          # 开放行上一次重听的时刻
        self._sp_len = 0           # 开放行上一次重听时的字数
        self._sp_text = ""         # 开放行上一次重听的结果
        # 流式 VAD。原来是我手写的"能量低于包络 12% 算静音"——在噪声和远场下不稳，
        # 实测撞过：句首一个噪声毛刺被当成字，识别结果开头多出一个"哇"；
        # 而句首切分对不对会连带影响整句（同一个"推广"被听成"飞广"）。
        # 模型的 VAD 就是专门干这个的，而且它给的是**语音段边界**，正好拿来
        # ① 判断断句 ② 裁掉首尾噪声再送去第二遍。
        self._vad = None
        self._vad_cache: dict = {}
        self._speaking = False
        self._last_speech_ms = 0.0
        self._vad_ok = False
        self._offline = None
        self.offline_ok = False
        self._live_text = ""
        self._punc_at = 0.0
        self._punc_len = 0
        self._next_idx = 0
        self._spk = SpeakerClusterer()              # 说话人归簇（在线先分 + 定稿重聚）
        self.level = 0.0                            # 最近一块的电平，界面用
        self.time_offset = 0.0

    # ── 清流状态（一次录音结束、下次开始前调用）────────────────────────────

    def reset(self) -> None:
        """清掉"这一次录音"的流状态，让下一次 start 从干净状态起跑。

        为什么必须有：`LocalMicReceiver.start()` 里写的是
        `if hasattr(self.asr, "reset"): self.asr.reset()`，注释声称会"清掉上一次录音留下的
        cache/文本缓冲/声纹簇"——而这个类一直没有 `reset()`，于是那行**静默不执行**：
        同一个实例第二次录音时，流式模型的 `_cache`、`_stream_s`、声纹簇都延续上一次。

        保留不清的只有两样，都是刻意的：
        * `_next_idx`：行号必须单调。下游按 idx 归属（`session.add_segment` 按 idx 更新、
          `mic_source` 把内部行号映射成会话行号），回退到 0 只会制造"覆盖旧行"的机会。
        * 模型本体（`_model/_punc/_sv/_vad/_offline`）与 `loaded`：加载要二十多秒，
          重录一次不该重付这个成本。
        """
        with self._lock:
            self._buf = np.zeros(0, dtype=np.float32)
            self._cache = {}
            self._tail = np.zeros(0, dtype=np.float32)
            self._tail_start = 0.0
            self._stream_s = 0.0
            self._cur_text = ""
            self._cur_start = None
            self._cur_end = 0.0
            self._silence_s = 0.0
            self._rows = []
            self._open_row = None
            self._utt = []
            self._utt_t0 = 0.0
            self._vad_first = None
            self._vad_last = None
            self._sp_at = 0.0
            self._sp_len = 0
            self._sp_text = ""
            self._vad_cache = {}
            self._speaking = False
            self._last_speech_ms = 0.0
            self._live_text = ""
            self._punc_at = 0.0
            self._punc_len = 0
            self._hot_cache = None
            self._spk = SpeakerClusterer()
            self.level = 0.0
            self.time_offset = 0.0

    # ── 热词纠错 ────────────────────────────────────────────────────────

    def set_hotwords(self, table) -> None:
        """注入热词表（HotwordTable）。None = 关闭纠错。由 server 装配时调用。"""
        self.hotwords = table
        self._hot_cache = None

    def _apply_hotwords(self, text: str) -> str:
        """对定稿/开放行文本做热词纠错。未启用或无表时原样返回。

        带一层"文本没变就复用"的缓存：开放行每个 tick 都会走到这里，但流式文本
        只在第二遍/标点重算时才真正变化，缓存命中时只花一次字符串比较。
        """
        if self.hotwords is None or not text:
            return text
        if self._hot_cache is not None and self._hot_cache[0] == text:
            return self._hot_cache[1]
        out = self.hotwords.apply(text)
        self._hot_cache = (text, out)
        return out

    # ── load ────────────────────────────────────────────────────────────

    def load(self) -> float:
        from funasr import AutoModel

        t0 = time.time()
        self._model = AutoModel(model="paraformer-zh-streaming", disable_update=True,
                                device=self.device)
        if self.use_punc:
            try:
                self._punc = AutoModel(model="ct-punc", disable_update=True,
                                       device=self.device)
            except Exception as e:  # noqa: BLE001
                print(f"[asr] 标点模型加载失败（继续不标点）：{e}", file=sys.stderr,
                      flush=True)
                self._punc = None
        if self.want_spk:
            try:
                self._sv = AutoModel(model=_find_sv_model(), disable_update=True,
                                     device=self.device)
                self.spk_ready = True
            except Exception as e:  # noqa: BLE001
                self.spk_error = f"{type(e).__name__}: {e}"
                print(f"[asr] 声纹模型加载失败：{self.spk_error}", file=sys.stderr,
                      flush=True)
        try:
            self._vad = AutoModel(model="fsmn-vad", disable_update=True,
                                  device=self.device)
            self._vad_ok = True
        except Exception as e:  # noqa: BLE001
            print(f"[asr] 流式 VAD 加载失败（退回能量判静音）：{e}",
                  file=sys.stderr, flush=True)
            self._vad = None

        if self.two_pass:
            try:
                self._offline = AutoModel(model="paraformer-zh", disable_update=True,
                                          device=self.device)
                self.offline_ok = True
            except Exception as e:  # noqa: BLE001
                print(f"[asr] 离线重听模型加载失败（退回只用流式）：{e}",
                      file=sys.stderr, flush=True)
                self._offline = None
        self.loaded = True
        self.load_s = time.time() - t0
        return self.load_s

    # ── push / decode ───────────────────────────────────────────────────

    def push(self, chunk: np.ndarray, sr: int = A.TARGET_SR) -> None:
        if sr != A.TARGET_SR:
            chunk = A.resample(chunk, sr, A.TARGET_SR)
        with self._lock:
            self._buf = np.concatenate([self._buf, chunk.astype(np.float32)])

    def _decode_ready(self) -> None:
        """把缓冲区里够一块的音频喂进流式模型。"""
        while len(self._buf) >= CHUNK:
            block = self._buf[:CHUNK]
            self._buf = self._buf[CHUNK:]
            self._decode_block(block)

    def _vad_feed(self, block: np.ndarray) -> None:
        """喂流式 VAD，并据此更新"是不是有人在说"和"已经静了多久"。

        ``value`` 里每个元素是 ``[起始ms, 结束ms]``；**结束为 -1 表示还在说**——
        这是流式 VAD 的约定（分段还没闭合）。用 200ms 一片喂，和官方示例的粒度一致。
        """
        if self._vad is None:
            return
        step = int(0.2 * A.TARGET_SR)
        got_any = False
        for i in range(0, len(block), step):
            piece = block[i:i + step]
            if len(piece) < step // 2:
                continue
            try:
                res = self._vad.generate(input=piece, cache=self._vad_cache,
                                         is_final=False)
            except Exception:  # noqa: BLE001
                return
            r = res[0] if isinstance(res, list) and res else res
            for seg in ((r or {}).get("value") or []):
                try:
                    beg, end = float(seg[0]), float(seg[1])
                except (TypeError, IndexError, ValueError):
                    continue
                got_any = True
                if end < 0:                      # 还在说
                    self._speaking = True
                    self._last_speech_ms = self._stream_s * 1000.0
                else:
                    self._speaking = False
                    self._last_speech_ms = max(self._last_speech_ms, end)
                # 记首尾语音边界（裁边用，不裁中间）
                if self._vad_first is None:
                    self._vad_first = beg / 1000.0
                self._vad_last = max(self._vad_last or 0.0,
                                     (end / 1000.0) if end >= 0 else self._stream_s)
        now_ms = self._stream_s * 1000.0
        if got_any:
            self._silence_s = 0.0 if self._speaking else \
                max(0.0, (now_ms - self._last_speech_ms) / 1000.0)

    def _decode_block(self, block: np.ndarray) -> None:
        t0 = self._stream_s
        self._stream_s += len(block) / A.TARGET_SR
        # 保留最近几秒音频，收句时给声纹切片用
        self._tail = np.concatenate([self._tail, block])[-A.TARGET_SR * 8:]
        self._tail_start = max(0.0, self._stream_s - len(self._tail) / A.TARGET_SR)

        rms = float(np.sqrt(np.mean(np.square(block.astype(np.float64))))) if block.size else 0.0
        self.level = 20.0 * math.log10(rms) if rms > 0 else -99.0
        # 自适应门限：跟踪语音包络（上升快下降慢），静音判据是"明显低于包络"
        if not hasattr(self, "_env"):
            self._env = 0.0
        self._env = max(rms, self._env * 0.98) if rms > self._env else self._env * 0.995
        # 静音判据：优先用 VAD（它给的是语音段边界），没有 VAD 才退回能量法
        self._vad_feed(block)
        if self._vad_ok:
            silent = not self._speaking
        else:
            silent = self._env > 1e-6 and rms < self._env * SILENCE_RATIO

        text = ""
        try:
            res = self._model.generate(input=block, cache=self._cache, is_final=False,
                                       chunk_size=[0, 10, 5],
                                       encoder_chunk_look_back=ENCODER_LOOK_BACK,
                                       decoder_chunk_look_back=DECODER_LOOK_BACK)
            r = res[0] if isinstance(res, list) and res else res
            text = ((r or {}).get("text") or "") if isinstance(r, dict) else ""
        except Exception as e:  # noqa: BLE001
            print(f"[asr] 流式解码出错：{type(e).__name__}: {e}", file=sys.stderr, flush=True)

        if text.strip():
            if self._cur_start is None:
                self._cur_start = t0
                self._utt = []
                self._utt_t0 = t0
                self._vad_first = None
                self._vad_last = None
            self._utt.append(block)
            # **官方流式返回的是"到目前为止整句的文本"**（实测：这一块给"这台平板"，
            # 下一块给"这台平板现在正"）。第一版我按"增量"理解、用 += 拼，于是文本滚雪球
            # （161 字的参考被拼成 1093 字）。这里两种都兼容：像是累积的就赋值，不像就追加。
            if text.startswith(self._cur_text):
                self._cur_text = text
            else:
                self._cur_text += text
            self._cur_end = self._stream_s
            self._silence_s = 0.0
        elif self._cur_text:
            # 句中停顿的块也要收进这一句的音频里，否则重听时音频中间是**断的**
            # （第一版只收了"解出字"的那些块，句子中间的停顿被丢掉）。
            # 只在 VAD 认为是语音（或刚说完的短尾）时收进这一句的音频——
            # 这样第二遍拿到的音频首尾是干净的，不会把噪声毛刺解码成一个字。
            # **每一块都收，保持连续。**
            # 第一版按 VAD 只收"有人在说"的块，于是句子中间的换气/短暂静音被丢掉，
            # 送给离线模型的音频**中间是断的**，那几个字就没了——用户看到的正是"丢失内容"。
            # 正确做法：音频连续，VAD 只用来裁**首尾**（见 _second_pass），绝不裁中间。
            self._utt.append(block)
            if silent and not self._vad_ok:
                self._silence_s += len(block) / A.TARGET_SR

    def tick(self, force: bool = False) -> list[Row]:
        """解码已缓冲的音频，返回**新建或修订**的行（和原来一样，按 idx 去重后使用）。"""
        with self._lock:
            if not self.loaded:
                return []
            self._decode_ready()
            out: list[Row] = []
            # **不能用 `or` 兜底**：`_cur_start` 是 0.0 时 Python 认为它是假值，
            # `0.0 or x` 会取 x，于是式子退化成 0 —— rule3（超 20 秒强制断句）被**静默禁用**。
            # 只在"语音从第 0 秒就开始"时发作（放视频/一开口就说），前面有静音时反而正常，
            # 所以之前那次测试没暴露它。实测：46.5 秒连续语音只出了 1 行。
            _st = self._cur_start if self._cur_start is not None else self._cur_end
            utt_s = (self._cur_end - _st) if self._cur_text else 0.0
            text = self._cur_text.strip()
            at_hard_gap = self._silence_s >= GAP_HARD_S          # rule1
            # rule2：静音够 1.2 秒，而且这一句**听起来说完了**（末尾是句末标点，
            # 或者标点模型在尾部给了句号）。不像说完就再等等——宁可晚半秒断句，
            # 也不要把一句话从中间劈开。
            at_soft_gap = (self._silence_s >= GAP_END_S and self._looks_finished(text))
            too_long = utt_s >= MAX_UTT_S                        # rule3
            if text and (force or at_hard_gap or at_soft_gap or too_long):
                if too_long and not (at_hard_gap or at_soft_gap):
                    print(f"[asr] 一句超过 {MAX_UTT_S:.0f} 秒，强制断句收尾（rule3）",
                          file=sys.stderr, flush=True)
                out.extend(self._close_utterance())
            elif self._cur_text.strip():
                out.append(self._open_utterance())
            return out

    def flush(self) -> list[Row]:
        with self._lock:
            # 不足一块的尾巴以前被直接丢掉（最多 0.6 秒语音不进模型）。补静音凑满一块再解码：
            # 真实内容一个采样都不少，多出来的只是静音，VAD / 标点不受影响。
            if self._model is not None and self._buf.size:
                pad = (-self._buf.size) % CHUNK
                if pad:
                    self._buf = np.concatenate(
                        [self._buf, np.zeros(pad, dtype=np.float32)])
            self._decode_ready()
            if self._model is not None:
                try:
                    self._model.generate(input=np.zeros(0, dtype=np.float32),
                                         cache=self._cache, is_final=True,
                                         chunk_size=[0, 10, 5],
                                         encoder_chunk_look_back=ENCODER_LOOK_BACK,
                                         decoder_chunk_look_back=DECODER_LOOK_BACK)
                except Exception:  # noqa: BLE001
                    pass
            out: list[Row] = []
            if self._cur_text.strip():
                out.extend(self._close_utterance())
            return out

    # ── utterance assembly ──────────────────────────────────────────────

    @staticmethod
    def _itn(text: str) -> str:
        """逆文本规范化：把"说出来的数字"写回数字形式。

        实测里这一类占了不少分歧，而且**它不是识别错误**——`十八` 和 `18` 说的
        是同一个东西，微信写 `18`，我们写 `十八`。所以这不属于"听错"，属于"没做 ITN"。

        保守起见只处理**有把握的形态**，不碰 `十八号`、`十点` 这种可能是口语表达的：
        * `X点Y` → X.Y（`二点五` → `2.5`）
        * 紧跟着拉丁字母/数字的中文数词 → 阿拉伯数字（`pad九` → `pad9`）
        * `十核` / `十核芯片` 这类"十+量词" → 10（`十核` → `10核`）
        """
        import re as _re
        d = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

        def cn2int(s: str):
            if not s:
                return None
            if s == "十":
                return 10
            if "十" in s:
                a, _, b = s.partition("十")
                hi = d.get(a, 1) if a else 1
                lo = d.get(b, 0) if b else 0
                return hi * 10 + lo if (a or b) else None
            if len(s) == 1 and s in d:
                return d[s]
            if all(c in d for c in s):        # 逐字读的数字，如"一八"
                return int("".join(str(d[c]) for c in s))
            return None

        out = text
        # X点Y → X.Y（只在小数场景；"十点"后面不跟数字就不动）
        def _dec(m):
            a, b = cn2int(m.group(1)), cn2int(m.group(2))
            return f"{a}.{b}" if a is not None and b is not None else m.group(0)
        out = _re.sub(r"([零一二两三四五六七八九十]+)点([零一二三四五六七八九]+)", _dec, out)
        # 拉丁字母/数字后面的中文数词 → 阿拉伯数字（pad九 → pad9）
        def _suffix(m):
            v = cn2int(m.group(2))
            return m.group(1) + str(v) if v is not None else m.group(0)
        out = _re.sub(r"([A-Za-z]+)([零一二两三四五六七八九十]+)", _suffix, out)
        # 中文数词**后面紧跟拉丁字母** → 阿拉伯数字（小米十八fold → 小米18fold）。
        # 只在后面确实是字母时才转，所以"十八号"这种口语表达不受影响。
        out = _re.sub(r"([零一二两三四五六七八九十]+)(?=[A-Za-z])",
                      lambda m: (str(cn2int(m.group(1)))
                                 if cn2int(m.group(1)) is not None else m.group(1)), out)
        # "十+量词" → 10+量词（十核 / 十号 / 十分）
        out = _re.sub(r"十(核|号|分|倍|条|款|台|个|人|年|月|日)",
                      lambda m: "10" + m.group(1), out)
        return out

    @staticmethod
    def _tidy_spaces(text: str) -> str:
        """收拾中英混排里的空格。

        标点模型会给中英混排插空格（实测 `小米18 fold`、`pad 9`），看着别扭。
        规则：**字母和数字之间的空格去掉**（`pad 9` → `pad9`）；紧挨中文的空格去掉；
        两个拉丁单词之间的空格**保留**（`DeepSeek harness` 是要的）。
        """
        import re as _re
        out = _re.sub(r"([A-Za-z])\s+(?=\d)", r"\1", text)          # pad 9 → pad9
        out = _re.sub(r"(?<=[\u4e00-\u9fff])\s+", "", out)          # 中文后面的空格
        out = _re.sub(r"\s+(?=[\u4e00-\u9fff])", "", out)           # 中文前面的空格
        return out

    @staticmethod
    def _normalize_punct(text: str) -> str:
        """半角标点跟着中文时改全角。

        标点模型会吐 `pro.` / `eys,` 这种半角（它是在中英混排上训的），
        在中文句子里看着很别扭。只在**前后是中文**时替换，别动 `2.5`、`v1.0` 里的点。
        """
        out = []
        half = {",": "，", ".": "。", "?": "？", "!": "！", ";": "；", ":": "："}
        for i, ch in enumerate(text):
            if ch in half:
                prev = text[i - 1] if i else ""
                nxt = text[i + 1] if i + 1 < len(text) else ""
                prev_cjk = "\u4e00" <= (prev or " ") <= "\u9fff"
                next_cjk = "\u4e00" <= (nxt or " ") <= "\u9fff"
                # 前一个字是中文，**或**后一个字是中文，都算中文语境。
                # 只看前面会漏掉 `harness,让他们` 这种（标点前是英文词、后面接中文）。
                # 数字点保留：`2.5` / `v1.0` 的下一个字符是数字，不转。
                if (prev_cjk or next_cjk) and not nxt.isdigit():
                    out.append(half[ch])
                    continue
            out.append(ch)
        return "".join(out)

    def _punctuate(self, text: str) -> str:
        if self._punc is None or not text.strip():
            return text
        try:
            res = self._punc.generate(input=text, cache={}, disable_pbar=True)
            r = res[0] if isinstance(res, list) and res else res
            got = ((r or {}).get("text") or text) if isinstance(r, dict) else text
            return self._normalize_punct(got)
        except Exception:  # noqa: BLE001
            return text

    def _embed(self, start: float, end: float):
        """切出这一段音频 → CAM++ 向量（L2 归一化）。归簇交给 self._spk。

        从整句缓冲 _utt 切（而非 8 秒 _tail 环）：长句定稿时早期段落早已滚出
        _tail，按 _tail 切会越界/取空导致丢标；_utt 覆盖整句，按 _utt_t0 换算
        下标，任意段落都能切到完整音频（与 _second_pass 同源）。
        """
        if self._sv is None or not self._utt:
            return None
        audio = np.concatenate(self._utt)
        i0 = max(0, int((start - EMB_PAD_S - self._utt_t0) * A.TARGET_SR))
        i1 = min(len(audio), int((end + EMB_PAD_S - self._utt_t0) * A.TARGET_SR))
        if i1 - i0 < int(MIN_EMB_S * A.TARGET_SR):
            return None
        try:
            res = self._sv.generate(input=audio[i0:i1], cache={}, disable_pbar=True)
            r = res[0] if isinstance(res, list) and res else res
            raw = (r or {}).get("spk_embedding") if isinstance(r, dict) else None
        except Exception:  # noqa: BLE001
            return None
        if raw is None:
            return None
        vec = raw.cpu().numpy().reshape(-1).tolist() if hasattr(raw, "cpu") \
            else [float(x) for x in list(raw)]
        n = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / n for x in vec]

    def _refit_spk(self, rows: list) -> None:
        """定稿后重聚：把被改标签的行更新进 self._rows，并把改了的行补进 rows 重发。"""
        changed = self._spk.refit()
        if not changed:
            return
        by_idx = {r.idx: r for r in self._rows}
        for idx, new_spk in changed.items():
            r = by_idx.get(idx)
            if r is None:
                continue
            r.spk = new_spk
            if r not in rows:
                rows.append(r)

    def _row(self, text: str, start: float, end: float, open_: bool) -> Row:
        row = Row(idx=self._next_idx, text=text, start=start, end=end)
        self._next_idx += 1
        row.text = text
        row.end = end
        row.open = open_
        if self.want_spk and not open_:
            vec = self._embed(start, end)
            if vec is not None:
                row.emb = vec
                row.spk = self._spk.assign(row.idx, vec)
            else:
                # 太短（< MIN_EMB_S=1.2s）算不出可信向量 —— 但**不能因此不给说话人**：
                # 真麦的段长中位数只有 1.1 秒，短行非常常见，而"这一句没人说"比"认错人"
                # 更糟（老实现里这些行 spk 为空，界面上就是一片未署名）。
                # 沿用当前说话人即可，和 streaming.py 那条路径的 inherit 语义一致。
                active = self._spk.active_label()
                if active:
                    row.spk = self._spk.inherit(row.idx, active)
        row.revisions += 1
        if not open_:
            row.final_at = time.time()
        if not self._rows or self._rows[-1] is not row:
            self._rows.append(row)
        return row

    def _maybe_punctuate_live(self, text: str) -> str:
        """给"正在说的这句"定期补标点，让它现在就读得通。

        节流：至少间隔 PUNC_EVERY_S，且至少多了 PUNC_MIN_CHARS 个字才重算——
        标点模型很便宜但也不是免费的，每个 600ms 块都调一次没必要。
        只在**末尾**动标点，前面的字不会被改写（实测过：标点模型不重写已定的内容）。
        """
        if self._punc is None or not text:
            return text
        now = time.time()
        if (now - self._punc_at) < PUNC_EVERY_S and (len(text) - self._punc_len) < PUNC_MIN_CHARS:
            return self._live_text or text
        self._punc_at = now
        self._punc_len = len(text)
        self._live_text = self._punctuate(text)
        return self._live_text

    @staticmethod
    def split_paragraphs(text: str) -> list[str]:
        """把长文按**句末标点**切成段落。句子绝不被切开。

        为什么需要它：LLM 是按行解析的，一行如果是一段 46 秒、没有任何句读的滚雪球文本，
        解析效率和准确率都会掉。但也不能像之前那样在 20 秒处硬砍——那会把句子拦腰截断
        （实测出现过"…新装过的软件，他。" / "居然学会了…"，`他们` 被切成两半）。

        规则：
        * 只在 `。！？!?…` 之后断段；
        * 一段不超过 ``PARA_MAX_CHARS`` 字，太短的尾巴并进上一段（避免"半句话"自成一段）；
        * 退让：如果**单句**本身就超长（标点模型在连续讲话时可能整段只给一个句号），
          才在 `，`/`；` 处退让切分——这是唯一会碰句子内部的情形，宁可如此也不要一行滚到底。
        """
        text = (text or "").strip()
        if not text:
            return []
        sents = [x for x in re.split(r"(?<=[。！？!?…])", text) if x.strip()]
        if not sents:
            return [text]
        out: list[str] = []
        buf = ""
        for s in sents:
            if buf and len(buf) + len(s) > PARA_MAX_CHARS:
                out.append(buf)
                buf = s
            else:
                buf += s
        if buf:
            if out and len(buf) < PARA_MIN_CHARS:
                out[-1] += buf
            else:
                out.append(buf)
        final: list[str] = []
        for para in out:
            while len(para) > PARA_MAX_CHARS * 1.6:
                limit = int(PARA_MAX_CHARS * 1.2)
                cut = max(para.rfind("，", 0, limit), para.rfind("；", 0, limit))
                if cut <= 0:
                    break
                final.append(para[:cut + 1])
                para = para[cut + 1:]
            if para:
                final.append(para)
        return final

    def _looks_finished(self, text: str) -> bool:
        """这一句听起来是不是说完了（rule2 的判据）。"""
        if not text:
            return False
        if text[-1] in "。！？!?…":
            return True
        # 标点模型对**当前这句**的判断也算数（它比单看末字符准）
        live = (self._live_text or "").strip()
        return bool(live) and live[-1] in "。！？!?…"

    def _live_second_pass(self, fallback: str) -> str:
        """开放行也定期拿整句音频重听一遍（节流）。详见 SP_EVERY_S 的说明。"""
        if self._offline is None or not self._utt:
            return self._sp_text or fallback
        now = time.time()
        grew = len(fallback) - self._sp_len
        if (now - self._sp_at) < SP_EVERY_S and grew < SP_MIN_CHARS:
            return self._sp_text or fallback
        self._sp_at = now
        self._sp_len = len(fallback)
        got = self._second_pass(fallback)
        self._sp_text = got
        return got

    def _open_utterance(self) -> Row:
        """还没收句：**同一个行对象**反复更新，返回给上层（idx 不变，界面就地刷新）。

        第一版每个 tick 都新建一行，于是 14.5 秒的音频产出 24 行、每行是更长的前缀。
        那不只是显示难看：下游是**按行**触发检索和线索分类的，
        一行一次 = 每 600ms 叫一次 LLM。一句话一行才是对的。
        """
        start = self._cur_start if self._cur_start is not None else self._stream_s
        # 开放行也定期用**离线模型重听**，而不是只用第一遍的流式文本。
        # 这是"正确率糟糕"的直接原因：不收句就永远只看到第一遍的结果。
        text = self._live_second_pass(self._cur_text.strip())
        # 增量标点：让"正在说的这一句"也带标点，而不是等收句才补
        text = self._tidy_spaces(self._maybe_punctuate_live(text))
        # 热词纠错：开放行实时也过一遍（参会名单/领域术语），带缓存不重跑
        text = self._apply_hotwords(text)
        if self._open_row is None:
            self._open_row = Row(idx=self._next_idx, text=text, start=start,
                                 end=self._cur_end, open=True)
            self._next_idx += 1
            self._rows.append(self._open_row)
            self._open_row.revisions += 1
            return self._open_row
        self._open_row.text = text
        self._open_row.end = self._cur_end
        self._open_row.revisions += 1
        return self._open_row

    def _second_pass(self, fallback: str) -> str:
        """**两遍法的第二半**：拿这一整句的音频给离线模型重听一遍。

        为什么值得：同一段真麦录音实测——流式 29.0% 分歧，整句离线 20.8%，
        而且离线把 `虽广` 修成了 `推广`。整句上下文一次性看全，同音词和专名
        才定得下来；流式解码器是单调的，吐出去就不能回头改。

        失败就退回流式的文本：第二遍是"锦上添花"，不该因为它的任何问题丢字。
        """
        if self._offline is None or not self._utt:
            return fallback
        audio = np.concatenate(self._utt)
        # 裁掉**首尾**的非语音（两端各留 0.25s 余量，别把起音和尾音切掉）。
        # 只裁两端：中间一动就会出现"音频有洞、字没了"。
        if self._vad_ok and self._vad_first is not None and self._vad_last is not None:
            pad = 0.25
            i0 = max(0, int((self._vad_first - pad - self._utt_t0) * A.TARGET_SR))
            i1 = min(len(audio), int((self._vad_last + pad - self._utt_t0) * A.TARGET_SR))
            if i1 - i0 >= int(0.4 * A.TARGET_SR):
                audio = audio[i0:i1]
        if len(audio) < int(0.6 * A.TARGET_SR):
            return fallback
        try:
            res = self._offline.generate(input=audio, cache={}, disable_pbar=True)
            r = res[0] if isinstance(res, list) and res else res
            got = ((r or {}).get("text") or "") if isinstance(r, dict) else ""
            # 离线模型的输出可能带空格（"本 期 是 推 广"），去掉
            got = got.replace(" ", "")
            # 明显更短说明这一遍没听清（比如切错了片段），那就别用它
            if len(got) >= max(2, int(len(fallback) * 0.6)):
                return got
        except Exception as e:  # noqa: BLE001
            print(f"[asr] 离线重听失败（用流式文本）：{type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
        return fallback

    def _close_utterance(self) -> list[Row]:
        """收尾一句：**按句末标点切成段落**，每段一行。

        返回列表（可能多行）。第一段复用原来那个"开放行"的 idx（界面上原地变成定稿），
        其余段落新建。时间戳按**字数比例**摊到这段时间里——切点落在哪一秒我们不知道，
        但按字数分摊比"整段都算第一行"准得多（音文对应关系本来就只是近似）。
        """
        raw = self._cur_text.strip()
        text = self._tidy_spaces(self._punctuate(self._itn(self._second_pass(raw))))
        # 热词纠错：离线遍定稿也过一遍（参会名单/领域术语），在切段前对整段做
        text = self._apply_hotwords(text)
        start = self._cur_start if self._cur_start is not None else self._stream_s
        end = self._cur_end or self._stream_s
        paras = self.split_paragraphs(text) or [text]

        rows: list[Row] = []
        total = sum(len(p) for p in paras) or 1
        acc = 0
        head = self._open_row
        for k, para in enumerate(paras):
            s = start + (end - start) * acc / total
            acc += len(para)
            e = start + (end - start) * acc / total
            if k == 0 and head is not None:
                row = head
                row.text = para
                row.start = s
                row.end = e
                row.open = False
                row.final_at = time.time()
                row.revisions += 1
            else:
                row = Row(idx=self._next_idx, text=para, start=s, end=e)
                self._next_idx += 1
                row.open = False
                row.final_at = time.time()
                self._rows.append(row)
            if self.want_spk:
                vec = self._embed(s, e)
                if vec is not None:
                    row.emb = vec
                    row.spk = self._spk.assign(row.idx, vec)
            rows.append(row)

        self._refit_spk(rows)
        self._cur_text = ""
        self._cur_start = None
        self._silence_s = 0.0
        self._open_row = None
        self._live_text = ""
        self._punc_len = 0
        self._sp_text = ""
        self._sp_len = 0
        return rows

    # ── misc ────────────────────────────────────────────────────────────

    @property
    def elapsed(self) -> float:
        return self._stream_s

    def stats(self) -> dict:
        return {"elapsed": round(self._stream_s, 1), "rows": len(self._rows),
                "open": bool(self._cur_text.strip()),
                "clusters": len(self._spk.centroids),
                "level": round(self.level, 1)}
