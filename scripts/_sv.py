import os, sys, time
sys.path.insert(0, "scripts")
os.environ.setdefault("MODELSCOPE_CACHE", r"E:\models\gguf-asr\.cache\modelscope")
import numpy as np, wave
from funasr import AutoModel

with wave.open(r"data\phone-pull\phone_rec_16k.wav") as w:
    sr = w.getframerate(); x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
x = x.astype(np.float32) / 32768.0
print(f"音频 {len(x)/sr:.1f}s @ {sr}Hz")

sv = AutoModel(model="cam++", disable_update=True, device="cuda:0")
def emb(a, b):
    r = sv.generate(input=x[int(a*sr):int(b*sr)], cache={}, disable_pbar=True)
    r = r[0] if isinstance(r, list) else r
    print("  返回字段:", list(r.keys()) if isinstance(r, dict) else type(r))
    v = r.get("spk_embedding")
    v = v.cpu().numpy().reshape(-1) if hasattr(v, "cpu") else np.asarray(v).reshape(-1)
    return v

segs = [(1.0, 6.0), (6.5, 11.5), (25.0, 31.0), (40.0, 46.0)]
E = [emb(a, b) for a, b in segs]
print("维度:", E[0].shape)
def cos(p, q): return float(np.dot(p, q) / (np.linalg.norm(p) * np.linalg.norm(q)))
print("\n两两相似度矩阵：")
for i in range(len(E)):
    print("  " + " ".join(f"{cos(E[i], E[j]):+.3f}" for j in range(len(E))))
