"""边说边出字：从麦克风实时识别，结果直接打在程序里。

为什么要有它：用户有两句反馈，其实是同一件事的两半。

1. **"声音还是很低，没办法，只能你自己拉高了。"**
   麦克风增益在 Windows 里调到顶还是不够（实测整体 -46 dBFS、峰值 0.048）。
   所以软件自己拉：按**语音电平**（而不是整体电平）算增益，拉到目标 -20 dBFS 左右，
   并且缓慢平滑、限幅，避免忽大忽小。
   要说清楚的一点：**软件放大不会凭空造出信息**，噪声也会一起放大。但这里这样做是站得住的——
   16 位量化噪声在 -96 dBFS，比 -46 dBFS 的信号低 50 dB，放大 20 dB 不会碰到它；
   而 ASR 模型大多是按 -20 dBFS 左右校准输入的，**绝对电平对识别率的影响比信噪比更直接**。
   真要好，还是麦克风离嘴近一点，那才是提高信噪比。

2. **"你能不能在程序里直接反馈出 ASR 的结果?"**
   能。这里就是：说一句、立刻看到识别结果、声纹簇、和电平。
   不用再来回贴日志——你自己跑、自己看。

用法:
    python scripts/mic_live.py                      # 从默认麦克风，说 60 秒
    python scripts/mic_live.py --seconds 30 --device 1
    python scripts/mic_live.py --wav data/mic-test/one-speaker.wav   # 拿录音当直播放（自测/演示）
    python scripts/mic_live.py --no-agc --save data/mic-test/take.wav
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
from phone_mic.streaming import FeedSentence, StreamingASR  # noqa: E402

TARGET_DBFS = -20.0     # 说话时的目标电平。ASR 模型大致按这个量级校准
MAX_GAIN = 40.0         # 增益上限：再高只是在放大底噪
ATTACK = 0.35           # 电平估计的上升速度（快，立刻跟上说话）
RELEASE = 0.06          # 下降速度（慢，句子之间不松懈）


class AutoGain:
    """按**语音电平**算增益，而不是整体电平。

    一录就是几十秒，里面有大量停顿；用整体 RMS 会把增益算得偏高，一说话就削顶。
    所以跟踪的是"较响的那部分"：块电平上升时快速跟（ATTACK），下降时慢慢放（RELEASE），
    再对增益本身做平滑——不然每个字都会忽大忽小。
    """

    def __init__(self, target_dbfs: float = TARGET_DBFS, max_gain: float = MAX_GAIN):
        self.target = 10 ** (target_dbfs / 20.0)
        self.max_gain = max_gain
        self.env = 0.0          # 语音包络（线性）
        self.gain = 1.0

    def process(self, block: np.ndarray) -> tuple[np.ndarray, float]:
        rms = float(np.sqrt(np.mean(np.square(block.astype(np.float64))))) if block.size else 0.0
        # 包络：上升快、下降慢；只在明显高于底噪时才更新，免得静音段把增益顶到上限
        if rms > self.env:
            self.env = (1 - ATTACK) * self.env + ATTACK * rms
        else:
            self.env = (1 - RELEASE) * self.env + RELEASE * rms
        want = 1.0
        if self.env > 1e-6:
            want = min(self.max_gain, self.target / self.env)
            want = max(1.0, want)      # 只放大，不衰减（衰减会把本来就正常的声音压小）
        # 增益平滑，避免抽气效应
        self.gain = 0.85 * self.gain + 0.15 * want
        out = block * self.gain
        peak = float(np.abs(out).max()) if out.size else 0.0
        if peak > 1.0:                 # 限幅而不是整体缩小
            out = np.tanh(out)
        return out.astype(np.float32), self.gain


def show(rows: dict, gain: float, level: float, final: bool = False) -> None:
    """把已确认的句子写到上面，把还在变的当前句写在最后一行（覆盖式）。"""
    done = [rows[k] for k in sorted(rows) if not rows[k].open]
    live = [rows[k] for k in sorted(rows) if rows[k].open]
    for r in done:
        if not getattr(r, "_printed", False):
            print(f"  [{r.start:6.1f}s] {r.spk or '(无声纹)':<10} {r.text}")
            r._printed = True
    if live:
        r = live[-1]
        bar = "#" * int(max(0.0, (level + 60) / 60 * 24))
        # \r 覆盖同一行：正在变的句子反复刷新，不刷屏
        sys.stdout.write(f"\r  [{r.start:6.1f}s] {r.spk or '(无声纹)':<10} {r.text[:60]}")
        sys.stdout.write(" " * max(0, 60 - len(r.text[:60])))
        sys.stdout.write(f"   ♪{gain:4.1f}x |{bar:<24}|")
        sys.stdout.flush()
    if final:
        print()


def run_wav(path: Path, asr: StreamingASR, agc: AutoGain | None, done: dict,
            realtime: bool) -> None:
    x, sr = A.read_wav(path)
    print(f"（回放模式）{path.name}  {len(x)/sr:.1f}s  原始 {A.rms_dbfs(x):.1f} dBFS")
    step = sr // 4        # 250ms 一块，和实时采集的粒度接近
    t0 = time.time()
    for i in range(0, len(x), step):
        block = x[i:i + step]
        if agc is not None:
            block, g = agc.process(block)
        else:
            g = 1.0
        asr.push(block, sr)
        for r in asr.tick():
            done[r.idx] = r
        show(done, g, A.rms_dbfs(block))
        if realtime:
            time.sleep(max(0.0, (i + step) / sr - (time.time() - t0)))
    for r in asr.flush():
        done[r.idx] = r
    show(done, agc.gain if agc else 1.0, -60.0, final=True)


def run_mic(seconds: float, device: int | None, asr: StreamingASR, agc: AutoGain | None,
            done: dict, save: Path | None) -> None:
    """采集 → 识别。**重活不在声音回调里做。**

    第一版把 `asr.tick()`（异步识别，几十到几百毫秒）直接写在 sounddevice 的回调里，
    于是回调跟不上：PortAudio 报 `input overflow`。那个警告不是噪音——
    **overflow 意味着这一段音频被丢掉了**，说话内容会凭空少一块，而且用户只会觉得
    "识别怎么漏字"，根本联想不到是回调太慢。

    所以回调只做一件事：把块拷进队列（PortAudio 会复用缓冲区，必须 copy）。
    真正的重采样 / 增益 / 识别在另一个线程里做，并且**攒到 0.5 秒再处理**——
    按 512 个采样一块去重采样，光是 scipy 的调用开销就够把回调拖垮。
    """
    import queue
    import threading

    import sounddevice as sd

    info = sd.query_devices(device, "input")
    native = int(info["default_samplerate"])
    print(f"设备：{info['name']}   采集 {native}Hz → 16kHz")
    print("开始说话。（每句说完稍停一下，识别会更准）\n")

    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    raw = []
    lost = [0]

    def cb(indata, _frames, _t, status):  # noqa: ANN001
        if status:                       # 仍然要报，但报出来也别慌：见下面统计
            lost[0] += 1
        q.put(indata[:, 0].astype(np.float32).copy())

    def worker() -> None:
        pending = np.zeros(0, dtype=np.float32)
        need = int(native * 0.5)         # 攒 0.5 秒再算
        while not stop.is_set() or not q.empty():
            try:
                block = q.get(timeout=0.2)
            except queue.Empty:
                continue
            pending = np.concatenate([pending, block])
            if len(pending) < need:
                continue
            chunk, pending = pending[:need], pending[need:]
            if agc is not None:
                chunk, g = agc.process(chunk)
            else:
                g = 1.0
            raw.append(chunk)
            if native != A.TARGET_SR:
                chunk = A.resample(chunk, native, A.TARGET_SR)
            asr.push(chunk, A.TARGET_SR)
            for r in asr.tick():
                done[r.idx] = r
            show(done, g, A.rms_dbfs(chunk))

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    t0 = time.time()
    with sd.InputStream(device=device, samplerate=native, channels=1, dtype="float32",
                        callback=cb, blocksize=0):
        while time.time() - t0 < seconds:
            time.sleep(0.2)
    stop.set()
    th.join(timeout=20)
    for r in asr.flush():
        done[r.idx] = r
    show(done, agc.gain if agc else 1.0, -60.0, final=True)
    if lost[0]:
        print(f"\n注意：采集过程报了 {lost[0]} 次 input overflow（音频可能丢了一部分）。"
              f"回调里现在只做拷贝，还报的话说明磁盘/CPU 太忙。")
    if save and raw:
        audio = np.concatenate(raw)
        A.write_wav(save, audio, A.TARGET_SR)
        back, _ = A.read_wav(save)
        print(f"已存 {save}  {A.rms_dbfs(back):.1f} dBFS（已含增益）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--wav", default=None, help="不采麦克风，回放一个 WAV（自测/演示）")
    ap.add_argument("--realtime", action="store_true", help="回放时按真实速度")
    ap.add_argument("--no-agc", action="store_true", help="关闭自动增益")
    ap.add_argument("--save", default=None, help="把（加过增益的）音频存下来")
    ap.add_argument("--no-spk", action="store_true", help="不做声纹（只验识别）")
    args = ap.parse_args()

    asr = StreamingASR(window_s=12.0, guard_s=1.2, want_spk=not args.no_spk)
    t0 = time.time()
    asr.load()
    print(f"模型加载 {time.time()-t0:.1f}s   声纹向量={'可用' if asr.spk_ready else '不可用'}"
          f"{'  (' + asr.spk_error + ')' if asr.spk_error else ''}")
    agc = None if args.no_agc else AutoGain()
    if agc:
        print(f"自动增益：目标 {TARGET_DBFS:.0f} dBFS（按语音电平算，不是整体电平）")
    done: dict[int, FeedSentence] = {}

    if args.wav:
        run_wav(Path(args.wav), asr, agc, done, args.realtime)
    else:
        run_mic(args.seconds, args.device, asr, agc, done,
                Path(args.save) if args.save else None)

    rows = [done[k] for k in sorted(done)]
    print(f"\n{'='*66}\n共 {len(rows)} 句：")
    for r in rows:
        print(f"  [{r.start:6.1f}s] {r.spk or '(无声纹)':<10} {r.text}")
    spk = {}
    for r in rows:
        spk.setdefault(r.spk or "(无)", 0)
        spk[r.spk or "(无)"] += 1
    print(f"声纹簇 {len(spk)} 个：" +
          "  ".join(f"{k}×{v}" for k, v in spk.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
