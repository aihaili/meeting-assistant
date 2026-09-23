"""无头冒烟测试：验证启动器 Backend 的子进程编排（拉起 meeting.server → 健康 → 停止）。

不依赖 GUI。用 audio=none（--no-asr）避免加载 ASR 模型，快速验证编排链路
（子进程启动 / 健康轮询 / 端点 / 停止清理）。

    python launcher/smoke.py
"""
from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from app import Backend, _resolve_root, DEFAULT_PORT  # noqa: E402


def main() -> int:
    root = _resolve_root()
    b = Backend(root)
    port = DEFAULT_PORT
    print(f"root  = {root}")
    print(f"venv  = {b.venv_py}  (exists={b.venv_py.exists()})")
    if not b.venv_py.exists():
        print("FAIL: venv python 未找到")
        return 1

    st = b.start({"engine": "funasr", "llm": True, "audio": "none", "port": port})
    print(f"after start: state={st['state']} pid={st['pid']}")

    ok = False
    for i in range(60):
        time.sleep(1)
        st = b.status()
        print(f"[{i + 1:02d}] state={st['state']} healthy={st['healthy']} alive={st['alive']}")
        if st["state"] == "running" and st["healthy"]:
            ok = True
            break
        if st["state"] == "stopped" and st["error"]:
            print("ERROR:", st["error"])
            break

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats", timeout=3) as r:
            body = r.read().decode("utf-8", "replace")
        print(f"GET /api/stats -> {r.status}  {body[:160]}")
    except Exception as e:  # noqa: BLE001
        print("endpoint check failed:", e)

    b.stop()
    time.sleep(1)
    st = b.status()
    print(f"after stop: state={st['state']} alive={st['alive']}")
    passed = ok and not st["alive"]
    print("RESULT:", "PASS" if passed else "CHECK")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
