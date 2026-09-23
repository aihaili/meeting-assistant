"""Meeting-assistant HTTP service (stdlib only, matching the existing wiki_server style).

Endpoints
    GET  /                     the demo page
    POST /api/segment          {"text": "...", "refine": bool} -> highlights + cards
    GET  /api/refine?term=...  per-hotspot drill-down (the hover path)
    POST /api/audio            {"path": "..."} -> ASR (FunASR GGUF) then segment
    GET  /api/stats            index + timing info
    GET  /api/demo             a segment pulled from the indexed corpus

Every endpoint returns JSON; the page does its own rendering.

The service exists to keep the expensive things warm: the RAG index and the ONNX
embedder are loaded once at startup. Measured cold vs warm on this box:
    cold process, first query   ~0.96 s   (index + int8 embedder + tokenizer)
    warm query                  ~5-16 ms
so a per-request process would lose the whole point of the design.

Start:
    python -u asst/server.py --db <index.db> --kb <corpus dir> --port 8500
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from asst.core import Assistant  # noqa: E402

# ASR binary: the FunASR llama.cpp/GGUF runtime (pure CLI, no port).
ASR_DEFAULT = Path(r"E:\models\gguf-asr")


class Handler(BaseHTTPRequestHandler):
    server_version = "plaud-asst/0.1"
    assistant: Assistant = None          # injected by main()
    asr_root: Path = ASR_DEFAULT
    ui_path: Path = _HERE / "ui.html"
    lock = threading.Lock()              # serialise model access

    # ── helpers ──────────────────────────────────────────────────────────

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):    # quieter default logging
        sys.stderr.write("[asst] %s\n" % (fmt % args))

    # ── routes ───────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            try:
                html = self.ui_path.read_bytes()
            except OSError as e:
                self._json({"error": f"ui.html missing: {e}"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if u.path == "/api/stats":
            a = self.assistant
            self._json({
                "index": a.stats,
                "load_ms": round(a.load_ms, 1),
                "warm_ms": round(a.warm_ms, 1),
                "top_k": a.top_k,
                "max_keywords": a.max_keywords,
                "asr_root": str(self.asr_root),
                "asr_available": (self.asr_root / "runtime").is_dir(),
            })
            return

        if u.path == "/api/refine":
            term = (q.get("term") or [""])[0].strip()
            if not term:
                self._json({"error": "term required"}, 400)
                return
            with self.lock:
                self._json(self.assistant.refine(term))
            return

        if u.path == "/api/demo":
            # hand back a real passage from the corpus so the page has something to show
            n = int((q.get("n") or ["1"])[0])
            with self.lock:
                rows = self.assistant.index.conn.execute(
                    "select text from chunks where length(text) between 120 and 400 "
                    "order by (id * 7919) % 1000 limit ?", (max(1, min(n, 5)),)).fetchall()
            docs = [r[0].replace("\n", " ") for r in rows]
            self._json({"segments": docs})
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        u = urlparse(self.path)

        if u.path == "/api/segment":
            data = self._body()
            text = (data.get("text") or "").strip()
            if not text:
                self._json({"error": "text required"}, 400)
                return
            try:
                with self.lock:
                    self._json(self.assistant.process(text, refine=bool(data.get("refine"))))
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            return

        if u.path == "/api/audio":
            data = self._body()
            path = (data.get("path") or "").strip()
            if not path or not Path(path).is_file():
                self._json({"error": f"audio path not found: {path!r}"}, 400)
                return
            t0 = time.time()
            try:
                text, asr_ms = self._transcribe(path)
            except Exception as e:
                self._json({"error": f"ASR failed: {type(e).__name__}: {e}"}, 500)
                return
            with self.lock:
                out = self.assistant.process(text, refine=bool(data.get("refine")))
            out["asr"] = {"text": text, "elapsed_ms": round(asr_ms, 1),
                          "total_ms": round((time.time() - t0) * 1000, 1)}
            self._json(out)
            return

        self._json({"error": "not found"}, 404)

    # ── ASR ──────────────────────────────────────────────────────────────

    def _transcribe(self, audio_path: str) -> tuple[str, float]:
        """Run the FunASR GGUF binary; stdout is the transcript, stderr is progress."""
        root = self.asr_root / "runtime"
        exe = root / "llama-funasr-sensevoice.exe"
        model = self.asr_root / "sensevoice-small-q8.gguf"
        vad = self.asr_root / "fsmn-vad.gguf"
        for p in (exe, model, vad):
            if not p.exists():
                raise FileNotFoundError(p)
        t0 = time.time()
        r = subprocess.run(
            [str(exe), "-m", str(model), "--vad", str(vad), "-a", audio_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 and not (r.stdout or "").strip():
            raise RuntimeError((r.stderr or "")[-300:])
        text = " ".join((r.stdout or "").split())
        return text, (time.time() - t0) * 1000


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="asst.server")
    ap.add_argument("--db", required=True, help="RAG index db path")
    ap.add_argument("--kb", required=True, help="corpus root the index was built from")
    ap.add_argument("--port", type=int, default=8500)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--max-keywords", type=int, default=6)
    ap.add_argument("--asr-root", default=str(ASR_DEFAULT))
    ap.add_argument("--no-llm", action="store_true",
                    help="skip LLM keyword extraction (use jieba); useful offline")
    args = ap.parse_args(argv)

    t0 = time.time()
    Handler.assistant = Assistant(db_path=args.db, kb_dir=args.kb, top_k=args.top_k,
                                  max_keywords=args.max_keywords,
                                  unused_llm=args.no_llm)
    Handler.asr_root = Path(args.asr_root)
    st = Handler.assistant.stats

    print("=" * 62)
    print("  plaud 实时会议助理")
    print(f"  index   : {st['db']}")
    print(f"  corpus  : {st['kb_dir']}")
    print(f"  chunks  : {st['chunks']}   embedder: {st['embedder']}")
    print(f"  startup : {(time.time()-t0):.2f}s (index {Handler.assistant.load_ms:.0f}ms, "
          f"warm {Handler.assistant.warm_ms:.0f}ms)")
    print(f"  ASR     : {'ready' if (Handler.asr_root/'runtime').is_dir() else 'missing'}"
          f"  ({Handler.asr_root})")
    print(f"  open    : http://{args.host}:{args.port}/")
    print("=" * 62)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
