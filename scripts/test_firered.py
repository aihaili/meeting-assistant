"""FireRedASR 后端独立测试：200ms 块喂真实录音，验证文本/标点/说话人/时间戳。

用法（scripts/ 目录下）：
    ..\\venv\\Scripts\\python.exe data\\test_firered.py [wav路径]
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import soundfile as sf  # noqa: E402

from phone_mic.firered_asr import FireRedASR  # noqa: E402

wav_path = Path(sys.argv[1]) if len(sys.argv) > 1 \
    else ROOT / "data" / "e2e" / "meeting_16k.wav"
wav, sr = sf.read(str(wav_path), dtype="float32")
dur = len(wav) / sr
print(f"音频: {wav_path.name} {dur:.1f}s sr={sr}", flush=True)

eng = FireRedASR(want_spk=True)
t0 = time.time()
eng.load()
print(f"加载: {eng.load_s:.1f}s  spk_ready={eng.spk_ready} "
      f"spk_error={eng.spk_error!r}", flush=True)

t0 = time.time()
rows: dict[int, object] = {}
CH = 3200  # 200ms @16k
for i in range(0, len(wav), CH):
    eng.push(wav[i:i + CH], sr)
    for r in eng.tick():
        rows[r.idx] = r
for r in eng.flush():
    rows[r.idx] = r
el = time.time() - t0
print(f"解码: {el:.1f}s（音频 {dur:.1f}s，RTF {el / dur:.3f}）", flush=True)
print(f"stats: {eng.stats()}", flush=True)
total_chars = 0
for idx in sorted(rows):
    r = rows[idx]
    total_chars += len(r.text)
    print(f"[{idx}] open={r.open} spk={r.spk!r} rev={r.revisions} "
          f"{r.start:7.2f}-{r.end:7.2f}  {r.text[:80]}", flush=True)
print(f"共 {len(rows)} 行 / {total_chars} 字", flush=True)
assert rows, "没有任何识别结果"
assert all(r.text.strip() for r in rows.values()), "有空文本行"
print("PASS", flush=True)
