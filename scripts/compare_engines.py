"""两个引擎对着微信识别结果比：老（离线模型+滑窗）vs 新（官方流式增量解码）。

用现成的真麦录音和微信文本做对照——这是手上最诚实的基准：

  data/mic-test/mic-001.wav        14.5s  ref2.txt   （Max环境里，给你们演示一下…）
  data/mic-test/take3-clipped.wav  45.0s  ref.txt    （本期是推广啊，我自费购买…）

比三件事：

1. **碎片化指标**：时间重叠 >30% 的相邻行有几对（同一句被切成两行的直接证据）；
2. **分歧率**：和微信文本比，只看**录音覆盖到的那一段**（否则"没录到的部分"会被算成删除，
   把数字彻底带偏——这个坑踩过两次）；
3. **重复**：同一段话出现两遍。

用法: python scripts/compare_engines.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

from mic_analyze import cer, strip_punct  # noqa: E402
from phone_mic import audio as A  # noqa: E402

MIC = HERE.parent / "data" / "mic-test"


def sim(a: str, b: str) -> float:
    P, Q = set(a), set(b)
    return len(P & Q) / max(1, len(P | Q))


def run_old(x: np.ndarray) -> list[dict]:
    from phone_mic.streaming import StreamingASR

    a = StreamingASR(window_s=20.0, guard_s=1.2, want_spk=False)
    a.load()
    final = {}
    for i in range(0, len(x), A.TARGET_SR // 2):
        a.push(x[i:i + A.TARGET_SR // 2], A.TARGET_SR)
        for r in a.tick():
            final[r.idx] = r
    for r in a.flush():
        final[r.idx] = r
    rows = sorted([final[k] for k in final], key=lambda r: (r.start, r.idx))
    return [{"start": r.start, "end": r.end, "text": r.text} for r in rows]


def run_new(x: np.ndarray) -> list[dict]:
    from phone_mic.stream_asr import StreamASR

    a = StreamASR(want_spk=False)
    a.load()
    final = {}
    for i in range(0, len(x), A.TARGET_SR // 2):
        a.push(x[i:i + A.TARGET_SR // 2], A.TARGET_SR)
        for r in a.tick():
            final[r.idx] = r
    for r in a.flush():
        final[r.idx] = r
    rows = sorted([final[k] for k in final], key=lambda r: (r.start, r.idx))
    return [{"start": r.start, "end": r.end, "text": r.text} for r in rows]


def overlap_pairs(rows: list[dict]) -> list[tuple]:
    out, prev = [], None
    for r in rows:
        if prev is not None:
            ov = min(prev["end"], r["end"]) - max(prev["start"], r["start"])
            span = min(prev["end"] - prev["start"], r["end"] - r["start"])
            if ov > 0 and span > 0 and ov / span > 0.3:
                out.append((ov / span, sim(prev["text"], r["text"]), prev, r))
        prev = r
    return out


def align_cer(ref: str, hyp: str) -> tuple[float, int, int]:
    """只在"录音覆盖到的那一段"上算分歧率。

    做法：把识别文本的第一个 8 字锚点拿去参考里找位置，从那里截。
    不做这一步的话，"没录到的开头/结尾"会被算成删除，数字会被彻底带偏
    （实测把 15% 的真实分歧算成了 46%）。
    """
    R, H = strip_punct(ref), strip_punct(hyp)
    anchor = H[:8]
    i = R.find(anchor)
    ref2 = R[i:] if i >= 0 else R
    rate, sub, dele, ins = cer(ref2, H)
    return rate, len(ref2), len(H) - len(ref2)


def show(name: str, ref_file: str, wav: str) -> None:
    p = MIC / wav
    rp = MIC / ref_file
    if not p.is_file() or not rp.is_file():
        print(f"  [跳过] 缺少 {p.name} 或 {rp.name}")
        return
    x, sr = A.read_wav(p)
    ref = rp.read_text(encoding="utf-8")
    print("=" * 74)
    print(f"{name}   {wav}  {len(x)/sr:.1f}s  {A.rms_dbfs(x):.1f} dBFS  "
          f"峰值 {abs(x).max():.3f}")
    for label, fn in (("老：paraformer-zh + 20s滑窗", run_old),
                      ("新：paraformer-zh-streaming 增量", run_new)):
        t0 = time.time()
        rows = fn(x)
        cost = time.time() - t0
        hyp = "".join(r["text"] for r in rows)
        pairs = overlap_pairs(rows)
        rate, nref, ndiff = align_cer(ref, hyp)
        print(f"\n  【{label}】{len(rows)} 行 · 重叠对 {len(pairs)} · "
              f"分歧率 {rate*100:.1f}%（参考 {nref} 字 / 识别 {len(strip_punct(hyp))} 字）"
              f" · 处理 {cost:.1f}s")
        for r in rows:
            print(f"      [{r['start']:6.1f}-{r['end']:6.1f}] {r['text']}")
        for frac, s, a, b in pairs:
            print(f"      ⚠ 重叠 {frac*100:3.0f}% 相似 {s:.2f}: {a['text'][:20]} | {b['text'][:20]}")


def main() -> int:
    print("老引擎 vs 新引擎（微信识别结果做对照）")
    show("① 短句", "ref2.txt", "mic-001.wav")
    show("② 长段", "ref.txt", "take3-clipped.wav")
    return 0


if __name__ == "__main__":
    sys.exit(main())
