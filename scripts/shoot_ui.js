/**
 * Screenshot the page, optionally with a panel open, so a human (or a model that can read
 * images) can look at the result.
 *
 * Why this exists alongside audit_ui.js: the audit computes things -- overflow, contrast,
 * column widths -- and it is good at that. But it passed a page that was visibly wrecked,
 * because a dialog that is missing its overlay CSS does not overflow anything and does not
 * fail any contrast rule; it just sits in the document flow and squeezes everything. That
 * bug was only found by looking at a screenshot. So: keep the computed audit for the things
 * arithmetic is good at, and take a picture for the things it is blind to.
 *
 * Usage:
 *   node scripts/shoot_ui.js --out data/ui-shots/main.png
 *   node scripts/shoot_ui.js --open plan --out data/ui-shots/plan.png
 *   node scripts/shoot_ui.js --open settings --theme light --width 1280
 *   node scripts/shoot_ui.js --open plan --open-panel-only
 */

"use strict";
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  if (i < 0) return dflt;
  const v = process.argv[i + 1];
  return v && !v.startsWith("--") ? v : true;
}

const OUT = arg("out", path.join("data", "ui-shots", "shot.png"));
const URL_ = arg("url", "http://127.0.0.1:8510/");
const WIDTH = parseInt(arg("width", "1680"), 10);
const HEIGHT = parseInt(arg("height", "1000"), 10);
const THEME = arg("theme", "dark");
const OPEN = arg("open", "");           // "" | plan | settings | import | search
const PORT = parseInt(arg("debug-port", "9371"), 10);
const SETTLE = parseInt(arg("settle", "2600"), 10);
const FULL = arg("full", false);

const sleep = ms => new Promise(r => setTimeout(r, ms));

function getJSON(url) {
  return new Promise((resolve, reject) => {
    http.get(url, res => {
      let b = "";
      res.on("data", c => (b += c));
      res.on("end", () => { try { resolve(JSON.parse(b)); } catch (e) { reject(e); } });
    }).on("error", reject);
  });
}

// 每个面板的打开方式。用点击而不是直接加 class：点击会跑真正的处理函数，
// 于是截图里看到的是用户点得到的样子，而不是我手动摆出来的样子。
const OPENERS = {
  // 发言计划已经没有弹窗了：这个入口改成把焦点放进写要点的输入框
  plan: "document.querySelector('#pp-input').focus()",
  settings: "document.querySelector('#btn-settings').click()",
  import: "document.querySelector('#pa-import').click()",
  search: "document.querySelector('#btn-search').click()",
  notes: "document.querySelector('#btn-notes').click()",
  // 使用引导。截图脚本启动的是全新 profile，页面自己会弹一次，但上面那一步已经
  // 按掉了它并写下标记，所以这里显式调 openGuide()——截图要的是稳定复现的那一屏。
  guide: "openGuide()",
  // 最后一步（快捷键清单）：6 步 = 点 5 次；多点一次会变成"开始使用"= 关掉引导
  "guide-keys": "openGuide(); for (let i = 0; i < 5; i++) document.querySelector('#g-next').click();",
};

async function main() {
  const profile = path.join(os.tmpdir(), "edge-shot-" + Date.now());
  const proc = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--no-default-browser-check", "--hide-scrollbars", "--force-device-scale-factor=1",
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, "about:blank"],
    { stdio: ["ignore", "pipe", "pipe"] });
  let stderr = "";
  proc.stderr.on("data", d => (stderr += d.toString()));

  let targets = null;
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    try {
      const list = await getJSON(`http://127.0.0.1:${PORT}/json/list`);
      targets = list.filter(t => t.type === "page" && t.webSocketDebuggerUrl);
      if (targets.length) break;
    } catch (e) { /* not up */ }
    await sleep(400);
  }
  if (!targets || !targets.length) {
    console.error("无法连接 Edge。stderr:\n" + stderr.slice(-600));
    proc.kill();
    return 1;
  }

  const ws = new WebSocket(targets[0].webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res);
    ws.addEventListener("error", rej);
  });
  const pending = new Map();
  let id = 0;
  const events = [];
  ws.addEventListener("message", ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
    } else if (m.method) { events.push(m); }
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const myId = ++id;
    pending.set(myId, { resolve, reject });
    ws.send(JSON.stringify({ id: myId, method, params }));
  });
  const evalJs = async (expression, awaitPromise = true) => {
    const r = await send("Runtime.evaluate",
      { expression, awaitPromise, returnByValue: true });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    }
    return r.result.value;
  };

  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride",
    { width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false });
  // ?guide=off：截图要的是界面，不是首次打开时盖在界面上的那层使用引导。
  await send("Page.navigate", { url: URL_ + (URL_.indexOf("?") >= 0 ? "&" : "?") + "guide=off" });
  await sleep(SETTLE);

  await evalJs('(() => { try { localStorage.setItem("plaud-theme", ' +
    JSON.stringify(THEME) + '); } catch (e) {} ' +
    'document.documentElement.setAttribute("data-theme", ' + JSON.stringify(THEME) +
    '); return 1; })()', false);
  await sleep(900);

  if (OPEN && OPENERS[OPEN]) {
    await evalJs(OPENERS[OPEN], false);
    await sleep(1500);
  } else if (OPEN) {
    console.error("未知面板: " + OPEN + "（可用: " + Object.keys(OPENERS).join(" / ") + "）");
    ws.close(); proc.kill();
    return 1;
  }

  const shot = await send("Page.captureScreenshot",
    { format: "png", captureBeyondViewport: !!FULL });
  const outPath = path.resolve(OUT);
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, Buffer.from(shot.data, "base64"));

  // 顺带把"页面是不是被挤坏了"量化出来，和截图一起看。
  const MEASURE_LIST = String(arg("measure", "") || "");
  const diag = await evalJs(`(() => {
    const r = n => { const b = n && n.getBoundingClientRect();
                     return b ? [Math.round(b.left), Math.round(b.top),
                                 Math.round(b.width), Math.round(b.height)] : null; };
    const cs = s => { const n = document.querySelector(s);
                      return n ? getComputedStyle(n) : null; };
    const panel = ${JSON.stringify(OPEN || "")};
    const sel = panel === 'settings' ? '#settings'
              : panel === 'import' ? '#imp' : null;
    const other = ['#settings', '#imp'].filter(s => s !== sel);
    return {
      main: r(document.querySelector('#main')),
      colLeft: r(document.querySelector('#col-left')),
      colMid: r(document.querySelector('#col-mid')),
      colRight: r(document.querySelector('#col-right')),
      panel: sel ? r(document.querySelector(sel)) : null,
      panelPos: sel && cs(sel) ? cs(sel).position : null,
      panelDisplay: sel && cs(sel) ? cs(sel).display : null,
      // 关着的面板绝不能占地方。这是那个"普通 div 待在文档流里"的 bug 的判据。
      others: other.map(s => ({ sel: s,
        display: cs(s) ? cs(s).display : null,
        box: r(document.querySelector(s)),
        position: cs(s) ? cs(s).position : null })),
      notes: [...document.querySelectorAll('#notes-layer .note')].map(n => ({
        id: n.dataset.id, kind: n.dataset.kind, box: r(n) })),
      docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      // 任意选择器的实测几何：靠猜"它为什么这么窄"不如把它量出来。
      measured: ${JSON.stringify(MEASURE_LIST)}.split(',')
        .map(s => s.trim()).filter(Boolean).map(s => {
          const n = document.querySelector(s);
          if (!n) return { sel: s, missing: true };
          const b = n.getBoundingClientRect();
          const c = getComputedStyle(n);
          return { sel: s, box: r(n), w: Math.round(b.width), h: Math.round(b.height),
                   display: c.display, overflowX: c.overflowX,
                   maxWidth: c.maxWidth, flex: c.flex, alignItems: c.alignItems,
                   scrollW: n.scrollWidth, clientW: n.clientWidth };
        }),
    };
  })()`);

  if (diag.measured && diag.measured.length) {
    console.log("实测选择器：");
    for (const m of diag.measured) {
      if (m.missing) { console.log("  " + m.sel + " —— 不存在"); continue; }
      console.log("  " + m.sel.padEnd(18) + JSON.stringify(m.box) +
        "  display=" + m.display + " maxW=" + m.maxWidth + " flex=" + m.flex);
      if (m.scrollW > m.clientW + 1) {
        console.log("      ! 内容溢出 " + m.scrollW + " > " + m.clientW);
      }
    }
  }

  console.log("截图写入 " + outPath +
    (fs.existsSync(outPath) ? "  (" + fs.statSync(outPath).size + " bytes)" : ""));
  console.log("视口 " + WIDTH + "x" + HEIGHT + " · 主题 " + THEME +
    (OPEN ? " · 打开 " + OPEN : ""));
  console.log("布局：");
  for (const k of ["main", "colLeft", "colMid", "colRight"]) {
    console.log("  " + k.padEnd(9) + JSON.stringify(diag[k]));
  }
  if (diag.panel) {
    console.log("  面板 " + JSON.stringify(diag.panel) + "  position=" + diag.panelPos +
      " display=" + diag.panelDisplay);
  }
  for (const o of diag.others) {
    console.log("  关闭的 " + o.sel + " display=" + o.display + " position=" + o.position +
      " box=" + JSON.stringify(o.box));
  }
  console.log("  文档横向溢出 " + diag.docOverflow + "px");
  console.log("  便签 " + diag.notes.length + " 张：");
  for (const n of diag.notes) {
    console.log("    " + (n.id || "?").padEnd(14) + " " + String(n.kind).padEnd(9) +
      JSON.stringify(n.box));
  }

  const exceptions = events.filter(e => e.method === "Runtime.exceptionThrown")
    .map(e => (e.params.exceptionDetails.exception?.description || "").split("\n")[0]);
  if (exceptions.length) {
    console.log("页面异常：");
    for (const e of exceptions.slice(0, 5)) console.log("  " + e.slice(0, 160));
  }

  ws.close(); proc.kill();
  return 0;
}

main().then(c => process.exit(c)).catch(e => {
  console.error("截图失败: " + (e && e.stack || e));
  process.exit(1);
});
