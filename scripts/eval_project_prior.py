"""Does preferring the project index fix the remaining tie?

Rank fusion scores every index's rank-1 hit identically (``1/(K+1)``), so the top hit of
each corpus ties and the order falls back to insertion — which still put a public
regulation first on every meeting question even after switching to RRF:

    第三方测评人员什么时候进场
        [公共] 开设规范                rrf=0.016393
        [项目] 项目会议纪要（20260907）  rrf=0.016393   <- correct, ranked second

During a meeting the corpus that matters is the project's own, so the project index should
win ties. This measures whether a small per-index prior actually changes the outcome rather
than just the code, on questions whose answer is only in the project corpus.

Usage:
    python scripts/eval_project_prior.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rag.rag_core import RagIndex  # noqa: E402

ROOT = HERE.parent
DATA = ROOT / "data" / "multi-project"

RRF_K = 60
# A nudge, not a filter: large enough to win a tie, small enough that a genuinely much
# better public hit still surfaces. 1/(60+1) = 0.016393, so 0.0006 is ~3.7% of one rank-1
# vote -- it breaks ties without overriding a real ranking difference.
PRIOR = 0.0006


def main() -> int:
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    public = RagIndex(db_path=str(ROOT / "data" / "ar.db"), kb_dir=r"<公共资料库>")
    per = {p["key"]: RagIndex(db_path=str(ROOT / "data" / f"mp-{p['key']}.db"),
                              kb_dir=str(DATA / p["key"]))
           for p in manifest["projects"]}

    # Questions that only the project corpus can answer.
    CASES = []
    for p in manifest["projects"]:
        CASES += [
            (p["key"], p["name"], f"{p['wiring']} 什么时候完成"),
            (p["key"], p["name"], f"{p['asset']} 什么时候之前必须完整"),
            (p["key"], p["name"], "验收大纲什么时候要完成"),
            (p["key"], p["name"], "薪资和人员稳定性有什么风险"),
        ]

    def fuse(q: str, proj_key: str, with_prior: bool):
        scores, best = {}, {}
        for label, idx in (("公共", public), (proj_key, per[proj_key])):
            try:
                hits = idx.search(q, top_k=5)
            except Exception:  # noqa: BLE001
                continue
            for rank, h in enumerate(hits, 1):
                key = f"{label}::{h.get('title')}"
                scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
                if with_prior and label != "公共":
                    scores[key] += PRIOR
                best.setdefault(key, (label, h.get("title")))
        return [best[k] for k, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:3]]

    print("=" * 88)
    print("项目内问题：不加优先 vs 加项目优先（看 top1 是否来自项目库）")
    print("=" * 88)
    print(f"{'问的是':<10}{'问句':<28}{'不加优先 top1':<26}{'加优先 top1':<26}")

    n = 0
    base_ok = prior_ok = 0
    flips = []
    for key, name, q in CASES:
        a = fuse(q, key, False)
        b = fuse(q, key, True)
        n += 1
        a_ok = bool(a) and a[0][0] != "公共"
        b_ok = bool(b) and b[0][0] != "公共"
        base_ok += a_ok
        prior_ok += b_ok
        if a_ok != b_ok:
            flips.append((q, a[0][1][:16] if a else "?", b[0][1][:16] if b else "?"))
        print(f"{name:<10}{q[:26]:<28}{str(a[0][1])[:24] if a else '-':<26}"
              f"{str(b[0][1])[:24] if b else '-':<26}")

    print()
    print(f"  不加优先：top1 来自项目库 {base_ok}/{n} = {100 * base_ok / n:.0f}%")
    print(f"  加项目优先：top1 来自项目库 {prior_ok}/{n} = {100 * prior_ok / n:.0f}%")
    if flips:
        print(f"\n  被优先规则纠正的 {len(flips)} 处：")
        for q, before, after in flips:
            print(f"    {q:<30} {before}  ->  {after}")
    print()
    if prior_ok > base_ok:
        print("结论：项目优先确实把并列的正确答案提到了第一位。")
    elif base_ok == n:
        print("结论：本来就已全部正确，优先规则没有可观测收益（不必加）。")
    else:
        print("结论：优先规则没能解决——并列之外还有别的原因，需要继续查。")

    # ── the prior must not bury the public corpus ───────────────────────
    # A tie-breaker that wins by dominating would be a different bug: questions whose answer
    # is a rule or a clause must still surface the public document. This checks that the
    # prior only breaks ties, by asking questions only the public corpus can answer and
    # confirming a public document still comes first.
    print()
    print("=" * 88)
    print("反证：公共类问题是否仍由公共库回答（优先项不能盖掉正确答案）")
    print("=" * 88)
    PUBLIC_CASES = [
        ("审查讯问时我方人员不得少于几人", "开设规范"),
        ("讯问过程应当录音录像", "开设规范"),
        ("心理危机干预的处置流程", "示例手册"),
        ("AR 展训平台的交互设计", "PRD"),
    ]
    ok = 0
    for q, expect in PUBLIC_CASES:
        top = fuse(q, "xinjiang", True)
        got_kb, got_title = top[0] if top else ("?", "?")
        good = expect in (got_title or "")
        ok += good
        print(f"  {'✓' if good else '✗'} {q:<26} 期望含「{expect}」  得到 [{got_kb}] {got_title}")
    print(f"\n  公共类问题命中 {ok}/{len(PUBLIC_CASES)}")
    if ok == len(PUBLIC_CASES):
        print("  → 优先项只破并列，没有盖住公共答案")
    else:
        print("  → 优先项太强，把公共答案压下去了（PRIOR 需要调小）")

    out = ROOT / "data" / "project-prior-eval.json"
    out.write_text(json.dumps({"n": n, "base_ok": base_ok, "prior_ok": prior_ok,
                               "flips": flips}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
