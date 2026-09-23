"""Synthesise a meeting recording with edge-tts, plus the ground truth for it.

Why a synthetic meeting exists at all
-------------------------------------
This machine has no usable microphone, and even with the phone attached, testing
the *content* path (ASR -> hotspot keywords -> retrieval) needs audio whose words
are known in advance. A real recording cannot tell you whether a missed retrieval
was the recogniser's fault or the retriever's. Here every line is written down
first, spoken by a distinct voice, and the transcripts are then scored against the
script -- so "ASR got 41 of 44 characters right" and "retrieval hit 7 of 9 expected
sources" are measurements rather than impressions.

Content is drawn from the real 20260831 minutes so the queries are representative:
software deployment deadlines, third-party evaluation, VR acceptance, equipment
certificates. Two voices are alternated so speaker-attribution work has something
to chew on.

Writes ``<out>/meeting.ogg`` (16 kHz mono, the pipeline's expected input) and
``<out>/ground-truth.json``.

Usage:
    python scripts/gen_meeting_audio.py [--out DIR] [--rate -10%]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# (speaker label, edge-tts voice, [lines])
# Deadlines and entity names are deliberately kept verbatim from the real minutes:
# those are exactly the tokens whose recognition decides whether retrieval works.
SCRIPT: list[tuple[str, str, list[str]]] = [
    ("甲方-林浩然", "zh-CN-YunyangNeural", [
        "好，咱们现在开始今天的进度汇报例会。",
        "先说时间节点，第三方测评人员九月五号左右进场，这个时间不能再往后退了。",
        "软件部署调通必须在九月五号之前完成，你们公司要尽快确定派谁来。",
    ]),
    ("史塔克-孙磊", "zh-CN-YunxiNeural", [
        "好的陈主任，我们这边已经安排了两位软件开发人员，下周一就能到现场。",
        "铺线这一块我们以机房为重点展开，各房间的网络和电源同步调通。",
        "争取两三天之内把铺线全部完成，为后面的联调联试打好基础。",
    ]),
    ("甲方-林浩然", "zh-CN-YunyangNeural", [
        "还有 VR 体验这块，如果效果不行，要抓紧时间调试，九月十号之前必须完整。",
        "验收前的软件准备工作要先做完，验收大纲不能再等了。",
    ]),
    ("史塔克-孙磊", "zh-CN-YunxiNeural", [
        "明白。已发货的物资我们会把第三方检测报告、合格证、产品说明书都收集齐。",
        "没有发货的物资，我们把合同和采购计划准备好，证明九月二十号之前能到货。",
        "另外现场的每日规划我们也会按时间节点做出来，每天报一次进度。",
    ]),
]


async def _tts_with_retry(text: str, voice: str, rate: str, dest: Path,
                          attempts: int = 4) -> None:
    """Synthesise one line, retrying the transient failures edge-tts throws.

    ``NoAudioReceived`` is raised when the service accepts the request but streams
    nothing back; it happens intermittently on consecutive requests and has nothing
    to do with the text or voice. Observed here on the 6th of 11 lines in one run
    and never on a retry. Without this the whole generation aborts at random.
    """
    import edge_tts

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(dest))
            if dest.exists() and dest.stat().st_size > 500:
                return
            last = RuntimeError(f"empty audio ({dest.stat().st_size if dest.exists() else 0} bytes)")
        except Exception as e:  # noqa: BLE001 - any transport failure is retryable
            last = e
        if attempt < attempts:
            wait = 1.0 * attempt
            print(f"      retry {attempt}/{attempts - 1} after {type(last).__name__}: {last}",
                  flush=True)
            await asyncio.sleep(wait)
    raise RuntimeError(f"TTS failed for {text[:24]!r}: {last}")


async def synth(out_dir: Path, rate: str, gap_s: float) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="meet_tts_"))
    parts: list[Path] = []
    lines: list[dict] = []

    for i, (spk, voice, texts) in enumerate(SCRIPT):
        for j, text in enumerate(texts):
            mp3 = tmp / f"{i:02d}_{j:02d}.mp3"
            await _tts_with_retry(text, voice, rate, mp3)
            parts.append(mp3)
            lines.append({"speaker": spk, "voice": voice, "text": text})
            print(f"  tts {spk:<14} {text[:30]}...", flush=True)
            # A short pause between utterances; without it the VAD sees one long
            # speech run and the merge logic never gets a boundary to work with.
            pause = tmp / f"sil_{i:02d}_{j:02d}.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i",
                 f"anullsrc=r=16000:cl=mono", "-t", str(gap_s), "-c:a", "pcm_s16le", str(pause)],
                capture_output=True, check=True)
            parts.append(pause)

    # Concatenate everything, then encode to the pipeline's input format in one pass.
    listfile = tmp / "list.txt"
    listfile.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8")

    raw = tmp / "joined.wav"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(raw)],
                   capture_output=True, check=True)

    ogg = out_dir / "meeting.ogg"
    subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-ar", "16000", "-ac", "1",
                    "-c:a", "libopus", "-b:a", "32k", str(ogg)],
                   capture_output=True, check=True)

    dur = probe_duration(ogg)
    gt = {
        "audio": str(ogg),
        "duration_s": round(dur, 2),
        "n_utterances": len(lines),
        "speakers": sorted({ln["speaker"] for ln in lines}),
        "utterances": lines,
        "full_text": "".join(ln["text"] for ln in lines),
        # Queries a meeting assistant is expected to answer from the indexed corpus,
        # with the minutes file that should supply each answer.
        "expected_queries": [
            {"q": "第三方测评人员什么时候进场", "expect": "20260907"},
            {"q": "软件部署调通什么时候完成", "expect": "20260907"},
            {"q": "VR 体验什么时候之前要完整", "expect": "20260907"},
            {"q": "已发货的物资要准备哪些材料", "expect": "20260831"},
            {"q": "没发货的物资要准备什么", "expect": "20260831"},
            {"q": "铺线以什么为重点", "expect": "20260831"},
        ],
    }
    (out_dir / "ground-truth.json").write_text(
        json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
    return gt


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    ap.add_argument("--out", default=str(root / "data" / "meeting-audio"))
    ap.add_argument("--rate", default="-8%", help="edge-tts speaking rate")
    ap.add_argument("--gap", type=float, default=0.55, help="pause between utterances (s)")
    args = ap.parse_args()

    print("synthesising meeting audio ...", flush=True)
    gt = asyncio.run(synth(Path(args.out), args.rate, args.gap))
    print()
    print(f"audio      : {gt['audio']}")
    print(f"duration   : {gt['duration_s']}s")
    print(f"utterances : {gt['n_utterances']}  speakers: {', '.join(gt['speakers'])}")
    print(f"chars      : {len(gt['full_text'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
