/**
 * 探针：热词悬浮框的出现/消失。
 *
 * 用户报的 bug 是"鼠标移开时悬浮框不消失，要移到别的热词才更新"。这类 bug 的特点是
 * **看代码像是做了**（确实有 mouseout 处理器），所以必须**在浏览器里真的走一遍鼠标事件**才算验过。
 *
 * 三种情形都要覆盖，少一种都可能放过 bug：
 *   ① 移到热词上  → 框出现
 *   ② 移开        → 框消失      ← 用户报的那条
 *   ③ 在热词**内部**移动（<mark> → 文本节点）→ 框**不**消失（否则会闪）
 *
 * 页面上可能还没有热词（会议刚开始 / 会话被清空），所以没有真实热词时会临时注入一个
 * 元素来测事件处理本身，跑完删掉，不留痕迹。
 *
 * Usage: node scripts/probe_hover_popup.js
 */

"use strict";
const http = require("http");
const { spawn } = require("child_process");
const os = require("os");
const path = require("path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}
const URL_ = arg("url", "http://127.0.0.1:8510/");
const PORT = parseInt(arg("debug-port", "9311"), 10);
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

const JS = `(async () => {
  const out = { checks: [], metrics: {} };
  const add = (lv, name, detail) => out.checks.push({ level: lv, name, detail });
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const pop = document.getElementById("pop");
  if (!pop) { add("fatal", "页面里没有 #pop", ""); return out; }

  const real = document.querySelectorAll("#feed .kw, .kw").length;
  out.metrics.real_kw = real;
  let el = document.querySelector("#feed .kw") || document.querySelector(".kw");
  let injected = false;
  if (!el) {
    // 没有真实热词：注入一个临时元素，**只测事件处理本身**
    el = document.createElement("span");
    el.className = "kw";
    el.dataset.term = "看守所";
    el.textContent = "看守所";
    el.style.cssText = "position:fixed;left:40px;top:120px;z-index:99999;padding:2px 4px";
    document.body.appendChild(el);
    injected = true;
    add("info", "页面上没有真实热词，用临时元素测事件处理", "");
  }

  // ① 移到热词上 → 框出现
  el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
  await sleep(600);
  const shown = getComputedStyle(pop).display !== "none";
  out.metrics.after_enter = getComputedStyle(pop).display;
  if (!shown) add("error", "移到热词上没有出现悬浮框", out.metrics.after_enter);

  // ③ 在热词内部移动 → 不该消失（先测，免得被 ② 收掉后测不到）
  //    用一个真的子节点当 relatedTarget：热词内部通常有 <mark>
  let inner = el.querySelector("*") || el;
  el.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, relatedTarget: inner }));
  await sleep(250);
  const stillShown = getComputedStyle(pop).display !== "none";
  out.metrics.after_inner_move = getComputedStyle(pop).display;
  if (!stillShown) add("error", "在热词内部移动就收起了悬浮框（会闪）", out.metrics.after_inner_move);

  // ② 移开 → 框消失（**用户报的那条**）
  const outside = document.body;
  el.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, relatedTarget: outside }));
  await sleep(400);
  out.metrics.after_leave = getComputedStyle(pop).display;
  if (getComputedStyle(pop).display !== "none") {
    add("error", "鼠标移开后悬浮框仍然显示（用户报的 bug）", out.metrics.after_leave);
  }
  // 高亮类也该一并清掉，否则关键词会一直亮着
  out.metrics.kw_on = document.querySelectorAll(".kw.on").length;
  if (out.metrics.kw_on > 0) add("warning", "移开后关键词仍带 .on 高亮", String(out.metrics.kw_on));

  if (injected) el.remove();
  return out;
})()`;

async function main() {
  const profile = path.join(os.tmpdir(), "edge-probe-hover-" + Date.now());
  const proc = spawn(EDGE, ["--headless=new", "--disable-gpu", "--no-first-run",
    "--no-default-browser-check", "--hide-scrollbars",
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, "about:blank"],
    { stdio: ["ignore", "pipe", "pipe"] });
  let err = "";
  proc.stderr.on("data", d => (err += d.toString()));

  let targets = null;
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    try {
      targets = (await getJSON(`http://127.0.0.1:${PORT}/json/list`))
        .filter(t => t.type === "page" && t.webSocketDebuggerUrl);
      if (targets.length) break;
    } catch (e) { /* 浏览器还没起来 */ }
    await sleep(400);
  }
  if (!targets || !targets.length) {
    console.error("无法连接 Edge。stderr:\n" + err.slice(-500));
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
    } else if (m.method) events.push(m);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const myId = ++id;
    pending.set(myId, { resolve, reject });
    ws.send(JSON.stringify({ id: myId, method, params }));
  });
  const evalJs = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  };

  await send("Runtime.enable");
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride",
    { width: 1680, height: 1000, deviceScaleFactor: 1, mobile: false });
  // ?guide=off：首次打开的使用引导会盖住整页，悬停摘要那一套断言会被它挡掉。
  await send("Page.navigate", { url: URL_ + (URL_.indexOf("?") >= 0 ? "&" : "?") + "guide=off" });
  await sleep(3200);

  console.log("=".repeat(70));
  console.log("热词悬浮框探针   " + URL_);
  console.log("=".repeat(70));
  let r;
  try {
    r = await evalJs(JS);
  } catch (e) {
    console.error("探针执行失败: " + e.message);
    ws.close(); proc.kill();
    return 1;
  }
  console.log(`页面上真实热词 ${r.metrics.real_kw} 个`);
  console.log(`移到热词上 : #pop display = ${r.metrics.after_enter}`);
  console.log(`内部移动   : #pop display = ${r.metrics.after_inner_move}`);
  console.log(`移开之后   : #pop display = ${r.metrics.after_leave}`);
  console.log("\n问题：");
  const bad = (r.checks || []).filter(c => c.level !== "info");
  if (!bad.length) console.log("  （无）");
  for (const c of bad) console.log(`  [${c.level.toUpperCase()}] ${c.name}  ${c.detail}`);
  const errors = bad.filter(c => c.level === "fatal" || c.level === "error").length;
  console.log(`\n结论：${errors} 错误 · ${bad.length - errors} 警告`);
  ws.close(); proc.kill();
  return errors ? 1 : 0;
}

main().then(c => process.exit(c)).catch(e => {
  console.error("探针失败: " + (e && e.stack || e));
  process.exit(1);
});
