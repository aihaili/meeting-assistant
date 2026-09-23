"""Check the clue classifier against real utterances.

Two passes are tested separately because they fail differently and must fail
independently:

* ``--rules`` runs offline and must always work. It is the deterministic floor: dates,
  obligations, organisations, named people. If this regresses, the board silently
  loses the facts that matter most.
* ``--llm`` exercises the local model and is opt-in, because it is slow and the model
  is the least reliable component. It asserts the *contract* rather than exact output:
  every returned type is in the closed vocabulary, every anchor is a literal substring
  of the utterance, and no prompt placeholder leaks through. Those three properties are
  what the UI depends on; the specific clues are the model's business.

Usage:
    python scripts/test_classify.py --rules
    python scripts/test_classify.py --llm
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from meeting.classify import classify_llm, classify_rules, is_placeholder  # noqa: E402
from meeting.session import CLUE_TYPES  # noqa: E402

# Utterances taken from the real 20260831 minutes and from the synthetic meeting
# audio, so the patterns are exercised on the phrasing this project actually sees.
CASES = [
    ("软件部署调通必须在九月五号之前完成，你们公司要尽快确定派谁来？",
     {"deadline", "requirement"}),
    ("已发货的物资我们会把第三方检测报告、合格证、产品说明书都收集齐。",
     {"commitment"}),
    ("争取两三天之内把铺线全部完成，为后面的联调联试打好基础。", {"deadline"}),
    ("还有 VR 体验这块，如果效果不行，要抓紧时间调试，九月十号之前必须完整。",
     {"deadline", "requirement"}),
    ("9月5日左右第三方测评人员进场。", {"deadline"}),
    ("这个方案我们下周再确认。", {"deadline"}),
    ("好，咱们现在开始今天的进度汇报例会。", set()),
    ("山西省信息产业技术研究院有限公司递交了投标文件。", {"org"}),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", action="store_true")
    ap.add_argument("--llm", action="store_true")
    args = ap.parse_args()
    if not args.rules and not args.llm:
        args.rules = True

    fails = 0

    if args.rules:
        print("=" * 74)
        print("规则通道（离线，必须始终可用）")
        print("=" * 74)
        for text, expect in CASES:
            got = classify_rules(text, known_people=["林浩然", "孙磊"])
            kinds = {c["type"] for c in got}
            # Deadlines on a sentence with no date, and absence of clues on pure
            # pleasantries, are the two directions that must both hold.
            ok = expect.issubset(kinds) if expect else not kinds
            mark = "PASS" if ok else "FAIL"
            if not ok:
                fails += 1
            print(f"\n  {mark}  {text[:44]}")
            print(f"        期望类型 {sorted(expect) or '(无)'}  得到 {sorted(kinds) or '(无)'}")
            for c in got:
                # Anchor integrity is the invariant the UI relies on for highlighting.
                inside = c["anchor"] in text
                bad = "" if inside else "   ← anchor 不在原文里！"
                if not inside:
                    fails += 1
                print(f"        · [{c['type']:<11}] {c['anchor']:<12} {c['text'][:34]}"
                      f"  ({c['confidence']:.2f}/{c['source']}){bad}")

        # Placeholder leakage: the model once copied the prompt's example back as a
        # real finding. The guard must catch it.
        ph = [classify_rules("请填写姓名或描述"), classify_rules("关键词1 关键词2")]
        flat = [c for group in ph for c in group]
        print(f"\n  {'PASS' if not any(is_placeholder(c['anchor']) for c in flat) else 'FAIL'}"
              f"  占位符未泄漏  ({len(flat)} 条结果)")
        if any(is_placeholder(c["anchor"]) for c in flat):
            fails += 1

    if args.llm:
        print("\n" + "=" * 74)
        print("LLM 通道（契约校验：类型封闭 / anchor 必须在原文 / 无占位符）")
        print("=" * 74)
        for text, _expect in CASES:
            try:
                clues, ms = classify_llm(text)
            except Exception as e:  # noqa: BLE001
                print(f"\n  FAIL  {text[:40]}")
                print(f"        LLM 调用失败: {type(e).__name__}: {e}")
                fails += 1
                continue
            bad_type = [c for c in clues if c["type"] not in CLUE_TYPES]
            bad_anchor = [c for c in clues if c["anchor"] not in text]
            bad_ph = [c for c in clues if is_placeholder(c["anchor"]) or is_placeholder(c["text"])]
            ok = not (bad_type or bad_anchor or bad_ph)
            if not ok:
                fails += 1
            print(f"\n  {'PASS' if ok else 'FAIL'}  {text[:44]}   ({ms:.0f} ms, {len(clues)} 条)")
            for c in clues:
                print(f"        · [{c['type']:<11}] {c['anchor']:<12} {c['text'][:34]}"
                      f"  ({c['confidence']:.2f})")
            if bad_type:
                print(f"        ← 非法类型: {[c['type'] for c in bad_type]}")
            if bad_anchor:
                print(f"        ← anchor 不在原文: {[c['anchor'] for c in bad_anchor]}")
            if bad_ph:
                print(f"        ← 占位符泄漏: {[c['anchor'] for c in bad_ph]}")

    print(f"\n{'全部通过' if fails == 0 else f'{fails} 处失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
