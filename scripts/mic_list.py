# 列设备的独立小工具（不用记 sounddevice 的 API）
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from phone_mic.mic_source import list_input_devices

for d in list_input_devices():
    print(f"  [{d['index']:2d}] {d['name'][:52]:<52} {d['sr']}Hz  {d['channels']}ch")
