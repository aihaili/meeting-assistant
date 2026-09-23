"""Keyword extraction for the meeting assistant.

Standalone on purpose: it needs no RAG index, so extraction quality can be
evaluated and unit-tested in isolation (see ``eval_keywords.py`` and
``keyword_eval.json``). ``Assistant.extract_keywords`` delegates here.

The prompt is kept deliberately terse: the local model degrades badly on long
prose prompts (measured earlier -- it echoed example placeholders verbatim and
once produced 10k characters of `evidence` text).
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PARENT = _HERE.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

# ── prompt ──────────────────────────────────────────────────────────────
# Kept deliberately terse: the local model degrades badly on long prompts with
# prose instructions (measured earlier -- it echoed example placeholders verbatim
# and once produced 10k characters of `evidence` text).
KEYWORD_PROMPT = """从下面这段会议发言中，抽取 2-4 个最重要的检索热点关键词。

要求：
- 只挑最能定位出处、最能代表本段主题的实体与术语：人名、机构、文件/报告、流程、专业术语、关键动作
- 每个 2-8 个字，必须是发言原文里出现过的连续片段
- 日期、数量、金额等具体数值，以及"我们/他们/现场/工作"这类泛化词，一律不要
- 宁少勿滥，拿不准就不抽
- 只输出 JSON 数组，不要解释

发言：
{segment}

输出：["关键词1","关键词2"]"""


def extract_keywords(segment: str, unused_llm: bool = False,
                    max_keywords: int = 6) -> tuple[list[str], float]:
    """Return (keywords, elapsed_ms). Falls back to jieba on any LLM trouble."""
    t0 = time.time()
    kws: list[str] = []
    if not unused_llm:
        try:
            from dotenv import load_dotenv

            load_dotenv(Path.home() / ".hermes" / ".env")
            from llm_client import llm_chat

            raw = llm_chat(KEYWORD_PROMPT.format(segment=segment), timeout=60)
            kws = _parse_keywords(raw or "")
        except Exception as e:  # noqa: BLE001
            print(f"[asst] LLM 抽取失败，回退 jieba: {type(e).__name__}: {e}",
                  file=sys.stderr)
    if not kws:
        kws = _fallback_keywords(segment)
    # Ground every keyword in the actual text: a term the model invented can never
    # be highlighted, and would silently produce an empty hotspot.
    kws = [k for k in kws if k in segment]
    # Drop keywords fully contained in a longer one, and short generic ones.
    kws = _dedupe(kws)
    return kws[:max_keywords], (time.time() - t0) * 1000


def _parse_keywords(raw: str) -> list[str]:
    m = re.search(r"\[[\s\S]*\]", raw)
    if m:
        try:
            got = json.loads(m.group(0))
            if isinstance(got, list):
                return [str(x).strip() for x in got if str(x).strip()]
        except Exception:  # noqa: BLE001
            pass
    # bare object, or the model dropped the brackets
    return [s for s in re.findall(r'"([^"]{2,12})"', raw)]


def _fallback_keywords(segment: str, n: int = 6) -> list[str]:
    """jieba + frequency, for when the LLM is unavailable."""
    import jieba
    from collections import Counter

    words = [w.strip() for w in jieba.cut(segment)]
    cand = [w for w in words
            if 2 <= len(w) <= 8 and re.match(r"^[\u4e00-\u9fffA-Za-z0-9]+$", w)]
    stop = {"我们", "他们", "这个", "那个", "就是", "什么", "现在", "可以",
            "应该", "需要", "进行", "如果", "因为", "所以", "但是", "然后"}
    cand = [w for w in cand if w not in stop]
    return [w for w, _ in Counter(cand).most_common(n)]


def _dedupe(kws: list[str]) -> list[str]:
    out: list[str] = []
    for k in sorted(set(kws), key=len, reverse=True):
        if not any(k in kept for kept in out):
            out.append(k)
    return out
