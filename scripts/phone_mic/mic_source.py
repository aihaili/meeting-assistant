"""本机麦克风输入源：让 Web 界面也能从电脑的麦克风实时出字。

音频来自声卡：采集 → ASR → 发布给 `MeetingService`。发布出去之后，检索、线索分类、
声纹、界面全都自动跟上——因为那些逻辑挂在 `on_segment` 上，和音频从哪来无关。
`mic_live.py` 那个控制台工具只往终端打字，不喂服务器，两者是分开的。

另外这里也提供文件回放（`replay_wav`）：把一段 WAV 当成麦克风输入喂进同一条链路，
好在没有可用麦克风的机器上把页面整个跑一遍。

两条硬要求，都是踩过的：

* **声音回调里只许拷贝。** 把识别放进回调会让回调跟不上，PortAudio 报 `input overflow`，
  而 overflow 意味着那段音频真的丢了。所以回调 → 队列 → 工作线程。
* **攒够 0.5 秒再处理。** 按一小块一小块去调 scipy 重采样，光调用开销就够拖垮回调。

另外带自动增益：用户的 USB 麦克风增益调到顶也只有 -46 dBFS，
而 ASR 模型大致按 -20 dBFS 校准，绝对电平对识别率的影响比信噪比更直接。
软件放大不增加信息（噪声一起放大），但 16 位量化噪声在 -96 dBFS，够用。
"""

from __future__ import annotations

from dataclasses import replace

import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import audio as A
from .stream_asr import StreamASR   # 官方流式增量解码（见该模块说明）
from .streaming import FeedSentence

TARGET_DBFS = -20.0
# 增益封顶。原来给到 40x，实测在安静房间里会自动跑到 30~38x——
# 那等于把底噪放大 30 倍再喂给 VAD 和 ASR。**放大会让识别在窗口之间变得不稳定**，
# 而不稳定的文本会让"同一句话的两次转写"匹配不上，于是被当成两句——这正是碎片化的成因之一。
# 所以封到 15x，并且加噪声门。
MAX_GAIN = 15.0
# 噪声门：低于这个电平（相对当前语音包络）不放大，静音段就保持静音。
GATE_RATIO = 0.18
# 绝对门限：低于这个电平（-50 dBFS）一律按 1:1 过去，**不放大**。
# 只有相对门限是不够的——没人说话时"语音包络"就是噪声本身，于是噪声被放大 12 倍
# 顶到 -20 dBFS，ASR 拿它当语音解，开头就是一大段错字（用户报过这一条）。
# 实测也撞过：277 秒纯噪声被泵成一堵 -28 dBFS 的声墙。
ABS_FLOOR = 10 ** (-50.0 / 20.0)


class AutoGain:
    """按**语音电平**算增益，不是整体电平。

    一次录几十秒，里面大量停顿；用整体 RMS 会把增益算高，一说话就削顶。
    跟踪"较响的那部分"：上升快、下降慢；再对增益本身平滑，免得每个字忽大忽小。
    """

    def __init__(self, target_dbfs: float = TARGET_DBFS, max_gain: float = MAX_GAIN):
        self.target = 10 ** (target_dbfs / 20.0)
        self.max_gain = max_gain
        self.env = 0.0
        self.gain = 1.0
        self.peak_in = 0.0
        # 峰值包络。只看 RMS 会出事：说话的中位电平才 -32.7 dBFS，可一声爆音或
        # 手机贴近了就是满量程——实测采到过 峰值 1.000（已经削顶了）。
        # 削顶是**不可逆的失真**，ASR 直接受影响，所以增益还要被峰值压住。
        self.peak_env = 0.0
        self.peak_target = 0.9

    def process(self, block: np.ndarray) -> tuple[np.ndarray, float]:
        if block.size:
            self.peak_in = max(self.peak_in, float(np.abs(block).max()))
        self.last_block_gain = 1.0
        rms = float(np.sqrt(np.mean(np.square(block.astype(np.float64))))) if block.size else 0.0
        pk = float(np.abs(block).max()) if block.size else 0.0
        # 峰值包络衰减得比上升慢：瞬时尖峰过去之后，增益不要马上弹回去
        self.peak_env = max(pk, self.peak_env * 0.995)
        a = 0.35 if rms > self.env else 0.06
        self.env = (1 - a) * self.env + a * rms
        want = 1.0
        if self.env > 1e-6:
            want = max(1.0, min(self.max_gain, self.target / self.env))
        self.gain = 0.85 * self.gain + 0.15 * want
        # 噪声门：这一块明显低于语音包络时，按 1:1 过去（不放大）。
        # 不这样做的后果是把整段静音的底噪也抬到 -20 dBFS，VAD 会一直在"有人说话"和
        # "没人说话"之间摇摆，切出大量碎段。
        silent_abs = rms < ABS_FLOOR          # 绝对意义上的"没声音"
        g = 1.0 if silent_abs else (
            self.gain if (self.env <= 0 or rms >= self.env * GATE_RATIO) else 1.0)
        # 峰值感知：把增益压到"最响的那一下也不超过 0.9"
        if self.peak_env > 1e-6:
            g = min(g, self.peak_target / self.peak_env)
        g = max(1.0, g)
        out = block * g
        if out.size and float(np.abs(out).max()) > 1.0:
            out = np.tanh(out)
        return out.astype(np.float32), self.gain


class LocalMicReceiver:
    """声卡 → ASR → on_segment：采集、落盘、编号、发布都在这里。"""

    def __init__(self, device: int | None = None, agc: bool = True,
                 want_spk: bool = True,
                 engine: str = "funasr", firered_dir: str | Path | None = None) -> None:
        self.device = device
        # 识别后端可切换（settings.json 的 asr.engine）：
        # * funasr（默认）——官方流式模型。实测同一段录音：45 秒的发言从 12 行
        #   （含 2 处重复）变成 1 行，和微信文本的分歧率 39.1% → 27.2%。
        #   而且下游是按行触发线索分类的，行数减少直接等于 LLM 调用减少约 10 倍。
        # * firered——FireRedASR2S（AED 识别 + 流式 VAD + BERT 标点），
        #   公开基准 CER 更低、标点 F1 更高；接口与 StreamASR 完全一致，
        #   所以换后端只换这一行，采集/发布/声纹/界面全都不用动。
        if engine == "firered":
            from .firered_asr import FireRedASR
            self.asr = FireRedASR(want_spk=want_spk,
                                  model_root=firered_dir or r"E:\WhisperX\FireRedASR2S")
        else:
            self.asr = StreamASR(want_spk=want_spk)
        self.agc = AutoGain() if agc else None
        self.on_segment = None            # 由 MeetingService 挂上
        self.on_audio_file = None
        self.device_name = ""
        self.overflows = 0
        self.published = 0
        self.load_ms = 0.0
        self.started_at: float | None = None
        self.last_error = ""
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._raw: list[np.ndarray] = []
        # 存盘路径。**边录边落盘，而且写成裸 PCM**：
        #
        # 原来是先在内存里攒、停止时才写 WAV。问题有两个，都是实测撞上的：
        #   · 我排查时是**强杀**进程的，强杀不会走 stop()，于是"最需要那份录音"的时刻
        #     文件根本不存在；
        #   · WAV 的文件头需要收尾时回填长度，被强杀就留下一个长度写着 0 的文件。
        # 裸 PCM 没有头，随时被杀都留下完整的采样，读的时候按 int16/16k 单声道解释即可。
        self.save_path: Path | None = None
        self.saved_bytes = 0
        self.save_dir: Path | None = None      # 由界面按钮控制时，每段录音各自一个文件
        self.take_no = 0
        self.last_take: dict | None = None     # 上一次录完的结果（时长/文件/电平）
        # **idx 必须重新编号**。ASR 的行号从 0 开始，而会话里可能已经有
        # 0..N 的发言；而 MeetingService 是**按 idx 更新**的——于是新录的话会把旧发言
        # 一条条覆盖掉（实测：讲了 14.5 秒，29 段全写到了已有行上，界面上一条新的都不出现，
        # 旧内容还被改掉了）。所以对外的 idx 用会话里唯一的号，内部映射保持稳定：
        # 同一个 ASR 行号永远映射到同一个会话行号，修正仍然更新同一行。
        self.idx_seed = 0                      # 由服务端在开始录音前设成"会话最大 idx + 1"
        # **时间戳也要平移**。ASR 给的 start/end 是"从这段音频开头算起"的相对秒数，
        # 而会议已经进行到几百秒了。不平移的话，新录的话会被插到时间轴**最前面**
        # （实测：0.09s / 6.81s / 10.33s 混进已有发言中间），用户盯着底部永远看不到，
        # 还会以为"录了没反应"。所以对外的时间 = 录音开始时的会议时刻 + 相对秒数。
        self.time_offset = 0.0                 # 由服务端在开始录音前设成"会议已进行的秒数"
        self._idx_map: dict[int, int] = {}
        self._next_seed = 0
        self._pcm_path: Path | None = None
        self.running = False
        # 文件回放进行中？（replay_wav 用：同一时刻只允许一路回放）
        self._replaying = False
        # 模型加载的完成信号。启动时后台预加载、用户点录音时这里等它——
        # **不能各加载一遍**：撞在一起会两次构建模型，GPU 上直接卡死（实测超时）。
        self._load_evt = threading.Event()

    # ── lifecycle ───────────────────────────────────────────────────────

    def start(self, load_models: bool = True) -> Path | None:
        """开始采集。**可以反复调用**：界面上的"录音"按钮就是靠它。

        每次开始新起一个文件（`mic-<序号>.pcm`），停止时收成同名 WAV——
        这样"录一段、停下来、拿去分析"是一个干净的循环，
        而不是"服务器一启动就在录、录到什么时候由别人决定"
        （用户的原话：「没有一个我可以让你告诉它什么时候停止录音的按钮」）。
        """
        if self.running:
            return self._pcm_path
        if self.save_dir is not None:
            self.take_no += 1
            self.save_dir.mkdir(parents=True, exist_ok=True)
            # 带时间戳：重启服务器后序号会从头开始，只用序号会**覆盖**上一次录音
            # （实测 mic-001.pcm 被两次录音共用）。
            stamp = time.strftime("%m%d-%H%M%S")
            self._pcm_path = self.save_dir / f"mic-{stamp}-{self.take_no:02d}.pcm"
            self.save_path = self._pcm_path
            self.saved_bytes = 0
        # 清掉上一次录音留下的流状态（cache/文本缓冲/声纹簇）。
        # 不清的话第二次录音会黏着第一次的内容（写测试时发现的）。
        if hasattr(self.asr, "reset"):
            self.asr.reset()
        self._stop = threading.Event()
        self.running = True
        self._next_seed = self.idx_seed
        self._idx_map = {}
        self._start_impl(load_models)
        return self._pcm_path

    def _start_impl(self, load_models: bool = True) -> None:
        # 已经加载过就跳过：模型加载要 20 多秒，放在"点录音"的那次请求里会让按钮
        # 二十秒没反应（实测用户以为没按到，于是要按两次）。所以改成启动时后台预加载。
        if load_models:
            if self.asr.loaded:
                pass                                  # 已经好了
            elif self._load_evt.is_set():
                pass                                  # 别处刚加载完
            else:
                # 没有别人在加载就自己加载；有的话等它（start 应当立刻返回）
                if not getattr(self, "_loading", False):
                    self._loading = True
                    try:
                        t0 = time.time()
                        self.asr.load()
                        self.load_ms = (time.time() - t0) * 1000
                    finally:
                        self._loading = False
                        self._load_evt.set()
                else:
                    self._load_evt.wait(timeout=180)
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> dict | None:
        """停止采集，并把这一段的 PCM 收成 WAV。返回这一段的摘要。

        收成 WAV 是刻意的：PCM 是给"被强杀也不丢"用的，但人要看、要听、要拿去分析
        都得是 WAV。停止时转换一次，两边的好处都拿到。
        """
        if not self.running:
            return self.last_take
        self._stop.set()
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=20)
            self._thread = None
        self._flush()
        path = self._pcm_path
        if path is not None and path.is_file():
            try:
                raw = path.read_bytes()
                x = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2") \
                    .astype(np.float32) / 32768.0
                wav = path.with_suffix(".wav")
                A.write_wav(wav, x, A.TARGET_SR)
                self.last_take = {
                    "wav": str(wav), "seconds": round(len(x) / A.TARGET_SR, 1),
                    "dbfs": round(A.rms_dbfs(x), 1),
                    "peak": round(float(np.abs(x).max()), 3) if x.size else 0.0,
                    "segments": self.published,
                }
                print(f"[mic] 这一段落盘 {wav.name}  "
                      f"{self.last_take['seconds']}s  {self.last_take['dbfs']} dBFS  "
                      f"峰值 {self.last_take['peak']}", flush=True)
            except Exception as e:  # noqa: BLE001
                self.last_error = f"收尾失败：{e}"
        return self.last_take
        # （音频已经边录边落盘，这里不用再做事）

    def _flush(self) -> None:
        for r in self.asr.flush():
            self._publish(r)

    def prepare(self, base: int, elapsed: float = 0.0) -> None:
        """开始录音前由服务端调用。

        ``base``：会话里还没被占用的最小 idx（否则新发言会覆盖已有行）。
        ``elapsed``：会议已经进行了多少秒（否则新发言会被插到时间轴最前面）。
        """
        self.idx_seed = int(base)
        self._next_seed = int(base)
        self._idx_map = {}
        self.time_offset = float(elapsed)

    def _publish(self, r: FeedSentence) -> None:
        if self.on_segment is not None:
            try:
                sid = self._idx_map.get(r.idx)
                if sid is None:
                    sid = self._next_seed
                    self._next_seed += 1
                    self._idx_map[r.idx] = sid
                self.on_segment(replace(
                    r, idx=sid,
                    start=r.start + self.time_offset,
                    end=r.end + self.time_offset))
                self.published += 1
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
                print(f"[mic] 发布失败：{self.last_error}", file=sys.stderr, flush=True)

    # ── capture loop ────────────────────────────────────────────────────

    def _run(self) -> None:
        import sounddevice as sd

        try:
            info = sd.query_devices(self.device, "input")
        except Exception as e:  # noqa: BLE001
            self.last_error = f"打不开麦克风：{e}"
            print(f"[mic] {self.last_error}", file=sys.stderr, flush=True)
            return
        self.device_name = info["name"]
        # **优先直接以 16000 打开**（实测这只 USB 麦克风支持）。
        # 让系统去做采样率转换，比我自己按 0.5 秒一块重采样干净得多：
        # 逐块重采样会在每个块边界留下滤波器瞬态（实测跳变 0.002，虽然不大，
        # 但没有任何理由留着它），而且少一段代码就少一个出错的地方。
        native = 16000
        try:
            import sounddevice as _sd
            _sd.check_input_settings(device=self.device, samplerate=16000,
                                     channels=1, dtype="float32")
        except Exception:  # noqa: BLE001
            native = int(info["default_samplerate"])
            print(f"[mic] 设备不支持 16000Hz，改用 {native}Hz 采集后重采样",
                  file=sys.stderr, flush=True)
        print(f"[mic] 设备「{self.device_name}」{native}Hz"
              f"{' → 16kHz' if native != 16000 else '（原生 16kHz，无需重采样）'}"
              f"  声纹={'开' if self.asr.spk_ready or self.asr.want_spk else '关'}"
              f"  自动增益={'开' if self.agc else '关'}", flush=True)

        def cb(indata, _frames, _t, status):  # noqa: ANN001
            # 这里**只许拷贝**。任何重活都会让回调跟不上 → overflow → 音频真的丢。
            if status:
                self.overflows += 1
            self._q.put(indata[:, 0].astype(np.float32).copy())

        sink = None
        if self.save_path is not None:
            try:
                self.save_path.parent.mkdir(parents=True, exist_ok=True)
                sink = open(self.save_path, "wb")      # 裸 PCM，追加写
                print(f"[mic] 音频实时落盘 -> {self.save_path.name}（裸 PCM int16/16k）",
                      flush=True)
            except OSError as e:
                print(f"[mic] 打不开存盘文件：{e}", file=sys.stderr, flush=True)
                sink = None
        try:
            with sd.InputStream(device=self.device, samplerate=native, channels=1,
                                dtype="float32", callback=cb, blocksize=0):
                pending = np.zeros(0, dtype=np.float32)
                need = int(native * 0.5)
                last_tick = time.time()
                while not self._stop.is_set():
                    try:
                        block = self._q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    pending = np.concatenate([pending, block])
                    if len(pending) < need:
                        continue
                    chunk, pending = pending[:need], pending[need:]
                    if self.agc is not None:
                        chunk, _g = self.agc.process(chunk)
                    self._raw.append(chunk)
                    if len(self._raw) > 240:           # 只留最近 2 分钟给电平显示
                        del self._raw[:60]
                    if sink is not None:
                        try:
                            sink.write(A.float_to_pcm_bytes(chunk))
                            sink.flush()               # 随时被杀都不丢
                            self.saved_bytes += chunk.size * 2
                        except OSError as e:
                            print(f"[mic] 写盘失败：{e}", file=sys.stderr, flush=True)
                            sink = None
                    if native != A.TARGET_SR:
                        chunk = A.resample(chunk, native, A.TARGET_SR)
                    self.asr.push(chunk, A.TARGET_SR)
                    for r in self.asr.tick():
                        self._publish(r)
                self._flush()
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
            print(f"[mic] 采集线程结束：{self.last_error}", file=sys.stderr, flush=True)
        finally:
            if sink is not None:
                try:
                    sink.close()
                except OSError:
                    pass
                print(f"[mic] 落盘结束：{self.saved_bytes/2/16000:.1f}s 音频", flush=True)

    # ── status (Web 界面上的"音频"指示) ──────────────────────────────────

    def status(self) -> dict:
        return {
            "session": {
                "source": "本机麦克风" + (f"：{self.device_name}" if self.device_name else ""),
                "opened_at": self.started_at,
                "chunks": self.published,
                "overflows": self.overflows,
            },
            "asr": {"loaded": bool(self.asr.loaded),
                    # 加载中（后台预加载线程正在加载模型）：True=还在加载，
                    # False+loaded=False=加载失败，False+loaded=True=就绪。
                    # 界面用它禁用录音按钮，避免用户在模型没加载完时说话丢语音。
                    "loading": bool(getattr(self, "_loading", False)),
                    "load_ms": round(self.load_ms, 1),
                    "spk": self.asr.spk_ready, "spk_error": self.asr.spk_error,
                    "gain": round(self.agc.gain, 2) if self.agc else 1.0,
                    # 采集到的峰值：接近 1.0 说明已经削顶（不可逆失真），
                    # 用户从界面上就能看出"麦克风太冲了"，不用等我分析音频才发现。
                    "peak_in": (round(self.agc.peak_in, 3) if self.agc else None),
                    "level_dbfs": (round(A.rms_dbfs(self._raw[-1]), 1)
                                   if self._raw else None)},
            "resampler": A.resample_used(),
            "tick_error": self.last_error,
        }

    def save(self, path: Path) -> Path | None:
        if not self._raw:
            return None
        return A.write_wav(path, np.concatenate(self._raw), A.TARGET_SR)

    # ── 文件回放（没有麦克风也能把整条链路跑起来）─────────────────────────

    def replay_wav(self, path: str | Path, speed: float = 1.0,
                   chunk_s: float = 0.5) -> dict:
        """把一段 WAV 当成麦克风输入喂进识别链路（界面右上角「试听回放」走这里）。

        走的是和采集线程**同一条**发布路径（``self.asr`` → ``_publish``），
        所以检索、线索分类、声纹、界面全都一样，区别只是音频来自文件而不是声卡。
        不碰声卡、也不置 ``running``；真的在录时直接拒绝，免得两条流搅在一起。

        调用方（server 的 /api/audio/test）负责先 ``prepare(base, tail)``：
        不重新编号的话，回放出来的发言会从 idx 0 开始，把会话里已有的行覆盖掉。
        """
        if self.running:
            raise RuntimeError("正在录音：先停止录音再回放文件")
        if self._replaying:
            # 两次回放会同时往同一个 asr 里喂音频、同时改 idx/时间戳映射，
            # 结果是一堆互相覆盖的发言。以前只挡"正在录音"，挡不住"正在回放"。
            raise RuntimeError("已经在回放文件了：等这一次放完再试")
        self._replaying = True
        try:
            x, sr = A.read_wav(str(path))
            x = np.ascontiguousarray(x, dtype=np.float32)
            if sr != A.TARGET_SR:
                x = A.resample(x, sr, A.TARGET_SR)
            if not self.asr.loaded:
                self.asr.load()
            step = max(1, int(A.TARGET_SR * chunk_s))
            t0 = time.time()
            for i in range(0, x.size, step):
                self.asr.push(x[i:i + step], A.TARGET_SR)
                for r in self.asr.tick():
                    self._publish(r)
                # 按 speed 倍速推进：1.0 时和真的对着麦克风说话一样快，
                # 界面上看到的时间轴也就和录音时一致。
                want = (i + step) / A.TARGET_SR / max(0.01, float(speed))
                slack = want - (time.time() - t0)
                if slack > 0:
                    time.sleep(min(slack, 1.0))
            for r in self.asr.flush():
                self._publish(r)
            return {"seconds": round(x.size / A.TARGET_SR, 1),
                    "wall_s": round(time.time() - t0, 1),
                    "segments": self.published}
        finally:
            self._replaying = False


def list_input_devices() -> list[dict]:
    import sounddevice as sd

    out = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) >= 1:
            out.append({"index": i, "name": d["name"],
                        "channels": d["max_input_channels"],
                        "sr": int(d["default_samplerate"])})
    return out
