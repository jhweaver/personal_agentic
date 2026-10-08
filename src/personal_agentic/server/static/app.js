"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
};

/* ---------------- small, safe markdown ---------------- */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function inline(s) {
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  s = esc(s);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g, (_, t, u) => `<a href="${u}" target="_blank" rel="noopener">${t}</a>`);
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]*[^\s<).,;:!?'"])/g, (_, p, u) => `${p}<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
}
function md(src) {
  src = String(src ?? "").replace(/<!--[\s\S]*?-->/g, "").replace(/\r\n/g, "\n");
  const out = [];
  const lines = src.split("\n");
  let i = 0, para = [], list = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`); list = null; } };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```(\w*)/);
    if (fence) {
      flushPara(); flushList();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    if (!line.trim()) { flushPara(); flushList(); i++; continue; }
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { flushPara(); flushList(); out.push(`<h${m[1].length + 1}>${inline(m[2])}</h${m[1].length + 1}>`); i++; continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) { flushPara(); flushList(); out.push("<hr>"); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flushPara(); flushList();
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    if ((m = line.match(/^\s*>\s?(.*)$/))) { flushPara(); flushList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); i++; continue; }
    if ((m = line.match(/^ ?([-*+]|\d+[.)])\s+(.*)$/))) {
      flushPara();
      const tag = /\d/.test(m[1]) ? "ol" : "ul";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      const task = m[2].match(/^\[([ xX])\]\s+(.*)$/);
      let text = task ? inline(task[2]) : inline(m[2]);
      const sub = [];
      i++;
      // Indented lines continue this item; indented markers make a nested list.
      while (i < lines.length && /^\s{2,}\S/.test(lines[i])) {
        const sm = lines[i].match(/^\s+([-*+]|\d+[.)])\s+(.*)$/);
        if (sm) sub.push(inline(sm[2]));
        else if (sub.length) sub[sub.length - 1] += " " + inline(lines[i].trim());
        else text += " " + inline(lines[i].trim());
        i++;
      }
      const nested = sub.length ? `<ul>${sub.map((x) => `<li>${x}</li>`).join("")}</ul>` : "";
      list.items.push(task
        ? `<li class="task"><input type="checkbox" ${task[1] !== " " ? "checked" : ""} disabled aria-label="done">${text}${nested}</li>`
        : `<li>${text}${nested}</li>`);
      continue;
    }
    flushList();
    para.push(line);
    i++;
  }
  flushPara(); flushList();
  return out.join("");
}
const mdEl = (text, cls = "") => h("div", { class: `md ${cls}`, html: md(text) });

/* ---------------- helpers ---------------- */
function fmtTime(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? t : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${t}`;
}
function srcInfo(source) {
  source = source || "system";
  if (source.startsWith("routine:")) return { cls: "routine", label: source.slice(8) };
  if (source === "terminal") return { cls: "terminal", label: "terminal" };
  if (source === "web") return { cls: "web", label: "web" };
  if (source === "agent" || source === "main agent") return { cls: "agent", label: "agent" };
  if (source === "system") return { cls: "system", label: "system" };
  return { cls: "script", label: source };
}
const short = (v, n = 160) => {
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
};
const kfmt = (n) => (n == null ? "—" : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));

/* ---------------- a feed (main conversation, or a transcript) ---------------- */
class Feed {
  constructor(el, { main = false } = {}) {
    this.el = el;
    this.main = main;
    this.turn = null;
    this.last = null; // {source, kind, ts, entry}
    this.approvalEls = new Map();
  }
  clear() {
    [...this.el.querySelectorAll(".entry")].forEach((e) => e.remove());
    this.turn = null;
    this.last = null;
  }
  atBottom() { return this.el.scrollHeight - this.el.scrollTop - this.el.clientHeight < 80; }
  scroll(force) { if (force || this.stick) this.el.scrollTop = this.el.scrollHeight; }

  entry(ev, cls, ...content) {
    const s = srcInfo(ev.source);
    const cont = this.last && this.last.source === ev.source && this.last.cls === cls && ev.ts - this.last.ts < 120 && cls !== "user";
    const gutter = h("div", { class: "gutter" }, h("time", {}, fmtTime(ev.ts)), h("span", { class: `src ${s.cls}` }, s.label));
    const body = h("div", { class: "body" }, ...content);
    const e = h("div", { class: `entry ${cls}${cont ? " cont" : ""}` }, gutter, body);
    this.el.append(e);
    this.last = { source: ev.source, cls, ts: ev.ts, entry: e, body };
    return body;
  }

  ensureTurn(ev) {
    if (!this.turn) {
      const body = this.entry({ ...ev, source: "agent" }, "agent");
      this.turn = { body, live: null, liveText: "", reason: null, reasonText: "", tools: null, toolCount: 0, toolLines: new Map() };
    }
    return this.turn;
  }

  add(ev, live = true) {
    this.stick = this.atBottom();
    const d = ev.data || {};
    const fn = this["on_" + ev.kind];
    if (fn) fn.call(this, ev, d, live);
    this.scroll(false);
  }

  on_user_message(ev, d) {
    if (!d.queued) this.turn = null;
    const body = this.entry(ev, "user", h("span", { class: "msg" }, d.text));
    if (d.queued && this.main) body.append(h("span", { class: "queued" }, "queued"));
  }
  on_routine_prompt(ev, d) {
    this.turn = null;
    const det = h("details", { class: "fold" }, h("summary", {}, "Scheduled prompt"), mdEl(d.text));
    this.entry(ev, "sys", det);
  }
  on_task(ev, d) {
    this.turn = null;
    this.entry(ev, "user", h("details", { class: "fold", open: true }, h("summary", {}, "Task"), mdEl(d.text)));
  }
  on_assistant_delta(ev, d) {
    const t = this.ensureTurn(ev);
    if (!t.live) {
      t.tools = null;
      t.live = h("div", { class: "md live-cursor" });
      t.liveText = "";
      t.body.append(t.live);
    }
    t.liveText += d.text;
    t.live.textContent = t.liveText;
  }
  on_reasoning_delta(ev, d) {
    const t = this.ensureTurn(ev);
    if (!t.reason) {
      t.reasonText = "";
      const text = h("div", { class: "reasoning-text" });
      t.reason = h("details", { class: "fold" }, h("summary", {}, "Thinking…"), text);
      t.body.append(t.reason);
    }
    t.reasonText += d.text;
    t.reason.lastChild.textContent = t.reasonText;
  }
  on_reasoning(ev, d) {
    const t = this.ensureTurn(ev);
    if (!t.reason) {
      t.reason = h("details", { class: "fold" }, h("summary"), h("div", { class: "reasoning-text" }));
      t.body.insertBefore(t.reason, t.live || null);
    }
    const words = (d.text || "").split(/\s+/).filter(Boolean).length;
    t.reason.firstChild.textContent = `Thought for ${words} words`;
    t.reason.lastChild.textContent = d.text;
    t.reason = null;
  }
  on_assistant_message(ev, d) {
    const t = this.ensureTurn(ev);
    const done = mdEl(d.text);
    if (t.live) t.live.replaceWith(done);
    else t.body.append(done);
    t.live = null;
    t.tools = null;
    if (!d.interim) this.turn = null;
  }
  on_tool_call(ev, d) {
    const t = this.ensureTurn(ev);
    if (t.live) { t.live.classList.remove("live-cursor"); t.live = null; }
    if (!t.tools) {
      t.toolCount = 0;
      t.tools = h("details", { class: "fold" }, h("summary"), h("div"));
      t.body.append(t.tools);
    }
    t.toolCount++;
    t.tools.firstChild.textContent = `Used ${t.toolCount} tool${t.toolCount > 1 ? "s" : ""}: ${[...t.tools.lastChild.querySelectorAll(".name")].map((n) => n.textContent).concat(d.name).slice(-4).join(", ")}`;
    const status = h("span", { class: "st quiet" }, " running…");
    const det = h("details", {}, h("summary", { class: "small quiet" }, "arguments and result"), h("pre", {}, typeof d.args === "string" ? d.args : JSON.stringify(d.args, null, 1)));
    const line = h("div", { class: "tool-line" }, h("span", { class: "name" }, d.name), status, det);
    t.tools.lastChild.append(line);
    t.toolLines.set(d.id, { status, det });
  }
  on_tool_result(ev, d) {
    const t = this.turn;
    const line = t && t.toolLines.get(d.id);
    if (!line) return;
    line.status.textContent = d.ok === false ? " failed" : " done";
    line.status.className = d.ok === false ? "st bad" : "st quiet";
    line.det.append(h("pre", {}, short(d.text, 6000)));
    if (this.main && /^working_context_(update|write)$/.test(d.name)) app.loadWC();
  }
  on_approval_request(ev, d) {
    const body = this.entry(ev, "notice", h("strong", {}, d.title || `Approve ${d.tool}?`), h("pre", {}, short(d.args, 1200)));
    const actions = approvalActions(d.attention_id);
    body.append(actions);
    this.approvalEls.set(d.attention_id, actions);
  }
  on_attention_resolved(ev, d) {
    const el = this.approvalEls.get(d.attention_id);
    if (el) el.replaceWith(h("div", { class: "quiet small" }, `${d.status}${d.note ? `: ${d.note}` : ""}`));
  }
  on_review_request(ev, d) {
    this.entry(ev, "notice", h("strong", {}, d.title), h("div", { class: "quiet small" }, "Reply below to answer."));
  }
  on_notification(ev, d) {
    this.entry(ev, "notice", h("strong", {}, d.title || "Notice"), mdEl(d.text));
  }
  on_note(ev, d) {
    const text = String(d.text || "").replace(/^\[[^\]]*\]\n?/, "");
    const head = (String(d.text).match(/^\[([^\]]*)\]/) || [])[1];
    this.entry(ev, "sys", head ? h("div", { class: "small quiet" }, head) : null, mdEl(text));
  }
  on_script_output(ev, d) {
    const L = this.last;
    let pre;
    if (L && L.cls === "out" && L.source === ev.source && ev.ts - L.ts < 90) {
      pre = L.body.querySelector("pre");
      L.ts = ev.ts;
    } else {
      pre = h("pre");
      this.entry(ev, "out", pre);
    }
    const lines = String(d.text || "").split("\n");
    for (const line of lines) {
      const cls = d.stream === "stderr" || d.level === "error" ? "stderr" : line.startsWith("$ ") ? "cmd" : "";
      pre.append(h("span", { class: cls }, line + "\n"));
    }
    while (pre.childNodes.length > 2000) pre.firstChild.remove();
    pre.scrollTop = pre.scrollHeight;
  }
  on_routine_started(ev, d) { this.entry(ev, "sys", `Started (${d.trigger}).`); }
  on_routine_finished(ev, d) {
    if (d.status === "error") this.entry(ev, "err", `Failed: ${short(d.text, 600)}`);
    else this.entry(ev, "sys", d.quiet ? "Finished: nothing new." : "Finished.");
  }
  on_routine_skipped(ev, d) { this.entry(ev, "sys", `Skipped: ${d.reason}`); }
  on_subagent_started(ev, d) {
    const open = h("button", { class: "ghost small", onclick: () => app.openTranscript(d.id, d.title) }, "Open");
    this.entry(ev, "sys", `Sub-agent started: ${d.title}${d.background ? " (background)" : ""} `, open);
  }
  on_subagent_finished(ev, d) {
    const open = h("button", { class: "ghost small", onclick: () => app.openTranscript(d.id, d.title) }, "Open");
    this.entry(ev, d.status === "error" ? "err" : "sys", `Sub-agent ${d.status}: ${d.title} `, open);
  }
  on_system_message(ev, d) { this.entry(ev, "sys", mdEl(d.text)); }
  on_error(ev, d) { this.entry(ev, "err", d.text); }
  on_compaction(ev, d) {
    this.entry(ev, "sys", `Older conversation summarized to save space (~${kfmt(d.before_tokens)} → ~${kfmt(d.after_tokens)} tokens). The originals are still searchable.`);
  }
  on_escalated(ev, d) { this.entry(ev, "sys", `Reminder sent by ${d.via}: ${d.title}`); }
  on_agent_started(ev, d) { this.turn = null; this.entry(ev, "sys", `Agent started with ${d.tools} tools and ${d.routines} routines/tasks.`); }
  on_mcp_status(ev, d) { if (d.status === "error") this.entry(ev, "err", `MCP server ${d.server}: ${d.error}`); }
}

function approvalActions(id) {
  const note = h("input", { type: "text", placeholder: "Note (optional)", "aria-label": "Note" });
  const send = (approved) => app.send({ type: "resolve", id, approved, note: note.value });
  return h("div", { class: "actions" },
    h("button", { class: "approve", type: "button", onclick: () => send(true) }, "Approve"),
    h("button", { class: "deny", type: "button", onclick: () => send(false) }, "Deny"),
    note);
}

/* ---------------- the app ---------------- */
const app = {
  ws: null,
  status: null,
  feed: new Feed($("#feed"), { main: true }),
  tr: new Feed($("#tr-feed")),
  trSession: null,
  events: [],
  lastId: 0,
  seen: new Set(),
  buffer: null,
  waiting: new Map(),
  editingWC: false,
  retry: 0,

  async init() {
    this.bindUI();
    await this.loadHistory();
    this.connect();
    this.loadWC();
    this.loadActivity();
    this.loadRoutines();
    this.loadWaiting();
    setInterval(() => this.refreshStatus(), 30000);
  },

  async api(path, opts = {}) {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  },

  async loadHistory(before) {
    const q = new URLSearchParams({ session: "main", limit: "200" });
    if (before) q.set("before", before);
    const evs = await this.api(`/api/events?${q}`);
    for (const ev of evs) this.seen.add(ev.id);
    this.events = before ? evs.concat(this.events) : evs;
    if (!before && evs.length) this.lastId = evs[evs.length - 1].id;
    this.feed.clear();
    for (const ev of this.events) this.feed.add(ev, false);
    $("#earlier").hidden = evs.length < 200;
    if (!before) this.feed.scroll(true);
  },

  connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?client=web`);
    this.ws = ws;
    ws.onopen = () => { this.retry = 0; };
    ws.onmessage = (m) => this.onMessage(JSON.parse(m.data));
    ws.onclose = () => {
      this.setState("off", "disconnected");
      setTimeout(() => this.connect(), Math.min(10000, 1000 * 2 ** this.retry++));
    };
  },

  send(msg) {
    if (this.ws && this.ws.readyState === 1) { this.ws.send(JSON.stringify(msg)); return true; }
    return false;
  },

  async onMessage(msg) {
    if (msg.type === "hello") {
      const gapFrom = this.lastId;
      this.applyStatus(msg.status);
      if (gapFrom) {
        // Catch up on what happened while disconnected; hold live events until then.
        this.buffer = [];
        try {
          const missed = await this.api(`/api/events?session=main&after=${gapFrom}&limit=1000`);
          for (const ev of missed) this.handle(ev);
        } finally {
          const held = this.buffer;
          this.buffer = null;
          for (const ev of held) this.handle(ev);
        }
        this.loadWaiting();
        this.loadActivity();
      }
      return;
    }
    if (msg.type === "event") {
      if (this.buffer) this.buffer.push(msg.event);
      else this.handle(msg.event);
    }
    if (msg.type === "error") this.feed.add({ kind: "error", ts: Date.now() / 1000, source: "system", data: { text: msg.text } });
  },

  handle(ev) {
    if (ev.id) {
      if (this.seen.has(ev.id)) return;
      this.seen.add(ev.id);
      if (ev.session === "main") this.lastId = Math.max(this.lastId, ev.id);
    }
    const k = ev.kind, d = ev.data || {};
    if (this.trSession && ev.session === this.trSession) this.tr.add(ev);
    const mainVisible = ev.session === "main" || ["approval_request", "attention_resolved", "subagent_started", "subagent_finished"].includes(k);
    if (mainVisible && !(k === "approval_request" && ev.session !== "main" && this.feed.approvalEls.has(d.attention_id))) {
      if (ev.id && ev.session === "main") this.events.push(ev);
      this.feed.add(ev);
    }
    if (k === "status" && ev.session === "main") this.onStatusEvent(d);
    if (["approval_request", "review_request", "attention_resolved"].includes(k)) this.loadWaiting();
    if (["subagent_started", "subagent_finished", "routine_finished"].includes(k)) this.loadActivity();
    if (["routine_started", "routine_finished", "routines_reloaded"].includes(k)) this.loadRoutines();
    if (k === "working_context_changed") this.loadWC();
    if (k === "mcp_status" || k === "agent_started") this.refreshStatus();
  },

  onStatusEvent(d) {
    if (d.state === "busy" || d.state === "generating") this.setState("busy", d.state === "busy" ? "working" : d.thinking ? "thinking" : "writing");
    else if (d.state === "compacting") this.setState("busy", "summarizing older conversation");
    else if (d.state === "idle") this.setState("idle", d.queue ? `idle, ${d.queue} queued` : "idle");
    else if (d.state === "usage" && this.status) { this.status.last_prompt_tokens = d.prompt_tokens; this.renderMeter(d.prompt_tokens); }
  },

  setState(cls, text) {
    const s = $("#state");
    s.className = `state ${cls}`;
    $("#state-text").textContent = text;
    $("#stop").hidden = cls !== "busy";
  },

  applyStatus(st) {
    this.status = st;
    const name = st.user_name ? `${st.user_name}'s agent` : "Agent";
    $("#agent-name").textContent = name;
    document.title = this.waiting.size ? `(${this.waiting.size}) ${name}` : name;
    $("#model").textContent = st.model;
    $("#model").title = st.model;
    this.setState(st.busy ? "busy" : "idle", st.busy ? "working" : "idle");
    this.renderMeter(st.context_tokens);
    this.renderSystem(st);
  },

  async refreshStatus() {
    try { this.applyStatus(await this.api("/api/status")); } catch { /* offline */ }
  },

  renderMeter(tokens) {
    const st = this.status;
    if (!st) return;
    const win = st.context_window || 1;
    tokens = tokens || st.context_tokens || 0;
    $("#meter-fill").style.width = `${Math.min(100, (100 * tokens) / win)}%`;
    $("#meter-mark").style.left = `${(100 * st.compact_at) / win}%`;
    $("#meter-text").textContent = `${kfmt(tokens)} of ${kfmt(win)} used, summarizes at ${kfmt(st.compact_at)}`;
  },

  renderSystem(st) {
    const dl = $("#sys");
    dl.replaceChildren();
    const row = (k, v, cls) => dl.append(h("dt", {}, k), h("dd", { class: cls }, v));
    row("Model", st.model);
    row("Context", `${(st.context_window || 0).toLocaleString()} tokens; summarizes at ${st.compact_at.toLocaleString()}`);
    row("Thinking", st.thinking ? "On for chat (/fast turns it off)" : "Off for chat (/think turns it on)");
    row("Tools", String(st.tools));
    if (!st.mcp.length) row("MCP", "No servers configured. Add them to mcp.json.");
    for (const m of st.mcp) row(m.name, m.status === "connected" ? `connected, ${m.tools} tools` : `${m.status}: ${m.error || ""}`, m.status === "connected" ? "" : "err");
    row("Up for", `${Math.round(st.uptime_s / 60)} min`);
    dl.append(h("dt"), h("dd", {},
      h("button", { class: "ghost small", onclick: async (e) => { e.target.disabled = true; await this.api("/api/mcp/reload", { method: "POST" }).catch(() => {}); e.target.disabled = false; this.refreshStatus(); } }, "Restart MCP servers"), " ",
      h("button", { class: "ghost small", onclick: async () => { await this.api("/api/routines/reload", { method: "POST" }); this.loadRoutines(); } }, "Reload routines")));
  },

  async loadWaiting() {
    const items = await this.api("/api/attention").catch(() => []);
    this.waiting = new Map(items.map((i) => [i.id, i]));
    const box = $("#waiting");
    box.replaceChildren();
    const sec = $("#waiting-section");
    sec.classList.toggle("has", items.length > 0);
    sec.querySelector("h2").dataset.count = items.length || "";
    $("#m-count").textContent = items.length || "";
    if (!items.length) box.append(h("p", { class: "quiet" }, "Nothing right now."));
    for (const it of items) {
      const when = fmtTime(it.ts);
      if (it.kind === "approval") {
        box.append(h("div", { class: "wait-item" },
          h("h3", {}, it.title),
          h("div", { class: "meta" }, `Asked ${when}`),
          h("pre", {}, it.body),
          approvalActions(it.id)));
      } else {
        box.append(h("div", { class: "wait-item" },
          h("h3", {}, it.title),
          h("div", { class: "meta" }, `Since ${when}. Reply in the conversation to answer.`),
          h("div", { class: "md body-md", html: md(it.body) }),
          h("div", { class: "actions" }, h("button", { class: "deny", type: "button", onclick: () => this.send({ type: "resolve", id: it.id, approved: true, note: "dismissed" }) }, "Dismiss"))));
      }
    }
    if (this.status) document.title = items.length ? `(${items.length}) ${$("#agent-name").textContent}` : $("#agent-name").textContent;
  },

  async loadWC() {
    if (this.editingWC) return;
    const wc = await this.api("/api/working-context").catch(() => null);
    if (!wc) return;
    this.wcContent = wc.content;
    $("#wc-path").textContent = wc.path;
    $("#wc-path").title = wc.path;
    $("#wc-view").innerHTML = md(wc.content);
  },

  async loadActivity() {
    const rows = await this.api("/api/sessions?limit=60").catch(() => []);
    const ul = $("#activity");
    ul.replaceChildren();
    if (!rows.length) ul.append(h("li", { class: "quiet small" }, "Nothing yet."));
    for (const r of rows) {
      const pill = h("span", { class: `pill ${r.status}` }, r.status);
      const sub = [r.routine ? `routine ${r.routine}` : "sub-agent", fmtTime(r.created)].join(", ");
      ul.append(h("li", {}, h("button", { type: "button", onclick: () => this.openTranscript(r.id, r.title) },
        h("span", { class: "t" }, r.title || r.id), pill, h("span", { class: "s" }, sub))));
    }
  },

  async loadRoutines() {
    const rows = await this.api("/api/routines").catch(() => []);
    const ul = $("#routines");
    ul.replaceChildren();
    if (!rows.length) ul.append(h("li", { class: "quiet small" }, "No routines or tasks yet. Add Markdown files to routines/ or tasks/."));
    for (const r of rows) {
      const next = r.next_run ? `next ${fmtTime(Date.parse(r.next_run) / 1000)}` : r.kind === "task" ? "runs when asked" : "not scheduled";
      const last = r.last_run ? `, last ${r.last_run.status} ${fmtTime(r.last_run.started)}` : "";
      const run = h("button", { class: "ghost small", type: "button", disabled: r.running, onclick: async (e) => {
        e.target.disabled = true;
        await this.api(`/api/routines/${encodeURIComponent(r.name)}/run`, { method: "POST", body: "{}" }).catch((err) => alert(err));
      } }, r.running ? "Running" : "Run now");
      ul.append(h("li", {},
        h("div", { class: "head" }, h("span", { class: "name" }, r.name, r.enabled ? null : h("span", { class: "off" }, " (off)")), run),
        r.description ? h("div", { class: "desc" }, r.description) : null,
        h("div", { class: "when" }, `${r.kind === "task" ? "Task" : r.schedule || "No schedule"}; ${next}${last}`)));
    }
  },

  async openTranscript(id, title) {
    this.trSession = id;
    this.tr.clear();
    $("#tr-title").textContent = title || id;
    const dlg = $("#transcript");
    if (!dlg.open) dlg.showModal();
    const evs = await this.api(`/api/events?session=${encodeURIComponent(id)}&limit=2000`);
    for (const ev of evs) this.tr.add(ev, false);
    this.tr.scroll(true);
    const sess = (await this.api("/api/sessions?limit=200").catch(() => [])).find((s) => s.id === id);
    $("#tr-cancel").hidden = !(sess && sess.status === "running" && sess.kind === "subagent");
  },

  bindUI() {
    const input = $("#input");
    const grow = () => { input.style.height = "auto"; input.style.height = `${Math.min(220, input.scrollHeight)}px`; };
    input.addEventListener("input", grow);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
    });
    $("#composer").addEventListener("submit", (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      const think = $("#think").checked;
      if (this.send({ type: "message", text, source: "web", thinking: think ? true : null })) {
        input.value = "";
        $("#think").checked = false;
        grow();
      }
    });
    $("#stop").addEventListener("click", () => this.send({ type: "stop" }));
    $("#earlier").addEventListener("click", () => this.events.length && this.loadHistory(this.events[0].id));

    document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
      document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
      document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("on", p.dataset.paneTab === b.dataset.tab));
      if (b.dataset.tab === "system") this.refreshStatus();
    }));
    const showPane = (view) => {
      document.querySelectorAll(".mobile-tabs button").forEach((x) => x.classList.toggle("on", x.dataset.view === view));
      document.querySelectorAll(".layout > [data-pane]").forEach((p) => p.classList.toggle("shown", p.dataset.pane === view));
    };
    document.querySelectorAll(".mobile-tabs button").forEach((b) => b.addEventListener("click", () => showPane(b.dataset.view)));
    showPane("feed");

    $("#wc-edit").addEventListener("click", () => {
      this.editingWC = true;
      $("#wc-text").value = this.wcContent || "";
      $("#wc-view").hidden = true;
      $("#wc-form").hidden = false;
      $("#wc-edit").hidden = true;
      $("#wc-text").focus();
    });
    const closeWC = () => { this.editingWC = false; $("#wc-view").hidden = false; $("#wc-form").hidden = true; $("#wc-edit").hidden = false; this.loadWC(); };
    $("#wc-cancel").addEventListener("click", closeWC);
    $("#wc-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      await this.api("/api/working-context", { method: "PUT", body: JSON.stringify({ content: $("#wc-text").value }) });
      closeWC();
    });

    $("#tr-close").addEventListener("click", () => $("#transcript").close());
    $("#transcript").addEventListener("close", () => { this.trSession = null; });
    $("#tr-cancel").addEventListener("click", () => { this.send({ type: "cancel", id: this.trSession }); $("#tr-cancel").hidden = true; });
  },
};

app.init();
