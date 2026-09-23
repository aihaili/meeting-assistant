"""Check the <style> block for the one CSS error class that fails silently: a stray brace.

Why this exists as a permanent check. A stray ``}`` produces no visible error. The CSS
parser reads ``} *`` as a single selector prelude and merges it with the following
``{...}`` block into one **invalid rule that is dropped whole**. The file keeps a
perfectly good-looking ``*{box-sizing:border-box}`` line while the computed style of every
element says ``content-box``.

That is exactly what happened here, and the symptom did not look like a CSS bug at all: the
framework's boxes were never border-box, so every ``width:100%`` input with padding was
22px too wide (``padding:7px 10px`` plus ``border:1px`` counted twice), which surfaced as a
horizontal scrollbar inside a panel.

The decisive test is therefore not "is the brace count even" -- it is **does the rule that
declares this property have a sane selector**. A merged rule's prelude contains the stray
``}``, so checking the prelude catches the bug and its cause in one assertion.

Usage:  python scripts/check_css_braces.py
Exit 1 on any problem, so it can gate a check suite.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "scripts" / "meeting" / "ui.html"

# Each property/selector must sit in a rule whose prelude is a real prelude. The property
# ones are declarations, so the check also confirms the selector that carries them.
WATCH = (
    ("box-sizing", "decl"),
    ("@media (max-width:1500px)", "prelude"),
    ("@media (max-width:1240px)", "prelude"),
    ("@media (max-width:1060px)", "prelude"),
    ("#settings,#imp", "prelude"),
    (".st-box", "prelude"),
)


def _mask_comments(css: str) -> str:
    """Blank out comments, preserving every offset and line break.

    Without this, a watch token matches the mention of itself inside its own explanatory
    comment -- the comment above the rule quotes it, so the check would read its own
    documentation as evidence. A checker that does that is worse than none.
    """
    out = list(css)
    i = 0
    while i < len(css):
        if css.startswith("/*", i):
            end = css.find("*/", i + 2)
            end = len(css) if end < 0 else end + 2
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
            continue
        i += 1
    return "".join(out)


def _rules(css: str) -> list[tuple[str, int, int, int]]:
    """Return ``(prelude, start, body_start, depth)`` for every rule.

    ``prelude`` is the selector text between the end of the previous rule and its ``{``. A
    stray brace shows up here as a leading ``}`` -- which is the whole point.
    """
    rules = []
    i, n = 0, len(css)
    depth = 0
    prelude_start = 0
    while i < n:
        c = css[i]
        if c == "{":
            prelude = css[prelude_start:i]
            rules.append((prelude, prelude_start, i, depth))
            depth += 1
            prelude_start = i + 1
        elif c == "}":
            depth -= 1
            prelude_start = i + 1
            if depth < 0:
                depth = 0
        i += 1
    return rules


def main() -> int:
    text = UI.read_text(encoding="utf-8")
    blocks = list(re.finditer(r"<style[^>]*>(.*?)</style>", text, re.S))
    print(f"ui.html: {len(blocks)} 个 style 块")
    problems = 0

    for bi, m in enumerate(blocks, 1):
        raw = m.group(1)
        css = _mask_comments(raw)
        start_line = text[: m.start(1)].count("\n") + 1
        line_of = lambda off: start_line + raw[:off].count("\n")  # noqa: E731

        rules = _rules(css)

        # 1. brace balance: a stray } or an unclosed { both shift every later rule
        depth, stray = 0, []
        for i, c in enumerate(css):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth < 0:
                    stray.append(i)
                    depth = 0
        for i in stray:
            print(f"  [错误] 第 {line_of(i)} 行有多余的 }} —— "
                  f"它会把紧随其后的规则吞掉：")
            print(f"         {raw[i:i + 110].splitlines()[0] if raw[i:i + 110] else ''}")
            problems += 1
        if depth > 0:
            print(f"  [错误] 第 {bi} 块结束时仍有 {depth} 个未闭合的 {{")
            problems += 1

        # 2. every rule's prelude must be a real prelude
        bad_preludes = []
        for prelude, pstart, bstart, d in rules:
            stripped = prelude.strip()
            if not stripped:
                # `@media { .a{} }` nests; an empty prelude right after a `}` is normal
                # only when the enclosing block just closed. Flag only a `}` that has
                # text after it, which is the merged-rule signature.
                continue
            if "}" in stripped or "{" in stripped:
                bad_preludes.append((stripped, pstart))
        for stripped, pstart in bad_preludes:
            print(f"  [错误] 第 {line_of(pstart)} 行的规则前奏里混进了括号：")
            print(f"         {stripped[:90]!r}  ← 选择器非法，整条规则会被丢弃")
            problems += 1

        # 3. the rules we actually depend on must exist, with the right selector
        for token, kind in WATCH:
            hits = [r for r in rules if token in r[0]]
            if not hits:
                # a declaration token sits in a body, not a prelude -- find its rule
                idx = css.find(token)
                if idx < 0:
                    print(f"  [警告] 找不到 {token}")
                    problems += 1
                    continue
                owner = None
                for prelude, pstart, bstart, d in rules:
                    close = css.find("}", bstart)
                    if bstart < idx < (close if close > 0 else len(css)):
                        owner = (prelude, pstart, bstart, d)
                if owner is None:
                    print(f"  [错误] {token} 不在任何规则里（第 {line_of(idx)} 行）")
                    problems += 1
                    continue
                sel = owner[0].strip().replace("\n", " ")
                ok = bool(sel) and "}" not in sel
                print(f"  {token:<26} 第 {line_of(idx):>5} 行  声明在规则 {sel!r} 里  "
                      f"{'ok' if ok else '被吞'}")
                if not ok:
                    problems += 1
            else:
                prelude, pstart, bstart, d = hits[0]
                sel = prelude.strip().replace("\n", " ")
                ok = "}" not in sel and "{" not in sel
                print(f"  {token:<26} 第 {line_of(pstart):>5} 行  前奏 {sel!r}  "
                      f"depth={d}  {'ok' if ok else '被吞'}")
                if not ok:
                    problems += 1

    print(f"\n结论：{'通过' if not problems else str(problems) + ' 个问题'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
