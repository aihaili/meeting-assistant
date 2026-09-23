/**
 * Probe: the first-open usage guide (#guide) in a real browser.
 *
 * NOTE FOR EDITORS: PROBE_JS below is a template literal. A backtick inside it ends the
 * string early and produces a SyntaxError that reads like a CSS or selector bug. Use
 * single quotes inside; never use a backtick.
 *
 * Why this exists: "第一次打开时弹一次引导" is a claim about **state that only exists once**
 * -- once the server-side marker is written, the feature (or the bug) can never be seen
 * again on that machine. So it cannot be checked by looking at the page later, and the ways
 * it breaks are all silent:
 *   · 弹不出来（标记被误写 / boot 抛异常 / 选择器改名）→ 用户第一次打开啥也没有；
 *   · 弹出来了但不走（Esc 被别的层吃掉、下一步不动、标记写不上）→ 每次打开都被盖住。
 * Both are measured here, in the only place they exist: a machine that has never seen it.
 *
 * 标记在**服务端**（`data/guide-seen`，见 server.py 的 GUIDE_SEEN 与 ui.html 的说明）：
 * 启动器跑的是 WebView2 private_mode，localStorage 每次启动都清空，所以不能记在浏览器里。
 * 这个探针因此在开头**删掉那个标记文件**（"这台机器还没看过"是它的前提），跑完再按
 * 原样还原——否则用户第一次打开应用时，引导已经被探针替他看过了。
 *
 * 另外验三件事：自动化用的 `?guide=off` 真的能压住它（其余探针/审计/截图都靠这个开关）、
 * 第二次打开不再弹、以及「设置 → 界面」能不能把它叫回来（只弹一次的东西必须有回头路）。
 *
 * Usage: node scripts/probe_guide_ui.js [--url http://127.0.0.1:8510/] [--theme dark]
 */

"use strict";
const fs = require("fs");
const http = require("http");
const { spawn } = require("child_process");
const os = require("os");
const path = require("path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const ROOT = path.join(__dirname, "..");
const MARKER = path.join(ROOT, "data", "guide-seen");

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}

const URL_ = arg("url", "http://127.0.0.1:8510/");
const WIDTH = parseInt(arg("width", "1680"), 10);
const HEIGHT = parseInt(arg("height", "1000"), 10);
const PORT = parseInt(arg("debug-port", "9347"), 10);
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

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.events = [];
    ws.addEventListener("message", ev => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      } else if (msg.method) { this.events.push(msg); }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error("CDP timeout: " + method));
        }
      }, 30000);
    });
  }
  async eval(expr) {
    const r = await this.send("Runtime.evaluate", {
      expression: expr, awaitPromise: true, returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error("eval threw: " + (r.exceptionDetails.exception?.description ||
        r.exceptionDetails.text));
    }
    return r.result.value;
  }
}

// ── 阶段 A：全新 profile 的第一次打开 ──────────────────────────────────
const PROBE_JS = `(async () => {
  const out = { metrics: {}, checks: [] };
  const add = (level, name, detail) => out.checks.push({ level, name, detail: detail || '' });
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const q = s => document.querySelector(s);
  const on = n => !!(n && n.classList.contains('on'));

  const guide = q('#guide');
  if (!guide) {
    add('fatal', '页面里没有 #guide', '引导既没渲染也没样式，选择器或 HTML 丢了');
    return out;
  }
  const step = () => (q('#g-count').textContent || '').trim();
  // 标题由两个 span 组成（序号 + 文字），textContent 会连成 "1先点「录音」"，
  // 所以只取第二个 span。
  const title = () => {
    const s = q('#g-body .g-title span:nth-child(2)');
    return ((s && s.textContent) || '').trim();
  };
  const hl = () => [...document.querySelectorAll('.g-hl')].map(n => '#' + n.id).join(',');

  // ── 1. 第一次打开应当自动弹出 ──────────────────────────────────────
  out.metrics.auto_open = on(guide);
  if (!on(guide)) {
    add('error', '第一次打开没有自动弹出使用引导',
        'localStorage 标记或 boot() 里那次 maybeShowGuide() 没生效');
    return out;
  }
  add('info', '第一次打开自动弹出', step() + '  ' + title());

  // ── 2. 覆盖层必须是覆盖层，而且**不压暗页面** ──────────────────────
  const cs = getComputedStyle(guide);
  out.metrics.layer = { position: cs.position, background: cs.backgroundColor,
                        backdrop: cs.backdropFilter, z: cs.zIndex };
  if (cs.position !== 'fixed') {
    add('error', '#guide 不是 fixed 覆盖层', 'position=' + cs.position);
  }
  const dim = cs.backgroundColor && cs.backgroundColor !== 'rgba(0, 0, 0, 0)' &&
              cs.backgroundColor !== 'transparent';
  if (dim) {
    add('warn', '#guide 压暗了页面',
        'bg=' + cs.backgroundColor + '——它要指的就是页面本身，压暗后强调环也跟着灰');
  }
  const box = q('#guide .g-box');
  const r = box ? box.getBoundingClientRect() : null;
  out.metrics.card = r ? { w: Math.round(r.width), h: Math.round(r.height),
                           fits: r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1 } : null;
  if (!r || !out.metrics.card.fits) {
    add('error', '引导卡片超出视口', JSON.stringify(out.metrics.card));
  }
  // 开着的时候必须挡住底下的点击：这一层的用处之一就是"点外面 = 关闭"，
  // 如果点击能漏到页面上，用户在引导里随手一点就可能真的按到录音、导入之类的按钮。
  const micBtn = q('#btn-mic');
  const mb = micBtn.getBoundingClientRect();
  const mpx = Math.round(mb.left + mb.width / 2);
  const mpy = Math.round(mb.top + mb.height / 2);
  const hitOpen = document.elementFromPoint(mpx, mpy);
  out.metrics.hit_open = hitOpen ? (hitOpen.id || hitOpen.className || hitOpen.tagName) : null;
  if (!hitOpen || hitOpen.id !== 'guide') {
    add('error', '引导开着时点击漏到了页面上', '命中 ' + out.metrics.hit_open);
  }

  // ── 3. 步骤数与第一步的状态 ────────────────────────────────────────
  const total = parseInt((step().split('/')[1] || '0').trim(), 10);
  out.metrics.steps = total;
  if (!(total >= 5)) add('error', '引导步骤数不合理', '共 ' + total + ' 步');
  if (!/^1 \\/ \\d+$/.test(step())) add('error', '计数不是从第 1 步开始', step());
  if (!q('#g-prev').disabled) add('error', '第 1 步的「上一步」没有禁用', '');
  out.metrics.hl_first = hl();
  if (hl() !== '#btn-mic') add('error', '第 1 步没有指向录音按钮', 'g-hl=' + hl());

  // ── 4. 逐步走完，看环有没有跟着走、文案有没有空 ────────────────────
  const titles = [title()];
  const wheres = [];
  for (let i = 1; i < total; i++) {
    q('#g-next').click();
    await sleep(90);
    titles.push(title());
    const w = q('#g-body .g-where');
    wheres.push(w ? (w.textContent || '').trim().length : 0);
    if (!(step() === (i + 1) + ' / ' + total)) {
      add('error', '第 ' + (i + 1) + ' 步的计数不对', step());
      break;
    }
  }
  out.metrics.titles = titles;
  out.metrics.hl_last = hl();
  if (titles.some(t => !t)) add('error', '有步骤没有标题', JSON.stringify(titles));
  if (wheres.some(n => n < 4)) add('warn', '有步骤没有「看哪里」的说明', JSON.stringify(wheres));
  if (q('#g-prev').disabled) add('error', '最后一步的「上一步」仍然禁用', '');
  const nextLabel = (q('#g-next').textContent || '').trim();
  out.metrics.last_btn = nextLabel;
  if (nextLabel !== '开始使用') add('error', '最后一步的按钮文案不对', nextLabel);
  const keys = document.querySelectorAll('#g-body .g-keys div').length;
  out.metrics.key_lines = keys;
  if (keys < 5) add('error', '最后一步没列出快捷键', keys + ' 行');

  // ── 5. 引导里的文字对比度 ──────────────────────────────────────────
  // 引导用了几组新的"文字色 + 背景色"搭配（卡片是 --surface，页脚是 --surface-2），
  // 而页面级的审计器量不到它：引导一关就是 display:none，那些元素根本不在渲染树里。
  // 只有它开着的时候能量，所以放在这里量。
  // 背景要沿祖先往上混合：getComputedStyle 对"继承来的"背景给的是 rgba(0,0,0,0)，
  // 直接读会当成黑底，所有比值都算错。
  const parseC = c => {
    const m = String(c).match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const effBg = n => {
    const stack = [];
    for (let e = n; e; e = e.parentElement) {
      const c = parseC(getComputedStyle(e).backgroundColor);
      if (c && c.a > 0) { stack.push(c); if (c.a === 1) break; }
    }
    let base = { r: 255, g: 255, b: 255 };
    for (let i = stack.length - 1; i >= 0; i--) {
      const c = stack[i];
      base = { r: c.r * c.a + base.r * (1 - c.a),
               g: c.g * c.a + base.g * (1 - c.a),
               b: c.b * c.a + base.b * (1 - c.a) };
    }
    return base;
  };
  const lum = c => {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 :
      Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const contrastOf = n => {
    const fg = parseC(getComputedStyle(n).color);
    if (!fg) return null;
    const bg = effBg(n);
    if (fg.a < 1) {
      fg.r = fg.r * fg.a + bg.r * (1 - fg.a);
      fg.g = fg.g * fg.a + bg.g * (1 - fg.a);
      fg.b = fg.b * fg.a + bg.b * (1 - fg.a);
    }
    const l1 = lum(fg), l2 = lum(bg);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };
  const pairs = [['正文', q('#g-body .g-text')],
                 ['看哪里', q('#g-body .g-where')],
                 ['快捷键', q('#g-body .g-keys')],
                 ['步骤号', q('#g-body .g-num')],
                 ['步骤标题', q('#g-body .g-title span:nth-child(2)')],
                 ['计数', q('.g-count')],
                 ['底部说明', q('.g-foot .g-once')]];
  out.metrics.contrasts = {};
  for (const pair of pairs) {
    const node = pair[1];
    if (!node) { add('error', '引导里找不到「' + pair[0] + '」这一块', ''); continue; }
    const c = contrastOf(node);
    const px = parseFloat(getComputedStyle(node).fontSize);
    out.metrics.contrasts[pair[0]] = { ratio: Math.round(c * 100) / 100, px: px };
    // 全部按正文门槛算：这几个字号都没到"大号文字"的 18.66px/24px，不能降标准。
    if (c < 4.5) {
      add('error', '引导的「' + pair[0] + '」对比度不足',
          (Math.round(c * 100) / 100) + ':1 < 4.5:1（' + px + 'px）');
    }
  }

  // ── 6. Esc 关闭 + **服务端**写下"已看过" ─────────────────────────
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await sleep(120);
  out.metrics.esc_closed = !on(guide);
  if (on(guide)) add('error', '按 Esc 没关掉引导', 'Esc 被别的层吃掉了');
  // 标记必须落到服务端：启动器里 localStorage 每次启动都清空，写在那儿等于没写。
  let seen = null;
  try {
    const r = await fetch('/api/state?since=0', { cache: 'no-store' });
    seen = (await r.json()).service.guide_seen;
  } catch (e) { seen = '(fetch failed)'; }
  out.metrics.server_flag = seen;
  if (seen !== true) {
    add('error', '关闭后服务端没记下"看过了"', 'service.guide_seen=' + String(seen));
  }
  const cs2 = getComputedStyle(guide);
  out.metrics.closed_display = cs2.display;
  if (cs2.display !== 'none') add('error', '关掉的引导仍然占版面', 'display=' + cs2.display);
  // 关掉之后，刚才被挡住的那个按钮必须真的能点到了——否则"透明层"会变成一个
  // 永久吃掉点击的隐形盖子，而那种 bug 从截图上一点都看不出来。
  const hitClosed = document.elementFromPoint(mpx, mpy);
  out.metrics.hit_closed = hitClosed
    ? (hitClosed.id || hitClosed.tagName) : null;
  if (!hitClosed || !micBtn.contains(hitClosed)) {
    add('error', '引导关掉后录音按钮仍然被挡住', '命中 ' + out.metrics.hit_closed);
  }

  // ── 7. ? 仍然只是快捷键提示，不该把整份引导拉起来 ─────────────────
  document.dispatchEvent(new KeyboardEvent('keydown', { key: '?', bubbles: true }));
  await sleep(150);
  out.metrics.q_reopens = on(guide);
  if (on(guide)) add('warn', '按 ? 又把引导拉起来了', '与「? 只弹快捷键提示」的说法不一致');

  return out;
})()`;

// ── 阶段 B/C：再打开一次时的状态（是否自动弹 + 服务端标记）────────────
const STATE_JS = `(async () => {
  const g = document.querySelector('#guide');
  const out = { present: !!g, open: !!(g && g.classList.contains('on')) };
  try {
    const r = await fetch('/api/state?since=0', { cache: 'no-store' });
    out.seen = (await r.json()).service.guide_seen;
  } catch (e) { out.seen = '(fetch failed)'; }
  return out;
})()`;

// ── 阶段 C：设置 → 界面 → 「再看一遍使用引导」────────────────────────
const SETTINGS_PATH_JS = `(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const q = s => document.querySelector(s);
  const out = { tab: false, row: false, opened: false, settings_closed: false, detail: '' };
  q('#btn-settings').click();
  await sleep(700);
  out.settings_open = !!(q('#settings') && q('#settings').classList.contains('on'));
  const tab = [...document.querySelectorAll('#st-tabs .st-tab')]
    .find(b => (b.textContent || '').indexOf('界面') >= 0);
  if (!tab) { out.detail = '设置里没有「界面」一节'; return out; }
  tab.click();
  await sleep(400);
  out.tab = true;
  const btn = q('#st-body [data-key="ui.guide"] button');
  if (!btn) { out.detail = '界面一节里没有 ui.guide 这一行'; return out; }
  out.row = true;
  out.row_text = (btn.textContent || '').trim();
  btn.click();
  await sleep(400);
  out.opened = !!(q('#guide') && q('#guide').classList.contains('on'));
  out.settings_closed = !(q('#settings') && q('#settings').classList.contains('on'));
  out.detail = 'count=' + ((q('#g-count') || {}).textContent || '').trim();
  // 「点外面 = 关闭」：处理函数判定的是 e.target.id === 'guide'，所以直接点这一层
  // 本身就是在模拟点卡片外面。这条不验的话，界面上会多一个"点哪儿都不关"的盖层。
  if (out.opened) {
    q('#guide').click();
    await sleep(200);
    out.backdrop_closes = !q('#guide').classList.contains('on');
  }
  return out;
})()`;

async function main() {
  const profile = path.join(os.tmpdir(), "edge-probe-guide-" + Date.now());
  const args = [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--hide-scrollbars", "--force-device-scale-factor=1",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    "about:blank",
  ];
  const proc = spawn(EDGE, args, { stdio: ["ignore", "pipe", "pipe"] });
  let stderr = "";
  proc.stderr.on("data", d => (stderr += d.toString()));

  const deadline = Date.now() + 25000;
  let targets = null;
  while (Date.now() < deadline) {
    try {
      const list = await getJSON(`http://127.0.0.1:${PORT}/json/list`);
      targets = list.filter(t => t.type === "page" && t.webSocketDebuggerUrl);
      if (targets.length) break;
    } catch (e) { /* not up yet */ }
    await sleep(400);
  }
  if (!targets || !targets.length) {
    console.error("无法连接 Edge 调试端口。stderr:\n" + stderr.slice(-800));
    proc.kill();
    return 1;
  }

  const ws = new WebSocket(targets[0].webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res);
    ws.addEventListener("error", rej);
    setTimeout(() => rej(new Error("ws open timeout")), 10000);
  });
  const cdp = new CDP(ws);
  await cdp.send("Runtime.enable");
  await cdp.send("Log.enable");
  await cdp.send("Page.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false,
  });

  const OFF_URL = URL_ + (URL_.indexOf("?") >= 0 ? "&" : "?") + "guide=off";

  // 每次导航都要等**这一次**的 load 事件：cdp.events 是累积的，只判断"有没有出现过
  // load"会让第二次导航立刻返回，然后去量上一页的状态。
  const load = async url => {
    const before = cdp.events.filter(e => e.method === "Page.loadEventFired").length;
    await cdp.send("Page.navigate", { url: url });
    const t0 = Date.now();
    while (Date.now() - t0 < 12000) {
      if (cdp.events.filter(e => e.method === "Page.loadEventFired").length > before) break;
      await sleep(120);
    }
    // 主题要在应用脚本第一轮轮询之前钉住；**不碰引导的状态**——"第一次打开"正是本探针
    // 要观察的东西，探针自己不能先把它标记成看过（标记也在服务端，见文件头）。
    await cdp.eval(`(() => {
      try { localStorage.setItem("plaud-theme", ${JSON.stringify(THEME)}); } catch (e) {}
      document.documentElement.setAttribute("data-theme", ${JSON.stringify(THEME)});
      return 1;
    })()`);
    await sleep(2500);
  };

  // 前提：这台机器"还没看过"。标记在服务端，所以只能由探针自己清掉；跑完再还原
  // （本来没有标记的，跑完也不能留下——否则用户第一次打开应用，引导已经被探针替他看过了）。
  // 挂在 exit 上而不是写在函数末尾：中途 return 的几条错误路径也要还原。
  const hadMarker = fs.existsSync(MARKER);
  if (hadMarker) { try { fs.rmSync(MARKER, { force: true }); } catch (e) { /* 忽略 */ } }
  if (!hadMarker) {
    process.on("exit", () => { try { fs.rmSync(MARKER, { force: true }); } catch (e) {} });
  }

  console.log("=".repeat(74));
  console.log("使用引导探针  " + URL_ + "  " + WIDTH + "x" + HEIGHT + "  " + THEME);
  console.log("=".repeat(74));

  const checks = [];

  // ── 阶段 0：?guide=off（其余探针/审计/截图用的开关）──────────────
  let phaseA = null;
  try {
    await load(OFF_URL);
    phaseA = await cdp.eval(STATE_JS);
  } catch (e) {
    checks.push({ level: "error", name: "阶段 0 读不到状态", detail: e.message });
  }
  if (phaseA) {
    console.log("\n阶段 0（?guide=off）：");
    console.log("  #guide 存在 " + phaseA.present + " · 自动弹出 " + phaseA.open +
                " · 服务端标记 " + phaseA.seen);
    if (!phaseA.present) checks.push({ level: "error", name: "页面里没有 #guide", detail: "" });
    if (phaseA.open) {
      checks.push({ level: "error", name: "?guide=off 没压住引导",
                    detail: "其余探针/审计/截图会被这一层挡住" });
    }
    if (phaseA.seen) {
      checks.push({ level: "warn", name: "阶段 0 开始时服务端已有标记",
                    detail: "本次不是真正的「第一次打开」，后面的断言可能不成立" });
    }
  }

  // ── 阶段 1：真正的第一次打开 ──────────────────────────────────────
  const r = await (async () => {
    try {
      await load(URL_);
      return await cdp.eval(PROBE_JS);
    } catch (e) {
      console.error("探针执行失败: " + e.message);
      const errs = cdp.events.filter(x => x.method === "Runtime.exceptionThrown");
      for (const x of errs.slice(0, 5)) {
        console.error("  页面异常: " +
          (x.params.exceptionDetails.exception?.description || "").split("\n")[0]);
      }
      try {
        const st = await cdp.eval("({ hasGuide: !!document.querySelector('#guide')," +
          " bodyLen: document.body ? document.body.innerHTML.length : -1 })");
        console.error("  页面实际状态: " + JSON.stringify(st));
      } catch (e2) { console.error("  读不到页面状态: " + e2.message); }
      ws.close(); proc.kill();
      return null;
    }
  })();
  if (!r) return 1;
  const m = r.metrics || {};
  checks.push(...(r.checks || []));

  console.log("\n第一次打开（全新 profile + 清掉服务端标记）：");
  console.log("  自动弹出 " + m.auto_open + " · 步骤 " + m.steps + " 步 · 计数「" +
              (m.titles ? "1 / " + m.steps : "") + "」");
  console.log("  覆盖层 " + JSON.stringify(m.layer));
  console.log("  卡片 " + JSON.stringify(m.card));
  console.log("  环 " + m.hl_first + " → " + m.hl_last + " · 最后一步按钮「" + m.last_btn +
              "」· 快捷键 " + m.key_lines + " 行");
  console.log("  Esc 关闭 " + m.esc_closed + " · 关闭后 display=" + m.closed_display +
              " · 服务端标记 " + m.server_flag + " · 按 ? 再拉起 " + m.q_reopens);
  console.log("  点击命中：开着时 " + m.hit_open + " · 关掉后 " + m.hit_closed +
              "（录音按钮那一处）");
  console.log("  对比度：");
  for (const [k, v] of Object.entries(m.contrasts || {})) {
    if (!v) { console.log("    " + k + " : 未渲染"); continue; }
    console.log("    " + k.padEnd(5) + " " + String(v.ratio).padStart(6) + ":1  " +
                v.px + "px  " + (v.ratio >= 4.5 ? "✓" : "✗ 不足 4.5:1"));
  }
  console.log("  步骤标题：");
  for (const t of (m.titles || [])) console.log("    · " + t);

  // ── 阶段 B：第二次打开（服务端已有标记）────────────────────────────
  console.log("\n第二次打开（同一个 profile + 服务端标记已写）：");
  let second = null;
  try {
    await load(URL_);
    second = await cdp.eval(STATE_JS);
  } catch (e) {
    checks.push({ level: "error", name: "第二次打开读不到状态", detail: e.message });
  }
  if (second) {
    console.log("  #guide 存在 " + second.present + " · 自动弹出 " + second.open +
                " · 服务端标记 " + second.seen);
    if (!second.present) {
      checks.push({ level: "error", name: "第二次打开时 #guide 不见了", detail: "" });
    }
    if (second.open) {
      checks.push({ level: "error", name: "第二次打开又自动弹了",
                    detail: "服务端标记没读上，用户每次打开都会被盖住" });
    }
    if (second.seen !== true) {
      checks.push({ level: "error", name: "第二次打开时服务端仍说没看过",
                    detail: "service.guide_seen=" + String(second.seen) });
    }
  }

  // ── 阶段 C：设置 → 界面 里能再打开 ────────────────────────────────
  console.log("\n设置 → 界面 →「再看一遍使用引导」：");
  let third = null;
  try {
    third = await cdp.eval(SETTINGS_PATH_JS);
  } catch (e) {
    checks.push({ level: "error", name: "设置路径打开引导失败", detail: e.message });
  }
  if (third) {
    console.log("  设置打开 " + third.settings_open + " · 界面一节 " + third.tab +
                " · 找到入口行 " + third.row + "（" + (third.row_text || "") + "）");
    console.log("  点开后引导 " + third.opened + " · 设置已关 " + third.settings_closed +
                " · 点外面关闭 " + third.backdrop_closes + " · " + third.detail);
    if (!third.settings_open) {
      checks.push({ level: "error", name: "点设置没打开设置面板", detail: "" });
    }
    if (!third.row) {
      checks.push({ level: "fatal", name: "「界面」一节里没有引导入口",
                    detail: third.detail });
    } else if (!third.opened) {
      checks.push({ level: "error", name: "从设置里打不开引导", detail: third.detail });
    } else if (!third.settings_closed) {
      checks.push({ level: "warn", name: "打开引导时设置面板没关",
                    detail: "两层叠在一起，Esc 会先关哪一个就说不清了" });
    }
    if (third.opened && !third.backdrop_closes) {
      checks.push({ level: "error", name: "点引导外面关不掉它",
                    detail: "透明层吃掉了点击却什么都不做，用户会以为界面卡住" });
    }
  }

  const order = { fatal: 0, error: 1, warn: 2, info: 3 };
  const bad = checks.filter(c => c.level !== "info")
    .sort((a, b) => order[a.level] - order[b.level]);
  console.log("\n问题：");
  if (!bad.length) console.log("  （无）");
  for (const c of bad) console.log("  [" + c.level.toUpperCase() + "] " + c.name + "  " + c.detail);
  for (const c of checks.filter(c => c.level === "info")) {
    console.log("  [INFO] " + c.name + "  " + c.detail);
  }

  const exceptions = cdp.events.filter(e => e.method === "Runtime.exceptionThrown")
    .map(e => (e.params.exceptionDetails.exception?.description || "").split("\n")[0]);
  const consoleErrs = cdp.events
    .filter(e => e.method === "Runtime.consoleAPICalled" && e.params.type === "error")
    .map(e => e.params.args.map(a => a.value ?? a.description ?? "").join(" "));
  const netErrs = cdp.events
    .filter(e => e.method === "Log.entryAdded" && e.params.entry.level === "error")
    .map(e => e.params.entry.text);
  console.log("\n控制台/网络错误：");
  if (!(exceptions.length || consoleErrs.length || netErrs.length)) console.log("  无");
  for (const e of [...exceptions, ...consoleErrs, ...netErrs].slice(0, 10)) {
    console.log("  " + String(e).slice(0, 190));
  }

  const errors = bad.filter(c => c.level === "fatal" || c.level === "error").length;
  const warns = bad.filter(c => c.level === "warn").length;
  console.log("\n结论：" + errors + " 错误 · " + warns + " 警告" +
    (exceptions.length ? " · " + exceptions.length + " 页面异常" : ""));

  ws.close(); proc.kill();
  return errors ? 1 : 0;
}

main().then(c => process.exit(c)).catch(e => {
  console.error("探针失败: " + (e && e.stack || e));
  process.exit(1);
});
