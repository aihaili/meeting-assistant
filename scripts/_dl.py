import os, sys, time
t0 = time.time()
ok = False
# 先试 ModelScope（funasr 的 spk_model="cam++" 默认走这里）
try:
    from modelscope.hub.snapshot_download import snapshot_download
    p = snapshot_download("iic/speech_campplus_sv_zh-cn_16k-common")
    print("ModelScope OK ->", p)
    ok = True
except Exception as e:
    print("ModelScope 失败:", type(e).__name__, str(e)[:300])
if not ok:
    # 兜底：HF 镜像（有的模型在 HF 上也有镜像仓库）
    try:
        from huggingface_hub import snapshot_download as hf
        p = hf("iic/speech_campplus_sv_zh-cn_16k-common")
        print("HF OK ->", p)
        ok = True
    except Exception as e:
        print("HF 也失败:", type(e).__name__, str(e)[:300])
print(f"用时 {time.time()-t0:.1f}s  ok={ok}")
