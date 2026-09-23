"""Guard the injected-JS template literals in the browser-driving scripts.

The trap, which has now cost real time **six times** in this project:

    const PROBE_JS = `(async () => {
      // 这里写了一句注释，里面用了 `backtick`，于是整个模板字符串在这里就结束了
      ...
    })()`;

A backtick inside the template literal ends it early. What the browser or Node then reports
is a syntax error on some unrelated-looking line ("Unexpected identifier 'mine'"), which
reads like a selector or CSS bug. The file headers warn about it, and the warning does not
work -- so this checks it mechanically.

The test is exact: after the opening backtick of a ``NAME_JS = `...` `` literal, the next
backtick must be followed by optional whitespace and then ``;``. If it is not, the literal
was terminated early.

Usage:
    python scripts/check_injected_js.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
# 只有这几个脚本把 JS 当字符串注入浏览器
TARGETS = ["audit_ui.js", "probe_settings_ui.js", "probe_plan_ui.js", "probe_note_drag.js",
           "probe_guide_ui.js", "shoot_ui.js"]

_OPEN = re.compile(r"const\s+([A-Za-z_$][\w$]*_JS)\s*=\s*`")


def main() -> int:
    problems = 0
    checked = 0
    for name in TARGETS:
        path = SCRIPTS / name
        if not path.is_file():
            continue
        src = path.read_text(encoding="utf-8")
        for m in _OPEN.finditer(src):
            checked += 1
            var = m.group(1)
            open_at = m.end() - 1
            close_at = src.find("`", open_at + 1)
            start_line = src[:open_at].count("\n") + 1
            if close_at < 0:
                print(f"  [错误] {name} 的 {var} 没有结束的反引号")
                problems += 1
                continue
            end_line = src[:close_at].count("\n") + 1
            tail = src[close_at + 1: close_at + 40]
            if not re.match(r"^\s*;", tail):
                # 提前结束了：真正的结束符在更后面
                print(f"  [错误] {name} 的 {var}（第 {start_line} 行起）提前结束于第 "
                      f"{end_line} 行")
                print(f"         后面的内容是 {tail.splitlines()[0]!r}，不是 ';'。")
                print(f"         几乎可以肯定：模板字符串内部又写了一个反引号。")
                print(f"         把它改成普通引号或书名号，或者写成一行的说明。")
                line_txt = src.splitlines()[end_line - 1] if end_line - 1 < \
                    len(src.splitlines()) else ""
                print(f"         第 {end_line} 行: {line_txt.strip()[:96]}")
                problems += 1
            else:
                print(f"  {name:<24} {var:<14} 第 {start_line:>4}-{end_line:<4} 行  ok")
    if not checked:
        print("  [警告] 一个注入用的模板字符串都没找到——检查本身可能失效了")
        problems += 1
    print(f"\n结论：{'通过' if not problems else str(problems) + ' 个问题'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
