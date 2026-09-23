"""分析一段麦克风采集（裸 PCM）并和对照文本比对。

为什么要有它：排查"识别太细碎"需要的不是再读一遍代码，而是三类**可量化的东西**——

1. **音频本身**：时长、电平分布、哪几段有人说话。音频差是碎片化的上游原因。
2. **切分**：跑一遍文件路径，看有多少行、以及"时间重叠 >30% 的相邻行"有几对——
   后者就是碎片化的直接指标（同一句被两个窗口各听一遍、又没合并上）。
3. **准确率**：有对照文本时算**字错率**。没有对照就只能凭感觉说"识别得不好"。

裸 PCM 是按 int16 / 16kHz / 单声道写的（见 phone_mic/mic_source.py 里的说明：
不写 WAV 是因为进程被强杀时 WAV 的头会留下长度 0）。

用法:
    python scripts/mic_analyze.py data/mic-test/take3.pcm
    python scripts/mic_analyze.py data/mic-test/take3.pcm --ref 对照文本.txt
    python scripts/mic_analyze.py data/mic-test/take3.pcm --ref-text "第一句。第二句。"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

from phone_mic import audio as A  # noqa: E402
from phone_mic.streaming import StreamingASR  # noqa: E402


def read_pcm(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    n = len(raw) // 2                      # 只取完整的帧
    return np.frombuffer(raw[: n * 2], dtype="<i2").astype(np.float32) / 32768.0


def strip_punct(s: str) -> str:
    """去掉标点与空白。算字错率时标点不该算错——ASR 的标点是另一个模型给的。"""
    drop = set("，。、！？；：\"'（）《》〈〉…—·,.;:!?()[]{}<>\"'- \t\n\r")
    return "".join(c for c in s if c not in drop)


def cer(ref: str, hyp: str) -> tuple[float, int, int, int]:
    """字符级编辑距离 / 参考长度。返回 (错率, 替换, 删除, 插入)。"""
    r, h = strip_punct(ref), strip_punct(hyp)
    if not r:
        return 0.0, 0, 0, 0
    prev = list(range(len(h) + 1))
    sub = dele = ins = 0
    back = []
    for i in range(1, len(r) + 1):
        cur = [i] + [0] * len(h)
        row = []
        for j in range(1, len(h) + 1):
            if r[i - 1] == h[j - 1]:
                cur[j] = prev[j - 1]
                row.append("=")
            else:
                opts = (prev[j - 1] + 1, prev[j] + 1, cur[j - 1] + 1)
                k = int(np.argmin(opts))
                cur[j] = opts[k]
                row.append(("S", "D", "I")[k])
        back.append(row)
        prev = cur
    # 回溯统计三类错误
    i, j = len(r), len(h)
    while i > 0 and j > 0:
        op = back[i - 1][j - 1]
        if op == "=":
            i -= 1
            j -= 1
        elif op == "S":
            sub += 1
            i -= 1
            j -= 1
        elif op == "D":
            dele += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    while i > 0:
        dele += 1
        i -= 1
    while j > 0:
        ins += 1
        j -= 1
    return prev[len(h)] / len(r), sub, dele, ins


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcm")
    ap.add_argument("--ref", default=None, help="对照文本文件")
    ap.add_argument("--ref-text", default=None, help="对照文本（直接给）")
    ap.add_argument("--window", type=float, default=20.0)
    ap.add_argument("--guard", type=float, default=1.2)
    ap.add_argument("--save-wav", default=None, help="顺便转成 WAV，便于回听")
    args = ap.parse_args()

    p = Path(args.pcm)
    if not p.is_file():
        print(f"找不到 {p}")
        return 1
    # 既收 PCM（采集时实时落盘的格式），也收 WAV（停止时收尾成的格式）——
    # 人要看、要听、要拿去分析的都是 WAV，而"被强杀也不丢"靠的是 PCM。
    x = A.read_wav(p)[0] if p.suffix.lower() == ".wav" else read_pcm(p)
    dur = len(x) / A.TARGET_SR
    print(f"音频 {p.name}  {dur:.1f}s  {len(x)} 采样  整体 {A.rms_dbfs(x):.1f} dBFS"
          f"  峰值 {abs(x).max():.3f}")
    if args.save_wav:
        A.write_wav(args.save_wav, x, A.TARGET_SR)
        print(f"（已转存 {args.save_wav}）")

    # 语音分布：每 0.5 秒一格
    half = A.TARGET_SR // 2
    cells = [A.rms_dbfs(x[i:i + half]) for i in range(0, len(x), half)]
    spoken = [c for c in cells if c >= -45]
    print(f"有人说话的小格 {len(spoken)}/{len(cells)}"
          + (f"  说话电平中位 {np.median(spoken):.1f} dBFS" if spoken else ""))

    asr = StreamingASR(window_s=args.window, guard_s=args.guard, want_spk=True)
    asr.load()
    final = {}
    step = A.TARGET_SR // 2
    for i in range(0, len(x), step):
        asr.push(x[i:i + step], A.TARGET_SR)
        for r in asr.tick():
            final[r.idx] = r
    for r in asr.flush():
        final[r.idx] = r
    rows = sorted([final[k] for k in final], key=lambda r: (r.start, r.idx))

    def sim(a: str, b: str) -> float:
        P, Q = set(a), set(b)
        return len(P & Q) / max(1, len(P | Q))

    pairs = []
    prev = None
    for r in rows:
        if prev is not None:
            ov = min(prev.end, r.end) - max(prev.start, r.start)
            span = min(prev.end - prev.start, r.end - r.start)
            if ov > 0 and span > 0 and ov / span > 0.3:
                pairs.append((ov / span, sim(prev.text, r.text), prev, r))
        prev = r

    print(f"\n切分：{len(rows)} 行；时间重叠 >30% 的相邻行 = {len(pairs)} 对"
          f"（碎片化指标，越少越好）")
    for frac, s, a, b in pairs:
        print(f"  重叠 {frac*100:3.0f}%  文本相似 {s:.2f}")
        print(f"     A[{a.start:6.1f}-{a.end:6.1f}] {a.text}")
        print(f"     B[{b.start:6.1f}-{b.end:6.1f}] {b.text}")

    print("\n逐行：")
    spk: dict = {}
    for r in rows:
        spk[r.spk or "(无)"] = spk.get(r.spk or "(无)", 0) + 1
        print(f"  [{r.start:6.1f}-{r.end:6.1f}] {r.spk or '(无声纹)':<10} {r.text}")
    print(f"声纹簇 {len(spk)} 个：" + "  ".join(f"{k}×{v}" for k, v in spk.items()))

    ref = args.ref_text
    if args.ref:
        ref = Path(args.ref).read_text(encoding="utf-8")
    if ref:
        hyp = "".join(r.text for r in rows)
        rate, sub, dele, ins = cer(ref, hyp)
        print(f"\n对照：参考 {len(strip_punct(ref))} 字，识别 {len(strip_punct(hyp))} 字")
        print(f"  字错率 CER = {rate*100:.1f}%   （替换 {sub} / 删除 {dele} / 插入 {ins}）")
        print("  参考：" + strip_punct(ref)[:80])
        print("  识别：" + strip_punct(hyp)[:80])
    return 0


if __name__ == "__main__":
    sys.exit(main())
