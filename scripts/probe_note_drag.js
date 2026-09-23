/**
 * Probe: drag a sticky note the way a real mouse does it.
 *
 * NOTE FOR EDITORS: PROBE_JS is a template literal -- no backticks inside it.
 *
 * Why this exists next to audit_ui.js instead of inside it: the audit used to drag with a
 * **single** pointermove, and its own comment said why --
 *
 *     "循环派发是本装置反复失败的根源：每次 pointermove 都会重算目标位置，而便签的
 *      拖动基准与'当前位置'在轮询重建 DOM 的间隙里会对不上，于是每步只累加一小段
 *      （实测 8 步只走了 512px），看起来就像'拖不动'"
 *
 * It found the bug, wrote it down, and then changed the input until the assertion passed.
 * A real mouse emits dozens of pointermove events over more than a second, so the user hit
 * exactly what the harness had dodged -- "便签移动还是非常困难".
 *
 * So this probe is deliberately shaped the other way: MANY steps, over LONGER than the poll
 * interval, and it measures the lag *during* the drag rather than only the endpoint. It also
 * drags by the note body, not just the 24px head strip, because that is where a person grabs.
 *
 * Restores the layout through the reset endpoint, so running it leaves no trace.
 *
 * Usage: node scripts/probe_note_drag.js [--url ...] [--width 1680] [--height 1000]
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
const PORT = parseInt(arg("debug-port", "9391"), 10);
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
  const layer = document.querySelector('#notes-layer');

  const plan = async () => (await (await fetch('/api/plan')).json());
  const before = await plan();
  out.metrics.notes = (before.items || []).length;
  if (!out.metrics.notes) {
    add('fatal', '没有便签可测', '发言计划是空的');
    return out;
  }

  // 真实鼠标的一步：派发到指针下的元素上（pointerdown 落在便签上，move/up 冒泡到 window）。
  const fire = (target, type, x, y, buttons) => {
    const ev = new PointerEvent(type, {
      bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y,
      pointerId: 1, pointerType: 'mouse', isPrimary: true, button: 0,
      buttons: buttons === undefined ? (type === 'pointerup' ? 0 : 1) : buttons,
    });
    target.dispatchEvent(ev);
  };

  const noteEl = () => layer.querySelector('.note');
  const targetId = noteEl().dataset.id;

  // 一次"真实"拖动：steps 步、每步间隔 stepMs，全程超过轮询周期（约 1s）。
  // 关键在**过程中**量偏差：只在终点量的话，正好漏掉"中途弹回起点"这个症状。
  //
  // 期望值按 **moveArea()**（整个工作区）算，不是 noteArea()（自动摆放的便签道）。
  // 这两者分开正是这一轮的核心：自动摆放待在便签道里（界面一打开就整齐），
  // 手动摆放可以到工作区任何地方。第一版把它们当成同一个东西，于是在便签道边上
  // 往左/往上拖可用空间是 0——用户的原话是"还是非常不跟手，而且可移动的范围被限制了"。
  async function drag({ label, grabSel, dx, dy, steps, stepMs, expectClamp }) {
    const note = noteEl();
    const grab = grabSel ? note.querySelector(grabSel) : note;
    if (!grab) { add('error', label + '：找不到抓手', String(grabSel)); return null; }
    const b = moveArea();
    const r0 = note.getBoundingClientRect();
    const roomL = r0.left - b.left, roomR = (b.right - r0.width) - r0.left;
    const roomU = r0.top - b.top, roomD = (b.bottom - r0.height) - r0.top;
    let wx = dx, wy = dy;
    if (expectClamp) {
      // 故意拖出界：往空间**更大**的那一侧拖，保证真的撞到边界而不是没动
      wx = (roomL > roomR ? -(roomL + 260) : (roomR + 260));
      wy = (roomU > roomD ? -(roomU + 260) : (roomD + 260));
    } else {
      // 往空间够的方向拖，保证位移不被夹紧吃掉
      if (dx < 0 && roomL < Math.abs(dx) + 8) wx = Math.min(roomR - 8, Math.abs(dx));
      if (dx > 0 && roomR < dx + 8) wx = -Math.min(roomL - 8, dx);
      if (dy < 0 && roomU < Math.abs(dy) + 8) wy = Math.min(roomD - 8, Math.abs(dy));
      if (dy > 0 && roomD < dy + 8) wy = -Math.min(roomU - 8, dy);
    }
    dx = Math.round(wx); dy = Math.round(wy);
    out.metrics[label + '_room'] = { left: Math.round(roomL), right: Math.round(roomR),
                                     up: Math.round(roomU), down: Math.round(roomD) };

    const gr = grab.getBoundingClientRect();
    const sx = Math.round(gr.left + Math.min(20, gr.width / 2));
    const sy = Math.round(gr.top + Math.min(10, gr.height / 2));
    const hit = document.elementFromPoint(sx, sy);
    out.metrics[label + '_hit'] = hit ? (hit.className || hit.tagName) : 'none';

    const startRect = note.getBoundingClientRect();
    // 目标 = 起点 + 位移，再夹进便签道（越界测试除外，它就是要撞边界）
    const wantX = Math.min(b.right - startRect.width,
                  Math.max(b.left, startRect.left + dx));
    const wantY = Math.min(b.bottom - startRect.height,
                  Math.max(b.top, startRect.top + dy));
    fire(hit || grab, 'pointerdown', sx, sy);
    let worst = 0;
    const trace = [];
    for (let i = 1; i <= steps; i++) {
      const x = Math.round(sx + dx * i / steps);
      const y = Math.round(sy + dy * i / steps);
      fire(window, 'pointermove', x, y);
      await sleep(stepMs);
      const cur = noteEl();   // 每次都重新取：重建是这条 bug 的表现之一
      const r = cur.getBoundingClientRect();
      const expX = Math.min(b.right - r.width,
                   Math.max(b.left, startRect.left + dx * i / steps));
      const expY = Math.min(b.bottom - r.height,
                   Math.max(b.top, startRect.top + dy * i / steps));
      const lag = Math.hypot(r.left - expX, r.top - expY);
      worst = Math.max(worst, lag);
      if (i === steps || i % Math.ceil(steps / 5) === 0) {
        trace.push({ step: i, x: Math.round(r.left), y: Math.round(r.top),
                     lag: Math.round(lag) });
      }
    }
    fire(window, 'pointerup', Math.round(sx + dx), Math.round(sy + dy));
    await sleep(500);
    const final = noteEl().getBoundingClientRect();
    const off = Math.hypot(final.left - wantX, final.top - wantY);
    out.metrics[label] = { worst_lag: Math.round(worst), final_off: Math.round(off),
                           moved: Math.round(final.left - startRect.left) + ',' +
                                  Math.round(final.top - startRect.top),
                           want: Math.round(wantX - startRect.left) + ',' +
                                 Math.round(wantY - startRect.top),
                           trace: trace };
    if (worst > 24) {
      add('fatal', label + '：拖动过程中便签没跟上指针',
          '最大偏差 ' + Math.round(worst) + 'px（' + steps + ' 步 / ' +
          (steps * stepMs) + 'ms，跨越轮询周期）');
    }
    if (off > 6) {
      add('fatal', label + '：松手后便签没停在指针位置',
          '偏 ' + Math.round(off) + 'px，实际位移 ' + out.metrics[label].moved +
          '，期望 ' + out.metrics[label].want);
    }
    if (expectClamp) {
      // 夹紧的正确表现：正好停在**工作区**边界上（4px 边距），而不是停在便签道上。
      const near = (v, t) => Math.abs(v - t) < 4;
      const atL = near(final.left, b.left), atR = near(final.right, b.right);
      const atT = near(final.top, b.top), atB = near(final.bottom, b.bottom);
      out.metrics[label + '_clamped_at'] = { left: atL, right: atR, top: atT, bottom: atB };
      if (!(atL || atR || atT || atB)) {
        add('error', label + '：越界拖动后没有停在屏幕边界上',
            JSON.stringify({ left: Math.round(final.left), top: Math.round(final.top),
                             right: Math.round(final.right), bottom: Math.round(final.bottom),
                             area: b }));
      }
    }
    // 松手后位置必须落到服务端
    const after = await plan();
    const it = (after.items || []).find(x => x.id === targetId);
    out.metrics[label + '_saved'] = it ? (it.nx + ',' + it.ny) : null;
    if (!it || it.nx == null) {
      add('error', label + '：位置没有落库', JSON.stringify(it && { nx: it.nx, ny: it.ny }));
    }
    return { note: noteEl().getBoundingClientRect() };
  }

  // ── 1. 抓正文拖（用户的第一反应）──────────────────────────────────
  // 正文是 contentEditable，旧版这里根本拖不动（只有顶部 24px 窄条能拖）。
  await drag({ label: '正文拖动', grabSel: '.note-topic', dx: -140, dy: 150,
               steps: 40, stepMs: 40 });

  // ── 2. 抓顶部拖 ───────────────────────────────────────────────────
  // 往下拖，不往上：第一张便签本来就贴着便签道顶边，往上拖会被夹住——那是设计行为
  // （便签不许盖住参会人员和线索栏），不是缺陷。探针第一版就是往上拖的，于是把
  // "夹紧生效"报成了"拖不动"。
  await resetNote(targetId);
  await drag({ label: '顶部拖动', grabSel: '.note-head', dx: 120, dy: 90,
               steps: 40, stepMs: 40 });

  // ── 3. 慢速拖动：全程 4 秒，必然跨过 3~4 次轮询 ────────────────────
  await resetNote(targetId);
  await drag({ label: '慢速拖动', grabSel: '.note-topic', dx: 60, dy: 220,
               steps: 40, stepMs: 100 });

  // ── 4. 越界拖动必须正好停在屏幕边界（夹紧是设计行为，要能验证）──
  await resetNote(targetId);
  await drag({ label: '越界夹紧', grabSel: '.note-topic', dx: 0, dy: 0,
               steps: 24, stepMs: 40, expectClamp: true });

  // ── 5. 可移动范围必须覆盖整个工作区 ───────────────────────────────
  // 这一条直接对应用户的话："可移动的范围被限制了"。
  // 便签自动摆在便签道里（中栏右侧），本项把它拖到**左栏上**——跨越整条便签道、
  // 跨过发言流、进入参会人员那一栏。上一版这里会被硬夹住，位移是 0。
  await resetNote(targetId);
  {
    const note = noteEl();
    const grab = note.querySelector('.note-topic');
    const gr = grab.getBoundingClientRect();
    const r0 = note.getBoundingClientRect();
    const ma = moveArea();
    const lane = noteArea();
    // 目标：左边界附近（远在便签道之外）
    const targetLeft = ma.left + 8;
    const targetTop = Math.max(ma.top + 8, r0.top);
    const dx = Math.round(targetLeft - r0.left);
    const dy = Math.round(targetTop - r0.top);
    fire(document.elementFromPoint(Math.round(gr.left + 10), Math.round(gr.top + 8)) || grab,
         'pointerdown', Math.round(gr.left + 10), Math.round(gr.top + 8));
    for (let i = 1; i <= 30; i++) {
      fire(window, 'pointermove', Math.round(gr.left + 10 + dx * i / 30),
           Math.round(gr.top + 8 + dy * i / 30));
      await sleep(35);
    }
    fire(window, 'pointerup', Math.round(gr.left + 10 + dx), Math.round(gr.top + 8 + dy));
    await sleep(400);
    const rf = noteEl().getBoundingClientRect();
    out.metrics.cross_lane = { from_x: Math.round(r0.left), to_x: Math.round(rf.left),
                               moved: Math.round(rf.left - r0.left), want: dx,
                               lane_left: lane.left, area_left: ma.left };
    if (Math.abs(rf.left - targetLeft) > 6) {
      add('error', '便签挪不到工作区左侧（可移动范围被限制）',
          '想要 left=' + targetLeft + '，实际 ' + Math.round(rf.left) +
          '；便签道左边界是 ' + lane.left);
    }
    if (Math.abs(rf.left - r0.left) < Math.abs(dx) * 0.9) {
      add('error', '跨栏拖动被截断', '位移 ' + Math.round(rf.left - r0.left) +
          '，期望 ' + dx);
    }
  }

  // ── 4. 单击正文应当进入编辑，而不是没反应 ─────────────────────────
  const note = noteEl();
  const topic = note.querySelector('.note-topic');
  const tr = topic.getBoundingClientRect();
  const tx = Math.round(tr.left + 10), ty = Math.round(tr.top + 8);
  fire(document.elementFromPoint(tx, ty) || topic, 'pointerdown', tx, ty);
  fire(window, 'pointerup', tx, ty);
  await sleep(300);
  out.metrics.click_edits = document.activeElement === topic;
  if (!out.metrics.click_edits) {
    add('error', '单击正文没有进入编辑',
        'activeElement=' + (document.activeElement && document.activeElement.className));
  }

  // ── 5. 轮询不该打断正在编辑的便签 ─────────────────────────────────
  // 这一条和拖拽同源：重建 DOM 会把 contentEditable 的元素换掉，焦点就没了。
  const editable = noteEl().querySelector('.note-topic');
  editable.focus();
  await sleep(1600);            // 至少跨一次轮询
  out.metrics.edit_survives_poll = document.activeElement === editable;
  if (!out.metrics.edit_survives_poll) {
    add('error', '轮询把正在编辑的便签打断了',
        '一秒后焦点丢了——用户改字改到一半');
  }
  if (editable === document.activeElement) editable.blur();

  // ── 6. 压到别人身上：其他便签不能动，自己必须在最上面 ──────────────
  // 直接对应两条用户反馈：
  //   "移动到其他便签后面会导致其他便签随机重排"
  //   "我希望真正移动的那个便签是最前面的"
  await resetNote(targetId);
  {
    const snap = () => [...document.querySelectorAll('#notes-layer .note')].map(n => {
      const r = n.getBoundingClientRect();
      return { id: n.dataset.id, z: parseInt(n.style.zIndex, 10) || 0,
               box: [Math.round(r.left), Math.round(r.top)],
               w: Math.round(r.width), h: Math.round(r.height) };
    });
    const before = snap();
    const me = before.find(b => b.id === targetId);
    const other = before.find(b => b.id !== targetId);
    if (!other) {
      add('warning', '只有一张便签，测不了"压到别人身上"', '');
    } else {
      const mine = layer.querySelector('.note[data-id="' + targetId + '"]');
      const otherEl = layer.querySelector('.note[data-id="' + other.id + '"]');
      const g = mine.querySelector('.note-topic').getBoundingClientRect();
      const ob = otherEl.getBoundingClientRect();
      // 目标：落到别人**正中央**（故意造成重叠）
      const dx = Math.round((ob.left + ob.width / 2) -
                            (g.left + Math.min(20, g.width / 2)));
      const dy = Math.round((ob.top + ob.height / 2) -
                            (g.top + Math.min(10, g.height / 2)));
      const sx = Math.round(g.left + Math.min(20, g.width / 2));
      const sy = Math.round(g.top + Math.min(10, g.height / 2));
      fire(document.elementFromPoint(sx, sy) || mine, 'pointerdown', sx, sy);
      for (let i = 1; i <= 24; i++) {
        fire(window, 'pointermove', Math.round(sx + dx * i / 24),
             Math.round(sy + dy * i / 24));
        await sleep(35);
      }
      fire(window, 'pointerup', Math.round(sx + dx), Math.round(sy + dy));
      // 等**超过一次轮询**：重排问题是在渲染之后才出现的，不等就抓不到
      await sleep(1900);
      const after = snap();
      out.metrics.stack_me = me.id;
      out.metrics.stack_target = other.id;
      // (a) 除我之外，每一张的位置都不能变
      const movedOthers = [];
      for (const b of before) {
        if (b.id === me.id) continue;
        const a2 = after.find(x => x.id === b.id);
        if (!a2) { movedOthers.push(b.id + '(消失)'); continue; }
        if (Math.abs(a2.box[0] - b.box[0]) > 2 || Math.abs(a2.box[1] - b.box[1]) > 2) {
          movedOthers.push(b.id + ' ' + JSON.stringify(b.box) + '->' + JSON.stringify(a2.box));
        }
      }
      out.metrics.stack_moved_others = movedOthers.length;
      if (movedOthers.length) {
        add('fatal', '把便签压到别人身上，其他便签被重排了',
            movedOthers.length + ' 张动了：' + movedOthers.slice(0, 3).join('；'));
      }
      // (b) 我必须在最上面
      const meAfter = after.find(x => x.id === me.id);
      const maxOther = Math.max(...after.filter(x => x.id !== me.id).map(x => x.z));
      out.metrics.stack_z = { mine: meAfter ? meAfter.z : null, max_other: maxOther };
      if (!meAfter) {
        add('error', '被拖动的便签消失了', me.id);
      } else if (meAfter.z <= maxOther) {
        add('error', '被拖动的便签没有在最前面',
            '层级 ' + meAfter.z + '，别人最高 ' + maxOther +
            '——拖到别人身上时应该盖住对方');
      }
      // (c) 我自己也要停在我放开的地方（不能被自动摆放挪走）
      //
      // 尺寸要用**拖之前**量到的：渲染之后那个元素引用已经脱落，
      // 从它身上读 getBoundingClientRect() 只会得到 0，夹紧算式就失真了
      // （探针第一版就是这么误报了一个 83px 的"偏移"）。
      // 注意：这段注释里不能出现反引号——PROBE_JS 是模板字符串，反引号会提前截断它。
      const ma2 = moveArea();
      const wantX = Math.min(ma2.right - me.w, Math.max(ma2.left, me.box[0] + dx));
      const wantY = Math.min(ma2.bottom - me.h, Math.max(ma2.top, me.box[1] + dy));
      out.metrics.stack_self_off =
        Math.round(Math.hypot(meAfter ? meAfter.box[0] - wantX : 999,
                              meAfter ? meAfter.box[1] - wantY : 999));
      if (out.metrics.stack_self_off > 6) {
        add('error', '压到别人身上之后，自己被挪走了',
            '偏 ' + out.metrics.stack_self_off + 'px（用户手摆的位置应当原样保留）');
      }
    }
  }
  await resetNote(targetId);

  async function resetNote(id) {
    await fetch('/api/prep/layout', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'reset', ids: [id] }) });
    await sleep(450);
    const n = layer.querySelector('.note[data-id="' + id + '"]');
    return n ? n.getBoundingClientRect() : null;
  }

  return out;
})()`;

async function main() {
  const profile = path.join(os.tmpdir(), "edge-probe-drag-" + Date.now());
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
  // ?guide=off：这个探针**用真实鼠标事件拖便签**，盖着的那层透明全屏层会把
  // mousedown 全接走，报出来却是"拖不动"。
  await send("Page.navigate", { url: URL_ + (URL_.indexOf("?") >= 0 ? "&" : "?") + "guide=off" });
  await sleep(3200);
  await evalJs('(() => { try { localStorage.setItem("plaud-theme", ' +
    JSON.stringify(THEME) + '); } catch (e) {} ' +
    'document.documentElement.setAttribute("data-theme", ' + JSON.stringify(THEME) +
    '); return 1; })()', false);
  await sleep(1200);

  console.log("=".repeat(74));
  console.log("便签拖拽探针（真实鼠标节奏：多步 + 跨轮询）  " + URL_ + "  " +
              WIDTH + "x" + HEIGHT);
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
  console.log("\n便签 " + m.notes + " 张（测第一张）");
  for (const k of ['正文拖动', '顶部拖动', '慢速拖动', '越界夹紧']) {
    const d = m[k];
    if (!d) continue;
    console.log("\n" + k + "：");
    console.log("  抓取点命中 " + (m[k + '_hit'] || '?') +
                "  便签道余量 " + JSON.stringify(m[k + '_room'] || {}));
    console.log("  期望位移 " + d.want + "  实际 " + d.moved +
                "  过程中最大偏差 " + d.worst_lag + "px  终点偏差 " + d.final_off + "px");
    if (m[k + '_clamped_at']) {
      console.log("  停在边界 " + JSON.stringify(m[k + '_clamped_at']));
    }
    console.log("  落库位置 " + m[k + '_saved']);
    console.log("  过程采样 " + d.trace.map(t => `#${t.step}:(${t.x},${t.y}) lag${t.lag}`)
      .join('  '));
  }
  console.log("\n单击正文进入编辑: " + m.click_edits);
  console.log("编辑不被轮询打断: " + m.edit_survives_poll);
  console.log("跨栏拖动: " + JSON.stringify(m.cross_lane || {}));
  console.log("压到别人身上: 我自己=" + m.stack_me + " 目标=" + m.stack_target +
              " 被挤动的其他便签=" + m.stack_moved_others + " 张" +
              " 层级=" + JSON.stringify(m.stack_z || {}) +
              " 自身偏移=" + m.stack_self_off + "px");

  const order = { fatal: 0, error: 1, warn: 2, info: 3 };
  const bad = (r.checks || []).filter(c => c.level !== "info")
    .sort((a, b) => (order[a.level] || 9) - (order[b.level] || 9));
  console.log("\n问题：");
  if (!bad.length) console.log("  （无）");
  for (const c of bad) console.log("  [" + c.level.toUpperCase() + "] " + c.name + "  " + c.detail);

  const exceptions = events.filter(e => e.method === "Runtime.exceptionThrown")
    .map(e => (e.params.exceptionDetails.exception?.description || "").split("\n")[0]);
  if (exceptions.length) {
    console.log("\n页面异常：");
    for (const e of exceptions.slice(0, 5)) console.log("  " + e.slice(0, 170));
  }

  const errors = bad.filter(c => c.level === "fatal" || c.level === "error").length;
  console.log("\n结论：" + errors + " 错误 · " +
    bad.filter(c => c.level === "warn").length + " 警告");

  ws.close(); proc.kill();
  return errors ? 1 : 0;
}

main().then(c => process.exit(c)).catch(e => {
  console.error("探针失败: " + (e && e.stack || e));
  process.exit(1);
});
