"""FireRedASR2S 识别后端：与 StreamASR 相同的公共接口（load/push/tick/flush/reset）。

为什么要有第二个后端
---------------------
FunASR（StreamASR）的 paraformer-zh 在 4 个中文公开基准上平均 CER 4.16%；
FireRedASR2（AED 架构）同基准 3.05%，且 FireRedPunc 的标点 F1 78.9% 也明显
高于 ct-punc（62.8%）。这个模块把 FireRed 三件套组装成和 StreamASR 一样的
"流式识别"形态，让 LocalMicReceiver 不用改接口就能换后端：

* **FireRedStreamVAD**（2.2MB，DFSMN）——流式 VAD，决定句界。
  和 StreamASR 里 fsmn-vad 的角色相同。min_silence_frame=120（1.2s 静音收句，
  对齐 StreamASR 的 GAP_END_S）；max_speech_frame=2000（20s 硬断，AED 输入上限
  是 60s，留足余量）。
* **FireRedASR2-AED**（4.5GB）——句级识别。VAD 收一句、整句送一次，
  没有增量解码，也就没有"两次识别文本漂移"的问题；开放句（还没收口的）
  定期整句重识别（节流，见 _LIVE_EVERY_S），界面上能看到文字在长。
* **FireRedPunc**（407MB，BERT）——给识别文本加标点。
* **CAM++ 声纹**——和 stream_asr 同一套（_find_sv_model + 归簇逻辑原样复用），
  说话人归属与识别后端无关。

模型位置（settings.json 的 asr.firered_dir 可改）：
    pretrained_models/FireRedASR2-AED
    pretrained_models/FireRedVAD/Stream-VAD
    pretrained_models/FireRedPunc
"""
from __future__ import annotations

import math
import sys
import threading
import time
from pathlib import Path

import kaldi_native_fbank as knf
import numpy as np
import soundfile as sf

from . import audio as A
from .stream_asr import Row, StreamASR
from .streaming import (EMB_PAD_S, MIN_EMB_S, SpeakerClusterer,
                       _find_sv_model)

# VAD 帧率 100 帧/秒（10ms 移位），frame_idx 从 1 计。
_FRAME_S = 0.01
# 按 200ms 一块喂 VAD（和官方示例同粒度；OnlineFbank 内部状态跨块连续）。
_CHUNK = 3200
# 首字回补秒数：VAD 确认语音有 ~80ms 延迟，且"触发块"在块首判断时 _cur_start
# 还是 None、没被收进 _utt（见 _feed_block），直接丢整块 = 每句丢第一个字。
# 从 _tail 回补触发点前这么多秒，保住首字（0.4s 足以覆盖 1~2 个 200ms 块）。
LEAD_S = 0.4
# 开放句重识别节流：段长 ≥3s 才识别，两次间隔 ≥3s（AED 跑 20s 音频约 2~4s，
# 间隔太短会背靠背排队）。
_LIVE_MIN_S = 3.0
_LIVE_EVERY_S = 3.0
# 短于 0.6s 的"句"当噪声丢掉（VAD 毛刺）。
_MIN_UTT_S = 0.6


def _to_int16(audio: np.ndarray) -> np.ndarray:
    """float32 (±1) → int16 (±32768)。

    FireRed 的 VAD 与 AED 的 CMVN 都按 int16 尺度训练（官方 str 路径分别用
    ``sf.read(dtype="int16")`` / ``kaldiio.load_mat`` 读 int16）。喂 float32 (±1)
    会 OOD：VAD 全零概率、AED 静默返回空文本（transcribe 内部吞异常）。
    """
    return np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)


class _StatefulFbank:
    """包一层 KaldifeatFbank：持有一个跨块存活的 OnlineFbank，只取新增帧。

    FireRed 的 ``KaldifeatFbank.__call__`` 每次新建 ``knf.OnlineFbank``，200ms 块
    之间丢最多 25ms 左上下文，导致流式 VAD 漏静音（实测 75s 音频漏掉 1 处 5s 段，
    退化成每 20s 硬断）。这里让 fbank 跨块连续，分块结果与整段 detect_full 一致。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._fbank = None
        self._extracted = 0

    def reset(self) -> None:
        self._fbank = None
        self._extracted = 0

    def __call__(self, wav, is_train: bool = False) -> np.ndarray:
        if isinstance(wav, str):
            wav_np, sample_rate = sf.read(wav, dtype="int16")
        else:
            sample_rate, wav_np = wav
        assert len(wav_np.shape) == 1
        self._inner.opts.frame_opts.dither = 0.0
        if self._fbank is None:
            self._fbank = knf.OnlineFbank(self._inner.opts)
        self._fbank.accept_waveform(sample_rate, wav_np.tolist())
        n = self._fbank.num_frames_ready
        feat = [self._fbank.get_frame(i) for i in range(self._extracted, n)]
        self._extracted = n
        if not feat:
            return np.zeros((0, self._inner.opts.mel_opts.num_bins))
        return np.vstack(feat)


class FireRedASR:
    """FireRedASR2S 流式识别。公共接口与 StreamASR 一致：
    load / push / tick / flush / reset，外加 loaded / spk_ready / spk_error /
    level / time_offset / load_s / elapsed / stats()。
    """

    def __init__(self, want_spk: bool = True,
                 model_root: str | Path = r"E:\WhisperX\FireRedASR2S",
                 device: str = "cuda:0") -> None:
        self.want_spk = want_spk
        self.model_root = Path(model_root)
        self.device = device
        self.use_gpu = "cuda" in device

        self._lock = threading.RLock()
        self._buf = np.zeros(0, dtype=np.float32)
        self._tail = np.zeros(0, dtype=np.float32)   # 最近 8s，声纹用
        self._tail_start = 0.0
        self._stream_s = 0.0                          # 已喂入的音频秒数
        self.level = 0.0
        self.time_offset = 0.0

        # VAD / 句状态
        self._vad = None
        self._cur_start: float | None = None          # 当前开放句的 VAD 起点（流秒）
        self._utt: list[np.ndarray] = []              # 开放句的音频（连续，不裁中间）
        self._utt_t0 = 0.0
        # 开放行
        self._open_row: Row | None = None
        self._rows: list[Row] = []
        self._next_idx = 0
        self._live_at = 0.0
        self._live_text = ""
        # 模型
        self._asr = None
        self._punc = None
        self._sv = None
        self.spk_ready = False
        self.spk_error = ""
        self.loaded = False
        self.load_s = 0.0
        # 声纹归簇（与 stream_asr 同一套）
        self._spk = SpeakerClusterer()

    # ── lifecycle ───────────────────────────────────────────────────────

    def load(self) -> float:
        t0 = time.time()
        root = self.model_root
        if not (root / "pretrained_models").is_dir():
            raise FileNotFoundError(
                f"找不到 FireRedASR2S 目录：{root}（缺 pretrained_models）")
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))   # fireredasr2s 包就在仓库根
        try:
            from fireredasr2s.fireredasr2.asr import FireRedAsr2, FireRedAsr2Config
            from fireredasr2s.fireredpunc.punc import FireRedPunc, FireRedPuncConfig
            from fireredasr2s.fireredvad.stream_vad import (
                FireRedStreamVad, FireRedStreamVadConfig)
        except Exception as e:
            raise RuntimeError(f"FireRedASR2S 包导入失败（{root}）：{e}") from e

        # 流式 VAD：1.2s 静音收句、20s 硬断
        self._vad = FireRedStreamVad.from_pretrained(
            root / "pretrained_models" / "FireRedVAD" / "Stream-VAD",
            FireRedStreamVadConfig(use_gpu=self.use_gpu,
                                   speech_threshold=0.5,
                                   min_speech_frame=8,
                                   max_speech_frame=2000,
                                   min_silence_frame=120))
        # 状态保持 fbank：跨 200ms 块连续，避免块间丢左上下文导致漏静音
        self._vad.audio_feat.fbank = _StatefulFbank(self._vad.audio_feat.fbank)
        # AED 识别：fp16（4.5GB fp32 → 约 2.3GB）
        self._asr = FireRedAsr2.from_pretrained(
            "aed", str(root / "pretrained_models" / "FireRedASR2-AED"),
            FireRedAsr2Config(use_gpu=self.use_gpu, use_half=self.use_gpu,
                              beam_size=3))
        self._punc = FireRedPunc.from_pretrained(
            str(root / "pretrained_models" / "FireRedPunc"),
            FireRedPuncConfig(use_gpu=self.use_gpu))
        if self.want_spk:
            try:
                from funasr import AutoModel
                self._sv = AutoModel(model=_find_sv_model(), disable_update=True,
                                     device=self.device)
                self.spk_ready = True
            except Exception as e:
                self.spk_error = f"{type(e).__name__}: {e}"
                print(f"[asr] 声纹模型加载失败（继续，不带说话人）：{self.spk_error}",
                      file=sys.stderr, flush=True)
        self.loaded = True
        self.load_s = time.time() - t0
        return self.load_s

    # ── 流式接口 ────────────────────────────────────────────────────────

    def push(self, chunk: np.ndarray, sr: int = A.TARGET_SR) -> None:
        if sr != A.TARGET_SR:
            chunk = A.resample(chunk, sr, A.TARGET_SR)
        with self._lock:
            self._buf = np.concatenate([self._buf, chunk.astype(np.float32)])

    def tick(self, force: bool = False) -> list[Row]:
        """处理已缓冲音频，返回**新建或修订**的行（和 StreamASR 一样按 idx 去重）。"""
        with self._lock:
            if not self.loaded:
                return []
            out = self._feed_ready()
            if self._cur_start is not None:
                row = self._open_utterance(force=force)
                if row is not None:
                    out.append(row)
            return out

    def flush(self) -> list[Row]:
        with self._lock:
            # 和 StreamASR 一样：不足一块的尾巴（这里最多 200ms）补静音凑满再喂，
            # 否则最后那两百毫秒的话就白说了 —— 补的是静音，真实内容一个采样不少。
            if self._buf.size:
                pad = (-self._buf.size) % _CHUNK
                if pad:
                    self._buf = np.concatenate(
                        [self._buf, np.zeros(pad, dtype=np.float32)])
            out = self._feed_ready()
            if self._cur_start is not None:
                out.extend(self._close_utterance(self._stream_s))
            return out

    def reset(self) -> None:
        with self._lock:
            self._buf = np.zeros(0, dtype=np.float32)
            self._tail = np.zeros(0, dtype=np.float32)
            self._tail_start = 0.0
            self._stream_s = 0.0
            self._cur_start = None
            self._utt = []
            self._utt_t0 = 0.0
            self._open_row = None
            self._rows = []
            self._next_idx = 0
            self._live_at = 0.0
            self._live_text = ""
            self._spk.reset()
            if self._vad is not None:
                try:
                    self._vad.reset()
                except Exception:
                    pass
                try:
                    self._vad.audio_feat.fbank.reset()
                except Exception:
                    pass

    @property
    def elapsed(self) -> float:
        return self._stream_s

    def stats(self) -> dict:
        return {"elapsed": round(self._stream_s, 1), "rows": len(self._rows),
                "open": self._cur_start is not None,
                "clusters": len(self._spk.centroids),
                "level": round(self.level, 1)}

    # ── 内部 ───────────────────────────────────────────────────────────

    def _feed_ready(self) -> list[Row]:
        """把缓冲按 200ms 一块喂给流式 VAD；收口时整句识别。"""
        out: list[Row] = []
        while len(self._buf) >= _CHUNK:
            block = self._buf[:_CHUNK]
            self._buf = self._buf[_CHUNK:]
            out.extend(self._feed_block(block))
        return out

    def _feed_block(self, block: np.ndarray) -> list[Row]:
        t0 = self._stream_s
        self._stream_s += len(block) / A.TARGET_SR
        # 电平 + 声纹尾窗
        rms = float(np.sqrt(np.mean(np.square(block.astype(np.float64))))) \
            if block.size else 0.0
        self.level = 20.0 * math.log10(rms) if rms > 0 else -99.0
        self._tail = np.concatenate([self._tail, block])[-A.TARGET_SR * 8:]
        self._tail_start = max(0.0, self._stream_s - len(self._tail) / A.TARGET_SR)
        # 开放句的音频**连续收**（VAD 只裁首尾，绝不裁中间——stream_asr 的教训）
        if self._cur_start is not None:
            self._utt.append(block)
        # 流式 VAD（FireRed 期望 int16 尺度，见 _to_int16）
        try:
            results = self._vad.detect_chunk(_to_int16(block))
        except Exception as e:
            print(f"[asr] FireRed VAD 出错：{type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            return []
        out: list[Row] = []
        for fr in results:
            if fr.is_speech_start and self._cur_start is None:
                self._cur_start = (fr.speech_start_frame - 1) * _FRAME_S
                # 回补首字：触发块本身在块首判断时 _cur_start 还是 None、没被收进
                # _utt，直接 _utt=[] 会丢掉每句的第一个字。改从 _tail 回补触发点前
                # LEAD_S 秒（_tail 每块更新、已含触发块），后续块照常 append 不重叠。
                t_lead = max(0.0, self._cur_start - LEAD_S)
                i0 = max(0, int((t_lead - self._tail_start) * A.TARGET_SR))
                self._utt = [self._tail[i0:]]
                self._utt_t0 = self._cur_start
                self._open_row = None
                self._live_at = 0.0
                self._live_text = ""
            elif fr.is_speech_end and self._cur_start is not None:
                end_s = (fr.speech_end_frame - 1) * _FRAME_S
                out.extend(self._close_utterance(end_s))
        return out

    def _open_utterance(self, force: bool = False) -> Row | None:
        """开放行：同一 Row 反复更新（idx 不变）；节流整句重识别，让文字实时地长。"""
        if not self._utt:
            return None
        utt_s = self._stream_s - self._utt_t0
        if (force or utt_s >= _LIVE_MIN_S) and \
                (time.time() - self._live_at) >= _LIVE_EVERY_S:
            got = self._transcribe(self._utt)
            if got:
                self._live_text = self._punctuate(got)
            self._live_at = time.time()
        text = self._live_text
        if not text:
            return None
        start = self._cur_start if self._cur_start is not None else self._utt_t0
        if self._open_row is None:
            self._open_row = Row(idx=self._next_idx, text=text, start=start,
                                 end=self._stream_s, open=True)
            self._next_idx += 1
            self._rows.append(self._open_row)
        else:
            self._open_row.text = text
            self._open_row.end = self._stream_s
            self._open_row.revisions += 1
        return self._open_row

    def _close_utterance(self, end_s: float) -> list[Row]:
        """收句：AED 整句重识别 + 标点 + 分段 + 声纹；第一段沿用开放行的 idx。"""
        start = self._cur_start if self._cur_start is not None else self._utt_t0
        end = max(end_s, self._stream_s)
        rows: list[Row] = []
        raw = self._transcribe(self._utt)
        if not raw:
            raw = self._live_text   # 定稿失败 → 沿用最后一次开放行识别
        if raw:
            text = self._punctuate(raw)
            text = StreamASR._tidy_spaces(text)
            paras = StreamASR.split_paragraphs(text) or [text]
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
        self._cur_start = None
        self._utt = []
        self._open_row = None
        self._live_text = ""
        self._live_at = 0.0
        return rows

    # ── 模型调用 ────────────────────────────────────────────────────────

    def _transcribe(self, utt: list[np.ndarray]) -> str:
        """AED 识别这一段的音频（VAD 边界已裁好，直接整段送）。"""
        if self._asr is None or not utt:
            return ""
        audio = np.concatenate(utt)
        if len(audio) < int(_MIN_UTT_S * A.TARGET_SR):
            return ""
        try:
            res = self._asr.transcribe(["fr"], [(A.TARGET_SR, _to_int16(audio))])
            r = res[0] if isinstance(res, list) and res else {}
            got = (r or {}).get("text") or ""
            return got.strip()
        except Exception as e:
            print(f"[asr] FireRed AED 识别失败：{type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            return ""

    def _punctuate(self, text: str) -> str:
        if self._punc is None or not text.strip():
            return text
        try:
            res = self._punc.process([text])
            got = (res[0] or {}).get("punc_text") or text
            return got
        except Exception as e:
            print(f"[asr] FireRed Punc 加标点失败：{type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
            return text

    # ── 声纹（与 stream_asr 同一套：尾窗切段 → CAM++ 向量 → 本场归簇） ──

    def _embed(self, start: float, end: float):
        """切出这一段音频 → CAM++ 向量（L2 归一化）。归簇交给 self._spk。

        从整句缓冲 _utt 切（而非 8 秒 _tail 环）：长句定稿时早期段落早已滚出
        _tail，按 _tail 切会越界/取空导致丢标；_utt 覆盖整句，按 _utt_t0 换算
        下标，任意段落都能切到完整音频（与 _transcribe 同源）。
        """
        if self._sv is None or not self._utt:
            return None
        audio = np.concatenate(self._utt)
        i0 = max(0, int((start - EMB_PAD_S - self._utt_t0) * A.TARGET_SR))
        i1 = min(len(audio), int((end + EMB_PAD_S - self._utt_t0)
                                      * A.TARGET_SR))
        if i1 - i0 < int(MIN_EMB_S * A.TARGET_SR):
            return None
        try:
            res = self._sv.generate(input=audio[i0:i1], cache={},
                                    disable_pbar=True)
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
