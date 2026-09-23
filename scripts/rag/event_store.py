r"""Flat event table + the three query shapes the product actually needs.

Measured basis for this design (7-meeting synthetic corpus, 41 events):

  A. find-a-passage   "不得少于几人"                -> hybrid chunk retrieval, 14/15
  B. find-a-value     "验收报告编号是多少"           -> event retrieval, needs the clause
  C. aggregation      "甲方一共提出多少项要求"       -> IMPOSSIBLE by similarity:
                        top-8 recall of the target type was 3/13. A type filter answers
                        it EXACTLY (13/13).
  D. traversal        "感应器的到货时间怎么变的"     -> needs ALL matching events across
                        meetings, ordered by date; top-1 can only ever return one.

C and D are why an event table exists. They are also why a knowledge graph does NOT:
this corpus produces no entity-relation triples (of 11 apparent entity pairs, 5 were
substring artefacts), so relation extraction would add a layer for queries nobody asks.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from rag.extract import Event, Meeting, TYPES  # noqa: E402

SCHEMA_VERSION = 1

DDL = """
create table if not exists meetings (
    meeting_id   text primary key,
    date         text not null default '',
    title        text not null default '',
    source_path  text not null default '',
    source_kind  text not null default 'md',
    attendees    text not null default '[]',
    extractor    text not null default '',
    indexed_at   real not null default 0
);
create table if not exists events (
    id           integer primary key autoincrement,
    meeting_id   text not null,
    date         text not null default '',
    type         text not null,
    content      text not null,
    deadline     text not null default '',
    owner        text not null default '',
    section      text not null default '',
    source_ref   text not null default '',
    seq          integer not null default 0,
    unique(meeting_id, seq, content)
);
create index if not exists idx_events_type   on events(type);
create index if not exists idx_events_date   on events(date);
create index if not exists idx_events_owner  on events(owner);
create index if not exists idx_events_mtg    on events(meeting_id);
create virtual table if not exists events_fts using fts5(content, tokenize='unicode61');
create table if not exists meta (key text primary key, value text);
"""


class EventStore:
    """SQLite event table. Deliberately single-file and dependency-free."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        self.conn.execute("insert or replace into meta(key,value) values ('schema_version',?)",
                          (str(SCHEMA_VERSION),))
        self.conn.commit()

    # ── writes ───────────────────────────────────────────────────────────

    def upsert_meeting(self, meeting: Meeting, events: Iterable[Event]) -> int:
        """Replace one meeting's events atomically. Returns the event count."""
        import json

        self.conn.execute(
            "insert or replace into meetings"
            "(meeting_id,date,title,source_path,source_kind,attendees,extractor,indexed_at)"
            " values (?,?,?,?,?,?,?,?)",
            (meeting.meeting_id, meeting.date, meeting.title, meeting.source_path,
             meeting.source_kind, json.dumps(meeting.attendees, ensure_ascii=False),
             meeting.extractor, time.time()))

        old = [r[0] for r in self.conn.execute(
            "select id from events where meeting_id=?", (meeting.meeting_id,))]
        if old:
            qm = ",".join("?" * len(old))
            self.conn.execute(f"delete from events_fts where rowid in ({qm})", old)
            self.conn.execute(f"delete from events where id in ({qm})", old)

        from rag.rag_core import segment

        # Deduplicate on normalised content within this meeting. Real corpora repeat
        # themselves (a summary section restates a decision, a JSON block mirrors the
        # prose), and a timeline that lists the same fact twice reads as broken.
        import re as _re

        def norm(s: str) -> str:
            return _re.sub(r"[\s\W_]+", "", s or "")[:60]

        existing = {norm(r[0]) for r in self.conn.execute(
            "select content from events where meeting_id=?", (meeting.meeting_id,))}

        n = 0
        for ev in events:
            key = norm(ev.content)
            if not key or key in existing:
                continue
            existing.add(key)
            cur = self.conn.execute(
                "insert or ignore into events"
                "(meeting_id,date,type,content,deadline,owner,section,source_ref,seq)"
                " values (?,?,?,?,?,?,?,?,?)",
                (ev.meeting_id, meeting.date, ev.type, ev.content, ev.deadline,
                 ev.owner, ev.section, ev.source_ref, ev.seq))
            if cur.rowcount:
                self.conn.execute("insert into events_fts(rowid,content) values (?,?)",
                                  (cur.lastrowid, segment(ev.content)))
                n += 1
        self.conn.commit()
        return n

    def delete_meeting(self, meeting_id: str) -> None:
        old = [r[0] for r in self.conn.execute(
            "select id from events where meeting_id=?", (meeting_id,))]
        if old:
            qm = ",".join("?" * len(old))
            self.conn.execute(f"delete from events_fts where rowid in ({qm})", old)
        self.conn.execute("delete from events where meeting_id=?", (meeting_id,))
        self.conn.execute("delete from meetings where meeting_id=?", (meeting_id,))
        self.conn.commit()

    # ── C: aggregation (exact, by filter) ────────────────────────────────

    def aggregate(self, type: str | None = None, owner: str | None = None,
                  has_deadline: bool | None = None,
                  date_from: str = "", date_to: str = "") -> dict:
        """Count events matching a filter — the query shape similarity cannot serve."""
        where, args = [], []
        if type:
            where.append("type = ?")
            args.append(type)
        if owner:
            where.append("(owner = ? or content like ?)")
            args += [owner, f"%{owner}%"]
        if has_deadline is True:
            where.append("deadline != ''")
        elif has_deadline is False:
            where.append("deadline = ''")
        if date_from:
            where.append("date >= ?")
            args.append(date_from)
        if date_to:
            where.append("date <= ?")
            args.append(date_to)
        sql = "select count(*) from events" + (" where " + " and ".join(where) if where else "")
        total = self.conn.execute(sql, args).fetchone()[0]

        by_type = {r["type"]: r["n"] for r in self.conn.execute(
            "select type, count(*) n from events group by type order by n desc")}
        return {"total": total, "filter": {
            "type": type, "owner": owner, "has_deadline": has_deadline,
            "date_from": date_from, "date_to": date_to}, "by_type_all": by_type}

    def list_by(self, type: str | None = None, owner: str | None = None,
                has_deadline: bool | None = None, limit: int = 50) -> list[dict]:
        where, args = [], []
        if type:
            where.append("type = ?")
            args.append(type)
        if owner:
            where.append("(owner = ? or content like ?)")
            args += [owner, f"%{owner}%"]
        if has_deadline is True:
            where.append("deadline != ''")
        sql = ("select e.*, m.title from events e left join meetings m on m.meeting_id=e.meeting_id"
               + (" where " + " and ".join(where) if where else "")
               + " order by e.date, e.seq limit ?")
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    # ── D: traversal (an entity/topic across all meetings, in order) ─────

    def timeline(self, keyword: str, limit: int = 60) -> dict:
        """Every event mentioning ``keyword``, oldest first, grouped by meeting."""
        rows = [dict(r) for r in self.conn.execute(
            "select e.*, m.title from events e left join meetings m on m.meeting_id=e.meeting_id"
            " where e.content like ? order by e.date, e.seq limit ?",
            (f"%{keyword}%", limit))]
        by_meeting: dict[str, list[dict]] = {}
        for r in rows:
            by_meeting.setdefault(r["date"] or r["meeting_id"], []).append(r)
        return {
            "keyword": keyword,
            "count": len(rows),
            "span": (min((r["date"] for r in rows if r["date"]), default=""),
                     max((r["date"] for r in rows if r["date"]), default="")),
            "timeline": [{"date": d, "events": evs} for d, evs in sorted(by_meeting.items())],
        }

    # ── B: find specific events by text (FTS, not similarity) ────────────

    def find(self, query: str, limit: int = 10) -> list[dict]:
        from rag.rag_core import build_fts_query, segment

        expr = build_fts_query(query)
        if not expr:
            return []
        try:
            rows = self.conn.execute(
                "select e.*, bm25(events_fts) s from events_fts f "
                "join events e on e.id = f.rowid where events_fts match ? "
                "order by s limit ?", (expr, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    # ── introspection ────────────────────────────────────────────────────

    def stats(self) -> dict:
        n_m = self.conn.execute("select count(*) from meetings").fetchone()[0]
        n_e = self.conn.execute("select count(*) from events").fetchone()[0]
        by_type = {r["type"]: r["n"] for r in self.conn.execute(
            "select type, count(*) n from events group by type order by n desc")}
        dates = [r[0] for r in self.conn.execute(
            "select distinct date from events where date != '' order by date")]
        return {
            "db": str(self.db_path),
            "meetings": n_m,
            "events": n_e,
            "by_type": by_type,
            "date_range": (dates[0], dates[-1]) if dates else ("", ""),
            "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
