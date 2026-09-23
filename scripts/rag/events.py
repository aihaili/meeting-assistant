r"""CLI for the meeting-memory event table.

    python -m rag.events import <file-or-dir> [...]   # 导入纪要（自动选规则/LLM）
    python -m rag.events stats                       # 台账概览
    python -m rag.events aggregate [--type T] [--owner O] [--deadline]
    python -m rag.events timeline <关键词>            # 跨会议时间线
    python -m rag.events find <查询>                  # 按文本找条目
    python -m rag.events list [--type T] [--limit N]

Why a flat table rather than a graph: measured on a 7-meeting corpus, aggregation
questions ("甲方一共提出多少项要求") are unanswerable by similarity (top-8 recall 3/13)
yet exact under a type filter (13/13), while traversal needs *all* matching events --
which top-1 can never return. Relations are not what this corpus produces.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import extract as ex  # noqa: E402
from rag.event_store import EventStore  # noqa: E402

DEFAULT_DB = r"E:\markdown\meeting-assistant\data\events.db"
SUPPORTED = {".md", ".markdown", ".txt", ".docx"}


def _gather(targets: list[str]) -> list[Path]:
    out: list[Path] = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            out += [q for q in sorted(p.rglob("*")) if q.suffix.lower() in SUPPORTED]
        elif p.is_file() and p.suffix.lower() in SUPPORTED:
            out.append(p)
        else:
            print(f"  跳过（不支持或不存在）: {t}", file=sys.stderr)
    return out


def cmd_import(args) -> int:
    store = EventStore(args.db)
    files = _gather(args.targets)
    if not files:
        print("没有可导入的文件")
        return 1

    print(f"导入 {len(files)} 个文件 -> {args.db}\n")
    print(f"{'文件':44} {'路径':6} {'会议':6} {'事件':>4}  {'日期':12} 类型分布")
    print("-" * 110)
    tot_e = 0
    by_path: dict[str, int] = {}
    for f in files:
        try:
            mtg, evs, used = ex.extract_document(f, use_llm=not args.no_llm)
        except Exception as e:
            print(f"{f.name[:42]:44} 失败: {type(e).__name__}: {str(e)[:60]}")
            continue
        n = store.upsert_meeting(mtg, evs)
        tot_e += n
        by_path[used] = by_path.get(used, 0) + 1
        from collections import Counter
        dist = dict(Counter(e.type for e in evs).most_common())
        print(f"{f.name[:42]:44} {used:6} {mtg.meeting_id[:5]:6} {n:>4}  "
              f"{mtg.date or '(无)':12} {dist}")

    st = store.stats()
    print("-" * 110)
    print(f"新增 {tot_e} 条事件   路径分布 {by_path}")
    print(f"台账现有: {st['meetings']} 场会议 / {st['events']} 条事件 / "
          f"时间跨度 {st['date_range'][0] or '-'} ~ {st['date_range'][1] or '-'}")
    store.close()
    return 0


def cmd_stats(args) -> int:
    store = EventStore(args.db)
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    store.close()
    return 0


def cmd_aggregate(args) -> int:
    store = EventStore(args.db)
    dl = None if args.deadline is None else bool(args.deadline)
    r = store.aggregate(type=args.type, owner=args.owner, has_deadline=dl)
    print(f"  匹配 {r['total']} 条")
    print(f"  过滤条件: {json.dumps(r['filter'], ensure_ascii=False)}")
    if r["total"]:
        rows = store.list_by(type=args.type, owner=args.owner, has_deadline=dl,
                             limit=args.limit)
        print()
        for e in rows:
            dl_s = f" 期限:{e['deadline']}" if e["deadline"] else ""
            print(f"  {e['date'] or '(无日期)':12} [{e['type']:6}] {e['content'][:66]}{dl_s}")
        if r["total"] > len(rows):
            print(f"  … 还有 {r['total'] - len(rows)} 条（--limit 可调）")
    print(f"\n  全表分布: {r['by_type_all']}")
    store.close()
    return 0


def cmd_timeline(args) -> int:
    store = EventStore(args.db)
    r = store.timeline(args.keyword, limit=args.limit)
    print(f"『{r['keyword']}』 共 {r['count']} 条，跨度 {r['span'][0] or '-'} ~ {r['span'][1] or '-'}\n")
    for grp in r["timeline"]:
        print(f"  ── {grp['date']}")
        for e in grp["events"]:
            dl_s = f" 期限:{e['deadline']}" if e["deadline"] else ""
            ow = f" @{e['owner']}" if e["owner"] else ""
            print(f"     [{e['type']:6}] {e['content'][:70]}{ow}{dl_s}")
    if not r["count"]:
        print("  （无匹配）")
    store.close()
    return 0


def cmd_find(args) -> int:
    store = EventStore(args.db)
    hits = store.find(args.query, limit=args.limit)
    print(f"『{args.query}』 -> {len(hits)} 条\n")
    for h in hits:
        print(f"  {h['date'] or '(无日期)':12} [{h['type']:6}] {h['content'][:70]}")
        print(f"      来源: {h['source_ref']}")
    store.close()
    return 0


def cmd_list(args) -> int:
    store = EventStore(args.db)
    rows = store.list_by(type=args.type, limit=args.limit)
    for e in rows:
        print(f"  {e['date'] or '(无日期)':12} [{e['type']:6}] {e['content'][:70]}")
    print(f"\n  {len(rows)} 条")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rag.events", description="会议记忆台账 CLI")
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="导入纪要文件或目录（自动选规则/LLM）")
    p.add_argument("targets", nargs="+")
    p.add_argument("--no-llm", action="store_true", help="只用规则抽取，不调模型")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("stats", help="台账概览")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("aggregate", help="按类型/责任方/期限聚合（相似度检索做不到）")
    p.add_argument("--type", default=None)
    p.add_argument("--owner", default=None)
    p.add_argument("--deadline", type=int, default=None, choices=[0, 1],
                   help="1=只看有期限的，0=只看无期限的")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_aggregate)

    p = sub.add_parser("timeline", help="某关键词跨会议的时间线")
    p.add_argument("keyword")
    p.add_argument("--limit", type=int, default=60)
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("find", help="按文本找具体条目")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("list", help="列出条目")
    p.add_argument("--type", default=None)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_list)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
