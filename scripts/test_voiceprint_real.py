"""CAM++ 声纹：**用真实会议录音**验证归簇和跨会匹配。

不用合成向量：合成向量只能证明"余弦相似度算对了"，证明不了"真声音能被分开"。
这里用的是 data/phone-pull/phone_rec_16k.wav——一场真实开标会的录音，
256 秒，至少两个人。实测过的相似度是这个量级：

    同一人相邻两段 0.72 / 0.81      不同人之间 0.08 ~ 0.22

所以这个测试要验的是三件事：

1. **同一个人的两段进同一簇**（换麦克风/离得远近也要进同一簇）；
2. **不同人的两段进不同簇**（不能把整场会都算成一个人）；
3. 登记进声纹库之后，**同一人的新录音能被认出来**，而陌生人不会被认错。

需要 CAM++ 模型（`iic/speech_campplus_sv_zh-cn_16k-common`）。没装就跳过而不是失败——
"模型不在"不是代码错。但跳过时必须**说清楚跳过了**，否则会变成一句永远绿的假断言。

Usage:  python scripts/test_voiceprint_real.py [wav]
"""

from __future__ import annotations

import os
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

os.environ.setdefault("MODELSCOPE_CACHE", r"E:\models\gguf-asr\.cache\modelscope")

import numpy as np  # noqa: E402

from meeting.voiceprints import VoiceprintStore  # noqa: E402
from phone_mic.streaming import SPK_CLUSTER_T, StreamingASR  # noqa: E402

WAV = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    HERE.parent / "data" / "phone-pull" / "phone_rec_16k.wav"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def read_wav(path: Path):
    with wave.open(str(path)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return x.astype(np.float32) / 32768.0, sr


def main() -> int:
    print("CAM++ 声纹：真实录音验证")
    if not WAV.is_file():
        print(f"  [跳过] 找不到真实录音 {WAV}")
        print("         这个测试要真实音频才有意义，合成向量证明不了任何事。")
        return 0

    asr = StreamingASR(want_spk=True)
    try:
        asr.load()
    except Exception as e:  # noqa: BLE001
        print(f"  [跳过] 模型加载失败：{type(e).__name__}: {e}")
        return 0
    if not asr.spk_ready or asr._sv is None:
        print(f"  [跳过] CAM++ 不可用：{asr.spk_error or '未加载'}")
        print("         装它的命令：snapshot_download('iic/speech_campplus_sv_zh-cn_16k-common')")
        return 0

    x, sr = read_wav(WAV)
    dur = len(x) / sr
    print(f"  音频 {WAV.name}  {dur:.1f}s @ {sr}Hz")

    # 四段：按前面实测，1&2 是同一人，3&4 是另一个人（首尾相隔很远）
    segs = [(1.0, 6.0), (6.5, 11.5), (25.0, 31.0), (40.0, 46.0)]
    embs = []
    for a, b in segs:
        seg = x[int(a * sr):int(b * sr)]
        try:
            r = asr._sv.generate(input=seg, cache={}, disable_pbar=True)
        except Exception as e:  # noqa: BLE001
            check(f"取向量 {a}-{b}s", False, f"{type(e).__name__}: {e}")
            continue
        r = r[0] if isinstance(r, list) else r
        v = r["spk_embedding"]
        v = v.cpu().numpy().reshape(-1) if hasattr(v, "cpu") else np.asarray(v).reshape(-1)
        embs.append(v)

    check("四段都拿到了向量", len(embs) == 4, f"{len(embs)} 段")
    if len(embs) != 4:
        return 1
    check("向量维度是 192（CAM++）", embs[0].shape == (192,), str(embs[0].shape))

    def cos(p, q):
        return float(np.dot(p, q) / (np.linalg.norm(p) * np.linalg.norm(q)))

    same = [cos(embs[0], embs[1]), cos(embs[2], embs[3])]
    diff = [cos(embs[0], embs[2]), cos(embs[0], embs[3]),
            cos(embs[1], embs[2]), cos(embs[1], embs[3])]
    print(f"  同一人：{['%.3f' % v for v in same]}")
    print(f"  不同人：{['%.3f' % v for v in diff]}")
    check("同一个人明显更像自己", min(same) > max(diff) + 0.2,
          f"同人最低 {min(same):.3f}，异人最高 {max(diff):.3f}")
    check(f"阈值 {SPK_CLUSTER_T} 落在两者之间",
          max(diff) < SPK_CLUSTER_T < min(same),
          f"异人最高 {max(diff):.3f} < {SPK_CLUSTER_T} < 同人最低 {min(same):.3f}")

    # ── 归簇：把四段交给真正的归簇逻辑 ────────────────────────────────
    asr._spk.reset()
    asr._buf = x[:int(60 * sr)]
    asr._buf_start = 0.0
    labels = []
    for i, (a, b) in enumerate(segs):
        # _embed_and_label 现在返回 (向量, 声纹id, meta)：meta 里写明这段音频用了多少、
        # 借了多少静音、有没有被缓冲截断。声纹 id 为空表示"音频不足，别拿它开新簇"。
        vec, sid, meta = asr._embed_and_label(a, b, i)
        labels.append(sid)
        check(f"第 {i+1} 段拿到可信向量", bool(sid),
              f"sid={sid!r} used={meta.get('used_s', 0):.2f}s "
              f"borrowed={meta.get('borrowed_s', 0):.2f}s reason={meta.get('reason', '')}")
    print(f"  归簇结果：{labels}")
    check("同一个人归到同一簇", labels[0] == labels[1] and labels[2] == labels[3],
          str(labels))
    check("不同人分成不同簇", labels[0] != labels[2], str(labels))
    check("簇号是匿名的（不是 spk_ 前缀）",
          all(s.startswith("匿名-") for s in labels), str(labels))

    # ── 跨会：登记第 1 段，用第 2 段去认 ──────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        store = VoiceprintStore(Path(td) / "vp.json")
        store.enroll("p1", "林浩然", [float(v) for v in embs[0]])
        store.save()
        hit = store.match([float(v) for v in embs[1]])
        miss = store.match([float(v) for v in embs[2]])
        print(f"  库里认同一个人：{hit['name']} {hit['score']:.3f} ok={hit['ok']}")
        print(f"  库里遇到陌生人：score={miss['score']:.3f} ok={miss['ok']} "
              f"({miss['reason']})")
        check("同一个人的新录音被认出", hit["ok"] and hit["name"] == "林浩然", str(hit))
        check("陌生人没有被认成他", not miss["ok"], str(miss))

        # 没听过的声音不该因为"库里有一个人"就被硬认成那个人
        store.enroll("p2", "徐博文", [float(v) for v in embs[2]])
        both = store.match([float(v) for v in embs[1]])
        check("库里有两个人时仍然认得对", both["name"] == "林浩然", str(both))

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
