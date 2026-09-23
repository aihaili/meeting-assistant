"""Find how many RAG index instances exist and which one breaks under threads.

The old wiki server (port 8899) logged, on every semantic search:

    [rag] search failed, falling back to keyword scan:
    SQLite objects created in a thread can only be used in that same thread.

so it kept serving answers from a keyword fallback while claiming to do semantic search.
`rag/rag_core.py` is supposed to prevent exactly this with ``check_same_thread=False``
plus an internal lock, so either a second implementation exists somewhere or the flag is
not reaching the connection. This counts the constructors and prints each call site, which
turns "somewhere in the project" into a specific file and line.

Usage:
    python scripts/probe_rag_instances.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    print("=" * 78)
    print("① 所有 sqlite3.connect 调用")
    print("=" * 78)
    for f in sorted(ROOT.rglob("*.py")):
        if any(p in f.parts for p in ("venv", "__pycache__", "node_modules")):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "sqlite3.connect" in line:
                safe = "check_same_thread=False" in line
                rel = f.relative_to(ROOT)
                print(f"  {rel}:{i}")
                print(f"      {line.strip()[:110]}")
                print(f"      -> check_same_thread=False: {'有' if safe else '缺失 ← 多线程下会炸'}")
    print()

    print("=" * 78)
    print("② RagIndex 被构造的位置")
    print("=" * 78)
    for f in sorted(ROOT.rglob("*.py")):
        if any(p in f.parts for p in ("venv", "__pycache__", "node_modules")):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bRagIndex\s*\(", line) and "class RagIndex" not in line:
                print(f"  {f.relative_to(ROOT)}:{i}  {line.strip()[:100]}")
    print()

    print("=" * 78)
    print("③ rag_core 里 self.lock 的使用统计")
    print("=" * 78)
    core = ROOT / "scripts" / "rag" / "rag_core.py"
    if core.exists():
        text = core.read_text(encoding="utf-8")
        n_with = len(re.findall(r"with self\.lock", text))
        n_def = len(re.findall(r"def \w+\(", text))
        print(f"  {core.relative_to(ROOT)}")
        print(f"  方法数 {n_def}，其中用 with self.lock 保护的 {n_with} 处")
        # Which methods touch the connection but do not take the lock is the interesting
        # question: those are the ones that will raise under the wiki server's threads.
        for m in re.finditer(r"\n    def (\w+)\(self[^)]*\)[^:]*:\n(.*?)(?=\n    def |\Z)",
                             text, re.S):
            name, body = m.group(1), m.group(2)
            if ("self.conn" in body or "self._conn" in body) and "with self.lock" not in body:
                print(f"    ! {name}() 用了连接但没有 with self.lock")
    else:
        print("  找不到 rag_core.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
