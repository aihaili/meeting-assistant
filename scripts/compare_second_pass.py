"""第二遍（离线重听）用哪个模型：同批真麦录音横评。

为什么第二遍的模型值得单独比：现在用的是 `paraformer-zh`。第二遍本来就是**离线**的，
换模型不用动架构、不影响实时那条路——是纯收益的改动。而"哪个更好"不该靠感觉，
手上正好有真麦录音 + 用户用微信识别出来的同段文本，可以直接算分歧率。

评法（踩过两次坑，所以口径写死在这里）：

* **只在录音覆盖到的那一段上比**。用户给的微信文本是整段的，而录音可能少头少尾；
  不截齐的话"没录到的部分"会被算成删除，把数字彻底带偏（实测把 15% 算成 46%）。
* **去掉标点再比**。标点是另一个模型给的，`，` 和 `,` 不该算错。
* **分歧率 = 字符级编辑距离 / 参考长度**，并且分开报替换/删除/插入——
  光看总率分不清"听错"和"漏字"。

用法: python scripts/compare_second_pass.py
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

os.environ.setdefault("MODELSCOPE_CACHE", r"E:\models\gguf-asr\.cache\modelscope")

import numpy as np  # noqa: E402

from mic_analyze import cer, strip_punct  # noqa: E402
from phone_mic import audio as A  # noqa: E402
from phone_mic.stream_asr import StreamASR  # noqa: E402

MIC = HERE.parent / "data" / "mic-test"

# 待比模型：名字 → 一句说明。都走 FunASR / ModelScope，缓存在 E: 盘。
CANDIDATES = [
    ("paraformer-zh", "现在用的（离线 paraformer）"),
    ("iic/SenseVoiceSmall", "SenseVoice Small（中文强、自带标点与 ITN）"),
]


def speech_span(x: np.ndarray) -> tuple[int, int]:
    """找出有人说话的那一段（首尾静音会污染重听结果，也会让分歧率失真）。"""
    half = A.TARGET_SR // 2
    cells = [(i, A.rms_dbfs(x[i:i + half])) for i in range(0, len(x), half)]
    loud = [c for c in cells if c[1] >= -45]
    if not loud:
        return 0, len(x)
    return loud[0][0], min(len(x), loud[-1][0] + half)


def clean(text: str) -> str:
    """去掉 SenseVoice 的富文本标记（<|zh|><|NEUTRAL|>…）和空格。"""
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out).replace(" ", "")


def divergence(ref: str, hyp: str) -> tuple[float, int, str]:
    R, H = strip_punct(ref), strip_punct(hyp)
    if not H:
        return 1.0, 0, ""
    i = R.find(H[:8])
    r2 = R[i:] if i >= 0 else R
    rate, sub, dele, ins = cer(r2, H)
    return rate, len(r2), f"替{sub}/删{dele}/插{ins}"


def main() -> int:
    from funasr import AutoModel

    itn = StreamASR.__dict__["_itn"].__func__        # 复用同一套 ITN，保证口径一致

    fixtures = [("mic-001.wav", "ref2.txt"), ("take3-clipped.wav", "ref.txt")]
    # 再加最新一次的录音（没有对照文本，只看可读性）
    extra = sorted(MIC.glob("mic-*.pcm"), key=os.path.getmtime)[-1:] if MIC.is_dir() else []

    audio_cache: dict[str, tuple[np.ndarray, int, str | None]] = {}
    for wav, reff in fixtures:
        p, rp = MIC / wav, MIC / reff
        if p.is_file():
            audio_cache[wav] = (*A.read_wav(p)[:1], A.TARGET_SR,
                                rp.read_text(encoding="utf-8") if rp.is_file() else None)
    for pcm in extra:
        raw = pcm.read_bytes()
        y = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        audio_cache[pcm.name] = (y, A.TARGET_SR, None)

    results: dict[str, dict[str, tuple]] = {}
    for name, note in CANDIDATES:
        print(f"\n=== 加载 {name} —— {note}")
        try:
            t0 = time.time()
            m = AutoModel(model=name, disable_update=True, device="cuda:0")
            print(f"    加载完成 {time.time()-t0:.1f}s")
        except Exception as e:  # noqa: BLE001
            print(f"    [跳过] 加载失败：{type(e).__name__}: {str(e)[:140]}")
            continue
        for key, (x, sr, ref) in audio_cache.items():
            i0, i1 = speech_span(x)
            seg = x[i0:i1]
            if len(seg) < A.TARGET_SR:
                continue
            t0 = time.time()
            try:
                if "SenseVoice" in name:
                    res = m.generate(input=seg, cache={}, language="zh", use_itn=True,
                                     batch_size_s=60, disable_pbar=True)
                else:
                    res = m.generate(input=seg, cache={}, disable_pbar=True)
                r = res[0] if isinstance(res, list) and res else res
                got = clean(((r or {}).get("text") or "")) if isinstance(r, dict) else ""
            except Exception as e:  # noqa: BLE001
                print(f"    [出错] {key}: {type(e).__name__}: {str(e)[:120]}")
                traceback.print_exc(limit=1)
                continue
            cost = time.time() - t0
            got = itn(got)
            if ref:
                rate, nr, detail = divergence(ref, got)
                results.setdefault(key, {})[name] = (rate, nr, len(strip_punct(got)),
                                                     detail, cost, got)
            else:
                results.setdefault(key, {})[name] = (None, 0, len(strip_punct(got)),
                                                     "", cost, got)

    lines = []
    for key, bymodel in results.items():
        lines.append("=" * 74)
        lines.append(f"【{key}】")
        for name, (rate, nr, nh, detail, cost, got) in bymodel.items():
            if rate is None:
                lines.append(f"  {name:24} 无对照  {nh} 字  {cost:.1f}s")
            else:
                lines.append(f"  {name:24} 分歧 {rate*100:5.1f}%  "
                             f"(参考{nr}/识别{nh} {detail})  {cost:.1f}s")
        for name, (_r, _n, _h, _d, _c, got) in bymodel.items():
            lines.append(f"    {name[:22]:24} {got[:66]}")
    (HERE.parent / "data" / "_sp2.txt").write_text("\n".join(lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
