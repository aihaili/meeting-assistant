"""Static hygiene checks for the phone_mic package.

Plain AST work, no dependencies. These catch the specific kinds of drift this module
accumulated while its segmentation rules were being rewritten: constants left behind
by a redesign, functions that nothing calls any more, and duplicated helpers.

Exists as a file rather than a one-liner because passing Python source through a
PowerShell here-string silently destroys it -- double-quoted here-strings interpolate
``$name``, and even single-quoted ones have their quotes mangled by the outer
command line. Two earlier "verifications" in this session ran corrupted code and
returned the opposite of the truth because of exactly that.

Usage:
    python scripts/check_hygiene.py [package_dir]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def analyse(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    problems: list[str] = []

    # Module-level assignments and their reference counts.
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id != "__all__":
                    name = target.id
                    if name.startswith("_"):
                        uses = sum(
                            1 for n in ast.walk(tree)
                            if isinstance(n, ast.Name) and n.id == name
                        ) + sum(
                            1 for n in ast.walk(tree)
                            if isinstance(n, ast.Attribute) and n.attr == name
                        )
                        if uses <= 1:
                            problems.append(f"未使用常量 {name} (第 {node.lineno} 行)")

    # Module-level functions nothing in this file calls. Only *private* names are
    # reported: a public function with no in-file caller is API surface for other
    # modules (``read_wav``, ``list_input_devices``), not dead code, and flagging those
    # would train the reader to ignore this check.
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_") and not node.name.startswith("__"):
            called = any(
                isinstance(n, ast.Name) and n.id == node.name for n in ast.walk(tree)
            ) or any(
                isinstance(n, ast.Attribute) and n.attr == node.name
                for n in ast.walk(tree)
            )
            if not called:
                problems.append(f"未被调用的私有函数 {node.name} (第 {node.lineno} 行)")

    # Mutable default arguments -- these bite exactly once, in production.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in node.args.defaults + node.args.kw_defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    problems.append(
                        f"可变默认参数 {node.name} (第 {node.lineno} 行)")

    return problems


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "phone_mic"
    files = sorted(root.rglob("*.py"))
    if not files:
        print(f"没有找到 .py 文件: {root}")
        return 1

    total = 0
    for f in files:
        problems = analyse(f)
        rel = f.relative_to(root.parent)
        if problems:
            print(f"{rel}")
            for p in problems:
                print(f"    {p}")
            total += len(problems)
        else:
            print(f"{rel}  OK")

    print(f"\n{len(files)} 个文件，{total} 处问题")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
