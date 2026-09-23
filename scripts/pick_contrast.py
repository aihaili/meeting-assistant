"""Pick grey levels that actually pass WCAG AA on this palette.

Guessing a hex value and hoping is how you end up with timestamps nobody can read on a
projector. The relationship is nonlinear, so the candidates are computed rather than
eyeballed.

Contrast is checked against every background these greys are used on, because the same
grey sits on three different surfaces in this UI (page, panel, subtle panel) and passing
on one says nothing about the others.

Usage:
    python scripts/pick_contrast.py
"""

from __future__ import annotations

from itertools import product


def srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * srgb_to_lin(r) + 0.7152 * srgb_to_lin(g) + 0.0722 * srgb_to_lin(b)


def ratio(fg: str, bg: str) -> float:
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# The three surfaces text actually sits on in this UI.
BACKGROUNDS = {
    "page": "#f6f7f9",
    "surface": "#ffffff",
    "surface-2": "#f1f3f6",
}

# Candidates, from the current (too light) value downward in steps.
CANDIDATES = [
    "#8a93a3", "#7d8694", "#6f7887", "#656e7d", "#5f6878",
    "#5b6472", "#565f6d", "#525b68", "#4b5563", "#454e5b",
]


def main() -> int:
    print("灰色候选在各底色上的对比度（AA 正文需 ≥ 4.5，AA 大字需 ≥ 3.0）\n")
    header = f"{'色值':<10}" + "".join(f"{k:>12}" for k in BACKGROUNDS)
    print(header)
    print("-" * len(header))
    best = {}
    for c in CANDIDATES:
        row = f"{c:<10}"
        for name, bg in BACKGROUNDS.items():
            r = ratio(c, bg)
            row += f"{r:>9.2f}{'✓' if r >= 4.5 else ('~' if r >= 3.0 else ' '):>3}"
            if r >= 4.5 and name not in best:
                best[name] = c
        print(row)

    print("\n每个底色上第一个通过 4.5:1 的候选：")
    for name in BACKGROUNDS:
        print(f"  {name:<10} {best.get(name, '（没有候选达标，需要更深）')}")

    # Pick one value that passes on the *worst* background, so a single variable can be
    # used everywhere without per-context overrides.
    worst = max(BACKGROUNDS.values(), key=lambda bg: -ratio("#000000", bg))
    print(f"\n最苛刻的底色是 {worst}（对浅灰最不利）")
    for c in CANDIDATES:
        if all(ratio(c, bg) >= 4.5 for bg in BACKGROUNDS.values()):
            print(f"  单一安全值: {c}  " +
                  "  ".join(f"{k}={ratio(c, bg):.2f}" for k, bg in BACKGROUNDS.items()))
            break
    else:
        print("  没有候选能同时在三种底色上达标")

    # A deliberately quieter level for ornamental text (rules, separators, hints that
    # are already conveyed by position). 3:1 is the AA threshold for non-text.
    print("\n装饰性灰（≥3:1，仅用于非必要信息）：")
    for c in CANDIDATES:
        rs = {k: ratio(c, bg) for k, bg in BACKGROUNDS.items()}
        if all(v >= 3.0 for v in rs.values()):
            print(f"  {c}  " + "  ".join(f"{k}={v:.2f}" for k, v in rs.items()))
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
