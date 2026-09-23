/**
 * Probe: drive the settings panel and the import dialog in a real browser.
 *
 * NOTE FOR EDITORS: PROBE_JS below is a template literal. A backtick inside it ends the
 * string early and produces a SyntaxError that reads like a CSS or selector bug. Use
 * single quotes inside; never a backtick.
 *
 * Why this exists: the settings page is the first screen in this project where a wrong
 * label, a missing field, or an echoed secret is *invisible* to reflection -- the code
 * looks right and the JSON looks right, but the user sees the wrong thing. So this drives
 * the actual DOM: opens the panel, walks every section, counts the fields, edits one
 * through the UI, saves it, and then reads the value back from the server. If the two
 * numbers agree, the whole chain (input -> POST -> settings.json -> describe) works.
 *
 * Also measured, because they are the two failures that matter here:
 *   · a secret rendered into the DOM (search the page for sk-)
 *   · contrast of the new .st-* text colours, blended through transparent backgrounds
 *
 * Usage: node scripts/probe_settings_ui.js [--url http://127.0.0.1:8510/] [--theme dark]
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
const WIDTH = parseInt(arg("width", "1680"), 10);
const HEIGHT = parseInt(arg("height", "1000"), 10);
const PORT = parseInt(arg("debug-port", "9341"), 10);
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

const PROBE_JS = `(async () => {
  const out = { checks: [], metrics: {} };
  const add = (level, name, detail) => out.checks.push({ level, name, detail });
  const q = s => document.querySelector(s);
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // ── effective background, blended through transparent ancestors ──────
  // getComputedStyle().backgroundColor is 'rgba(0,0,0,0)' for anything that inherits,
  // so a naive read reports black and every ratio comes out wrong. Walk up, collect
  // backgrounds, then composite from the bottom of the stack upward.
  const parse = c => {
    const m = String(c).match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const effBg = n => {
    const stack = [];
    for (let e = n; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
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
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };
  const contrastOf = n => {
    const fg = parse(getComputedStyle(n).color);
    if (!fg) return null;
    if (fg.a < 1) {
      const bg = effBg(n);
      fg.r = fg.r * fg.a + bg.r * (1 - fg.a);
      fg.g = fg.g * fg.a + bg.g * (1 - fg.a);
      fg.b = fg.b * fg.a + bg.b * (1 - fg.a);
    }
    return ratio(fg, effBg(n));
  };

  // ── 1. the button exists and opens the panel ─────────────────────────
  const btn = q('#btn-settings');
  if (!btn) { add('fatal', '没有设置按钮', '#btn-settings'); return out; }
  const br = btn.getBoundingClientRect();
  out.metrics.btn = { w: Math.round(br.width), h: Math.round(br.height) };
  if (br.height < 26) add('warn', '设置按钮过小', Math.round(br.height) + 'px 高');

  btn.click();
  await sleep(900);
  const panel = q('#settings');
  if (!panel || !panel.classList.contains('on')) {
    add('fatal', '点设置没打开面板', 'class=' + (panel ? panel.className : 'missing'));
    return out;
  }

  const box = q('.st-box');
  const rb = box.getBoundingClientRect();
  out.metrics.panel = { w: Math.round(rb.width), h: Math.round(rb.height),
                        fits_w: rb.right <= innerWidth + 1, fits_h: rb.bottom <= innerHeight + 1 };
  if (!out.metrics.panel.fits_w || !out.metrics.panel.fits_h) {
    add('error', '设置面板超出视口',
        'right=' + Math.round(rb.right) + '/' + innerWidth +
        ' bottom=' + Math.round(rb.bottom) + '/' + innerHeight);
  }

  // ── 2. every section renders its fields ──────────────────────────────
  // Expected field counts come from SCHEMA in settings.py. Hard-coding them here is the
  // point: if a field is dropped from describe() the panel silently shows fewer rows,
  // which looks like a design choice rather than a bug.
  //
  // 这两个数字随功能长过：语音识别从 2 项（引擎 / 模型目录）长到 6 项（+ 设备、
  // FireRedASR 目录、热词开关、热词目录），界面从 2 项长到 3 项（+ 使用引导入口）。
  // 探针没跟着改就会一直报"字段数不对"——而那是探针过时，不是界面缺字段。
  const EXPECT = { '语音识别': 6, '检索': 1, '大模型': 7, '知识库': 3, '界面': 3 };
  const tabs = [...document.querySelectorAll('.st-tab')];
  out.metrics.tabs = tabs.length;
  if (tabs.length !== 5) add('error', '设置分节数量不对', tabs.length + ' 节，应为 5');

  const sections = [];
  for (const tab of tabs) {
    tab.click();
    await sleep(120);
    const name = q('#st-title').textContent.trim();
    // 必须限定在 #st-body 内：导入面板也用 .st-row，而它此刻是隐藏的。
    // 第一版没限定，于是每一节都多出「文件路径 / 归入哪个知识库」两行，
    // 字段数检查全部报错——错的是探针，不是界面。
    const rows = [...document.querySelectorAll('#st-body .st-row')];
    const fields = rows.map(r => ({
      key: r.dataset.key || '',
      label: (r.querySelector('.st-lab') || {}).textContent || '',
      ctrl: r.querySelector('input,select') ? r.querySelector('input,select').tagName.toLowerCase() : 'none',
      type: r.querySelector('input,select') ? (r.querySelector('input,select').type || '') : '',
      value: r.querySelector('input,select') ? r.querySelector('input,select').value : '',
      ph: r.querySelector('input') ? (r.querySelector('input').placeholder || '') : '',
      hint: (r.querySelector('.st-hint') || {}).textContent || '',
    }));
    const inPanel = fields.length;
    const state = q('#st-state');
    sections.push({ name: name, n: inPanel, setup_tag: state.textContent.trim(),
                    tag_class: state.className,
                    dot_ok: (tab.querySelector('.st-dot') || {}).className || '',
                    labels: fields.map(f => f.label),
                    empty_required: fields.filter(f => !f.hint).length,
                    secret_boxes: fields.filter(f => f.type === 'password').length });
    // 空白控件只对"必须填"的几项报警。corpora.project_db 留空是合法的
    // （留空 = 用 <项目文件夹>/.plaud/rag.db），把它算成缺陷就是误报。
    const MUST = ['llm.base_url', 'llm.model', 'asr.model_dir', 'embedding.backend'];
    const blank = fields.filter(f => MUST.indexOf(f.key) >= 0 &&
                                     String(f.value).trim() === '');
    if (blank.length) {
      add('error', name + ' 必填项是空的',
          blank.map(f => f.label).join('、'));
    }
    const body = q('#st-body');
    if (body.scrollWidth > body.clientWidth + 1) {
      // 光说"溢出 2px"没法改。指出是哪个元素越了界，才是可下手的结论。
      const bb = body.getBoundingClientRect();
      const csb = getComputedStyle(body);
      const innerRight = bb.right - parseFloat(csb.paddingRight);
      const bad = [];
      for (const n of body.querySelectorAll('*')) {
        const rr = n.getBoundingClientRect();
        if (rr.width === 0 && rr.height === 0) continue;
        const over = rr.right - innerRight;
        if (over > 0.5) {
          bad.push(n.tagName.toLowerCase() +
            (n.id ? '#' + n.id : '') +
            (typeof n.className === 'string' && n.className
              ? '.' + n.className.trim().split(/\\s+/)[0] : '') +
            ' 越界 ' + Math.round(over) + 'px');
        }
      }
      add('error', name + ' 一节横向溢出',
          body.scrollWidth + ' > ' + body.clientWidth +
          (bad.length ? '（' + bad.slice(0, 3).join('；') + '）' : ''));
    }
    const expect = EXPECT[name];
    if (expect != null && inPanel !== expect) {
      add('error', name + ' 字段数不对', inPanel + ' 行，应为 ' + expect);
    }
  }
  out.metrics.sections = sections;
  const names = sections.map(s => s.name).join(' / ');
  if (names !== Object.keys(EXPECT).join(' / ')) {
    add('error', '分节顺序或名称不对', names);
  }
  // 密钥字段必须渲染成密码框且为空：它是"不回声"这条规则在界面上的样子
  const secrets = sections.reduce((a, s) => a + s.secret_boxes, 0);
  out.metrics.secret_boxes = secrets;
  if (secrets !== 1) add('error', '密钥框数量不对', secrets + ' 个，应为 1');

  // ── 3. the page must not contain a live credential ───────────────────
  const html = document.body.innerHTML;
  const bodyText = document.body.innerText || '';
  const leaked = /sk-[A-Za-z0-9_\\-]{8,}/.test(html) || /sk-[A-Za-z0-9_\\-]{8,}/.test(bodyText);
  out.metrics.secret_in_dom = leaked;
  if (leaked) add('fatal', '页面里出现了 API key 明文', '正则 sk-… 命中页面的 HTML 或文本');

  // ── 4. edit one value through the UI and save it ─────────────────────
  // 超时时间是这一节里最无害的一项：改错了也不会毁掉任何东西，但它的链路和
  // base_url、模型名完全一样，所以它证明了整条链。
  const llmTab = tabs.find(t => t.textContent.trim() === '大模型');
  llmTab.click();
  await sleep(150);
  let timeoutInput = null;
  for (const r of document.querySelectorAll('#st-body .st-row')) {
    if (r.dataset.key === 'llm.timeout') timeoutInput = r.querySelector('input');
  }
  if (!timeoutInput) {
    add('fatal', '大模型一节里找不到超时字段', '按 label 含"超时"查找');
    return out;
  }
  out.metrics.timeout_before = await (await fetch('/api/settings')).json()
    .then(d => d.settings.groups.llm.find(x => x.key === 'llm.timeout').value);
  const NEWVAL = out.metrics.timeout_before === 77 ? 78 : 77;
  timeoutInput.value = String(NEWVAL);
  timeoutInput.dispatchEvent(new Event('input', { bubbles: true }));
  await sleep(80);
  out.metrics.dirty_marked = timeoutInput.classList.contains('dirty');
  out.metrics.msg_after_edit = q('#st-msg').textContent.trim();
  if (!out.metrics.dirty_marked) add('warn', '改了值没有高亮', '缺少 .dirty');
  if (out.metrics.msg_after_edit.indexOf('待保存') < 0) {
    add('warn', '改了值没有提示待保存', '底栏显示 ' + out.metrics.msg_after_edit);
  }
  q('#st-save').click();
  await sleep(1200);
  out.metrics.msg_after_save = q('#st-msg').textContent.trim();
  const after = await (await fetch('/api/settings')).json();
  out.metrics.timeout_after = after.settings.groups.llm.find(x => x.key === 'llm.timeout').value;
  if (Number(out.metrics.timeout_after) !== NEWVAL) {
    add('fatal', '在界面上保存的值没有落库',
        '界面改成 ' + NEWVAL + '，服务端读回 ' + out.metrics.timeout_after);
  }
  if (out.metrics.msg_after_save.indexOf('立即生效') < 0) {
    add('warn', '保存后没有说明生效方式', '底栏显示 ' + out.metrics.msg_after_save);
  }
  // 存回去，别把测试改出来的值留在用户的配置里
  timeoutInput.value = String(out.metrics.timeout_before);
  timeoutInput.dispatchEvent(new Event('input', { bubbles: true }));
  await sleep(80);
  q('#st-save').click();
  await sleep(1000);
  const restored = await (await fetch('/api/settings')).json();
  out.metrics.timeout_restored =
    restored.settings.groups.llm.find(x => x.key === 'llm.timeout').value;
  if (Number(out.metrics.timeout_restored) !== Number(out.metrics.timeout_before)) {
    add('error', '测试值没能还原', '现在是 ' + out.metrics.timeout_restored);
  }
  // 密钥必须活过这两次保存：空字符串不能当成"清空"
  out.metrics.key_still_set = restored.settings.groups.llm
    .find(x => x.key === 'llm.api_key').is_set;
  if (!out.metrics.key_still_set) {
    add('fatal', '改别的设置把 API key 抹掉了', '空密钥被当成清空处理');
  }

  // ── 5. contrast of the new text colours ──────────────────────────────
  const probes = [['.st-tab.on', '分节选中'], ['.st-lab', '字段名'],
                  ['.st-hint', '字段说明'], ['.st-note', '说明块'],
                  ['.st-msg', '底栏提示'], ['#st-state', '可用状态标签']];
  const contrasts = {};
  for (const [sel, name] of probes) {
    const n = q(sel);
    if (!n) { contrasts[name] = null; continue; }
    const c = contrastOf(n);
    const size = parseFloat(getComputedStyle(n).fontSize);
    const need = size >= 18.66 ? 3.0 : 4.5;
    contrasts[name] = { ratio: c == null ? null : Math.round(c * 100) / 100,
                        px: size, need: need };
    if (c != null && c < need) {
      add('error', '对比度不足 ' + name,
          c.toFixed(2) + ':1，' + size + 'px 需要 ' + need + ':1');
    }
  }
  // 成功/警告两种状态色要单独画出来才有得量：把面板切到一节并看健康标签
  const tagOk = document.querySelector('.st-tag.ok');
  const tagBad = document.querySelector('.st-tag.bad, .st-note.warn');
  if (tagOk) {
    const c = contrastOf(tagOk);
    contrasts['可用标签'] = { ratio: Math.round(c * 100) / 100, px: 11, need: 4.5 };
    if (c < 4.5) add('error', '对比度不足 可用标签',
                     c.toFixed(2) + ':1（--ok-ink on --ok-soft）');
  }
  if (tagBad) {
    const c = contrastOf(tagBad);
    contrasts['警告块'] = { ratio: Math.round(c * 100) / 100, px: 12, need: 4.5 };
    if (c < 4.5) add('error', '对比度不足 警告块', c.toFixed(2) + ':1（--date on --date-bg）');
  }
  out.metrics.contrasts = contrasts;

  // ── 6. Esc closes the panel ──────────────────────────────────────────
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await sleep(250);
  out.metrics.esc_closes = !q('#settings').classList.contains('on');
  if (!out.metrics.esc_closes) add('error', 'Esc 关不掉设置面板', '');

  // ── 7. import dialog: the scope choice must be visible ────────────────
  const impBtn = q('#pa-import');
  if (!impBtn) { add('error', '找不到导入按钮', '#pa-import'); return out; }
  impBtn.click();
  await sleep(400);
  const imp = q('#imp');
  if (!imp.classList.contains('on')) {
    add('fatal', '点导入没打开面板', 'class=' + imp.className);
    return out;
  }
  const opts = [...document.querySelectorAll('#imp-scope .st-opt')];
  out.metrics.import_opts = opts.map(o => ({
    title: (o.querySelector('.st-opt-t') || {}).textContent || '',
    picked: o.classList.contains('on'),
    radio: o.querySelector('input') ? o.querySelector('input').checked : null,
  }));
  if (opts.length !== 3) add('error', '导入归属选项不是 3 个', opts.length + ' 个');
  const picked = out.metrics.import_opts.filter(o => o.picked);
  if (picked.length !== 1) {
    add('error', '导入归属没有唯一选中项', picked.length + ' 个选中');
  }
  out.metrics.import_path = q('#imp-path').value;
  out.metrics.import_hint = q('#imp-hint').textContent.trim();
  if (!out.metrics.import_path) add('warn', '导入面板没有预填路径', '');
  if (!out.metrics.import_hint) add('warn', '导入面板没说清当前项目', '');
  // 归属必须能改：默认选中项目库，但用户要能指到公共库
  const globalOpt = opts.find(o => /公共库/.test(o.textContent));
  if (globalOpt) {
    globalOpt.querySelector('input').click();
    await sleep(150);
    out.metrics.import_switch = [...document.querySelectorAll('#imp-scope .st-opt')]
      .map(o => o.classList.contains('on'));
    const okNow = out.metrics.import_switch.filter(Boolean).length === 1 &&
                  out.metrics.import_switch[1] === true;
    if (!okNow) add('error', '导入归属选不动或选成了多个', JSON.stringify(out.metrics.import_switch));
  } else {
    add('error', '导入面板里没有"公共库"选项', '');
  }
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await sleep(250);
  out.metrics.import_esc_closes = !q('#imp').classList.contains('on');
  if (!out.metrics.import_esc_closes) add('error', 'Esc 关不掉导入面板', '');

  return out;
})()`;

async function main() {
  const profile = path.join(os.tmpdir(), "edge-probe-set-" + Date.now());
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
  const loaded = new Promise(res => {
    const t = setTimeout(res, 12000);
    const iv = setInterval(() => {
      if (cdp.events.some(e => e.method === "Page.loadEventFired")) {
        clearInterval(iv); clearTimeout(t); res();
      }
    }, 120);
  });
  // ?guide=off：首次打开的使用引导会盖住整页，而它在 Esc 判定里排在最前面——
  // 不关掉它，这个探针的两次 Esc 断言（关设置、关导入）会变成"关掉引导"。
  await cdp.send("Page.navigate", { url: URL_ + (URL_.indexOf("?") >= 0 ? "&" : "?") + "guide=off" });
  await loaded;

  await cdp.eval(`(() => {
    try { localStorage.setItem("plaud-theme", ${JSON.stringify(THEME)}); } catch (e) {}
    document.documentElement.setAttribute("data-theme", ${JSON.stringify(THEME)});
    return true;
  })()`);
  await sleep(2500);

  console.log("=".repeat(74));
  console.log("设置/导入界面探针  " + URL_ + "  " + WIDTH + "x" + HEIGHT + "  " + THEME);
  console.log("=".repeat(74));

  let r;
  try {
    r = await cdp.eval(PROBE_JS);
  } catch (e) {
    console.error("探针执行失败: " + e.message);
    const errs = cdp.events.filter(x => x.method === "Runtime.exceptionThrown");
    for (const x of errs.slice(0, 5)) {
      console.error("  页面异常: " +
        (x.params.exceptionDetails.exception?.description || "").split("\n")[0]);
    }
    try {
      const st = await cdp.eval(
        "({ hasPanel: !!document.querySelector('#settings')," +
        " hasImport: !!document.querySelector('#imp')," +
        " hasBtn: !!document.querySelector('#btn-settings')," +
        " scripts: document.scripts.length })");
      console.error("  页面实际状态: " + JSON.stringify(st));
    } catch (e2) { console.error("  读不到页面状态: " + e2.message); }
    ws.close(); proc.kill();
    return 1;
  }

  const m = r.metrics || {};
  console.log("\n几何：");
  console.log("  设置按钮 " + JSON.stringify(m.btn));
  console.log("  面板 " + JSON.stringify(m.panel));
  console.log("\n分节：");
  console.log("  标签 " + m.tabs + " 个 · 密码框 " + m.secret_boxes + " 个 · " +
              "页面含 sk- 明文: " + m.secret_in_dom);
  for (const s of (m.sections || [])) {
    console.log("  " + s.name.padEnd(5) + " " + String(s.n).padStart(2) + " 字段 · " +
                "状态[" + s.setup_tag + "] " + s.tag_class + " · 点 " + s.dot_ok);
    console.log("        " + s.labels.join(" / "));
  }
  console.log("\n保存链路：");
  console.log("  超时 " + m.timeout_before + " -> 界面改成 " + m.timeout_after +
              "（服务端读回）-> 还原 " + m.timeout_restored);
  console.log("  改动高亮 " + m.dirty_marked + " · 编辑后「" + m.msg_after_edit +
              "」· 保存后「" + m.msg_after_save + "」");
  console.log("  密钥仍在: " + m.key_still_set);
  console.log("\n对比度：");
  for (const [k, v] of Object.entries(m.contrasts || {})) {
    if (!v) { console.log("  " + k + " : 未渲染"); continue; }
    console.log("  " + k.padEnd(10) + " " + String(v.ratio).padStart(6) + ":1  " +
                v.px + "px 需要 " + v.need + ":1  " + (v.ratio >= v.need ? "✓" : "✗"));
  }
  console.log("\n导入面板：");
  console.log("  选项 " + JSON.stringify(m.import_opts));
  console.log("  预填路径 " + m.import_path);
  console.log("  提示 " + m.import_hint);
  console.log("  切到公共库后选中状态 " + JSON.stringify(m.import_switch));
  console.log("\nEsc：设置 " + m.esc_closes + " · 导入 " + m.import_esc_closes);

  const order = { fatal: 0, error: 1, warn: 2, info: 3 };
  const bad = (r.checks || []).filter(c => c.level !== "info")
    .sort((a, b) => order[a.level] - order[b.level]);
  console.log("\n问题：");
  if (!bad.length) console.log("  （无）");
  for (const c of bad) console.log("  [" + c.level.toUpperCase() + "] " + c.name + "  " + c.detail);

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
