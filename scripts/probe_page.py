"""Open the page in a real browser and report what it actually shows.

"没有找到页面，显示错误" is a symptom, not a diagnosis. My own headless run reports a
clean page, so the disagreement has to be resolved by reading what the browser *renders*
rather than what the server sends: the page could be a 404 body, a partially built DOM,
or a fully built DOM whose visible text is an error message.

This prints the document title, the readiness state, the size of the built DOM, the
count of every element the UI depends on, and the visible text -- so a missing element and
an error page look completely different in the output.

Usage:
    python scripts/probe_page.py [--port 8510] [--path /]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

PROBE = r"""
(() => {
  const ids = ["main", "notes-layer", "feed", "clues", "people", "filters",
               "pa-list", "notes", "pop", "search", "detail", "btn-notes"];
  const present = {};
  for (const i of ids) present[i] = !!document.getElementById(i);
  const q = s => document.querySelectorAll(s).length;
  return {
    href: location.href,
    title: document.title,
    readyState: document.readyState,
    bodyHTMLLen: document.body ? document.body.innerHTML.length : -1,
    visibleTextHead: document.body ? document.body.innerText.slice(0, 400) : "",
    ids: present,
    counts: {
      utt: q("#feed .utt"), clue: q("#clues .clue"), person: q("#people .person"),
      note: q("#notes-layer .note"), agendaItem: q("#pa-list .pl-item"),
      filterChip: q("#filters .fchip"), kw: q("#feed .kw"),
    },
    scripts: document.scripts.length,
    styleSheets: document.styleSheets.length,
    hasInlineScript: [...document.scripts].some(s => !s.src && s.textContent.length > 5000),
  };
})()
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8510)
    ap.add_argument("--debug-port", type=int, default=9351)
    ap.add_argument("--width", type=int, default=1680)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--path", default="/")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}{args.path}"
    print(f"目标: {url}\n")

    # Raw HTTP first: this says whether the server is even involved in the problem.
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            body = r.read().decode("utf-8", "replace")
        print(f"HTTP {r.status}  {len(body)} 字节  Content-Type={r.headers.get('Content-Type')}")
        print(f"  开头: {body[:90]!r}")
        print(f"  结尾: {body[-90:]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"HTTP 取不到: {type(e).__name__}: {e}")
        return 1

    profile = Path(tempfile.gettempdir()) / f"edge-page-{int(time.time())}"
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--hide-scrollbars",
         f"--remote-debugging-port={args.debug_port}",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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

    node = f"""
const ws = new WebSocket({json.dumps(ws_url)});
let id = 0; const pending = new Map(); const events = [];
ws.addEventListener("message", ev => {{
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {{ pending.get(m.id)(m); pending.delete(m.id); }}
  else if (m.method) events.push(m);
}});
const send = (method, params) => new Promise(res => {{
  const i = ++id; pending.set(i, res);
  ws.send(JSON.stringify({{ id: i, method, params: params || {{}} }}));
}});
ws.addEventListener("open", async () => {{
  await send("Runtime.enable"); await send("Log.enable"); await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride",
    {{ width: {args.width}, height: {args.height}, deviceScaleFactor: 1, mobile: false }});
  await send("Page.navigate", {{ url: {json.dumps(url)} }});
  await new Promise(r => setTimeout(r, 4500));
  const r = await send("Runtime.evaluate",
    {{ expression: {json.dumps(PROBE)}, returnByValue: true }});
  const out = {{ probe: r.result && r.result.result ? r.result.result.value : null }};
  out.exceptions = events.filter(e => e.method === "Runtime.exceptionThrown")
    .map(e => (e.params.exceptionDetails.exception
      && e.params.exceptionDetails.exception.description || "").split("\\n").slice(0,3).join(" | "));
  out.logErrors = events.filter(e => e.method === "Log.entryAdded"
      && e.params.entry.level === "error").map(e => e.params.entry.text);
  out.consoleErrors = events.filter(e => e.method === "Runtime.consoleAPICalled"
      && e.params.type === "error")
    .map(e => e.params.args.map(a => a.value || a.description || "").join(" "));
  console.log(JSON.stringify(out, null, 2));
  ws.close(); process.exit(0);
}});
ws.addEventListener("error", () => {{ console.error("WS ERROR"); process.exit(1); }});
"""
    tmp = Path(tempfile.gettempdir()) / "_probe_page.js"
    tmp.write_text(node, encoding="utf-8")
    r = subprocess.run(["node", str(tmp)], capture_output=True, text=True)
    proc.kill()
    print("\n浏览器渲染结果:")
    print(r.stdout or r.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
