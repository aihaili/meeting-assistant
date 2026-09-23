"""Probe why a note drag does not reach the pointer.

The audit's trail showed the note moving 64px on the *first* pointermove and then the
same 64px on every subsequent one, instead of accumulating. That means the drag origin is
being reset between moves, or the horizontal clamp is engaging -- two very different bugs
with the same symptom.

Rather than reasoning about it further, this asks the page itself: it instruments the
note's own drag handler by wrapping `fetch` (to see what is being persisted) and reading
the note's inline style after each synthetic move, then prints every intermediate value.

Usage:
    python scripts/probe_note_drag.py [--port 8510] [--width 1680] [--height 1000]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

PROBE_JS = r"""
(() => {
  const note = document.querySelector("#notes-layer .note");
  if (!note) return { error: "没有便签" };
  const head = note.querySelector(".note-head");
  const hb = head.getBoundingClientRect();
  const sx = hb.left + hb.width / 2, sy = hb.top + hb.height / 2;

  const log = [];
  const fire = (type, x, y, pid) => {
    (type === "pointerdown" ? head : window).dispatchEvent(new PointerEvent(type, {
      bubbles: true, cancelable: true, clientX: x, clientY: y,
      pointerId: pid || 9, pointerType: "mouse", button: 0,
      buttons: type === "pointerup" ? 0 : 1,
    }));
  };

  // What the page persists, captured instead of sent.
  const saved = [];
  const realFetch = window.fetch;
  window.fetch = function (u, o) {
    if (typeof u === "string" && u.indexOf("/api/prep/layout") >= 0) {
      saved.push(o && o.body);
      return Promise.resolve(new Response("{}", { status: 200 }));
    }
    return realFetch.apply(this, arguments);
  };

  const read = () => ({
    left: note.style.left, top: note.style.top, width: note.style.width,
    rectLeft: Math.round(note.getBoundingClientRect().left),
  });

  log.push({ step: "start", ...read(), sx: Math.round(sx) });
  fire("pointerdown", sx, sy);

  const TX = sx + 500, TY = sy + 100;
  for (let i = 1; i <= 5; i++) {
    const mx = sx + (TX - sx) * i / 5, my = sy + (TY - sy) * i / 5;
    fire("pointermove", mx, my);
    log.push({ step: "move" + i, px: Math.round(mx), ...read() });
  }
  fire("pointerup", TX, TY);
  log.push({ step: "up", ...read() });

  return {
    log,
    innerWidth, innerHeight,
    savedBody: saved.length ? saved[0] : null,
    noteW: note.getBoundingClientRect().width,
  };
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8510)
    ap.add_argument("--debug-port", type=int, default=9345)
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=1000)
    args = ap.parse_args()

    import subprocess
    profile = Path(__import__("tempfile").gettempdir()) / f"edge-probe-{int(time.time())}"
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--hide-scrollbars",
         f"--remote-debugging-port={args.debug_port}",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    import urllib.request
    ws_url = None
    for _ in range(60):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{args.debug_port}/json/list", timeout=2) as r:
                pages = [t for t in json.loads(r.read()) if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
        except Exception:
            pass
        time.sleep(0.4)
    if not ws_url:
        proc.kill()
        print("无法连接浏览器调试端口")
        return 1

    import asyncio

    async def run():
        import websockets  # 不一定装了
        raise RuntimeError("unused")

    # 用 node 自带的 WebSocket 走 CDP，避免额外依赖
    node_script = f"""
const ws = new WebSocket({json.dumps(ws_url)});
let id = 0; const pending = new Map();
ws.addEventListener("message", ev => {{
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {{ pending.get(m.id)(m); pending.delete(m.id); }}
}});
const send = (method, params) => new Promise(res => {{
  const i = ++id; pending.set(i, res);
  ws.send(JSON.stringify({{ id: i, method, params: params || {{}} }}));
}});
ws.addEventListener("open", async () => {{
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride",
    {{ width: {args.width}, height: {args.height}, deviceScaleFactor: 1, mobile: false }});
  await send("Page.navigate", {{ url: "http://127.0.0.1:{args.port}/" }});
  await new Promise(r => setTimeout(r, 4000));
  const r = await send("Runtime.evaluate", {{
    expression: {json.dumps(PROBE_JS)}, returnByValue: true, awaitPromise: true }});
  if (r.result && r.result.exceptionDetails) {{
    console.log("EVAL ERROR: " + JSON.stringify(r.result.exceptionDetails));
  }} else {{
    console.log(JSON.stringify(r.result.result.value, null, 2));
  }}
  ws.close(); process.exit(0);
}});
ws.addEventListener("error", e => {{ console.error("WS ERROR"); process.exit(1); }});
"""
    tmp = Path(__import__("tempfile").gettempdir()) / "_probe_note.js"
    tmp.write_text(node_script, encoding="utf-8")
    r = subprocess.run(["node", str(tmp)], capture_output=True, text=True)
    proc.kill()
    print(r.stdout or r.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
