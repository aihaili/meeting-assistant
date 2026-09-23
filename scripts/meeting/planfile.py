"""The speaking plan as a real Markdown file in the project folder.

Why a file rather than a field in ``sessions/*.json``:

* **The plan is the user's document, not the assistant's record.** They write it before the
  meeting, revise it during, and expect to keep it afterwards. A file they can open in
  Obsidian, version, mail to themselves or print is a different object from a JSON blob
  inside a session log.
* **It has to travel with the project.** The project folder already syncs into the project
  knowledge base (see ``rag/sync.py``), so a plan written there is *retrievable* by the
  assistant in later meetings -- "上次我说过要请甲方确认测评机构" becomes answerable.
* **The tags belong to the file.** ``kind`` (汇报 / 请确认 / 风险 / 跟进) is what drives the
  note's colour band. Storing it beside each item here means the labels survive a round trip
  through any editor; if kinds lived only in the session, hand-editing the file would strip
  the colour off every note.

Format. Deliberately something a human would write and a parser can still read exactly:

    ## 1. 汇报联调联试进展

    <!-- plan id=p_1 kind=report done=false x=120 y=340 -->

    进度、卡点、需要谁配合。

    - 参考：合同 5.2 交付节点 — 联调联试应在 9 月 20 日前完成

Every machine field is inside one HTML comment, so Obsidian renders the document as clean
headings, paragraphs and bullets. The comment is optional on read: a file typed by hand
without it still loads, and gets a fresh id and a default kind.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path

PLAN_NAME = "发言计划.md"
PLAN_KINDS = ("topic", "report", "confirm", "risk", "followup")
KIND_LABELS = {
    "topic": "要点",
    "report": "汇报",
    "confirm": "请确认",
    "risk": "风险",
    "followup": "跟进",
}

_META_RE = re.compile(r"<!--\s*plan\s*(.*?)-->", re.S)
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\"(?:[^\"\\]|\\.)*\"|\S+)")
_ITEM_RE = re.compile(r"^##\s+(?:\d+[.、]\s*)?(.*)$")
_REF_RE = re.compile(r"^\s*[-*]\s*(?:参考|依据)\s*[:：]\s*(.*)$")
# Trailing "［公共库］" marker written by render(); parse() takes it back off, which is what
# keeps the line stable across saves instead of growing a marker per round trip.
_KB_TAIL_RE = re.compile(r"［([^［］]+?)库］\s*$")
_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def plan_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / PLAN_NAME


# ── writing ─────────────────────────────────────────────────────────────


def _q(value) -> str:
    """Quote a meta value only when it needs it, so ids and numbers stay readable.

    Numbers are normalised to integers when they are whole. Without this the file is not
    stable across a save: ``z=1`` is read back as the float ``1.0`` and written as
    ``z=1.0``, so every plan edit rewrites every layout number. The round-trip test caught
    it on the second pass -- a single save looks perfectly fine.
    """
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = "" if value is None else str(value)
    if s and re.fullmatch(r"[^\s\"'<>]+", s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unq(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        body = value[1:-1]
        return body.replace('\\"', '"').replace("\\\\", "\\")
    return value


def _one_line(text: str) -> str:
    """Collapse to one line: the topic lives in a heading, and a newline in a heading
    would silently split one item into two on the next read."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def render(items, *, project: str = "", meeting: str = "", now=None) -> str:
    """Serialise plan items (dataclasses or dicts) to the Markdown file."""
    now = now or _dt.datetime.now()
    out: list[str] = ["---", "type: speaking-plan"]
    if project:
        out.append(f"project: {_one_line(project)}")
    if meeting:
        out.append(f"meeting: {_one_line(meeting)}")
    out.append(f"updated: {now.strftime('%Y-%m-%dT%H:%M:%S')}")
    out.append(f"items: {len(items)}")
    out.append("---")
    out.append("")
    out.append("# 我的发言计划")
    out.append("")
    out.append("> 这份文件就是发言计划的真身：助理每次改动都会重写它，"
               "你也可以直接在 Obsidian 里改，改完在助理里点「从文件重载」。")
    out.append("> 每一项的彩色标签写在标题下面那行注释里，改 `kind` 就能换颜色。")
    out.append("")

    for n, it in enumerate(items, 1):
        get = it.get if isinstance(it, dict) else lambda k, d=None: getattr(it, k, d)
        topic = _one_line(get("topic")) or "（未命名）"
        kind = get("kind") or "topic"
        if kind not in PLAN_KINDS:
            kind = "topic"
        meta = [f"id={_q(get('id'))}", f"kind={kind}",
                f"done={'true' if get('done') else 'false'}"]
        if get("source"):
            meta.append(f"src={_q(_one_line(get('source')))}")
        if get("seg_id"):
            meta.append(f"seg={_q(get('seg_id'))}")
        for key, name in (("nx", "x"), ("ny", "y"), ("nw", "w"), ("nz", "z")):
            v = get(key)
            # ``nw == 0`` means "auto" (the UI decides from the lane width), so writing
            # ``w=0`` would read back as a real zero-width note. Skip falsy layout numbers.
            if v:
                meta.append(f"{name}={_q(v)}")
        out.append(f"## {n}. {topic}")
        out.append("")
        out.append("<!-- plan " + " ".join(meta) + " -->")
        out.append("")
        detail = str(get("detail") or "").strip()
        if detail:
            out.append(detail)
            out.append("")
        for ref in (get("refs") or []):
            if isinstance(ref, dict):
                # Two shapes reach here. A session ref uses ``title``/``snippet``; a ref
                # read back from this file uses ``source``/``detail``. Rendering only the
                # first pair meant the passage itself was silently dropped from the file --
                # the title survived and the evidence did not, which defeats the point of
                # attaching a reference at all.
                src = _one_line(ref.get("source") or ref.get("title") or "")
                d = _one_line(ref.get("detail") or ref.get("snippet") or ref.get("text") or "")
                body = f"{src} — {d}" if src and d else (src or d)
                # The corpus goes on the line as a trailing marker that parse() takes back
                # off. Appending it without an inverse read would grow the line on every
                # save ("［公共库］［公共库］"), because the marker would be read as part of
                # the snippet and then written again.
                kb = _one_line(ref.get("kb") or "")
                if kb and body:
                    body = f"{body}［{kb}库］"
            else:
                body = _one_line(ref)
            if body:
                out.append(f"- 参考：{body}")
        if get("refs"):
            out.append("")

    return "\n".join(out).rstrip("\n") + "\n"


def write(path: str | os.PathLike, items, *, project: str = "", meeting: str = "",
          now=None) -> Path:
    """Write atomically.

    The file is rewritten on every plan change, so a crash mid-write would leave the user's
    own document truncated. Write beside it and replace.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = render(items, project=project, meeting=meeting, now=now)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return p


# ── reading ─────────────────────────────────────────────────────────────


def parse(text: str) -> tuple[list[dict], dict]:
    """Parse the file back. Returns ``(items, frontmatter)``.

    Tolerant by design: a hand-written file with no meta comment, no numbering, or
    ``- 参考：`` written as ``- 依据：`` must still load. Being strict here would mean the
    user's own edits get rejected by their own tool, which is the wrong way round.
    """
    meta: dict[str, str] = {}
    m = _FRONT_RE.match(text)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        text = text[m.end():]

    items: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        h = _ITEM_RE.match(line)
        if h and not line.startswith("###"):
            if cur is not None:
                items.append(cur)
            # Every optional field is present with its default, so a caller can read
            # ``it["nx"]`` without a KeyError. A shape that changes depending on which
            # meta keys happened to be written is how "the note jumped back to the
            # corner" bugs get in: the layout pass reads None, the reader got no key.
            #
            # ``nw=0`` means "auto" and must stay 0 here -- defaulting it to the old
            # fixed 232 made every reload look like "the user resized this", and a reload
            # would then hand 232 to notes the layout wanted at 172, overlapping them.
            cur = {"topic": _one_line(h.group(1)), "kind": "topic", "detail": "",
                   "refs": [], "done": False, "source": "", "seg_id": "",
                   "nx": None, "ny": None, "nw": 0.0, "nz": 0.0}
            continue
        if cur is None:
            continue
        mm = _META_RE.search(line)
        if mm:
            for k, v in _KV_RE.findall(mm.group(1)):
                v = _unq(v)
                if k == "id":
                    cur["id"] = v
                elif k == "kind":
                    cur["kind"] = v if v in PLAN_KINDS else "topic"
                elif k == "done":
                    cur["done"] = v.lower() in ("1", "true", "yes", "是")
                elif k == "src":
                    cur["source"] = v
                elif k == "seg":
                    cur["seg_id"] = v
                elif k in ("x", "y", "w", "z"):
                    try:
                        cur[{"x": "nx", "y": "ny", "w": "nw", "z": "nz"}[k]] = float(v)
                    except ValueError:
                        pass
            continue
        r = _REF_RE.match(line)
        if r:
            body = _one_line(r.group(1))
            kb = ""
            km = _KB_TAIL_RE.search(body)
            if km:
                kb = km.group(1)
                body = body[:km.start()].strip()
            if body:
                # "来源 — 说明" is how render() writes it; keep the split so a reload does
                # not glue the two fields into one and lose the structure on the next save.
                if " — " in body:
                    src, d = body.split(" — ", 1)
                    ref = {"source": src.strip(), "detail": d.strip()}
                else:
                    ref = {"source": body, "detail": ""}
                if kb:
                    ref["kb"] = kb
                cur["refs"].append(ref)
            continue
        if line.startswith((">", "<!--")):
            continue
        if line.strip():
            cur["detail"] = (cur["detail"] + "\n" + line.strip()).strip()
    if cur is not None:
        items.append(cur)

    # Drop the document title and any stray heading the user left empty
    items = [it for it in items if it.get("topic")]
    return items, meta


def load(path: str | os.PathLike) -> tuple[list[dict], dict]:
    p = Path(path)
    if not p.is_file():
        return [], {}
    return parse(p.read_text(encoding="utf-8"))
