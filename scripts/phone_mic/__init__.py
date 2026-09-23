"""音频与识别链路：本机麦克风 → 流式 ASR → 段落事件。

Layout::

    sound card (sounddevice) --> mic_source.LocalMicReceiver --> ASR
                                                                    |
                                                  FeedSentence --> on_segment
                                                                    |
                                                     transcript / retrieval JSON

`audio` 负责采样率与格式换算（一律 16 kHz 单声道 float32），`streaming` /
`stream_asr` / `firered_asr` 是三个可切换的流式识别后端，`hotwords` 是文本级热词纠错。
包名是历史遗留（这里曾经是手机当麦克风那条通道），内容为所有输入源共用。
"""

from .audio import TARGET_SR, float_to_pcm_bytes, pcm_bytes_to_float, read_wav, resample, write_wav  # noqa: F401
from .streaming import FeedSentence, StreamingASR  # noqa: F401

__all__ = [
    "TARGET_SR",
    "FeedSentence",
    "StreamingASR",
    "float_to_pcm_bytes",
    "pcm_bytes_to_float",
    "read_wav",
    "resample",
    "write_wav",
]
