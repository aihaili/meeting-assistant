"""流式识别管线的回归测试：拿**手上所有真实录音**跑一遍，专查"只有真麦才会犯"的错。

为什么要有它：这一路的错都有一个共同点——**我的自测用的是干净音频，用户用的是有噪声的真麦**，
于是"丢内容""开头一串错字"这类问题在我的自测里根本不出现。所以这个测试的原则是：

* **用真麦录音，不用合成音频**（合成音频证明不了任何事）；
* **查结构，不只是查字**。字错率是给人的，而"音频有洞""行重复""强制断句切在词中间"
  是结构问题——它们才是丢内容的来源，而且**在干净音频上不一定暴露**；
* 每条断言都写清"这条能抓住什么错"，免得以后有人以为它是凑数的。

前提：`data/mic-test/` 里有真麦录音（`mic-*.pcm` 是采集时落的裸 PCM，`*.wav` 是收尾的）。

用法: python scripts/test_stream_pipeline.py
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

os.environ.setdefault("MODELSCOPE_CACHE", r"E:\models\gguf-asr\.cache\modelscope")

import numpy as np  # noqa: E402

from mic_analyze import cer, strip_punct  # noqa: E402
from phone_mic import audio as A  # noqa: E402
from phone_mic.stream_asr import StreamASR  # noqa: E402

MIC = HERE.parent / "data" / "mic-test"
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"   {detail}"))
    if not cond:
        FAILS.append(name)


def read_any(p: Path) -> np.ndarray:
    if p.suffix.lower() == ".wav":
        return A.read_wav(p)[0]
    raw = p.read_bytes()
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


_ASR_CACHE: dict = {}


def _get_asr(two_pass: bool) -> StreamASR:
    """模型只加载一次并复用。

    第一版每个文件、每种模式都新建一个 StreamASR 并 load()，而 load 要 25 秒
    （流式+标点+VAD+离线+声纹五个模型）——于是测试跑到一半就超时了。
    加载一次、之后 reset() 复用，才跑得动。
    """
    if two_pass not in _ASR_CACHE:
        a = StreamASR(want_spk=False, two_pass=two_pass)
        a.load()
        _ASR_CACHE[two_pass] = a
    return _ASR_CACHE[two_pass]


def run(x: np.ndarray, two_pass: bool, spy: list | None = None) -> tuple[dict, dict]:
    """跑一遍管线。返回 (逐行的最终文本, 额外统计)。

    ``spy`` 会在每次第二遍被调用时收到 (已攒音频采样点数, 该句已持续秒数)，
    用来验证"音频是不是连续的"——这是"丢内容"的直接证据。
    """
    a = _get_asr(two_pass)
    a.reset()
    if spy is not None:
        orig = a._second_pass

        def wrapped(fb, _a=a, _o=orig, _s=spy):
            _s.append((sum(len(b) for b in _a._utt), _a._stream_s))
            return _o(fb)

        a._second_pass = wrapped
    final: dict = {}
    for i in range(0, len(x), A.TARGET_SR // 2):
        a.push(x[i:i + A.TARGET_SR // 2], A.TARGET_SR)
        for r in a.tick():
            final[r.idx] = r
    for r in a.flush():
        final[r.idx] = r
    rows = {k: final[k] for k in sorted(final)}
    return rows, {"vad": a._vad_ok, "load_s": a.load_s}


def main() -> int:
    files: list[Path] = []
    files += sorted(MIC.glob("mic-*.pcm"), key=os.path.getmtime)
    files += sorted(MIC.glob("*.wav"), key=os.path.getmtime)
    files = [p for p in files if p.stat().st_size > 32000]      # 少于 1 秒的跳过
    if not files:
        print("data/mic-test/ 里没有可用的真麦录音，无法测试。")
        return 1

    refs = {("take3-clipped.wav",): "ref.txt", ("mic-001.wav",): "ref2.txt"}
    print(f"用 {len(files)} 段真麦录音跑管线（按时间从早到晚）")
    for p in files:
        x = read_any(p)
        dur = len(x) / A.TARGET_SR
        print("=" * 74)
        print(f"{p.name}  {dur:.1f}s  整体 {A.rms_dbfs(x):.1f} dBFS  峰值 {abs(x).max():.3f}")

        spy: list = []
        rows, info = run(x, two_pass=True, spy=spy)
        check("流式 VAD 可用", info["vad"], "VAD 没加载起来 → 退回能量判静音")

        # ── 结构 1：送给第二遍的音频必须**连续**（这是丢内容的直接来源）──
        # 上一版按 VAD 只收"有人说话"的块，句子中间的换气被丢掉 → 音频有洞 → 字没了。
        holes = []
        for k in range(1, len(spy)):
            grew = spy[k][0] - spy[k - 1][0]
            dt = (spy[k][1] - spy[k - 1][1]) * A.TARGET_SR
            if grew < 0:
                continue          # 计数器归零 = 上一句收了、新的一句开始（正常）
            # **真正的洞是"时间过了、缓冲却没涨"**：句子里被丢掉的块就是这么来的。
            # （第一版把"同一位置被调两次"和"句子切换"也判成洞，那是断言太糙。）
            if dt > 0.3 * A.TARGET_SR and grew < dt * 0.6:
                holes.append((spy[k - 1], spy[k]))
            if grew > dt + 2.0 * A.TARGET_SR:
                holes.append(("缓冲涨得比时间还快", spy[k - 1], spy[k]))
        check("第二遍收到的音频是连续的（没有洞）", not holes, str(holes[:2]))

        # ── 结构 2：不能有"时间重叠且文本相似"的重复行（老碎片化的特征）──
        order = [rows[k] for k in rows]
        dups = []
        for i in range(len(order)):
            for j in range(i + 1, len(order)):
                a_, b_ = order[i], order[j]
                ov = min(a_.end, b_.end) - max(a_.start, b_.start)
                span = min(a_.end - a_.start, b_.end - b_.start)
                if span > 0 and ov / span > 0.5:
                    P, Q = set(a_.text), set(b_.text)
                    if len(P & Q) / max(1, len(P | Q)) > 0.5:
                        dups.append((a_.text[:16], b_.text[:16]))
        check("没有重复行", not dups, str(dups[:2]))

        # ── 结构 3：两遍法不能比只用流式**少**内容 ──
        only, _ = run(x, two_pass=False)
        hyp2 = strip_punct("".join(r.text for r in rows.values()))
        hyp1 = strip_punct("".join(r.text for r in only.values()))
        if hyp1:
            keep = len(hyp2) / len(hyp1)
            check(f"两遍法没有丢内容（保留 {keep*100:.0f}%，流式 {len(hyp1)} 字）",
                  keep >= 0.9, f"两遍法只剩 {len(hyp2)} 字")

        # ── 结构 4：强制断句不该把词切成孤零零一两个字 ──
        tiny = [r for r in rows.values() if len(strip_punct(r.text)) <= 2]
        check("没有只剩一两个字的碎行", len(tiny) <= 1,
              str([r.text for r in tiny][:3]))

        if "。" in "".join(r.text for r in rows.values()) or len(rows) > 1:
            check("定稿文本带标点", any("。" in r.text or "，" in r.text
                                        for r in rows.values()), "整段没有标点")

        joined = "".join(r.text for r in rows.values())
        ref_file = refs.get((p.name,))
        if ref_file and (MIC / ref_file).is_file():
            R = strip_punct((MIC / ref_file).read_text(encoding="utf-8"))
            H = strip_punct(joined)
            i = R.find(H[:8])
            r2 = R[i:] if i >= 0 else R
            rate, sub, dele, ins = cer(r2, H)
            print(f"    对微信文本分歧 {rate*100:.1f}%（替{sub}/删{dele}/插{ins}）"
                  f"  参考 {len(r2)} 字 / 识别 {len(H)} 字")

        for r in rows.values():
            print(f"    [{r.start:6.1f}-{r.end:6.1f}] {r.text}")

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  · " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
