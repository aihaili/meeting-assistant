"""Extract the inline <script> from ui.html and syntax-check it -- plus one semantic check.

Two things are checked, and the second one exists because the first cannot see it:

1. **语法。** The page is a single HTML file with the whole application inline, so a stray
   brace left behind by an edit takes down the entire UI at once -- and the browser reports
   only "Unexpected token '}'" with no line number in this harness. Node parses the extracted
   source and prints the exact line, which turns a hunt into a lookup.

2. **局部变量遮蔽外层函数。** A local ``const tick = ...`` inside a function silently
   shadows the top-level ``function tick()``, so every ``tick()`` call in that function
   throws "tick is not a function" -- **at runtime, only when that code path runs**.
   Syntax is fine, the render audit is fine, and the bug waits in a button nobody clicked.
   This file has now hit that twice (the other was a dataclass field named ``list`` shadowing
   the builtin). One grep-shaped heuristic finds it, so it is checked here.

Usage:
    python scripts/check_ui_syntax.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

UI = Path(__file__).resolve().parent / "meeting" / "ui.html"

# 顶层声明：`function name(` 或行首的 `const/let/var name =`
# 注意 `async function` 也要算——漏了它，遮蔽检查对 `async function tick()` 就完全失效，
# 而这次正好就是 tick。检验装置本身写错，是最容易骗过自己的失败模式：它会报"通过"。
_TOP_FUNC = re.compile(r"^(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", re.M)
_TOP_VAR = re.compile(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", re.M)
# 缩进的局部声明（函数体内）
_INNER_DECL = re.compile(r"^[ \t]+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", re.M)


def check_shadowing(js: str, offset: int) -> int:
    """Report local declarations that shadow a top-level name in the same script block."""
    outer = set(_TOP_FUNC.findall(js)) | set(_TOP_VAR.findall(js))
    if not outer:
        return 0
    problems = 0
    for m in _INNER_DECL.finditer(js):
        name = m.group(1)
        if name not in outer:
            continue
        line = offset + js[:m.start()].count("\n") + 1
        print(f"  [错误] ui.html 第 {line} 行的局部变量 {name!r} "
              f"遮蔽了同名的外层函数/变量")
        print(f"         在这一层调用 {name}() 会抛 \"{name} is not a function\"，"
              f"而且只在运行时、只在这段代码被执行时才炸。")
        print(f"         改个局部名（例如 {name}Btn）即可。")
        problems += 1
    return problems


def main() -> int:
    html = UI.read_text(encoding="utf-8")
    # Every inline script, not just the first. The page has a small theme bootstrap in
    # <head> (it must run before first paint) plus the application in one large block near
    # </body>. Checking only the first silently validated 8 lines instead of 1300 and
    # reported "语法检查通过" for a file whose real script had never been parsed -- a
    # verification that passes for the wrong reason is worse than one that fails.
    blocks = list(re.finditer(r"<script>([\s\S]*?)</script>", html))
    if not blocks:
        print("找不到内联 <script>")
        return 1
    print(f"内联脚本 {len(blocks)} 块")

    fails = 0
    for n, m in enumerate(blocks, 1):
        js = m.group(1)
        offset = html[:m.start(1)].count("\n")
        print(f"  #{n}: {len(js.splitlines())} 行（ui.html 第 {offset + 1} 行起）", end="")
        tmp = Path(tempfile.gettempdir()) / f"_ui_check_{n}.js"
        tmp.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
        if r.returncode == 0:
            print("  通过")
        else:
            fails += 1
            print("  语法错误")
            reported = False
            for line in (r.stderr or "").splitlines():
                mm = re.search(r"_ui_check_\d+\.js:(\d+)", line)
                if mm and not reported:
                    reported = True
                    ln = int(mm.group(1))
                    print(f"      -> ui.html 第 {offset + ln} 行")
                    src = js.splitlines()
                    for i in range(max(0, ln - 4), min(len(src), ln + 3)):
                        mark = ">>" if i == ln - 1 else "  "
                        print(f"         {mark} {i + 1:5d}: {src[i][:96]}")
                    continue
                print("      " + line)
        fails += check_shadowing(js, offset)

    print("全部通过" if not fails else f"{fails} 个问题")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
