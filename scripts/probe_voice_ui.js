/**
 * Probe: 声纹署名在界面上的表现 —— 一次绑定改一片，同一个人同一个颜色。
 *
 * NOTE FOR EDITORS: PROBE_JS is a template literal; a backtick inside it ends it early.
 * check_injected_js.py guards that.
 *
 * 用户的要求是两句话，这个探针就盯这两句：
 *   "相同声纹的人,点击一次绑定之后,其他相同声纹的就自动把名字改过来"
 *   "可以用相同颜色表示同一个人的发言"
 *
 * 它自己造一段带声纹标注的发言（两人各两句），绑一次，然后从**界面上**读回：
 * 四行的名字是不是都对、两种声音的颜色是不是各自一致、同色的行是不是只属于同一个人。
 * 跑完把自己造的东西删干净。
 *
 * Usage: node scripts/probe_voice_ui.js
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
const PORT = parseInt(arg("debug-port", "9431"), 10);
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
  const post = async (p, d) => (await (await fetch(p, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(d) })).json());
  const state = async () => (await (await fetch('/api/state?since=0')).json());

  const MARK = '__声纹自测__';
  const SPK_A = 'spk_probe_A', SPK_B = 'spk_probe_B';

  // 清掉上一次可能留下的自测发言（有真删接口，所以探针不留垃圾）
  let st = await state();
  for (const s of st.segments.filter(x => x.text.indexOf(MARK) === 0)) {
    await post('/api/segment', { action: 'remove', id: s.id });
  }
  // 手工通道按 (文本, 起点) 判重，所以用固定文本与起点即可覆盖
  const test = [[MARK + '甲一', 600.0, SPK_A], [MARK + '乙一', 601.0, SPK_B],
                [MARK + '甲二', 602.0, SPK_A], [MARK + '乙二', 603.0, SPK_B]];
  for (const [text, start, spk] of test) {
    await post('/api/segment', { text: text, start: start, end: start + 1.2, spk: spk });
  }
  await sleep(1600);   // 等界面把它们画出来

  st = await state();
  const mine = st.segments.filter(s => s.text.indexOf(MARK) === 0)
    .sort((a, b) => a.start - b.start);
  out.metrics.injected = mine.length;
  if (mine.length !== 4) {
    add('fatal', '测试发言没有全部灌进去', mine.length + ' / 4');
    return out;
  }
  // 探针自带两个人：**绝不能用用户的参会人**。
  // 第一版用了 people[0]/people[1]，而解绑是"按人"清的（一个人可能挂多个声纹），
  // 于是探针跑一次就把那两个人真实的声纹绑定全清掉——测试不许动用户的数据。
  const PN = '__声纹自检';
  for (const p of st.participants.filter(x => x.name.indexOf(PN) === 0)) {
    await post('/api/participants', { action: 'remove', id: p.id });
  }
  for (const nm of [PN + '甲__', PN + '乙__']) {
    await post('/api/participants', { action: 'add', name: nm, org: '自检', role: '自检' });
  }
  st = await state();
  const people = st.participants.filter(x => x.name.indexOf(PN) === 0);
  if (people.length < 2) { add('fatal', '自检用的人没建起来', String(people.length)); return out; }
  // 只解掉**自己造的**那两个声纹上的绑定。
  // 第一版无条件解绑所有人——于是每跑一次探针，就把用户真实的声纹绑定全清掉。
  // 测试不许动用户的绑定关系，和便签、发言是同一个道理。
  for (const p of people) {
    if ((p.voice_id || '').indexOf('spk_probe_') === 0) {
      await post('/api/participants', { action: 'unbind_voice', id: p.id });
    }
  }

  const rowOf = id => document.querySelector('#feed .utt[data-idx="' + id + '"]');
  const idxOf = seg => seg.idx;

  // ── 1. 未署名时应当提示"同一声音 N 句" ────────────────────────────
  // 等够两轮轮询：刚灌进去的 4 句要等页面画出来、并且拿到 spk_counts 才知道该提示几句。
  // 等不够的话读到的还是旧 DOM，这条警告会一直误报——而"永远在响的警告"等于没有警告。
  await sleep(2600);
  st = await state();
  const first = st.segments.filter(s => s.text.indexOf(MARK) === 0)
    .sort((a, b) => a.start - b.start)[0];
  // 轮询式等待：界面是异步更新的，读一次就断言会误报（第一版就是这样一直误报）。
  // 断言写成"最多等 6 秒，等到为止"，超时才算失败。
  let hint = '';
  for (let i = 0; i < 20; i++) {
    const r0 = rowOf(first.idx);
    hint = r0 ? ((r0.querySelector('.u-spk') || {}).textContent || '') : '';
    if (hint) break;
    await sleep(300);
  }
  if (!rowOf(first.idx)) {
    add('fatal', '发言没有出现在发言流里', 'idx=' + first.idx);
    return out;
  }
  out.metrics.hint_before = hint;
  if (!hint) {
    add('error', '未署名时没有提示"同一声音还有几句"',
        '用户不知道点一下会影响别的句子');
  }

  // ── 2. 点一次绑定：同声纹的两句都该改名 ──────────────────────────
  const jia = people[0], yi = people[1];
  await post('/api/participants', { action: 'bind_voice', id: jia.id, seg_id: first.id });
  await sleep(1800);
  st = await state();
  const rows = st.segments.filter(s => s.text.indexOf(MARK) === 0)
    .sort((a, b) => a.start - b.start);
  const named = rows.map(s => s.speaker);
  out.metrics.names_after_bind = named;
  const wantA = [jia.name, '', jia.name, ''];
  if (JSON.stringify(named) !== JSON.stringify(wantA)) {
    add('fatal', '一次绑定没有把同声纹的句子一起改名', JSON.stringify(named));
  }
  // 界面上也要看得见
  const domNames = rows.map(s => {
    const r = rowOf(s.idx);
    return r ? (r.querySelector('.u-who').textContent || '').trim() : '(缺行)';
  });
  out.metrics.dom_names = domNames;
  if (domNames[0] !== jia.name || domNames[2] !== jia.name) {
    add('error', '界面上没有显示出改过的名字', JSON.stringify(domNames));
  }

  // ── 3. 同一个人的发言用同一个颜色 ─────────────────────────────────
  const bars = rows.map(s => {
    const r = rowOf(s.idx);
    return { name: s.speaker, spk: s.spk,
             cls: r ? r.classList.contains('has-spk') : null,
             bar: r ? getComputedStyle(r).getPropertyValue('--utt-bar').trim() : '' };
  });
  out.metrics.bars = bars;
  const byName = {};
  for (const b of bars) {
    if (!b.name) continue;
    (byName[b.name] = byName[b.name] || []).push(b.bar);
  }
  for (const [nm, list] of Object.entries(byName)) {
    if (new Set(list).size !== 1) {
      add('error', '同一个人的发言颜色不一致', nm + ': ' + JSON.stringify(list));
    }
    if (!list[0]) add('error', '已署名的发言没有拿到颜色', nm);
  }
  const barA = bars.find(b => b.spk === SPK_A && b.bar);
  if (barA && !barA.cls) add('error', '已署名但没有加 has-spk 类', SPK_A);

  // ── 4. 第二个人绑上之后：两种颜色、两张脸 ─────────────────────────
  const firstB = rows.find(s => s.spk === SPK_B);
  await post('/api/participants', { action: 'bind_voice', id: yi.id, seg_id: firstB.id });
  await sleep(1800);
  st = await state();
  const rows2 = st.segments.filter(s => s.text.indexOf(MARK) === 0)
    .sort((a, b) => a.start - b.start);
  out.metrics.names_after_two = rows2.map(s => s.speaker);
  const bars2 = rows2.map(s => {
    const r = rowOf(s.idx);
    return r ? getComputedStyle(r).getPropertyValue('--utt-bar').trim() : '';
  });
  out.metrics.bars2 = bars2;
  const uniq = new Set(bars2.filter(Boolean));
  out.metrics.distinct_colors = uniq.size;
  if (uniq.size !== 2) {
    add('error', '两个人的发言应当有两种颜色', uniq.size + ' 种：' + JSON.stringify(bars2));
  }
  // 同色的行必须同人：不能两个人共用一个颜色
  const colorToNames = {};
  rows2.forEach((s, i) => {
    if (!s.speaker) return;
    (colorToNames[bars2[i]] = colorToNames[bars2[i]] || new Set()).add(s.speaker);
  });
  for (const [c, names] of Object.entries(colorToNames)) {
    if (names.size > 1) {
      add('error', '一种颜色对应了多个人', c + ' -> ' + [...names].join(' / '));
    }
  }

  // ── 5. 收尾：解绑 + 删掉自测发言 ─────────────────────────────────
  // 把自己建的那两个人删掉（删人会连带清掉他们的声纹映射）
  for (const p of [jia, yi]) {
    await post('/api/participants', { action: 'remove', id: p.id });
  }
  await sleep(400);
  const last = await state();
  for (const s of last.segments.filter(x => x.text.indexOf(MARK) === 0)) {
    await post('/api/segment', { action: 'remove', id: s.id });
  }
  const end = await state();
  out.metrics.leftover = end.segments.filter(x => x.text.indexOf(MARK) === 0).length;
  if (out.metrics.leftover) add('error', '探针造出来的发言没删干净', String(out.metrics.leftover));
  return out;
})()`;

async function main() {
  const profile = path.join(os.tmpdir(), "edge-probe-voice-" + Date.now());
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
  console.log("声纹署名探针（一次绑定改一片 + 同人同色）  " + URL_ + "  " + THEME);
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
  console.log("\n灌入 " + m.injected + " 句带声纹的发言（两人各两句）");
  console.log("未署名时的提示：「" + (m.hint_before || "") + "」");
  console.log("绑第一次之后的名字: " + JSON.stringify(m.names_after_bind));
  console.log("界面上读到的名字  : " + JSON.stringify(m.dom_names));
  console.log("绑第二次之后的名字: " + JSON.stringify(m.names_after_two));
  console.log("每行的颜色        : " + JSON.stringify(m.bars2));
  console.log("不同颜色数        : " + m.distinct_colors);

  const order = { fatal: 0, error: 1, warning: 2, warn: 2, info: 3 };
  const bad = (r.checks || []).filter(c => c.level !== "info")
    .sort((a, b) => (order[a.level] || 9) - (order[b.level] || 9));
  console.log("\n问题：");
  if (!bad.length) console.log("  （无）");
  for (const c of bad) console.log("  [" + c.level.toUpperCase() + "] " + c.name + "  " + c.detail);

  const exceptions = events.filter(e => e.method === "Runtime.exceptionThrown")
    .map(e => (e.params.exceptionDetails.exception?.description || "").split("\n")[0]);
  if (exceptions.length) {
    console.log("\n页面异常：");
    for (const e of exceptions.slice(0, 4)) console.log("  " + e.slice(0, 170));
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
