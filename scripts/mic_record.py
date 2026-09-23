"""从真麦克风录一段，落成 16k 单声道 WAV —— 与实时链路完全一样的目标格式。

存在的理由：整条链路（麦克风 → ASR → 声纹 → 库）此前只在**回放文件**上验过。
回放文件是干净的；真麦克风有环境噪声、有远近变化、有喷麦，声纹阈值最该在这里被压。
所以需要一个"按一下录一段"的最小工具，而不是每次去翻 ffmpeg 命令行参数。

不自己实现重采样与落盘：走 `phone_mic.audio`，与实时链路共用同一套
（红线：不绕过项目自己的 16k WAV 规约，否则声纹和 ASR 拿到的就不是同一种音频）。

用法:
    python scripts/mic_record.py --list
    python scripts/mic_record.py --seconds 20 --out data/mic-test/session-1.wav
    python scripts/mic_record.py --seconds 20 --device 1 --gain 1.4
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

from phone_mic import audio as A  # noqa: E402


def list_devices() -> int:
    import sounddevice as sd

    print("输入设备（默认采样率按设备报的值，录完统一重采样到 16k）：")
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) < 1:
            continue
        mark = " <- 默认" if i == sd.default.device[0] else ""
        print(f"  [{i:2d}] {d['name'][:52]:<52} 输入通道 {d['max_input_channels']} "
              f"默认 {int(d['default_samplerate'])}Hz{mark}")
    return 0


def record(seconds: float, out: Path, device: int | None, gain: float,
           sr: int | None) -> int:
    import sounddevice as sd

    info = sd.query_devices(device, "input")
    native = int(sr or info["default_samplerate"])
    # 单声道够用，声纹不做立体声定位。原来是 `min(1, max_input_channels) or 1`，
    # 算出来恒等于 1（通道数 >=1 时 min 得 1，==0 时被 or 兜成 1），直接写 1。
    ch = 1
    print(f"设备：{info['name']}")
    print(f"采集：{native}Hz / {ch}ch / {seconds:.0f}s → 目标 16kHz 单声道")
    if out.exists():
        # 不覆盖：录废了要能重来，而"上一次的"往往正是要对比的那次
        out = out.with_name(out.stem + f"-{int(time.time())}" + out.suffix)
        print(f"目标文件已存在，改写成 {out.name}")

    frames = []
    peak = 0.0
    t0 = time.time()

    def cb(indata, _frames, _time, status):  # noqa: ANN001
        nonlocal peak
        if status:
            print(f"  [采集告警] {status}", file=sys.stderr)
        block = indata[:, 0].astype(np.float32)
        peak = max(peak, float(np.abs(block).max()))
        frames.append(block)

    with sd.InputStream(device=device, samplerate=native, channels=ch,
                        dtype="float32", callback=cb, blocksize=0):
        # 边录边报电平：录的时候就知道麦克风有没有在工作，不用等录完再看波形。
        # 这是"沉默的失败"最容易发生的地方——录到一个全零文件，一切看起来都正常。
        while time.time() - t0 < seconds:
            time.sleep(1.0)
            done = time.time() - t0
            lvl = A.rms_dbfs(np.concatenate(frames[-8:])) if frames else -99.0
            bar = "#" * int(max(0.0, (lvl + 60) / 60 * 24))
            print(f"  {done:5.1f}s / {seconds:.0f}s  电平 {lvl:6.1f} dBFS |{bar:<24}|",
                  flush=True)

    audio = np.concatenate(frames) if frames else np.zeros(1, dtype=np.float32)
    if gain != 1.0:
        audio = np.clip(audio * gain, -1.0, 1.0)
    if native != A.TARGET_SR:
        audio = A.resample(audio, native, A.TARGET_SR)
    out.parent.mkdir(parents=True, exist_ok=True)
    # **不要**在这里乘 32767：write_wav 内部就按 float[-1,1] → int16 缩放。
    # 第一版就是这么写的，于是音频被缩放了两次、整个文件削顶——
    # 而"录音器自己报的电平"是对的（那是在缩放之前量的），所以看起来一切正常，
    # 直到拿去识别才发现是垃圾。教训：写完要**读回来核对**，别信中间值。
    A.write_wav(out, audio.astype(np.float32), A.TARGET_SR)

    # 读回来核对：写出去的和量到的是不是同一个东西。
    back, back_sr = A.read_wav(out)
    dur = len(back) / back_sr
    level = A.rms_dbfs(back)
    if abs(level - A.rms_dbfs(audio)) > 3.0:
        print(f"!! 写盘前后电平差得太多（写入前 {A.rms_dbfs(audio):.1f} → "
              f"读回 {level:.1f} dBFS）：缩放次数不对，这个文件不能用。")
        return 3
    print(f"\n写入 {out}  {dur:.1f}s  整体 {level:.1f} dBFS  峰值 {peak:.3f}"
          f"  重采样 {A.resample_used()}")
    if peak <= 0.0005:
        print("!! 全程几乎没有信号：麦克风没采到声音（静音、选错设备、或系统里被静音）。"
              "这样的文件拿去识别只会得到空结果。")
        return 2
    if level < -45:
        print(f"!! 电平偏低（{level:.1f} dBFS）：能用，但识别率会掉。"
              f"把麦克风拿近一些，或者 --gain 放大（当前 {gain}）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="列出输入设备")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--out", default="data/mic-test/take.wav")
    ap.add_argument("--device", type=int, default=None, help="设备序号（见 --list）")
    ap.add_argument("--sr", type=int, default=None, help="采集采样率（默认用设备自己的）")
    ap.add_argument("--gain", type=float, default=1.0)
    args = ap.parse_args()
    if args.list:
        return list_devices()
    return record(args.seconds, Path(args.out), args.device, args.gain, args.sr)


if __name__ == "__main__":
    sys.exit(main())
