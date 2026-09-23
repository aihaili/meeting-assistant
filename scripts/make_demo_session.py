"""Build a demo session file so the UI can be designed against real content.

Designing an interface against an empty state is how you end up with a layout that
only works when there is nothing in it. This writes a session that looks like a
meeting that actually happened: nine utterances from the synthetic meeting's script
(with the speakers the script assigned), plus the clue types the rule classifier
derives from each one, plus the retrieval evidence the assistant returns from the
minutes corpus.

The utterances come from ``ground-truth.json`` rather than from ASR output, because
the point is realistic *content*, and hand-writing nine lines of plausible meeting
speech is slower and worse than reusing text already written for the audio.

Optionally runs the folder through the real ``MeetingService`` so the clues and
evidence are produced by the shipping code path, not by a mock.

Usage:
    python scripts/make_demo_session.py [--out PATH] [--with-llm]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from meeting.classify import classify_rules  # noqa: E402
from meeting.session import MeetingSession  # noqa: E402

ROOT = HERE.parent

# Who says what, and when, matched to the speaker labels in ground-truth.json. The
# timings are the ones the end-to-end run actually produced, so the demo's time column
# looks like a real transcript rather than an evenly spaced mock.
SPEAKERS = [
    ("林浩然", 0.79, 4.60),
    ("林浩然", 5.53, 8.47),
    ("林浩然", 8.85, 12.48),
    ("孙磊", 12.60, 16.90),
    ("孙磊", 17.10, 21.60),
    ("孙磊", 22.30, 26.05),
    ("林浩然", 27.00, 31.40),
    ("林浩然", 32.00, 35.20),
    ("孙磊", 36.10, 41.80),
    ("孙磊", 42.60, 47.90),
    ("孙磊", 48.70, 53.10),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "sessions" / "demo.json"))
    ap.add_argument("--gt", default=str(ROOT / "data" / "meeting-audio" / "ground-truth.json"))
    ap.add_argument("--db", default=str(ROOT / "data" / "meet.db"))
    ap.add_argument("--kb", default=str(ROOT / "data" / "synth-corpus"))
    ap.add_argument("--with-llm", action="store_true",
                    help="classify with the LLM too (slow); default is rules only")
    args = ap.parse_args()

    gt = json.loads(Path(args.gt).read_text(encoding="utf-8"))
    texts = [u["text"] for u in gt["utterances"]]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    session = MeetingSession(title="项目进度汇报例会（演示）", path=out)
    for name, org, role in (("林浩然", "甲方单位", "甲方"),
                            ("徐博文", "甲方单位", "甲方"),
                            ("孙磊", "史塔克", "我方"),
                            ("高志远", "史塔克", "我方"),
                            ("郑涛", "史塔克", "我方")):
        session.add_participant(name, org, role)

    assistant = None
    if Path(args.db).exists():
        from asst.core import Assistant

        assistant = Assistant(db_path=args.db, kb_dir=args.kb, top_k=3)
        print(f"检索索引: {assistant.stats['chunks']} chunks")

    # Utterances and their retrieval evidence.
    for i, text in enumerate(texts):
        who, start, end = SPEAKERS[i] if i < len(SPEAKERS) else ("", i * 4.0, i * 4.0 + 3.0)
        seg = session.add_segment(text, start, end, idx=i, speaker=who)
        if assistant is not None:
            r = assistant.process(text)
            seg["keywords"] = [k["term"] for k in r["keywords"]]
            seg["results"] = [{"title": x["title"], "rel": x["rel"], "label": x["label"],
                               "score": x["score"], "heading": x.get("heading", ""),
                               "snippet": x["snippet"]} for x in r["results"]]
        session._save()

    # Clues: rules always; the LLM only when asked, since it is the slow part.
    people = [p.name for p in session.participants]
    for i, text in enumerate(texts):
        seg = next(s for s in session.segments if s["idx"] == i)
        specs = classify_rules(text, known_people=people)
        if args.with_llm:
            from meeting.classify import classify_llm, merge

            try:
                llm, ms = classify_llm(text)
                specs = merge(specs, llm)
                print(f"  LLM {ms:.0f}ms -> {len(llm)} 条")
            except Exception as e:  # noqa: BLE001
                print(f"  LLM 失败，仅用规则: {type(e).__name__}: {e}")
        for sp in specs:
            refs = []
            if assistant is not None and len(sp.get("anchor", "")) >= 3:
                try:
                    refs = assistant._search(sp["anchor"])[:2]
                except Exception:  # noqa: BLE001
                    refs = []
            session.add_clue(kind=sp["type"], text=sp["text"], seg_id=seg["id"],
                             t=seg["start"], anchor=sp.get("anchor", ""),
                             confidence=sp.get("confidence", 0.6),
                             actor=sp.get("actor", ""), due=sp.get("due", ""),
                             refs=refs or (seg.get("results") or [])[:2])

    session.set_agenda([
        {"topic": "上次甲方要求的落实情况", "kind": "followup",
         "detail": "9月5日软件部署调通、9月10日VR完整"},
        {"topic": "铺线与机房部署进度", "kind": "report"},
        {"topic": "第三方测评进场安排", "kind": "confirm"},
        {"topic": "未发货物资的证明材料", "kind": "confirm"},
    ])

    # Pin one clue so the "置顶" state is visible without clicking.
    for c in session.clues:
        if c.kind == "deadline":
            c.pinned = True
            break
    session._save()

    n_types = {}
    for c in session.clues:
        n_types[c.kind] = n_types.get(c.kind, 0) + 1
    print(f"\n写入 {out}")
    print(f"  发言 {len(session.segments)} 段")
    print(f"  线索 {len(session.clues)} 条  " +
          "  ".join(f"{k}×{v}" for k, v in sorted(n_types.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
