"""Turn an utterance into typed clues, using the local LLM with a rule fallback.

Design decisions that come from measured failure modes of the local 4B model
-----------------------------------------------------------------------------
* **Closed vocabulary.** The type must be one of ``CLUE_TYPES``. Asked for
  "categories", a small model invents a fresh label per utterance, and a filter
  list where every row is unique is unusable. The prompt enumerates the allowed
  values and anything off-list is discarded, not coerced.
* **The anchor must be a substring of the utterance.** Same rule the keyword
  extractor already follows. A clue whose anchor is not literally present cannot be
  highlighted or traced, so it is rejected -- which also filters out the model's
  habit of restating the prompt's examples back as findings.
* **Terse prompt, JSON only.** Long prose instructions make this model echo
  placeholders; it once wrote ``"type": ""甲方要求",`` -- invalid JSON from trying to
  quote inside a quote. The parser is therefore tolerant and the prompt minimal.
* **Rules always run first.** Deadlines, dates, obligation phrasing (应当/必须/不得)
  and named people are exactly what regex gets right, and they are also the most
  important clue types. The LLM adds judgement about intent, not arithmetic about
  dates. When the LLM is unreachable the assistant still produces a usable board,
  which matters because the venue network is often worse than the laptop.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .session import CLUE_TYPES

# ── prompts ─────────────────────────────────────────────────────────────
# Kept short: this model degrades quickly on long instructions. The type list is
# inline because it is the one thing that must not be paraphrased.

CLUE_PROMPT = """从下面这段会议发言里找出值得记下来的“线索”。

类别只能从这些里选：
{types}

规则：
- 每个线索的 anchor 必须是发言原文里原样出现的连续片段（2-12 个字）
- 没有值得记的内容就返回空数组
- 只输出 JSON 数组，不要解释

发言：
{segment}

输出：[{{"type":"requirement","text":"要加快现场施工","anchor":"加快速度","confidence":0.8}}]"""


def _type_menu() -> str:
    return "\n".join(f"- {k}：{v['hint']}" for k, v in CLUE_TYPES.items())


# ── rule pass ───────────────────────────────────────────────────────────

# Obligation phrasing. The corpus analysis counted 应当/必须/不得 208 times, so these
# are the load-bearing words in this domain rather than generic hedges.
_OBLIGATION = re.compile(r"必须|应当|不得|需要|要求|务必|尽快|抓紧|不能")
# Third-person duty. "你们公司要尽快确定" carries no 必须/应当, and without this the
# most common form of a demand in this domain -- an imperative addressed at the vendor --
# produced no requirement clue at all.
_DUTY = re.compile(r"要(?:尽快|抓紧|加快|提前|保证|确保|注意|加强|做好)")
# First-person promises. Separate from _OBLIGATION because "我们会把材料收齐" carries
# no obligation word at all yet is the single most important clue type for this
# project -- the promise is what the next meeting checks up on. Missing these meant
# the board showed the other side's demands and none of our own undertakings.
# Requires a first-person marker *followed by* a commitment word, and must not match
# "我们公司" / "我们会同" style organisation references.
_PROMISE = re.compile(r"(?:我们|我方|我)(?:会|要|将|来|负责|安排|争取|已经)"
                      r"|(?:我们|我方)(?:这边|的)?(?:负责|安排)")

# Date fragments. Matched as small units and then *extended*, because writing one big
# pattern produced wrong spans: "九月十号之前" came out as two separate clues,
# "十号之前" and "九月", the second of which is not a deadline at all.
_DATE_CORE = re.compile(
    r"\d{1,4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{1,2}\s*月"
    r"|(?:\d{1,2}|[一二三四五六七八九十两])\s*[日号]"
    r"|(?:\d{1,2}|[一二三四五六七八九十两])\s*(?:天|周|个?月)"
)
_DATE_TAIL = re.compile(r"(?:之内|以内|以前|之前|左右|前|后|内|底|初)")
# Continuation of a date already in progress: optional separator, optional number
# (Arabic or Chinese), then a unit. Applied repeatedly by _extend_date.
_DATE_MORE = re.compile(r"[ 　]*(?:\d{1,2}|[一二三四五六七八九十两])?\s*(?:[日号]|月|年)")
_REL_DATE = re.compile(r"[下本这]\s*(?:周|个月|月)\s*[一二三四五六日天]?"
                       r"|月底|月初|年底|年后|节前|验收前|进场前")

# Organisation suffixes. The prefix class excludes date and particle characters: with a
# plain character class the pattern matched "日左右第三方测评" (a fragment of a date plus
# a topic) and "有限公司" (a bare suffix) as separate organisations.
_ORG = re.compile(
    r"(?<![\d日月号年])"
    r"([\u4e00-\u9fff]{2,12}?(?:公司|集团|研究院|研究所|学院|大学|医院|测评中心|监理|设计院))")
_ORG_STOP = {"有限公司", "公司", "贵公司", "你们公司", "我们公司", "该公司", "第三方"}

_ROLE = re.compile(r"([\u4e00-\u9fff]{2,4})(?:主任|经理|总工|工程师|老师|院长|科长|处长|主管)")
# Roles that are generic nouns rather than names -- "负责人" alone is not a person.
_ROLE_STOP = {"负责", "技术", "项目", "主管", "售后", "客户", "甲方", "乙方", "现场", "相关"}
_NAMED = re.compile(r"(?:请|让|由|叫|找)\s*([\u4e00-\u9fff]{2,4})(?=[^\u4e00-\u9fff]|$|来|去|把|说|做|负责|安排)")

PLACEHOLDER = re.compile(r"姓名或描述|关键词1|关键词2|示例|xxx|XXX|如：|例如")


def is_placeholder(text: str) -> bool:
    return bool(text) and bool(PLACEHOLDER.search(text))


def _extend_date(seg: str, start: int, end: int) -> tuple[int, int]:
    """Grow a date match over the trailing words that belong to the same deadline.

    ``九月五号之前`` must be one clue, not ``九月`` plus ``五号之前``. The regex matches
    the leading unit ("九月") and this walks forward through whatever continues the same
    expression -- further units and their numerals, then one deadline suffix.

    The loop keys on the *unit* coming after optional digits or a Chinese numeral,
    because the unit is what marks a continuation. Keying on a unit character first
    never worked: ``九月`` is followed by ``五``, which is not a unit, so the walk
    stopped immediately and the date split in two.

    The suffix pattern is optional and therefore always matches at zero width, so the
    result is only adopted when it actually advanced -- an earlier version returned
    ``m.end()`` unconditionally, truncating the match to nothing.
    """
    i = end
    for _ in range(4):
        m = _DATE_MORE.match(seg, i)
        if not m:
            break
        i = m.end()
    m = _DATE_TAIL.match(seg, i)
    if m and m.end() > i:
        i = m.end()
    return start, i


def _org_name(raw: str) -> str:
    """Trim a matched organisation down to the part that is actually a name."""
    name = raw.strip("　 ")
    for bad in ("有限公司", "有限责任公司", "股份有限公司"):
        if name.endswith(bad) and len(name) > len(bad):
            # Keep the full legal name -- it is the identifying part -- but never let
            # the bare suffix stand alone as an organisation.
            break
    return name


def classify_rules(segment: str, known_people: list[str] | None = None) -> list[dict]:
    """Deterministic clues: dates, obligations, promises, organisations, people.

    Runs before the LLM and its results are merged, not replaced. Dates in particular
    are worth extracting mechanically -- the model paraphrases "9月5日之前" into
    "九月初", which silently changes a contract-relevant fact.
    """
    out: list[dict] = []
    seg = segment or ""
    known_people = known_people or []
    taken: list[tuple[int, int]] = []          # spans already claimed, to avoid overlap

    def add(kind: str, text: str, anchor: str, conf: float) -> None:
        if not anchor or anchor not in seg or is_placeholder(anchor):
            return
        out.append({"type": kind, "text": text, "anchor": anchor,
                    "confidence": conf, "source": "rule"})

    def claim(a: int, b: int) -> bool:
        if any(a < y and x < b for x, y in taken):
            return False
        taken.append((a, b))
        return True

    # ── dates ───────────────────────────────────────────────────────────
    def add_dates(rx: re.Pattern) -> None:
        for m in rx.finditer(seg):
            a, b = _extend_date(seg, m.start(), m.end())
            span = seg[a:b].strip()
            if not span or not claim(a, a + len(span)):
                continue
            add("deadline", f"时间节点：{span}", span, 0.85 if rx is _DATE_CORE else 0.7)

    add_dates(_DATE_CORE)
    add_dates(_REL_DATE)

    # ── obligations, duties and promises ────────────────────────────────
    # Promises first: "我们会把…收集齐" contains no obligation word, so checking
    # _OBLIGATION alone missed every undertaking the project made.
    for rx, kind, conf in ((_PROMISE, "commitment", 0.7),
                           (_DUTY, "requirement", 0.7),
                           (_OBLIGATION, "requirement", 0.65)):
        clause = _clause_around(seg, rx)
        if not clause:
            continue
        m = re.search(re.escape(clause), seg)
        if not m or not claim(m.start(), m.end()):
            continue
        # A clause that carries an obligation but is phrased as our own undertaking is
        # a commitment, not a demand from the other side.
        if kind == "requirement" and _PROMISE.search(clause):
            kind = "commitment"
        add(kind, clause, clause, conf)

    # ── organisations ───────────────────────────────────────────────────
    for m in _ORG.finditer(seg):
        name = _org_name(m.group(1))
        if name in _ORG_STOP or len(name) < 4:
            continue
        if claim(m.start(1), m.start(1) + len(name)):
            add("org", name, name, 0.8)

    # ── people ──────────────────────────────────────────────────────────
    for m in _ROLE.finditer(seg):
        if m.group(1) not in _ROLE_STOP and claim(m.start(), m.end()):
            add("person", m.group(0), m.group(0), 0.7)
    for name in known_people:
        if name and name in seg:
            i = seg.index(name)
            if claim(i, i + len(name)):
                add("person", name, name, 0.9)
    for m in _NAMED.finditer(seg):
        if claim(m.start(1), m.end(1)):
            add("person", m.group(1), m.group(1), 0.5)

    return _dedupe(out)


def _clause_around(seg: str, rx: re.Pattern, limit: int = 30) -> str:
    """The shortest clause containing a match of ``rx``, trimmed to stay scannable."""
    for part in re.split(r"[，。；！？,;!?]", seg):
        part = part.strip("　 ")
        if rx.search(part) and len(part) >= 4:
            return part if len(part) <= limit else part[:limit]
    return ""


# ── LLM pass ────────────────────────────────────────────────────────────

def classify_llm(segment: str, timeout: int = 45) -> tuple[list[dict], float]:
    """Ask the local model for clues. Returns (clues, elapsed_ms).

    Raises nothing: a classification failure is a degraded board, not a broken
    meeting, so the caller gets an empty list and the reason is recorded on the
    session for the UI to show.
    """
    t0 = time.time()
    from dotenv import load_dotenv

    load_dotenv(Path.home() / ".hermes" / ".env")
    from llm_client import llm_chat

    raw = llm_chat(CLUE_PROMPT.format(types=_type_menu(), segment=segment), timeout=timeout)
    ms = (time.time() - t0) * 1000
    return _parse(raw or "", segment), ms


def _parse(raw: str, segment: str) -> list[dict]:
    """Tolerant parse of the model's JSON, then validate every field hard.

    Both stages are necessary. The parse survives a small model's near-miss syntax
    (stray quotes, a missing bracket, trailing prose); the validation is what stops
    hallucinated types and anchors that are not in the utterance from reaching the
    board, since those cannot be highlighted or traced back.
    """
    items: list[dict] = []
    m = re.search(r"\[[\s\S]*\]", raw)
    if m:
        for candidate in (m.group(0), _repair(m.group(0))):
            try:
                got = json.loads(candidate)
                if isinstance(got, list):
                    items = got
                    break
            except Exception:
                continue
    if not items:
        # Last resort: pull the pieces out one by one. Loses structure, keeps content.
        for km in re.finditer(r'"type"\s*:\s*"([^"]+)"[\s\S]{0,220}?"anchor"\s*:\s*"([^"]+)"', raw):
            items.append({"type": km.group(1), "anchor": km.group(2),
                          "text": km.group(2)})

    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("type") or "").strip().strip('"').strip()
        if kind not in CLUE_TYPES:
            continue
        anchor = str(it.get("anchor") or "").strip()
        text = str(it.get("text") or anchor).strip()
        if not anchor or anchor not in segment or is_placeholder(anchor) or is_placeholder(text):
            continue
        try:
            conf = float(it.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        row = {"type": kind, "text": text, "anchor": anchor,
               "confidence": max(0.0, min(1.0, conf)), "source": "llm"}
        for k in ("actor", "due"):
            v = str(it.get(k) or "").strip()
            if v and v in segment:
                row[k] = v
        out.append(row)
    return _dedupe(out)


def _repair(s: str) -> str:
    """Fix the syntax slips this model actually makes, without guessing content."""
    s = s.replace('""', '"')                     # ""甲方要求" -> "甲方要求
    s = re.sub(r'"\s*([\u4e00-\u9fff]+)"\s*:', r'"\1":', s)
    s = re.sub(r',\s*([\]}])', r'\1', s)          # trailing commas
    return s


def _dedupe(items: list[dict]) -> list[dict]:
    """Drop clues whose anchor is contained in another clue's anchor."""
    items = sorted(items, key=lambda x: -len(x.get("anchor", "")))
    out: list[dict] = []
    for it in items:
        a = it.get("anchor", "")
        if any(a in kept.get("anchor", "") or kept.get("anchor", "") in a for kept in out):
            continue
        out.append(it)
    return out


def merge(rule_clues: list[dict], llm_clues: list[dict]) -> list[dict]:
    """Combine the two passes, keeping the higher-confidence phrasing per anchor.

    Rules win on dates and obligations (they quote exactly), the LLM wins on
    everything judgemental. Where both fire on the same anchor the rule's text is
    kept but the LLM is allowed to upgrade the *type*, since deciding that a
    "要求" is really a "风险" is precisely the judgement wanted from it.
    """
    by_anchor: dict[str, dict] = {}
    for it in rule_clues:
        by_anchor[it["anchor"]] = dict(it)
    for it in llm_clues:
        a = it["anchor"]
        if a in by_anchor:
            cur = by_anchor[a]
            if it["type"] != cur["type"] and it.get("source") == "llm":
                # LLM's type wins only when the rule pass was not certain about people
                # or organisations, where the regex is more reliable than the model.
                if cur["type"] not in ("person", "org", "deadline"):
                    cur["type"] = it["type"]
            cur["confidence"] = max(cur["confidence"], it["confidence"])
            for k in ("actor", "due"):
                if it.get(k) and not cur.get(k):
                    cur[k] = it[k]
        else:
            by_anchor[a] = dict(it)
    return sorted(by_anchor.values(), key=lambda x: -x["confidence"])
