/**
 * Probe: the speaking plan without a dialog -- inline input + on-note tag dots.
 *
 * NOTE FOR EDITORS: PROBE_JS is a template literal. A backtick inside it ends the string
 * early and the error looks like a selector bug. check_injected_js.py guards this.
 *
 * This replaced a probe that drove the old pop-up editor. The editor is gone by request:
 *
 *     "我的发言计划其实最好不好搞弹出框，容易造成麻烦。其实最好是自己做个输入框，
 *      输入文字后自己点击新便签生成。"
 *
 * So the first assertion is that the dialog does **not** exist any more, and the rest drive
 * the new path: type, press Enter, a sticky note appears; click a colour dot on the note,
 * the category changes. Everything it creates, it removes.
 *
 * Usage: node scripts/probe_plan_ui.js [--url ...]
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
const PORT = parseInt(arg("debug-port", "9411"), 10);
const THEME = arg("theme", "dark");
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

const PROBE_JS = `(async () => {
  const out = { checks: [], metrics: {} };
  const add = (level, name, detail) => out.checks.push({ level, name, detail });
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const q = s => document.querySelector(s);
  const plan = async () => (await (await fetch('/api/plan')).json());

  // ── 0. 弹窗必须真的没了 ─────────────────────────────────────────────
  // 用户明确要求去掉它。留着隐藏的 DOM 会让"它已经不弹了"这件事没法验证。
  out.metrics.dialog_gone = !q('#plandlg') && !q('#btn-plan');
  if (!out.metrics.dialog_gone) {
    add('fatal', '发言计划的弹窗还在', '#plandlg 或 #btn-plan 仍然存在于 DOM 里');
  }

  // ── 1. 输入框与按钮 ────────────────────────────────────────────────
  const input = q('#pp-input');
  const addBtn = q('#pp-add');
  if (!input || !addBtn) {
    add('fatal', '没有写要点的输入框或「＋ 新便签」按钮',
        'input=' + !!input + ' button=' + !!addBtn);
    return out;
  }
  const ir = input.getBoundingClientRect();
  out.metrics.input_box = [Math.round(ir.left), Math.round(ir.top),
                           Math.round(ir.width), Math.round(ir.height)];
  if (ir.width < 120) add('error', '输入框太窄，写不下一条要点', Math.round(ir.width) + 'px');
  const ar = addBtn.getBoundingClientRect();
  if (ar.height < 22) add('warning', '「＋ 新便签」按钮偏小', Math.round(ar.height) + 'px');

  // 起点：清掉上一次可能留下的测试条目
  const MARK = '__输入框自测__';
  const stale = (await plan()).items.filter(x => x.topic.indexOf(MARK) === 0);
  for (const it of stale) {
    await fetch('/api/prep', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'remove', id: it.id }) });
  }
  if (stale.length) await sleep(700);
  const before = (await plan()).items.length;
  out.metrics.before = before;

  // ── 2. 打字 → 点按钮 → 便签出现 ────────────────────────────────────
  const text1 = MARK + '点击生成';
  input.value = text1;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  addBtn.click();
  await sleep(1400);
  let after = (await plan()).items;
  const made1 = after.find(x => x.topic === text1);
  out.metrics.made_by_click = !!made1;
  out.metrics.input_cleared = input.value === '';
  if (!made1) add('fatal', '点「＋ 新便签」没有生成便签', text1);
  if (!out.metrics.input_cleared) add('error', '生成后输入框没有清空', input.value);
  const dom1 = made1 ? q('#notes-layer .note[data-id="' + made1.id + '"]') : null;
  out.metrics.note_in_dom = !!dom1;
  if (made1 && !dom1) add('error', '便签生成了但界面上没有出现', made1.id);

  // ── 3. 回车也要能生成 ──────────────────────────────────────────────
  const text2 = MARK + '回车生成';
  input.value = text2;
  input.focus();
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true,
    cancelable: true }));
  await sleep(1400);
  after = (await plan()).items;
  const made2 = after.find(x => x.topic === text2);
  out.metrics.made_by_enter = !!made2;
  if (!made2) add('fatal', '在输入框里按回车没有生成便签', text2);

  // ── 4. 空输入要有反馈，不能悄悄什么都不做 ──────────────────────────
  const n0 = (await plan()).items.length;
  input.value = '   ';
  addBtn.click();
  await sleep(900);
  out.metrics.empty_ignored = (await plan()).items.length === n0;
  out.metrics.empty_msg = (q('#toast') || {}).textContent || '';
  if (!out.metrics.empty_ignored) add('error', '空输入也生成了便签', '');
  if (!out.metrics.empty_msg) {
    add('warning', '空输入时没有任何提示', '用户会以为按钮坏了');
  }

  // ── 5. 便签上的五个色点：点一下换类别 ──────────────────────────────
  if (made1) {
    const note = q('#notes-layer .note[data-id="' + made1.id + '"]');
    const dots = note ? [...note.querySelectorAll('.note-dot')] : [];
    out.metrics.dots_per_note = dots.length;
    if (dots.length !== 5) add('error', '每张便签的标签点不是 5 个', String(dots.length));
    const colors = new Set(dots.map(d => getComputedStyle(d).backgroundColor));
    out.metrics.dot_colors = colors.size;
    if (dots.length && colors.size < 4) {
      add('error', '标签点颜色区分度不够', colors.size + ' 种');
    }
    const on = note ? note.querySelectorAll('.note-dot.on').length : 0;
    out.metrics.dots_on = on;
    if (on !== 1) add('error', '每张便签应当恰好有一个选中的标签点', String(on));
    // 点第 4 个点（风险），确认落库
    if (dots[3]) {
      dots[3].click();
      await sleep(1300);
      const it = (await plan()).items.find(x => x.id === made1.id);
      out.metrics.kind_after_click = it ? it.kind : null;
      if (!it || it.kind !== 'risk') {
        add('fatal', '点便签上的标签点没有改类别',
            '期望 risk，得到 ' + (it && it.kind));
      }
      const note2 = q('#notes-layer .note[data-id="' + made1.id + '"]');
      out.metrics.band_class_after = note2 ? note2.dataset.kind : null;
      if (note2 && note2.dataset.kind !== 'risk') {
        add('error', '色带没有跟着标签变', String(note2.dataset.kind));
      }
    }
  }

  // ── 6. 重排与读文件两个按钮还在，且不报错 ──────────────────────────
  const reset = q('#pp-reset');
  const reload = q('#pp-reload');
  out.metrics.buttons = { reset: !!reset, reload: !!reload };
  if (!reset || !reload) add('error', '「重排」或「读文件」按钮不见了', '');

  // ── 7. 收拾：把这一趟造出来的便签删掉 ─────────────────────────────
  const junk = (await plan()).items.filter(x => x.topic.indexOf(MARK) === 0);
  for (const it of junk) {
    await fetch('/api/prep', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'remove', id: it.id }) });
  }
  await sleep(700);
  const end = (await plan()).items.length;
  out.metrics.after_cleanup = end;
  out.metrics.leftover = (await plan()).items.filter(x => x.topic.indexOf(MARK) === 0).length;
  if (out.metrics.leftover) add('error', '测试条目没删干净', String(out.metrics.leftover));
  if (end !== before) add('warning', '收尾后条数与开始时不同', before + ' -> ' + end);

  return out;
})()`;

async function main() {
  const profile = path.join(os.tmpdir(), "edge-probe-plan-" + Date.now());
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
    { width: 1680, height: 1000, deviceScaleFactor: 1, mobile: false });
  // ?guide=off：首次打开的使用引导是一层透明全屏层，会吃掉这个探针的点击与 Esc。
  await send("Page.navigate", { url: URL_ + (URL_.indexOf("?") >= 0 ? "&" : "?") + "guide=off" });
  await sleep(3200);
  await evalJs('(() => { try { localStorage.setItem("plaud-theme", ' +
    JSON.stringify(THEME) + '); } catch (e) {} ' +
    'document.documentElement.setAttribute("data-theme", ' + JSON.stringify(THEME) +
    '); return 1; })()', false);
  await sleep(1200);

  console.log("=".repeat(74));
  console.log("发言计划（输入框 + 便签上的标签点）探针  " + URL_ + "  " + THEME);
  console.log("=".repeat(74));

  let r;
  try {
    r = await evalJs(PROBE_JS);
  } catch (e) {
    console.error("探针执行失败: " + e.message);
    for (const x of events.filter(v => v.method === "Runtime.exceptionThrown").slice(0, 4)) {
      console.error("  页面异常: " +
        (x.params.exceptionDetails.exception?.description || "").split("\n")[0]);
    }
    ws.close(); proc.kill();
    return 1;
  }

  const m = r.metrics || {};
  console.log("\n弹窗已移除: " + m.dialog_gone + "   （#plandlg / #btn-plan 都不存在）");
  console.log("输入框 " + JSON.stringify(m.input_box) + " · 便签数 " + m.before);
  console.log("点按钮生成: " + m.made_by_click + " · 输入框清空: " + m.input_cleared +
              " · 便签上出现: " + m.note_in_dom);
  console.log("回车生成: " + m.made_by_enter);
  console.log("空输入被忽略: " + m.empty_ignored + " · 提示「" + (m.empty_msg || "") + "」");
  console.log("每张便签的标签点: " + m.dots_per_note + " 个 / " + m.dot_colors +
              " 种颜色 / 选中 " + m.dots_on + " 个");
  console.log("点第 4 个点 → 类别 " + m.kind_after_click + "，色带 " + m.band_class_after);
  console.log("按钮: " + JSON.stringify(m.buttons || {}));
  console.log("收尾: " + m.after_cleanup + " 条，残留 " + m.leftover);

  const order = { fatal: 0, error: 1, warning: 2, warn: 2, info: 3 };
  const bad = (r.checks || []).filter(c => c.level !== "info")
    .sort((a, b) => (order[a.level] || 9) - (order[b.level] || 9));
  console.log("\n问题：");
  if (!bad.length) console.log("  （无）");
  for (const c of bad) console.log("  [" + c.level.toUpperCase() + "] " + c.name + "  " + c.detail);

  const exceptions = events.filter(e => e.method === "Runtime.exceptionThrown")
    .map(e => (e.params.exceptionDetails.exception?.description || "").split("\n")[0]);
  const consoleErrs = events
    .filter(e => e.method === "Runtime.consoleAPICalled" && e.params.type === "error")
    .map(e => e.params.args.map(a => a.value ?? a.description ?? "").join(" "));
  if (exceptions.length || consoleErrs.length) {
    console.log("\n控制台错误：");
    for (const e of [...exceptions, ...consoleErrs].slice(0, 6)) {
      console.log("  " + String(e).slice(0, 170));
    }
  }

  const errors = bad.filter(c => c.level === "fatal" || c.level === "error").length +
    exceptions.length;
  console.log("\n结论：" + errors + " 错误 · " +
    bad.filter(c => c.level === "warning" || c.level === "warn").length + " 警告");

  ws.close(); proc.kill();
  return errors ? 1 : 0;
}

main().then(c => process.exit(c)).catch(e => {
  console.error("探针失败: " + (e && e.stack || e));
  process.exit(1);
});
