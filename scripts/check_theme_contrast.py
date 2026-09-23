"""Verify that a theme's colours actually pass contrast, in both themes.

`audit_ui.js` checks contrast on the *rendered page*, which catches whatever the visible
elements happen to use. This checks the palettes themselves, exhaustively -- every semantic
colour against every background it is ever placed on -- so a combination that is not on
screen during the audit is still covered.

The reason this matters for the dark theme specifically: the light palette was measured and
tuned (see ``pick_contrast.py``), and a dark palette made by swapping values is a set of
ratios nobody has computed. Dark mode failures are also the quiet kind -- a dim grey on a
dark grey reads as "design choice" rather than as a bug, until someone tries to read a
timestamp in a meeting room.

Usage:
    python scripts/check_theme_contrast.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pick_contrast import ratio  # noqa: E402

UI = HERE / "meeting" / "ui.html"

# Minimum ratio per use. WCAG AA: 4.5 for body text, 3.0 for large text and for non-text
# indicators that carry meaning (a 3px colour bar standing in for a type label).
AA_TEXT = 4.5
AA_LARGE = 3.0
AA_NONTEXT = 3.0

# (token, is_it_text) -- semantic colours are both (a badge label on their own tinted
# background, and a 3px bar on the card background), so both are checked.
SEMANTIC = ["req", "com", "date", "risk", "agr", "clause", "dec", "term", "who", "org"]


def read_theme(html: str, selector: str) -> dict:
    """Pull the custom properties out of one theme block."""
    i = html.find(selector)
    if i < 0:
        raise SystemExit(f"找不到 {selector}")
    j = html.find("\n}", i)
    block = html[i:j]
    out: dict[str, str] = {}
    for m in re.finditer(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", block):
        out[m.group(1)] = m.group(2)
    return out


def check(label: str, theme: dict) -> list[tuple]:
    bg = theme.get("bg", "#ffffff")
    surf = theme.get("surface", bg)
    s2 = theme.get("surface-2", surf)
    s3 = theme.get("surface-3", s2)
    note = theme.get("note-bg", surf)
    note_in = theme.get("note-inner", surf)
    problems = []

    def need(name: str, fg: str, bgs: list[tuple[str, str]], minimum: float) -> None:
        for bname, b in bgs:
            if not fg or not b:
                continue
            r = ratio(fg, b)
            if r < minimum:
                problems.append((name, bname, round(r, 2), minimum))

    text_bgs = [("底色", bg), ("卡片", surf), ("surface-2", s2), ("便签", note)]

    # Body text levels.
    need("--text", theme.get("text"), text_bgs, AA_TEXT)
    need("--text-2", theme.get("text-2"), text_bgs, AA_TEXT)
    need("--text-3", theme.get("text-3"), text_bgs, AA_TEXT)
    # --text-4 is explicitly decorative (separators, hints whose meaning is positional),
    # so it is held to the non-text threshold.
    need("--text-4", theme.get("text-4"), text_bgs, AA_NONTEXT)

    # Accent: link text and the primary button's background.
    need("--accent(文字)", theme.get("accent"), text_bgs, AA_TEXT)
    need("--accent 上的字", theme.get("on-accent"), [("accent", theme.get("accent", "#000"))],
         AA_TEXT)

    # Every semantic colour: as text on its own tint, and as a 3px bar on card surfaces.
    for k in SEMANTIC:
        fg = theme.get(k)
        tint = theme.get(f"{k}-bg")
        need(f"{k} 在其浅底上", fg, [(f"{k}-bg", tint or surf)], AA_TEXT)
        need(f"{k} 色条", fg, [("卡片", surf), ("surface-3", s3)], AA_NONTEXT)

    # A pinned clue uses the note background; a note's inner reference card sits on the note.
    need("--text 在便签内层", theme.get("text"), [("便签内层", note_in)], AA_TEXT)
    return problems


def main() -> int:
    html = UI.read_text(encoding="utf-8")
    light = read_theme(html, "\n:root{")
    dark = read_theme(html, ':root[data-theme="dark"]{')

    print(f"亮色令牌 {len(light)} 个，暗色令牌 {len(dark)} 个")
    missing = [k for k in light if k not in dark and not k.startswith("radius")]
    if missing:
        print(f"暗色缺少的令牌: {missing}")
    print()

    total = 0
    for label, theme in (("亮色", light), ("暗色", dark)):
        problems = check(label, theme)
        total += len(problems)
        print("=" * 78)
        print(f"{label}主题")
        print("=" * 78)
        if not problems:
            print("  全部达标")
        else:
            for name, bname, r, minimum in problems:
                print(f"  ✗ {name:<22} 对 {bname:<12} {r:>5.2f}:1  (需 {minimum})")
        print()

    # Also report the actual numbers for the most-used pairs, so a change can be reviewed
    # rather than merely accepted.
    print("=" * 78)
    print("关键组合实测值")
    print("=" * 78)
    print(f"  {'用途':<22}{'亮色':>10}{'暗色':>10}")
    for k, label in (("text", "正文"), ("text-2", "次要正文"), ("text-3", "小字/时间戳"),
                     ("text-4", "装饰"), ("accent", "强调"),
                     ("req", "甲方要求"), ("com", "我方承诺"), ("date", "时间节点"),
                     ("risk", "风险"), ("clause", "条款依据")):
        rl = ratio(light[k], light["bg"]) if k in light else float("nan")
        rd = ratio(dark[k], dark["bg"]) if k in dark else float("nan")
        print(f"  {label:<22}{rl:>9.2f}{rd:>10.2f}")

    print()
    print(f"结论：{'两个主题都达标' if total == 0 else f'{total} 处不达标'}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
