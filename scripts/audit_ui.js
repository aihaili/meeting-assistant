/**
 * Render audit: drive the real page in a real browser and report objective faults.
 *
 * NOTE FOR EDITORS: the big ``AUDIT_JS`` block below is a template literal, so any
 * backtick inside it ends the string early and produces a SyntaxError that looks like a
 * CSS or selector bug. This has bitten three times; write inner strings with single
 * quotes and never use a backtick between the opening and closing delimiters.
 *
 * Why this exists rather than "look at the screenshot": the agent that wrote this CSS
 * cannot see images. Worse, a screenshot only shows what the page looks like at one
 * instant, whereas most of the things that actually break a dense three-column layout
 * are invisible in a still frame -- a column overflowing by 4px, a title truncated to
 * an ellipsis, a contrast ratio of 3.1:1 on a timestamp, a JS exception that left half
 * the UI unrendered. All of those are *computable*, so they are computed here.
 *
 * Checks:
 *   1. console errors and failed network requests
 *   2. horizontal overflow of the document and of each scroll container
 *   3. text truncation (scrollWidth > clientWidth) on elements that should not clip
 *   4. presence and geometry of the three columns and their essential regions
 *   5. WCAG contrast of every distinct text colour against its background
 *   6. keyboard shortcuts actually change state
 *   7. tap-target size of interactive chips and buttons
 *
 * Usage:
 *   node scripts/audit_ui.js [--url http://127.0.0.1:8510/] [--width 1680] [--height 1000]
 */

"use strict";
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}

const URL_ = arg("url", "http://127.0.0.1:8510/");
const WIDTH = parseInt(arg("width", "1680"), 10);
const HEIGHT = parseInt(arg("height", "1000"), 10);
const PORT = parseInt(arg("debug-port", "9333"), 10);
const SHOT = arg("shot", "");
// Which theme to audit. Dark is the primary theme now, and dark mode is where contrast
// failures hide: the light palette's ratios were measured, and swapping in a dark palette
// produces colours whose ratios nobody checked. Running the same assertions in both themes
// is the only way to know the second one is not quietly unreadable.
const THEME = arg("theme", "dark");

const sleep = ms => new Promise(r => setTimeout(r, ms));

function getJSON(url) {
  return new Promise((resolve, reject) => {
    http.get(url, res => {
      let b = "";
      res.on("data", c => (b += c));
      res.on("end", () => {
        try { resolve(JSON.parse(b)); } catch (e) { reject(e); }
      });
    }).on("error", reject);
  });
}

/** Minimal CDP client over the ws:// URL that /json/list hands out. */
class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.events = [];
    ws.addEventListener("message", ev => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      } else if (msg.method) {
        this.events.push(msg);
      }
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

const AUDIT_JS = `(async () => {
  const NOTE_W_AUDIT = 232;   // mirrors NOTE_W in ui.html, for clamp arithmetic
  const out = { checks: [], metrics: {} };
  const add = (level, name, detail) => out.checks.push({ level, name, detail });

  // ── 3. three columns present and sized ───────────────────────────────
  const q = s => document.querySelector(s);
  const cols = {
    left: q("#col-left"), mid: q("#col-mid"), right: q("#col-right"),
  };
  // At narrow widths the layout deliberately collapses the side columns to zero and
  // removes them (display:none). Reporting that as "column too small" would make the
  // audit cry wolf on every narrow run, and a check that always fires is a check
  // nobody reads. A collapsed-and-removed column is the intended design; a column that
  // is 1px wide with content still laid out is the bug, and that is a different
  // assertion (the overflow checks below).
  const collapsed = n => !n || n.getBoundingClientRect().width < 2;
  for (const [k, n] of Object.entries(cols)) {
    if (!n) { add("fatal", "缺少栏位 " + k, "selector #col-" + k); continue; }
    const r = n.getBoundingClientRect();
    out.metrics[k + "_w"] = Math.round(r.width);
    out.metrics[k + "_display"] = getComputedStyle(n).display;
    const gone = getComputedStyle(n).display === "none";
    if (r.width < 40 && !gone) {
      add("warn", "栏位宽度过小 " + k, Math.round(r.width) + "px（未 display:none）");
    }
  }
  out.metrics.collapsed_side = collapsed(cols.left) || collapsed(cols.right);
  // The middle column must always get the leftover space. If it is squeezed, the
  // transcript is unreadable, so this is an error regardless of width.
  if (cols.mid && !collapsed(cols.mid)) {
    const avail = document.documentElement.clientWidth;
    const got = cols.mid.getBoundingClientRect().width;
    const sides = (collapsed(cols.left) ? 0 : cols.left.getBoundingClientRect().width) +
                  (collapsed(cols.right) ? 0 : cols.right.getBoundingClientRect().width);
    const expect = avail - sides;
    out.metrics.mid_expected = Math.round(expect);
    if (got < expect - 24) {
      add("error", "中栏没有占满剩余宽度",
          Math.round(got) + "px，应为 " + Math.round(expect) + "px");
    }
  }

  // ── 2. overflow ──────────────────────────────────────────────────────
  const de = document.documentElement;
  out.metrics.doc_scrollW = de.scrollWidth;
  out.metrics.doc_clientW = de.clientWidth;
  if (de.scrollWidth > de.clientWidth + 1) {
    add("error", "页面横向溢出", de.scrollWidth + " > " + de.clientWidth);
    // Name the element responsible. "The page overflows by 35px" is not actionable;
    // "the header's stats block is 855px wide" is. Walk the tree and report every
    // element whose right edge or intrinsic width exceeds the viewport, deepest first,
    // so the culprit is the last thing printed rather than a guess.
    const vw = de.clientWidth;
    const culprits = [];
    for (const n of document.querySelectorAll("*")) {
      const cs = getComputedStyle(n);
      if (cs.display === "none" || cs.position === "fixed") continue;
      const r = n.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      const overRight = r.right - vw;
      const intrinsic = n.scrollWidth - vw;
      if (overRight > 2 || intrinsic > 2) {
        culprits.push({
          sel: n.tagName.toLowerCase() +
            (n.id ? "#" + n.id : "") +
            (n.className && typeof n.className === "string"
              ? "." + n.className.trim().split(/\s+/).slice(0, 2).join(".") : ""),
          right: Math.round(r.right), w: Math.round(r.width),
          scrollW: n.scrollWidth, clientW: n.clientWidth,
          over: Math.round(Math.max(overRight, intrinsic)),
        });
      }
    }
    culprits.sort((a, b) => b.over - a.over);
    out.metrics.overflow_culprits = culprits.slice(0, 8);
    for (const c of culprits.slice(0, 3)) {
      add("error", "溢出元素 " + c.sel,
          "right=" + c.right + " w=" + c.w + " scrollW=" + c.scrollW +
          " 超 " + c.over + "px");
    }
  }
  for (const [k, n] of Object.entries(cols)) {
    if (!n || getComputedStyle(n).display === "none") continue;   // collapsed on purpose
    const body = n.querySelector(".col-body");
    if (body && body.scrollWidth > body.clientWidth + 2) {
      add("error", "栏内横向溢出 " + k,
          body.scrollWidth + " > " + body.clientWidth);
    }
  }

  // ── 3. truncation ────────────────────────────────────────────────────
  // Only look at elements whose job is to show a complete string. Ellipsised
  // names are intentional; a clipped *utterance* is data loss.
  const mustFit = [...document.querySelectorAll(".u-text, .c-text, .ref-x, .d-quote")];
  let clipped = 0;
  for (const n of mustFit) {
    if (n.scrollHeight > n.clientHeight + 2 && getComputedStyle(n).overflow === "hidden") {
      clipped++;
      if (clipped <= 3) add("error", "正文被裁剪", n.className + " :: " +
        n.textContent.slice(0, 26));
    }
  }
  out.metrics.clipped_bodies = clipped;

  // ── 4. content actually rendered ─────────────────────────────────────
  out.metrics.segments = document.querySelectorAll("#feed .utt").length;
  out.metrics.clues = document.querySelectorAll("#clues .clue").length;
  out.metrics.people = document.querySelectorAll("#people .person").length;
  out.metrics.keywords = document.querySelectorAll("#feed .kw").length;
  out.metrics.badges = document.querySelectorAll("#feed .badge").length;
  out.metrics.filters = document.querySelectorAll("#filters .fchip").length;
  if (out.metrics.segments === 0) add("error", "发言流为空", "没有任何 .utt");
  if (out.metrics.clues === 0) add("error", "线索列为空", "没有任何 .clue");
  if (out.metrics.people === 0) add("error", "人员列为空", "没有任何 .person");
  if (out.metrics.keywords === 0) add("warn", "没有关键词热区", "高亮没生效");
  if (document.querySelector("#feed .empty")) {
    add("error", "发言流显示空态", "数据没渲染出来");
  }

  // The two plan lists are separate documents with separate affordances; a regression
  // that merges them, or drops one, is invisible in a screenshot of an empty session.
  // Agenda is a collapsible panel; the speaking plan is a full-viewport layer of notes.
  out.metrics.agenda_items = document.querySelectorAll("#pa-list .pl-item").length;
  out.metrics.notes = document.querySelectorAll("#notes-layer .note").length;
  if (!document.querySelector("#pa-import")) {
    add("error", "缺少会议流程导入按钮", "#pa-import 不存在");
  }
  const noteLayer = document.querySelector("#notes-layer");
  if (!noteLayer) {
    add("error", "缺少便签层", "#notes-layer 不存在");
  }
  // Notes must be positioned inside the viewport, or dragging would carry them out of
  // reach. They sit in a fixed full-page layer, so the valid range is the whole window --
  // that is the point of the layer, and it is what lets a note sit over any column.
  if (noteLayer) {
    for (const n of noteLayer.querySelectorAll(".note")) {
      const r = n.getBoundingClientRect();
      if (r.width < 40 || r.height < 20) {
        add("error", "便签尺寸异常", n.dataset.id + " " +
            Math.round(r.width) + "x" + Math.round(r.height));
        break;
      }
      if (r.right < 40 || r.bottom < 40) {
        add("error", "便签跑到视口外", n.dataset.id + " 在 " +
            Math.round(r.left) + "," + Math.round(r.top));
        break;
      }
    }
  }
  out.metrics.note_kinds = [...new Set([...document.querySelectorAll("#notes-layer .note")]
    .map(n => n.dataset.kind))].join(",");
  out.metrics.note_bands = document.querySelectorAll("#notes-layer .note-band").length;
  out.metrics.note_resizers = document.querySelectorAll("#notes-layer .note-resize").length;
  // Segments must offer a drag affordance; without one the drop path never fires and
  // "add to my plan" silently does nothing. The grip rather than the whole row, because
  // dragging a row disables text selection inside it. It carries no HTML5 drag
  // attribute -- dragging is pointer-driven now, and that attribute belongs to the API
  // that was replaced.
  const utts = [...document.querySelectorAll("#feed .utt")];
  out.metrics.draggable_segments = utts.filter(u => u.querySelector(".u-grip")).length;
  if (utts.length && !out.metrics.draggable_segments) {
    add("error", "发言段没有拖拽柄", utts.length + " 段都没有 .u-grip");
  }
  // Text must remain selectable -- copying a sentence verbatim is a core need and
  // whole-row draggable would break it.
  const sample = document.querySelector("#feed .utt .u-text");
  if (sample && getComputedStyle(sample).userSelect === "none") {
    add("error", "发言正文无法选中", "user-select: none");
  }
  // Reference pinning: every clue card must carry a parseable payload, because the drop
  // handler reads the data-ref attribute and silently does nothing if it is missing or
  // malformed -- a failure with no symptom other than "dragging does nothing".
  const clueCards = [...document.querySelectorAll("#clues .clue")];
  out.metrics.ref_payloads = clueCards.filter(c => {
    try { return !!JSON.parse(c.dataset.ref || "null"); } catch (_) { return false; }
  }).length;
  if (clueCards.length && out.metrics.ref_payloads !== clueCards.length) {
    add("error", "线索卡缺少可拖参考载荷",
        out.metrics.ref_payloads + "/" + clueCards.length + " 有 data-ref");
  }
  // The note layer is where a reference lands; it must exist and be a valid drop area.
  if (!document.querySelector("#notes-layer")) {
    add("error", "缺少便签层", "#notes-layer 不存在");
  }
  out.metrics.notes_with_refs = [...document.querySelectorAll("#notes-layer .note")]
    .filter(n => n.querySelector(".note-refs")).length;
  out.metrics.note_ref_chips = document.querySelectorAll("#notes-layer .note-ref").length;

  // Keyboard safety: every global shortcut must require a modifier (or be a key that
  // cannot produce text). A bare letter shortcut steals characters from the note
  // editor -- the user typed a note and the app ran commands instead. This asserts the
  // handler's own rule by dispatching a printable key into a note and checking that
  // nothing navigational happened.
  const noteTopic = document.querySelector("#notes-layer .note-topic");
  if (noteTopic) {
    const before = document.querySelectorAll("#feed .utt.sel").length;
    noteTopic.focus();
    for (const ch of "jske/") {
      noteTopic.dispatchEvent(new KeyboardEvent("keydown",
        { key: ch, bubbles: true, cancelable: true }));
    }
    const after = document.querySelectorAll("#feed .utt.sel").length;
    const searchOpened = document.querySelector("#search").classList.contains("on");
    out.metrics.typing_sel_delta = after - before;
    if (after !== before) {
      add("error", "便签内打字触发了句子跳转",
          "选中数 " + before + " -> " + after);
    }
    if (searchOpened) {
      add("error", "便签内打字打开了搜索", "按 / 时不应打开搜索框");
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    }
  }

  // ── 5. contrast ──────────────────────────────────────────────────────
  // 对比度是"好不好看"里唯一可以客观验证的部分：太浅的字在会议室投影上直接看不见。
  const lum = c => {
    const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 :
      Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  };
  const parse = s => {
    const m = s.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const p = m[1].split(",").map(x => parseFloat(x));
    return { rgb: p.slice(0, 3), a: p.length > 3 ? p[3] : 1 };
  };
  const bgOf = n => {
    let e = n;
    while (e) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c && c.a > 0.9) return c.rgb;
      e = e.parentElement;
    }
    return [255, 255, 255];
  };
  const seen = new Map();
  const samples = [...document.querySelectorAll(
    ".u-text, .u-time, .c-text, .c-src, .p-name, .p-sub, .col-head h2, .stat, .fchip, .c-time, .c-conf, .ref-x, .d-label"
  )];
  for (const n of samples) {
    const cs = getComputedStyle(n);
    const fg = parse(cs.color);
    if (!fg) continue;
    const bg = bgOf(n);
    const l1 = lum(fg.rgb), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const px = parseFloat(cs.fontSize);
    const bold = parseInt(cs.fontWeight, 10) >= 600;
    const large = px >= 24 || (px >= 18.66 && bold);
    const need = large ? 3.0 : 4.5;
    const key = cs.color + "|" + Math.round(px);
    if (seen.has(key)) continue;
    seen.set(key, 1);
    if (ratio < need) {
      add(ratio < need - 1.2 ? "error" : "warn", "对比度不足",
          n.className.split(" ")[0] + " " + px.toFixed(0) + "px " +
          ratio.toFixed(2) + ":1 (需 " + need + ":1)");
    }
  }

  // ── 7. tap targets ───────────────────────────────────────────────────
  let small = 0;
  for (const n of document.querySelectorAll(".fchip, .c-act, .badge")) {
    const r = n.getBoundingClientRect();
    if (r.height > 0 && r.height < 16) { small++; }
  }
  out.metrics.small_targets = small;

  // ── geometry snapshot ────────────────────────────────────────────────
  const geo = {};
  for (const [k, n] of Object.entries(cols)) {
    if (!n) continue;
    const r = n.getBoundingClientRect();
    geo[k] = [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
  }
  out.metrics.geometry = geo;
  const first = document.querySelector("#feed .utt .u-text");
  if (first) {
    const cs = getComputedStyle(first);
    out.metrics.body_font = cs.fontSize + " / " + cs.lineHeight + " / " + cs.fontFamily.split(",")[0];
  }
  const h = document.querySelector(".u-text");
  if (h) out.metrics.line_height_px = getComputedStyle(h).lineHeight;

  // ── 6. keyboard shortcuts ────────────────────────────────────────────
  // Modifier-based on purpose: single-letter shortcuts stole characters from the note
  // editor, so the contract under test is "a modifier combo works" AND "plain typing
  // does nothing navigational". Testing the old bare keys here would have kept passing
  // while the real bug (typing in a note ran commands) went unnoticed.
  const press = async (key, opts) => {
    document.dispatchEvent(new KeyboardEvent("keydown",
      Object.assign({ key, bubbles: true, cancelable: true }, opts || {})));
    await new Promise(r => setTimeout(r, 260));
  };
  const feed = document.querySelector("#feed");
  const hasSegs = document.querySelectorAll("#feed .utt").length > 0;
  if (hasSegs) {
    const before = document.querySelectorAll("#feed .utt.sel").length;
    await press("j", { altKey: true });
    const after = document.querySelectorAll("#feed .utt.sel").length;
    out.metrics.sel_before = before;
    out.metrics.sel_after = after;
    if (after === 0) add("error", "Alt+J 没有选中发言", "快捷键无效");
  }
  await press("k", { ctrlKey: true });
  const searchOn = document.querySelector("#search").classList.contains("on");
  out.metrics.search_opens = searchOn;
  if (!searchOn) add("error", "Ctrl+K 没打开搜索", "快捷键无效");
  await press("Escape");
  if (document.querySelector("#search").classList.contains("on")) {
    add("error", "按 Esc 没关闭搜索", "");
  }

  // The regression that started this: a bare "/" inside a note must not open search.
  const noteTopic2 = document.querySelector("#notes-layer .note-topic");
  if (noteTopic2) {
    noteTopic2.focus();
    noteTopic2.dispatchEvent(new KeyboardEvent("keydown",
      { key: "/", bubbles: true, cancelable: true }));
    await new Promise(r => setTimeout(r, 120));
    if (document.querySelector("#search").classList.contains("on")) {
      add("error", "便签内按 / 打开了搜索", "输入字符被命令抢走");
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    }
  }

  const main = document.querySelector("#main");
  await press("1");
  out.metrics.left_toggled = main.className;
  if (main.className.includes("hide-left") || main.className.includes("show-left")) {
    await press("1");
  }

  // ── theme ────────────────────────────────────────────────────────────
  out.metrics.theme = document.documentElement.getAttribute("data-theme");
  const themeBtn = document.querySelector("#btn-theme");
  if (!themeBtn) {
    add("error", "缺少主题切换按钮", "#btn-theme 不存在");
  } else {
    const bgBefore = getComputedStyle(document.body).backgroundColor;
    themeBtn.click();
    await new Promise(r => setTimeout(r, 240));
    const bgAfter = getComputedStyle(document.body).backgroundColor;
    const themeAfter = document.documentElement.getAttribute("data-theme");
    out.metrics.theme_after_click = themeAfter;
    out.metrics.bg_changed = bgBefore !== bgAfter;
    if (bgBefore === bgAfter) {
      add("error", "切换主题没有改变背景色", bgBefore + " -> " + bgAfter);
    }
    if (themeAfter === out.metrics.theme) {
      add("error", "切换主题没有改变 data-theme", themeAfter);
    }
    themeBtn.click();                     // 切回去，后面的断言按原主题评估
    await new Promise(r => setTimeout(r, 240));
    if (document.documentElement.getAttribute("data-theme") !== out.metrics.theme) {
      add("error", "主题切不回原值", document.documentElement.getAttribute("data-theme"));
    }
  }

  // ── 8. drag behaviour, measured rather than assumed ──────────────────
  // The first version of drag-and-drop used HTML5 drag events and the user's verdict was
  // "拖拽都非常难". A screenshot cannot show that, so this dispatches synthetic pointer
  // events and measures whether the element actually moved.
  //
  // Nodes are re-queried immediately before use, and the drag handler reaches the live
  // node through the event target rather than through a captured variable: the page
  // rebuilds the note layer on every poll, so a reference captured earlier goes detached
  // -- and a detached node still reports geometry, which produced nonsense deltas like
  // -916 and made a harness bug look like a product bug.
  const noteNow = () => document.querySelector("#notes-layer .note");
  if (noteNow()) {
    const head = noteNow().querySelector(".note-head");
    if (head) {
      // 记下这张便签在**服务端**的原始布局状态，拖完用它精确恢复。
      // 只读的诊断工具不该改被测数据——这条是从"每跑一次审计，用户的便签布局就漂一点"
      // 里学来的。
      const preDragId = noteNow().dataset.id;
      let preDrag = { nx: null, ny: null, nw: 0, nz: 0 };
      let preDragNil = true;
      try {
        const st = await (await fetch("/api/state?since=0")).json();
        const it = (st.prep || []).find(p => p.id === preDragId);
        if (it) {
          preDrag = { nx: it.nx, ny: it.ny, nw: it.nw, nz: it.nz };
          preDragNil = it.nx == null && it.ny == null;
        }
      } catch (e) { /* 拿不到就退化成重置，见下 */ }
      out.metrics.drag_pre_pos = preDragNil ? "auto" : (preDrag.nx + "," + preDrag.ny);
      // 单次拖动：断言位移等于派发的位移。这条在多次运行里稳定。
      const hb = head.getBoundingClientRect();
      const before = noteNow().getBoundingClientRect();
      // 抓头部内侧的空白区：避开左边的 ⠿ 手柄与类型文字，也避开右边的 ✓/× 按钮。
      // 抓在按钮上时 pointerdown 被按钮吃掉，拖动会毫无反应——这正是这条断言
      // 第一次失败的原因。稳妥起见先记下头部区域，若命中的元素不是头部就跳过断言。
      const startX = hb.left + 52, startY = hb.top + hb.height / 2;
      const hitEl = document.elementFromPoint(startX, startY);
      out.metrics.drag_hit_tag = hitEl ? (hitEl.className || hitEl.tagName) : "none";
      if (hitEl && !hitEl.closest(".note-head")) {
        add("error", "拖拽抓取点没落在头部上", String(hitEl.className || hitEl.tagName));
      }
      const fire = (type, x, y, extra) => {
        const ev = new PointerEvent(type, Object.assign({
          bubbles: true, cancelable: true, clientX: x, clientY: y,
          pointerId: 1, pointerType: "mouse", button: 0, buttons: type === "pointerup" ? 0 : 1,
        }, extra || {}));
        (type === "pointerdown" ? noteNow().querySelector(".note-head") : window)
          .dispatchEvent(ev);
      };
      // 多步派发 pointermove —— 按真实鼠标的节奏。
      //
      // 这里曾经是**单步**的，而注释写明了原因："循环派发是本装置反复失败的根源…每步只
      // 累加一小段（实测 8 步只走了 512px），看起来就像'拖不动'"。也就是说：装置发现了
      // bug，把症状记了下来，然后把输入改小到断言能过。真实鼠标每秒发几十个 pointermove，
      // 于是用户必然撞上测试绕开的那条路径——"便签移动还是非常困难"。
      //
      // 真正的根因是 renderNotes 每个轮询周期都重建便签层，把拖拽抓着的元素换掉了。
      // 修好之后这里恢复成多步，并且**在过程中**量偏差：只量终点会漏掉"中途弹回起点"。
      // 完整版在 probe_note_drag.js（4 秒慢拖 + 越界夹紧 + 正文/顶部两种抓手）。
      // 便签被夹在工作区里（只保证不被推出屏幕），所以贴着边的便签往外的拖动会完全不动——
      // 那是设计行为。写死 "+120,+72" 会把"夹紧生效"报成"拖不动"，这个误报真的发生过。
      //
      // 注意用的是 moveArea()（可挪动的范围）而不是 noteArea()（自动摆放的便签道）。
      // 把两者当成同一个东西，正是"不跟手 + 范围被限制"的来源：便签道只有两百多像素的自由。
      const lane = moveArea();
      const nb0 = noteNow().getBoundingClientRect();
      const roomR = (lane.right - nb0.width) - nb0.left;
      const roomL = nb0.left - lane.left;
      const roomD = (lane.bottom - nb0.height) - nb0.top;
      const roomU = nb0.top - lane.top;
      const dxWant = (roomR >= 120 ? 120 : (roomL >= 120 ? -120 : Math.round(Math.max(roomR, roomL))));
      const dyWant = (roomD >= 72 ? 72 : (roomU >= 72 ? -72 : Math.round(Math.max(roomD, roomU))));
      out.metrics.drag_room = { left: Math.round(roomL), right: Math.round(roomR),
                                up: Math.round(roomU), down: Math.round(roomD) };
      const targetX = Math.round(startX + dxWant), targetY = Math.round(startY + dyWant);
      fire("pointerdown", startX, startY);
      let worstLag = 0;
      for (let i = 1; i <= 24; i++) {
        const x = Math.round(startX + dxWant * i / 24);
        const y = Math.round(startY + dyWant * i / 24);
        fire("pointermove", x, y, { buttons: 1 });
        await new Promise(r => setTimeout(r, 45));   // 全程约 1.1s，跨过一次轮询
        const cur = noteNow();
        if (!cur) continue;
        const rr = cur.getBoundingClientRect();
        // 期望位置同样要夹一次，否则"贴着边的便签"会被算成没跟上
        const expX = Math.min(lane.right - rr.width,
                     Math.max(lane.left, before.left + dxWant * i / 24));
        const expY = Math.min(lane.bottom - rr.height,
                     Math.max(lane.top, before.top + dyWant * i / 24));
        worstLag = Math.max(worstLag, Math.hypot(rr.left - expX, rr.top - expY));
      }
      fire("pointerup", targetX, targetY);
      await new Promise(r => setTimeout(r, 260));
      const after = noteNow().getBoundingClientRect();
      const dx = Math.round(after.left - before.left);
      const dy = Math.round(after.top - before.top);
      out.metrics.note_drag_dx = dx;
      out.metrics.note_drag_dy = dy;
      // 断言"确实跟着指针动了"，而不是"两轴都必须精确位移"。
      //
      // 便签被夹在视口内（X 保留一整张便签宽的可抓区域，Y 保留 34px），所以某一轴为 0
      // 是夹紧生效，属于设计行为：实测 dx=120 / dy=0 在三次连跑里完全一致。要求
      // "两轴都必须动"会把这个正确行为判成缺陷——这个断言因此改过一次。
      out.metrics.drag_moved_px = Math.abs(dx) + Math.abs(dy);
      out.metrics.drag_worst_lag = Math.round(worstLag);
      // 过程中的偏差才是这条 bug 的真身：终点可能因为夹紧而"看起来对了"，
      // 但中途每蹦回起点一次，用户就要重抓一次。
      if (worstLag > 24) {
        add("error", "拖动过程中便签没跟上指针",
            "最大偏差 " + Math.round(worstLag) + "px（24 步 / 约 1.1s，跨过轮询）");
      }
      if (Math.abs(dx) + Math.abs(dy) < 60) {
        add("error", "便签拖不动", "总位移 " + (Math.abs(dx) + Math.abs(dy)) +
            "px（期望 ≥60；dx=" + dx + " dy=" + dy + "）");
      } else if (Math.abs(dxWant) > 8 && dx !== 0 && Math.sign(dx) !== Math.sign(dxWant)) {
        // 比较**符号**，不比较具体数值：拖动方向现在是按可用余量挑的，可能往左也可能往右。
        // 旧版写死"指针向右移动了 120px"，于是在方向变成向左之后，正确的拖动被报成"拖反了"。
        add("error", "便签被拖反了", "指针 dx=" + dxWant + "，便签 dx=" + dx);
      }
      // 把便签放回原位。
      //
      // 这一步不是"整洁"而是必需：拖拽会调 /api/prep/layout 把新位置**持久化到会话文件**。
      // 第一版靠"反向再拖一次"来还原，那是错的，有两个后果：
      //   · 夹紧让反向拖动回不到原点，每跑一次审计便签就偏一点；
      //   · 更要命的是，"拖一下"会给一张**从没摆过**的便签写上坐标——于是一份只读的
      //     诊断工具把自动排布的便签悄悄变成了钉住的便签，用户每次跑审计布局都变。
      // 现在改成：先记下服务端的原始状态，拖完调重置接口**精确恢复**（原来没摆过的
      // 就恢复成没摆过）。只读的工具必须真的只读。
      await fetch("/api/prep/layout", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(preDragNil ? { action: "reset", ids: [preDragId] }
          : { items: [{ id: preDragId, nx: preDrag.nx, ny: preDrag.ny,
                        nw: preDrag.nw, nz: preDrag.nz }] }) });
      await new Promise(r => setTimeout(r, 320));
      // 恢复不靠"再拖一次"，所以这里直接量：回到原位（±8px）就算成功，
      // 回到自动排布的位置同样算成功（那正是它原来的样子）。
      const rb = noteNow().getBoundingClientRect();
      out.metrics.drag_restored = Math.abs(rb.left - before.left) < 8;
      out.metrics.drag_restored_by_api = true;
      out.metrics.note_left_of_mid =
        Math.round(noteNow().getBoundingClientRect().left -
                   document.querySelector("#col-mid").getBoundingClientRect().left);
      out.metrics.notes_layer_is_viewport_fixed =
        getComputedStyle(document.querySelector("#notes-layer")).position === "fixed";
      if (!out.metrics.notes_layer_is_viewport_fixed) {
        add("error", "便签层不是全页固定层",
            "position=" + getComputedStyle(document.querySelector("#notes-layer")).position +
            "，便签将无法摆到整个界面");
      }
    }
  }

  // ── 8b. 便签自己的按钮必须真的能用 ─────────────────────────────────
  // 这一节是被一个真实的报错补上的：便签上的 ✓ 和 × 里调用的 tick() 被**同名的局部
  // 变量**（那个 ✓ 按钮自己）遮蔽了，于是点下去抛 "tick is not a function"。
  // 语法检查通过、渲染审计通过、拖拽探针也通过——因为**没有任何一个测试点过便签上的按钮**。
  // 没有被点击过的控件，就是没有测试过的控件。
  const nbWait = ms => new Promise(r => setTimeout(r, ms));
  {
    const nbNote = noteNow();
    // 用稳定的类名，不要用"头部第一个 button"——头部现在有 5 个标签色点按钮，
    // 点错了会改掉便签的类别（而且是在用户的数据上）
    const nbDone = nbNote && nbNote.querySelector(".note-done");
    if (nbDone && nbNote) {
      const nbWas = nbNote.classList.contains("done");
      nbDone.click();
      await nbWait(800);
      const nbA = noteNow();
      out.metrics.note_done_toggled = nbA ? (nbA.classList.contains("done") !== nbWas) : null;
      const nbBack = nbA && nbA.querySelector(".note-done");
      if (nbBack) { nbBack.click(); await nbWait(800); }
      const nbB = noteNow();
      out.metrics.note_done_restored = nbB ? (nbB.classList.contains("done") === nbWas) : null;
      if (out.metrics.note_done_toggled === false) {
        add("error", "便签上的「已办」按钮没生效", "点了之后 done 状态没变");
      }
      if (out.metrics.note_done_restored === false) {
        add("warning", "便签「已办」状态没能还原", "审计把这张便签留在已办状态了");
      }
    } else {
      add("warning", "便签上没有可点的头部按钮", "这一节没测到");
    }

    // 删除按钮：**新建一张临时便签来删**，绝不动用户自己的便签。
    let nbTmpId = null;
    try {
      const nbRes = await fetch("/api/prep", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "add", topic: "__按钮自测__" }),
      });
      const nbMade = await nbRes.json();
      nbTmpId = (nbMade.item || {}).id || null;
      await nbWait(1500);
      let nbDel = null;
      for (const n of document.querySelectorAll("#notes-layer .note")) {
        if (n.dataset.id === nbTmpId) {
          nbDel = n.querySelector(".note-del");
        }
      }
      out.metrics.note_temp_id = nbTmpId;
      if (!nbDel) {
        add("error", "临时便签没有出现（或没有删除按钮）", String(nbTmpId));
      } else {
        nbDel.click();
        await nbWait(1500);
        const nbGone = !document.querySelector(
          '#notes-layer .note[data-id="' + nbTmpId + '"]');
        out.metrics.note_temp_deleted = nbGone;
        if (!nbGone) add("error", "便签上的删除按钮没生效", "临时便签还在");
      }
    } catch (e) {
      add("error", "便签按钮自测出错", String(e).slice(0, 120));
    } finally {
      // 兜底清理：无论上面哪一步挂了，都不能把 __按钮自测__ 留在用户的计划里
      if (nbTmpId) {
        try {
          await fetch("/api/prep", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "remove", id: nbTmpId }),
          });
        } catch (e) { /* 已经删掉了 */ }
      }
    }
  }

  // Reference cards must expose a payload for the pointer drag to pick up.
  out.metrics.ref_cards = document.querySelectorAll("[data-ref]").length;
  if (!out.metrics.ref_cards) {
    add("error", "没有可拖的参考卡", "[data-ref] 一个都没有");
  }
  // Type is conveyed by colour, not by text chips: the utterance rows should carry a
  // tone attribute and no badge spam.
  out.metrics.toned_segments = [...document.querySelectorAll("#feed .utt")]
    .filter(u => u.dataset.tone).length;
  out.metrics.utt_badges = document.querySelectorAll("#feed .u-badges .badge").length;
  out.metrics.filter_dots = document.querySelectorAll("#filters .fdot").length;

  // ── 8. overlays must overlay, and must vanish when closed ──────────────
  // 这一节是一次真实事故换来的。发言计划编辑器第一次写的时候漏了它自己的选择器，
  // 于是它没有 position:fixed、没有 display:none——就变成一个普通 div 待在文档流里，
  // 永远显示、把 main 挤成一条，整个界面看着"乱"。
  //
  // 上一版的审计器一条都没报：它只查溢出和对比度，而"一个面板挤在上方"既不溢出也不
  // 影响对比度。它能发现被要求检查的东西，仅此而已。所以这里补上结构断言。
  // 发言计划的弹窗已被用户否掉（改成条上输入框），所以不在这里
  const OVERLAYS = ["#settings", "#imp", "#guide"];
  out.metrics.overlays = {};
  for (const sel of OVERLAYS) {
    const n = document.querySelector(sel);
    if (!n) { add("error", "缺少覆盖层 " + sel, ""); continue; }
    const cs = getComputedStyle(n);
    const open = n.classList.contains("on");
    out.metrics.overlays[sel] = { position: cs.position, display: cs.display, open: open };
    if (cs.position !== "fixed") {
      add("error", sel + " 不是 fixed 覆盖层",
          "position=" + cs.position + "——它会在文档流里挤走 main");
    }
    if (!open && cs.display !== "none") {
      add("error", sel + " 关着的时候仍然占版面", "display=" + cs.display);
    }
    if (!open) {
      const b = n.getBoundingClientRect();
      if (b.width > 1 || b.height > 1) {
        add("error", sel + " 关着但仍有尺寸",
            Math.round(b.width) + "x" + Math.round(b.height));
      }
    }
  }

  // ── 9. main must keep the height it was given ────────────────────────
  // 覆盖层若在文档流里，main 会被压扁。压扁不会产生溢出，所以必须直接量高度。
  const mainEl = q("#main");
  if (mainEl) {
    const mh = mainEl.getBoundingClientRect().height;
    const head = document.querySelector("header");
    const headH = head ? head.getBoundingClientRect().height : 0;
    const avail = innerHeight - headH;
    out.metrics.main_h = Math.round(mh);
    out.metrics.main_avail = Math.round(avail);
    if (mh < avail * 0.85) {
      add("error", "main 被挤扁了",
          Math.round(mh) + "px，可用 " + Math.round(avail) + "px——有东西在文档流里占位");
    }
  }

  // ── 10. notes: on screen, not overlapping, and auto-placed tidily ─────
  // 先等一轮轮询。上面第 8 节**自己拖过便签**（那是为了验证拖拽），拖完虽然调了重置接口，
  // 但 DOM 要等下一次渲染才跟上。不等的话，第 10 节量的是一张被测试拖到边上的旧位置——
  // 测出来的"重叠/出屏"是装置自己造成的，跟界面无关。1060 那一档就因此偶发假报。
  await new Promise(r => setTimeout(r, 1500));

  // 契约在这一轮拆成了两件事，所以断言也跟着拆：
  //   · 便签**可以**被拖到发言流或侧栏上面——那是用户自己摆的，不是缺陷；
  //   · 但**自动摆放**的便签必须待在便签道里（界面一打开就整齐，不糊住发言）；
  //   · 任何一张都不能被推出屏幕；任何两张不能互相压住。
  // 旧的"便签不能压住侧栏/发言流"在拆开自动摆放与手动摆放之后就是错的了——
  // 它会拦住用户明确想要的动作。
  //
  // AUDIT_JS 是**一个**函数体，所以变量名必须全文件唯一：noteUtts / sideCols 上面用过。
  // 重名会让整块脚本以 SyntaxError 整体失效。这段注释里也不能出现反引号。
  // 判据是"肉眼看得见的重叠"，不是"面积 > N"：便签宽 232px，纵向叠 0.11px 面积就超 24px²。
  const visibleOverlap = (a, b) => {
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 2 && h > 2;
  };
  const noteEls = [...document.querySelectorAll("#notes-layer .note")];
  const laneBox = noteArea();
  const areaBox = moveArea();
  out.metrics.notes = noteEls.length;
  let offscreen = 0, noteOverlap = 0, autoOverlap = 0, autoOutside = 0;
  const noteBoxes = noteEls.map(n => n.getBoundingClientRect());
  // 便签道到底装不装得下？装不下就不该断言"不能重叠"。
  //
  // 断言必须是**精确的**，否则它会变成噪声：用户手动把几张 232 宽的便签摆在便签道里，
  // 再自动加一张时，150 宽的列已经跨不过那几张了——这时候"零重叠"在物理上做不到，
  // 正确行为是"留在屏幕上、叠得最少"，而不是报一个谁也修不好的错。
  // 有空间却不重叠，那才是缺陷。
  const laneH = laneBox.bottom - laneBox.top;
  const sumH = noteBoxes.reduce((s, r) => s + r.height + 12, 0);
  const laneCols = Math.max(1, Math.floor((laneBox.right - laneBox.left + 12) /
    (Math.min(...noteBoxes.map(r => r.width)) + 12)) || 1);
  const capacity = laneH * laneCols;
  out.metrics.lane_capacity = Math.round(capacity);
  out.metrics.lane_needed = Math.round(sumH);
  out.metrics.lane_overfull = sumH > capacity;
  // 服务端状态：nx == null 就是"从没被摆过"，也就是自动摆放的那一类
  let prepNow = [];
  try {
    const rs = await fetch("/api/state?since=0", { cache: "no-store" });
    prepNow = ((await rs.json()).prep) || [];
  } catch (e) { /* 拿不到就只做与状态无关的断言 */ }
  const byId = {};
  for (const it of prepNow) byId[it.id] = it;
  const isAutoNote = n => {
    const st = byId[n.dataset.id];
    return !st || (st.nx == null && st.ny == null);
  };
  for (let i = 0; i < noteEls.length; i++) {
    const r = noteBoxes[i];
    if (r.left < areaBox.left - 2 || r.right > areaBox.right + 2 ||
        r.top < areaBox.top - 2 || r.bottom > areaBox.bottom + 2) offscreen++;
    for (let j = i + 1; j < noteEls.length; j++) {
      if (!visibleOverlap(noteBoxes[i], noteBoxes[j])) continue;
      noteOverlap++;
      // 只要**两张都是自动摆放的**，重叠就是布局缺陷。
      // 有一张是用户自己摆的，那叠在一起是用户的意图（靠层级决定谁在上面）——
      // 便签本来就是可以叠的东西，把它判成错误会拦住用户明确想要的动作。
      if (isAutoNote(noteEls[i]) && isAutoNote(noteEls[j])) autoOverlap++;
    }
    if (isAutoNote(noteEls[i]) &&
        (r.left < laneBox.left - 2 || r.right > laneBox.right + 2)) {
      autoOutside++;
    }
  }
  out.metrics.note_overlap = noteOverlap;
  out.metrics.note_auto_overlap = autoOverlap;
  out.metrics.note_offscreen = offscreen;
  out.metrics.note_auto_outside_lane = autoOutside;
  out.metrics.note_boxes = noteEls.map((n, i) => ({
    id: n.dataset.id, box: [Math.round(noteBoxes[i].left), Math.round(noteBoxes[i].top),
                            Math.round(noteBoxes[i].width), Math.round(noteBoxes[i].height)],
    auto: !(byId[n.dataset.id] && byId[n.dataset.id].nx != null),
  }));
  out.metrics.area_box = [Math.round(areaBox.left), Math.round(areaBox.top),
                          Math.round(areaBox.right), Math.round(areaBox.bottom)];
  out.metrics.lane_box = [Math.round(laneBox.left), Math.round(laneBox.top),
                          Math.round(laneBox.right), Math.round(laneBox.bottom)];
  if (offscreen) {
    const bad = noteEls.filter((n, i) => {
      const r = noteBoxes[i];
      return r.left < areaBox.left - 2 || r.right > areaBox.right + 2 ||
             r.top < areaBox.top - 2 || r.bottom > areaBox.bottom + 2;
    }).map(n => {
      const r = n.getBoundingClientRect();
      return n.dataset.id + " [" + Math.round(r.left) + "," + Math.round(r.top) + " " +
             Math.round(r.width) + "x" + Math.round(r.height) + "]";
    });
    add("error", "有便签被推出屏幕", offscreen + " 张：" + bad.join("；") +
        "  工作区=" + JSON.stringify(out.metrics.area_box));
  }
  if (autoOverlap) {
    // 自动摆放的便签之间重叠 = 真的没摆好（多开一列就能解决）
    add("error", "自动摆放的便签互相重叠", autoOverlap + " 对重叠（便签道还有空间）");
  }
  if (noteOverlap && !autoOverlap) {
    // 有一张是用户自己摆的：这是用户的意图，记录但不判错
    add("info", "便签叠在一起（用户自己摆的）", noteOverlap + " 对重叠，层级决定谁在上面");
  }
  if (autoOutside) {
    add("error", "自动摆放的便签跑出了便签道", autoOutside + " 张——会盖住发言或侧栏");
  }

  return out;
})()`;

async function main() {
  const profile = path.join(require("os").tmpdir(), "edge-audit-" + Date.now());
  // Launch on about:blank and set the viewport through CDP rather than trusting
  // --window-size. A headless window's outer size is not its layout viewport (browser
  // chrome eats ~100px of width), so breakpoint tests driven by --window-size report
  // the wrong column widths and hide exactly the bugs they are meant to find: the
  // first run of this audit showed a 236px sidebar at a "1280px" viewport that was
  // really ~1180px.
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

  // Exact layout viewport, then navigate.
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
  // ?guide=off：审计启动的是全新 profile，靠"已看过"的标记跳不过引导，而那一层
  // 透明全屏层会吃掉键盘断言与点击热区（见 ui.html 里"使用引导"一节）。
  await cdp.send("Page.navigate", { url: URL_ + (URL_.indexOf("?") >= 0 ? "&" : "?") + "guide=off" });
  await loaded;

  // Force the requested theme before the application script runs its first poll, and
  // verify the switch actually changes the rendered colours -- 审计"暗色好不好看"没意义，
  // 但"暗色下对比度是否达标"是能算的。
  await cdp.eval(`(() => {
    try { localStorage.setItem("plaud-theme", ${JSON.stringify(THEME)}); } catch (e) {}
    document.documentElement.setAttribute("data-theme", ${JSON.stringify(THEME)});
    return document.documentElement.getAttribute("data-theme");
  })()`);
  await sleep(1200);

  // 上面带了 ?guide=off，引导不会弹。这里只确认页面里确实有这件东西：选择器改名之后
  // 审计会一片绿，而用户第一次打开什么都没有——那种"通过得毫无意义"最值得报一句。
  const guideState = await cdp.eval("({ present: !!document.querySelector('#guide') })");
  if (!guideState || !guideState.present) {
    console.error("  [警告] 页面里没有 #guide —— 使用引导的探针/审计会失效");
  }
  await sleep(300);

  // Let the page's polling loop fetch state and render.
  await sleep(3500);

  let audit;
  try {
    audit = await cdp.eval(AUDIT_JS);
  } catch (e) {
    console.error("审计脚本执行失败: " + e.message);
    const errs = cdp.events.filter(x => x.method === "Runtime.exceptionThrown");
    for (const x of errs.slice(0, 5)) {
      console.error("  页面异常: " +
        (x.params.exceptionDetails.exception?.description || "").split("\n")[0]);
    }
    // Also dump what the page actually rendered, so "the UI is broken" can be told
    // apart from "the audit could not read it". An empty body or an error page is a
    // completely different bug from a selector that moved.
    try {
      const probe = await cdp.eval(
        "({ url: location.href, title: document.title, readyState: document.readyState," +
        " bodyLen: document.body ? document.body.innerHTML.length : -1," +
        " head: document.body ? document.body.innerText.slice(0, 300) : ''," +
        " scripts: document.scripts.length," +
        " hasMain: !!document.querySelector('#main')," +
        " hasNotesLayer: !!document.querySelector('#notes-layer') })");
      console.error("  页面实际状态: " + JSON.stringify(probe, null, 2));
    } catch (e2) {
      console.error("  连页面状态都读不到: " + e2.message);
    }
    for (const x of errs.slice(0, 8)) {
      console.error("  完整异常: " +
        JSON.stringify(x.params.exceptionDetails, null, 2).slice(0, 900));
    }
    ws.close(); proc.kill();
    return 1;
  }

  // Console errors collected while the page was live.
  const consoleErrs = cdp.events
    .filter(e => e.method === "Runtime.consoleAPICalled" && e.params.type === "error")
    .map(e => e.params.args.map(a => a.value ?? a.description ?? "").join(" "));
  const exceptions = cdp.events
    .filter(e => e.method === "Runtime.exceptionThrown")
    .map(e => (e.params.exceptionDetails.exception?.description || "").split("\n")[0]);
  const netErrs = cdp.events
    .filter(e => e.method === "Log.entryAdded" && e.params.entry.level === "error")
    .map(e => e.params.entry.text);

  // 未捕获异常必须**算失败**，不能只打印。
  //
  // 这条也是用户报错换来的：便签上的按钮抛 "tick is not a function"，审计器把它印在了
  // "控制台/网络错误"一节里，但 `errors` 只统计 checks 里的条目，于是结论是
  // "0 错误"，退出码 0——一次**报出了异常却判为通过**的审计。任何页面级异常都让这次审计不通过。
  for (const e of exceptions.slice(0, 5)) {
    audit.checks.push({ level: "error", name: "页面抛出未捕获异常",
                        detail: String(e).slice(0, 200) });
  }
  for (const e of consoleErrs.slice(0, 5)) {
    audit.checks.push({ level: "error", name: "控制台报错",
                        detail: String(e).slice(0, 200) });
  }
  for (const e of netErrs.slice(0, 5)) {
    audit.checks.push({ level: "error", name: "网络请求失败",
                        detail: String(e).slice(0, 200) });
  }

  console.log("=".repeat(74));
  console.log("渲染审计  " + URL_ + "   " + WIDTH + "x" + HEIGHT);
  console.log("=".repeat(74));
  const m = audit.metrics;
  console.log("\n几何：");
  for (const [k, v] of Object.entries(m.geometry || {})) {
    console.log(`  ${k.padEnd(6)} left=${String(v[0]).padStart(5)} top=${String(v[1]).padStart(4)} ` +
      `w=${String(v[2]).padStart(5)} h=${String(v[3]).padStart(4)}`);
  }
  console.log(`  文档 scrollW=${m.doc_scrollW} clientW=${m.doc_clientW}`);
  console.log("\n内容：");
  console.log(`  发言 ${m.segments} 段 · 线索 ${m.clues} 条 · 人员 ${m.people} 人 · ` +
    `热区 ${m.keywords} 个 · 徽章 ${m.badges} 个 · 筛选 ${m.filters} 个`);
  console.log(`  正文排版: ${m.body_font}, 行高 ${m.line_height_px}`);
  console.log(`  被裁剪正文块: ${m.clipped_bodies}`);
  console.log(`  过小点击目标: ${m.small_targets}`);
  console.log("\n交互：");
  console.log(`  j 键选中: ${m.sel_before} -> ${m.sel_after}`);
  console.log(`  / 键打开搜索: ${m.search_opens}`);

  const order = { fatal: 0, error: 1, warn: 2, info: 3 };
  const bad = audit.checks.filter(c => c.level !== "info")
    .sort((a, b) => order[a.level] - order[b.level]);
  console.log("\n问题：");
  if (!bad.length) console.log("  （无）");
  for (const c of bad) console.log(`  [${c.level.toUpperCase()}] ${c.name}  ${c.detail}`);

  if (consoleErrs.length || exceptions.length || netErrs.length) {
    console.log("\n控制台/网络错误：");
    for (const e of [...exceptions, ...consoleErrs, ...netErrs].slice(0, 10)) {
      console.log("  " + String(e).slice(0, 190));
    }
  } else {
    console.log("\n控制台/网络错误：无");
  }

  const errors = bad.filter(c => c.level === "fatal" || c.level === "error").length;
  const warns = bad.filter(c => c.level === "warn").length;
  console.log(`\n结论：${errors} 错误 · ${warns} 警告` +
    (consoleErrs.length + exceptions.length ? ` · ${consoleErrs.length + exceptions.length} 控制台异常` : ""));

  const outPath = path.join(__dirname, "..", "data", "ui-shots", "audit.json");
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(
    { url: URL_, size: [WIDTH, HEIGHT], metrics: m, problems: bad,
      console: { consoleErrs, exceptions, netErrs } }, null, 2), "utf-8");
  console.log("明细写入 " + outPath);

  ws.close();
  proc.kill();
  return errors ? 1 : 0;
}

main().then(c => process.exit(c)).catch(e => {
  console.error("审计失败: " + (e && e.stack || e));
  process.exit(1);
});
