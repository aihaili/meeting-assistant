"""Generate long-utterance meeting audio: the case the sliding window may not survive.

The existing synthetic meeting is eleven short sentences of 4-8 seconds each, which is
not what a real meeting sounds like. In practice one person talks for half a minute
without stopping, and that is structurally different for a sliding-window recogniser:

* **A sentence longer than the window cannot be transcribed whole.** The buffer is
  capped at ``window_s`` (20 s by default), so once the utterance exceeds it the head is
  dropped and the row can never contain the beginning of what was said.
* **The guard band delays the commit indefinitely.** A row only settles once the window
  has advanced past its end by ``guard_s``, so a 40 s monologue keeps being rewritten for
  its whole duration.
* **The clue board may see only fragments.** If the recognised text is per-window
  fragments, deadlines and requirements in the discarded head are never classified.

So this script builds the adversarial case on purpose: a few long monologues, one
speaker per paragraph, minimal pauses inside each paragraph. Runs are then compared by
how much of the source text survives recognition -- a comparison that needs ground
truth, which is why the text is generated rather than recorded.

Usage:
    python scripts/gen_long_meeting.py [--out DIR] [--rate -12%] [--pause 0.35]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Four monologues. Each is one speaker holding the floor for roughly 30-50 seconds of
# speech, with commas rather than full stops inside so the VAD has few chances to break
# it up -- the pauses that would let a naive implementation off the hook are removed on
# purpose. Content is drawn from the real project so the retrieval queries stay
# meaningful (deadlines, VR acceptance, equipment certificates, staffing).
MONOLOGUES: list[tuple[str, str, list[str]]] = [
    ("甲方-林浩然", "zh-CN-YunyangNeural", [
        "关于这个时间节点的问题我要再强调一遍，第三方测评人员九月五号左右就要进场，"
        "这个时间不是我们定的，是上级机关定的，所以没有任何往后推的余地，",
        "你们公司必须在这之前把软件部署调通，而且要明确派谁来，来几个人，待多久，"
        "这些事情不能等到测评人员站在门口了才说，那样我们双方都很被动，",
        "我再说一遍，九月五号之前软件必须调通，九月十号之前 VR 体验必须完整，"
        "这两个节点是硬指标，完不成的话后面整个验收计划都要往后拖，",
    ]),
    ("史塔克-孙磊", "zh-CN-YunxiNeural", [
        "好的陈主任，我先把我们这边的安排汇报一下，软件开发人员我们已经确定了两位，"
        "一位负责软件联调联试，一位负责配合第三方测评，下周一就能到现场，",
        "铺线这一块我们是以机房为重点展开的，各个房间的网络和电源同步调通，"
        "争取两三天之内把铺线全部完成，为后面的联调联试打好基础，",
        "物资方面我说明一下，已经发货的部分我们会把第三方检测报告、合格证、"
        "产品说明书都收集齐，还没有发货的部分我们会准备好合同和采购计划，"
        "用它来证明九月二十号之前能够到货，这一点请陈主任放心，",
    ]),
    ("甲方-林浩然", "zh-CN-YunyangNeural", [
        "还有一个事情我要提醒你们，现场的每日规划必须做出来，而且要按照时间节点完工，"
        "不能今天干一点明天干一点，那样到了验收的时候我们还是拿不出东西，",
        "另外薪资方面的问题要提前解决，该加人的时候就要加人，"
        "推动力度一定要大，不要等到问题堆在一起了再来找我，那时候我也没办法，",
    ]),
]


async def _tts(text: str, voice: str, rate: str, dest: Path, attempts: int = 4) -> None:
    """edge-tts with retries: NoAudioReceived is intermittent and unrelated to input."""
    import edge_tts

    last = None
    for i in range(1, attempts + 1):
        try:
            await edge_tts.Communicate(text, voice, rate=rate).save(str(dest))
            if dest.exists() and dest.stat().st_size > 500:
                return
            last = RuntimeError("empty audio")
        except Exception as e:  # noqa: BLE001
            last = e
        if i < attempts:
            print(f"      retry {i}: {type(last).__name__}", flush=True)
            await asyncio.sleep(1.0 * i)
    raise RuntimeError(f"TTS failed: {text[:20]!r}: {last}")


async def build(out_dir: Path, rate: str, pause: float) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="long_tts_"))
    parts: list[Path] = []
    utterances: list[dict] = []

    for si, (spk, voice, paras) in enumerate(MONOLOGUES):
        for pi, text in enumerate(paras):
            mp3 = tmp / f"{si:02d}_{pi:02d}.mp3"
            await _tts(text, voice, rate, mp3)
            dur = _probe(mp3)
            parts.append(mp3)
            utterances.append({"speaker": spk, "text": text,
                               "chars": len(text), "seconds": round(dur, 2)})
            print(f"  {spk:<12} {dur:5.1f}s  {len(text):3d}字  {text[:26]}…", flush=True)

            # The pause between paragraphs is the only place the VAD can legitimately
            # break. Kept short (and configurable) because the whole point is to test
            # long uninterrupted speech.
            sil = tmp / f"sil_{si:02d}_{pi:02d}.wav"
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                            "-t", str(pause), "-c:a", "pcm_s16le", str(sil)],
                           capture_output=True, check=True)
            parts.append(sil)

    lst = tmp / "list.txt"
    lst.write_text("\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8")

    wav = out_dir / "long-meeting-16k.wav"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                   capture_output=True, check=True)
    ogg = out_dir / "long-meeting.ogg"
    subprocess.run(["ffmpeg", "-y", "-i", str(wav), "-c:a", "libopus", "-b:a", "32k", str(ogg)],
                   capture_output=True, check=True)

    gt = {
        "audio": str(wav),
        "duration_s": round(_probe(wav), 2),
        "n_monologues": len(utterances),
        "speakers": sorted({u["speaker"] for u in utterances}),
        "utterances": utterances,
        "full_text": "".join(u["text"] for u in utterances),
        "params": {"rate": rate, "pause_s": pause},
        # Paragraphs that must survive recognition. Long enough that losing the head of
        # one is immediately visible in the character count.
        "expected_queries": [
            {"q": "第三方测评人员什么时候进场", "expect": "项目会议纪要（20260907）"},
            {"q": "软件部署调通什么时候完成", "expect": "项目会议纪要（20260831）"},
            {"q": "VR 体验什么时候之前要完整", "expect": "项目会议纪要（20260831）"},
            {"q": "已发货的物资要准备哪些材料", "expect": "项目会议纪要（20260831）"},
            {"q": "铺线以什么为重点", "expect": "项目会议纪要（20260831）"},
        ],
    }
    (out_dir / "ground-truth.json").write_text(
        json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
    return gt


def _probe(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    ap.add_argument("--out", default=str(root / "data" / "long-meeting"))
    ap.add_argument("--rate", default="-12%", help="slower speech -> longer utterances")
    ap.add_argument("--pause", type=float, default=0.35,
                    help="silence between paragraphs; keep short to force long runs")
    args = ap.parse_args()

    print("synthesising long-utterance meeting ...", flush=True)
    gt = asyncio.run(build(Path(args.out), args.rate, args.pause))
    print()
    print(f"audio      : {gt['audio']}")
    print(f"duration   : {gt['duration_s']}s")
    print(f"monologues : {gt['n_monologues']}  speakers: {', '.join(gt['speakers'])}")
    print(f"chars      : {len(gt['full_text'])}")
    longest = max(u["seconds"] for u in gt["utterances"])
    print(f"longest paragraph: {longest:.1f}s   "
          f"(window is 20s -- anything longer is the stress case)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
