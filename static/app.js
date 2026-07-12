// claude-hub front-end — "Reading Room" (Concept 1A).
// Three zones: a grouped SESSION RAIL (left) · an inline FOCUSED THREAD that
// reads like correspondence (center) · an ADVISOR terminal docked right, with
// the situation strip + the Bureau-stamp approval rider.
//
// The backend (routes + SSE event protocol) is untouched. This file re-binds the
// existing logic to the new layout AND fixes the dark advisor: it now streams via
// POST /advisor/send + EventSource('/advisor/stream') (the old one-shot POST
// /advisor 404'd — that route was removed).

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// S7: the queued-relay count badge on a target tile (Rail + Map). A relay into a busy
// target is queued, not 409'd; this shows the count until the target finishes and the
// relay re-surfaces as a confirm chip.
function queueBadge(n) {
  const b = el("span", "queue-badge", "⏳ " + n);
  b.title = `${n} relay${n > 1 ? "s" : ""} queued — re-surfaces as a confirm chip when this session finishes`;
  return b;
}

let HUB_DEFAULT_CWD = "";
let briefMap = {}; // panel_id -> secretary brief line (situation strip + rail subtitles)
let suggestMap = {}; // panel_id -> {id, name}  secretary name proposal (S4; user accepts)
let frameSuggests = []; // S5: secretary frame proposals [{id,label,panels}] (user accepts -> a frame)
let relaySuggests = []; // S1: secretary relay proposals [{id,from,to,type,reason}] (user accepts -> /route)
let inlineEditing = false; // S4: a rename input is open — pause re-renders so it isn't clobbered

// RAIL-TYPE: typewriter reveal tuning. Live assistant deltas paint out one char at
// a time on a rAF loop instead of dumping each SSE burst (the laggy clump effect).
// REVEAL_BASE = chars/frame floor (≈ a brisk teletype on a slow trickle); the loop
// ADDS backlog/REVEAL_CATCHUP on top, so a big burst or the final message drains
// fast and the cursor never visibly trails the real content.
const REVEAL_BASE = 2;     // chars per frame at minimum (~120 cps @60fps)
const REVEAL_CATCHUP = 5;  // + ceil(unpainted_backlog / this) per frame

// --- Map view (4A) helpers: SVG edge factory + relation-type palette ---------
const SVGNS = "http://www.w3.org/2000/svg";
const svgEl = (tag) => document.createElementNS(SVGNS, tag);
const REL_COLOR = { FEEDS: "var(--feed)", REVIEWS: "var(--review)", DEPENDS: "var(--amber)", CONFLICT: "var(--red)" };
const relColor = (t) => REL_COLOR[(t || "").toUpperCase()] || "var(--feed)";
// state cls -> the CSS color var for a tile's left border.
const STATE_VAR = { run: "--green", need: "--amber", done: "--stab", idle: "--paper-mute", err: "--red" };

// 5-tier session state — each is GLYPH + WORD + COLOR (never colour-alone).
const STATE = {
  empty: { cls: "idle", glyph: "◌", word: "idle" }, // ◌
  idle: { cls: "idle", glyph: "◌", word: "idle" },
  running: { cls: "run", glyph: "▶", word: "run" }, // ▶
  done: { cls: "done", glyph: "✓", word: "done" }, // ✓
  "awaiting-input": { cls: "need", glyph: "◧", word: "needs you" }, // ◧
  "awaiting-approval": { cls: "need", glyph: "◧", word: "needs you" },
  error: { cls: "err", glyph: "✕", word: "error" }, // ✕
};
const sinfo = (state) => STATE[state] || STATE.idle;
const isLive = (state) => state === "running" || sinfo(state).cls === "need";

// workstream = the token before the first separator in the session name. Only a
// prefix SHARED by >=2 sessions earns its own group; loners fall to "sessions".
function prefixOf(name) {
  const m = (name || "").trim().toLowerCase().match(/^([a-z0-9]+)[-_/ ]/);
  return m ? m[1] : "sessions";
}
function groupSessions(panels) {
  const byPrefix = {};
  for (const s of panels) (byPrefix[prefixOf(s.name)] ||= []).push(s);
  const groups = {};
  for (const [p, arr] of Object.entries(byPrefix)) {
    if (p !== "sessions" && arr.length >= 2) groups[p] = arr;
    else (groups.sessions ||= []).push(...arr);
  }
  return groups;
}
const groupOf = (name) => {
  const p = prefixOf(name);
  return p === "sessions" ? "session" : p;
};

// ============================================================= FOCUSED THREAD
// The center reading column. Reuses the original FocusView.onEvent render logic
// (delta/message/tool_use/result/approval/routed) — retargeted from a modal
// overlay to the always-present inline thread. Approvals render as the rider in
// the right dock.
class FocusView {
  constructor() {
    this.empty = $("focus-empty");
    this.head = $("focus-head");
    this.dot = $("focus-dot");
    this.group = $("focus-group");
    this.nameEl = $("focus-name");
    this.stateEl = $("focus-state");
    this.metaEl = $("focus-meta");
    this.thread = $("thread");
    this.launch = $("launch-form");
    this.reply = $("reply-form");
    this.replyInput = $("reply-input");
    this.replySend = $("reply-send");
    this.routeBtn = $("route-btn");
    this.routeBar = $("route-bar");
    this.routeTarget = $("route-target");
    this.routePreview = $("route-preview"); // S6: verbatim preview of what the target receives
    this.routeSection = $("route-section"); // S6: opt-in human-authored framing (empty by default)
    this.rider = $("approval-rider"); // lives in the dock
    // S3 context fuel-gauge (focus header)
    this.ctxGauge = $("ctx-gauge");
    this.ctxFill = $("ctx-fill");
    this.ctxCompact = $("ctx-compact");
    this.ctxLabel = $("ctx-label");
    // Slice 7: fork-with-handoff affordance (revealed as ctx nears the auto-compact line)
    this.forkBtn = $("fork-btn");
    this.forkBtn.addEventListener("click", () => this.onFork());

    this.id = null;
    this.es = null;
    this.live = null; // the in-progress assistant message element
    this._reveal = null; // RAIL-TYPE: active typewriter-reveal controller (see _reveal* below)
    this.session = null;
    this._lastMsg = ""; // S6: full text of this panel's last complete assistant message (route preview)

    this.reply.addEventListener("submit", (e) => this.onReply(e));
    this.launch.addEventListener("submit", (e) => this.onLaunch(e));
    $("launch-cancel").addEventListener("click", () => this.show("empty"));
    this.routeBtn.addEventListener("click", () => this.toggleRoute(true));
    $("route-cancel").addEventListener("click", () => this.toggleRoute(false));
    $("route-send").addEventListener("click", () => this.onRoute());
  }

  // focus a launched session inline
  open(panelId, session) {
    if (this.id === panelId && this.es) {
      this.session = session;
      this.applyHeader(session);
      this.setCtxFromSession(session);
      return; // already streaming this one — just refresh the header
    }
    this.disconnect();
    this._revealStop(); // RAIL-TYPE: abandon any reveal loop from the previous session
    this.id = panelId;
    this.session = session;
    this.live = null;
    this.thread.innerHTML = "";
    this.rider.innerHTML = "";
    this.show("thread");
    this.applyHeader(session);
    this.setCtxFromSession(session); // paint last known ctx% now; SSE replays the live one
    this.connect();
    sessionRail.markActive(panelId);
  }

  // show the launch form for a fresh panel (+ NEW)
  openLaunch(panelId) {
    this.disconnect();
    this.id = panelId;
    this.session = null;
    this.rider.innerHTML = "";
    this.show("launch");
    $("launch-name").value = "";
    $("launch-cwd").value = HUB_DEFAULT_CWD;
    $("launch-prompt").value = "";
    $("launch-model").value = ""; // reset to account default each time the form opens
    $("launch-name").focus();
    sessionRail.markActive(null);
  }

  // mode: "empty" | "launch" | "thread"
  show(mode) {
    if (mode === "empty") this.id = null;
    this.empty.classList.toggle("hidden", mode !== "empty");
    this.launch.classList.toggle("hidden", mode !== "launch");
    this.head.classList.toggle("hidden", mode !== "thread");
    this.thread.classList.toggle("hidden", mode !== "thread");
    this.reply.classList.toggle("hidden", mode !== "thread");
    if (mode !== "thread") this.toggleRoute(false);
    if (mode !== "thread") sessionRail.markActive(null);
    if (mode !== "thread") this._hideCtx();
  }

  applyHeader(session) {
    const name = (session && session.name) || `session ${this.id}`;
    const state = (session && session.state) || "idle";
    this.nameEl.textContent = name;
    this.group.textContent = groupOf(name);
    this.metaEl.textContent = session && session.session_id ? session.session_id.slice(0, 8) : "";
    this.setStateTag(state);
  }

  setStateTag(state) {
    const si = sinfo(state);
    this.dot.className = "dot bg-" + si.cls + (isLive(state) ? " live" : "");
    this.stateEl.className = "tag st-" + si.cls;
    this.stateEl.textContent = si.glyph + " " + si.word;
    const busy = state === "running" || state === "awaiting-approval";
    const canReply = !!this.id && state !== "empty" && !busy;
    this.replyInput.disabled = !canReply;
    this.replySend.disabled = !canReply;
  }

  // ---- S3 context fuel-gauge (focus header) ----
  setCtx(ev) {
    if (!ev || ev.pct == null) { this._hideCtx(); return; }
    this._paintCtx(ev.pct, ev.compact_pct);
  }
  setCtxFromSession(s) {
    if (s && s.ctx_pct != null) this._paintCtx(s.ctx_pct, s.ctx_compact_pct);
    else this._hideCtx();
  }
  _paintCtx(pct, compactPct) {
    if (!this.ctxGauge) return;
    const compact = compactPct != null ? compactPct : 0.92;
    this.ctxGauge.classList.remove("hidden");
    this.ctxFill.style.width = Math.min(100, pct * 100) + "%";
    this.ctxFill.className = "ctx-fill " + ctxHeat(pct, compact);
    this.ctxCompact.style.left = Math.min(100, compact * 100) + "%";
    this.ctxLabel.textContent = Math.round(pct * 100) + "% ctx";
    this.ctxGauge.title = `context ${Math.round(pct * 100)}% — auto-compact near ${Math.round(compact * 100)}%`;
    // Slice 7: surface the fork affordance as context approaches the auto-compact line
    if (this.forkBtn) this.forkBtn.classList.toggle("hidden", !(pct >= compact * 0.85));
  }
  _hideCtx() { if (this.ctxGauge) this.ctxGauge.classList.add("hidden"); if (this.forkBtn) this.forkBtn.classList.add("hidden"); }

  async onFork() {
    if (!this.id) return;
    this.forkBtn.disabled = true;
    try {
      const r = await fetch(`/fork/${this.id}`, { method: "POST" });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        this.addMsg("error", "Error", j.error || `fork failed (${r.status})`);
      } else {
        this.addMsg("relay", "Fork", "↪ writing a handoff summary, then continuing in a fresh session on this tile…");
      }
    } catch { this.addMsg("error", "Error", "hub unreachable"); }
    setTimeout(() => { this.forkBtn.disabled = false; }, 4000);
    // the summary turn + new session stream over the SAME /stream/<panel> channel (same tile)
  }

  connect() {
    if (this.es) this.es.close();
    this.es = new EventSource(`/stream/${this.id}`);
    this.es.onmessage = (e) => this.onEvent(JSON.parse(e.data));
  }
  disconnect() {
    if (this.es) { this.es.close(); this.es = null; }
  }
  scroll() { this.thread.scrollTop = this.thread.scrollHeight; }

  addMsg(roleClass, label, text) {
    const m = el("div", "msg " + roleClass);
    // assistant turns pass no label — the per-turn "Claude · model" eyebrow was noise;
    // the message body alone reads cleanly. Other roles (You/Tool/Relay/Error) keep theirs.
    if (label) m.appendChild(el("div", "msg-role eyebrow", label));
    m.appendChild(el("div", "msg-text", text));
    this.thread.appendChild(m);
    this.scroll();
    return m;
  }

  // ---- RAIL-TYPE: typewriter reveal -----------------------------------------
  // Live assistant text arrives in irregular SSE `delta` bursts. Instead of dumping
  // each burst into the DOM (the laggy clump effect), we accumulate it in `full` and
  // paint `full.slice(0, shown)` one step at a time on a rAF loop — the 90s
  // secure-terminal teletype look, decoupled from network jitter. The step is
  // adaptive (REVEAL_BASE + backlog/REVEAL_CATCHUP) so a big burst or the finalized
  // message drains briskly and the cursor never visibly trails the real content.
  _revealPush(text) {
    if (!this.live) {
      this.live = this.addMsg("assistant", "", "");
      this.live.classList.add("streaming");
      this._reveal = { full: "", shown: 0, raf: 0, done: false, el: this.live.querySelector(".msg-text") };
    }
    if (!this._reveal) return; // defensive — should not happen with a live element
    this._reveal.full += text || "";
    this._revealTick();
  }

  // The final, authoritative message text. Keep typing out the remaining tail, then
  // settle: drop the cursor and release the live element. No snap — the tail still
  // reveals, just fast (its backlog is small or drains via the catch-up term).
  _revealFinalize(text) {
    if (!this.live) { this.addMsg("assistant", "", text || ""); return; }
    const r = this._reveal;
    r.full = text || "";
    r.done = true;
    if (r.shown > r.full.length) r.shown = r.full.length; // final shorter than streamed — clamp
    this._revealTick();
  }

  _revealTick() {
    const r = this._reveal;
    if (!r || r.raf) return; // loop already running
    const step = () => {
      r.raf = 0;
      if (this._reveal !== r || !r.el) return; // superseded/torn down
      const backlog = r.full.length - r.shown;
      if (backlog > 0) {
        r.shown = Math.min(r.full.length, r.shown + Math.max(REVEAL_BASE, Math.ceil(backlog / REVEAL_CATCHUP)));
        r.el.textContent = r.full.slice(0, r.shown);
        this.scroll();
      }
      if (r.shown < r.full.length) {
        r.raf = requestAnimationFrame(step); // still painting
      } else if (r.done) {
        this._revealFinish(); // fully drained AND finalized — settle
      }
      // else: caught up but not finalized — stop and wait for the next delta to re-tick
    };
    r.raf = requestAnimationFrame(step);
  }

  _revealFinish() {
    const r = this._reveal;
    if (!r) return;
    if (r.raf) cancelAnimationFrame(r.raf);
    if (r.el) r.el.textContent = r.full; // guarantee exact final text
    if (this.live) this.live.classList.remove("streaming");
    this.live = null;
    this._reveal = null;
  }

  _revealStop() {
    if (this._reveal && this._reveal.raf) cancelAnimationFrame(this._reveal.raf);
    this._reveal = null;
  }

  onEvent(ev) {
    switch (ev.kind) {
      case "cleared": this._revealStop(); this.thread.innerHTML = ""; this.live = null; this._lastMsg = ""; break;
      case "deleted": this._revealStop(); this.disconnect(); this.id = null; this.session = null; this.show("empty"); break;
      case "state": this.setStateTag(ev.state); break;
      case "ctx": this.setCtx(ev); break;
      case "user": this.addMsg("you", "You", ev.text); break;
      case "delta":
        // RAIL-TYPE: buffer the burst; the rAF loop paints it out as a typewriter
        this._revealPush(ev.text);
        break;
      case "message":
        // RAIL-TYPE: set the authoritative final text, then let the reveal drain to it
        this._revealFinalize(ev.text);
        this._lastMsg = ev.text || ""; // S6: remember the full last reply for the route preview
        break;
      case "tool_use": this.addMsg("tool", "Tool", `${ev.name} ${ev.preview || ""}`.trim()); break;
      case "result": {
        const bits = [];
        if (ev.cost != null) bits.push(`$${Number(ev.cost).toFixed(4)}`);
        if (ev.duration_ms != null) bits.push(`${ev.duration_ms}ms`);
        if (ev.is_error) bits.push("ERROR");
        this.metaEl.textContent = bits.join(" · ");
        break;
      }
      case "system":
        if (ev.subtype === "session_captured" && ev.session_id) this.metaEl.textContent = ev.session_id.slice(0, 8);
        break;
      case "approval_request": this.addApproval(ev); break;
      case "approval_resolved": this.resolveApproval(ev.id, ev.behavior); break;
      case "routed": this.addMsg("relay", "Relay", `→ routed ${ev.chars} chars to ${ev.to_name} · ${ev.to}${ev.augmented ? " · with your framing" : ""}`); break;
      // S6: provenance lands on the TARGET feed as [from <source>] — UI-only; the delivered
      // body itself (rendered as the "You" turn) carries no hub provenance string.
      case "relay_in": this.addMsg("relay", "Relay", `[from ${ev.from_name || "session " + ev.from}] received ${ev.chars} chars${ev.augmented ? " · with framing" : ""}`); break;
      // S7: target was busy — the relay is QUEUED (a badge on its tile) and re-surfaces as a
      // confirm chip when that session next finishes. Never auto-sent, so the human re-confirms.
      case "queued": this.addMsg("relay", "Relay", `⏳ queued for ${ev.to_name || "session " + ev.to} · ${ev.to} (busy — re-surfaces when it finishes)`); break;
      // S8: a per-group AUTO-relay fired (the human never clicked ✓ — the group is trusted).
      // Render an `undo` affordance: the delivered text can't be recalled, but undo retracts
      // the auto-drawn Map edge so an unwanted auto-route doesn't linger as a relation.
      case "auto_routed": {
        const m = this.addMsg("relay", "Relay", `⚡ auto-routed → ${ev.to_name || "session " + ev.to} · ${ev.to} `);
        const undo = el("button", "relay-undo", "undo");
        undo.title = "retract the auto-drawn relation (the delivered message itself can't be recalled)";
        undo.onclick = () => {
          fetch("/relations/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ from: ev.from, to: ev.to }) }).catch(() => {});
          undo.replaceWith(el("span", "relay-undone", "· undone"));
        };
        m.querySelector(".msg-text").appendChild(undo);
        break;
      }
      case "renamed": // S4: hub-side rename — refresh the focused header live
        if (this.session) this.session.name = ev.name;
        this.applyHeader(this.session || { name: ev.name, state: "idle" });
        break;
      case "error": this.addMsg("error", "Error", ev.text); break;
    }
  }

  // ---- approval rider (Bureau-stamp), rendered in the dock --------------
  addApproval(ev) {
    this.rider.innerHTML = "";
    const card = el("div", "rider");
    card.dataset.req = ev.id;
    card.appendChild(el("div", "rider-stamp", "RIDER"));
    const who = (this.session && this.session.name) || `session ${this.id}`;
    card.appendChild(el("div", "rider-head eyebrow", `◧ Needs approval · ${who}`));
    card.appendChild(el("div", "rider-cmd", riderCmd(ev)));
    const actions = el("div", "rider-actions");
    const allow = el("button", "rider-allow", "Sign & allow");
    const refuse = el("button", "rider-refuse", "Refuse");
    allow.onclick = () => this.decide(ev.id, "allow");
    refuse.onclick = () => this.decide(ev.id, "deny");
    actions.appendChild(allow);
    actions.appendChild(refuse);
    card.appendChild(actions);
    this.rider.appendChild(card);
  }

  resolveApproval(id, behavior) {
    const card = this.rider.querySelector(`.rider[data-req="${id}"]`);
    if (!card) return;
    card.classList.add(behavior === "allow" ? "allowed" : "denied");
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const head = card.querySelector(".rider-head");
    if (head) head.textContent += behavior === "allow" ? "  → allowed" : "  → refused";
    setTimeout(() => {
      if (card.parentNode === this.rider) this.rider.innerHTML = "";
    }, 2500);
  }

  async decide(id, behavior) {
    await fetch(`/permission/decide/${id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ behavior }),
    });
  }

  // ---- route the focused session's last output into another session ----
  toggleRoute(on) {
    if (on) {
      this.populateRouteTargets();
      // S6: confirm step — preview the verbatim body the target will receive and start
      // with an EMPTY framing section (augment is opt-in per relay, default off).
      this.routePreview.textContent = this._lastMsg || "(no captured output yet)";
      this.routeSection.value = "";
    }
    this.routeBar.classList.toggle("hidden", !on);
  }
  populateRouteTargets() {
    const cur = this.routeTarget.value;
    this.routeTarget.innerHTML = "";
    const panels = sessionRail.byPanel || {};
    let added = 0;
    for (const pid of Object.keys(panels)) {
      if (pid === this.id || !panels[pid].session_id) continue;
      const opt = el("option", null, `${panels[pid].name || "session " + pid} · ${pid}`);
      opt.value = pid;
      this.routeTarget.appendChild(opt);
      added++;
    }
    if (!added) {
      const opt = el("option", null, "no other sessions");
      opt.value = "";
      this.routeTarget.appendChild(opt);
    }
    if (cur) this.routeTarget.value = cur;
  }
  async onRoute() {
    const to = this.routeTarget.value;
    if (!to) return;
    // S6: send ONLY {from_panel,to_panel,section}. The body is server-resolved verbatim
    // from the source's last_message — we deliberately never send a `text` override (#13).
    // `section` is the human's opt-in framing, prepended server-side to the verbatim body.
    const section = this.routeSection.value.trim();
    const r = await fetch("/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from_panel: this.id, to_panel: to, section }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      this.addMsg("error", "Error", j.error || `route failed (${r.status})`);
    }
    this.toggleRoute(false);
    // success renders via the "routed" SSE event (avoids a double marker)
  }

  async onLaunch(e) {
    e.preventDefault();
    const name = $("launch-name").value.trim();
    const cwd = $("launch-cwd").value.trim();
    const prompt = $("launch-prompt").value.trim();
    const model = $("launch-model").value; // "" = CLI/account default
    if (!prompt) return;
    const r = await fetch(`/launch/${this.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, cwd, prompt, model }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      alert(j.error || `launch failed (${r.status})`);
      return;
    }
    this.session = { name: name || `session ${this.id}`, state: "running", cwd };
    this.thread.innerHTML = "";
    this.rider.innerHTML = "";
    this.live = null;
    this.show("thread");
    this.applyHeader(this.session);
    this._hideCtx(); // fresh launch — no ctx% until the first turn's result lands
    this.connect();
    sessionRail.markActive(this.id);
    sessionRail.refresh();
  }

  async onReply(e) {
    e.preventDefault();
    const text = this.replyInput.value.trim();
    if (!text) return;
    this.replyInput.value = "";
    const r = await fetch(`/reply/${this.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      this.addMsg("error", "Error", j.error || `reply failed (${r.status})`);
    }
  }
}

// turn an approval payload into a single readable command line for the rider
function riderCmd(ev) {
  const i = ev.input || {};
  if (i.command) return "$ " + i.command;
  for (const k of ["file_path", "path", "url", "pattern", "query"]) if (i[k]) return `${ev.tool_name}: ${i[k]}`;
  const s = JSON.stringify(i);
  return `${ev.tool_name} ${s.length > 220 ? s.slice(0, 220) + "…" : s}`;
}

// ============================================================= SESSION RAIL
// The left column: live sessions grouped by workstream, polled from /sessions.
// The focused row gets the red active stripe. Also drives the masthead tally.
class SessionRail {
  constructor() {
    this.list = $("session-rail");
    this.byPanel = {};
    this.activeId = null;
    $("new-session").addEventListener("click", () => focus.openLaunch(this.nextFreeId()));
    this.refresh();
    setInterval(() => this.refresh(), 1000);
  }

  // lowest id not held by a launched session (matches the backend's slot model)
  nextFreeId() {
    let i = 0;
    while (this.byPanel[String(i)] && this.byPanel[String(i)].session_id) i++;
    return String(i);
  }

  markActive(id) {
    this.activeId = id;
    this.list.querySelectorAll(".srow").forEach((r) => r.classList.toggle("active", r.dataset.panel === id));
  }

  async refresh() {
    let panels;
    try {
      panels = (await (await fetch("/sessions")).json()).panels || [];
    } catch {
      return; // hub down — conn dot handles it
    }
    this.byPanel = {};
    for (const p of panels) this.byPanel[p.panel_id] = p;
    this.render(panels);
    this.updateTally(panels);
    // keep the focused header fresh as its state changes
    if (focus.id && this.byPanel[focus.id]) focus.session = this.byPanel[focus.id];
  }

  updateTally(panels) {
    let run = 0, need = 0;
    for (const p of panels) {
      const c = sinfo(p.state).cls;
      if (c === "run") run++;
      else if (c === "need") need++;
    }
    const parts = [`${run} running`];
    if (need) parts.push(`<span style="color:var(--amber)">${need} needs you</span>`);
    parts.push(`${panels.length} total`);
    $("tally-text").innerHTML = parts.join(" · ");
  }

  render(panels) {
    if (inlineEditing) return; // a rename input is open in the rail — don't rebuild over it
    if (!panels.length) {
      this.list.innerHTML = '<p class="rail-left-empty">no sessions yet — <span class="lk">+ NEW</span> to launch one.</p>';
      return;
    }
    const groups = groupSessions(panels);
    const order = Object.keys(groups).filter((n) => n !== "sessions").sort();
    if (groups.sessions) order.push("sessions");

    this.list.innerHTML = "";
    for (const g of order) {
      const arr = groups[g].slice().sort((a, b) => (parseInt(a.panel_id) || 0) - (parseInt(b.panel_id) || 0));
      this.list.appendChild(this.groupHead(g, arr.length));
      for (const s of arr) this.list.appendChild(this.row(s));
    }
    this.markActive(this.activeId);
  }

  groupHead(name, count) {
    const h = el("div", "rail-group");
    h.appendChild(el("span", "rail-group-name", name));
    h.appendChild(el("span", "rail-group-rule"));
    h.appendChild(el("span", "rail-group-count", String(count)));
    return h;
  }

  row(s) {
    const si = sinfo(s.state);
    const r = el("div", "srow");
    r.dataset.panel = s.panel_id;
    const top = el("div", "srow-top");
    top.appendChild(el("span", "dot bg-" + si.cls + (isLive(s.state) ? " live" : "")));
    top.appendChild(el("span", "srow-name", s.name || `session ${s.panel_id}`));
    if (s.pending) top.appendChild(queueBadge(s.pending)); // S7: queued-relay count badge
    top.appendChild(el("span", "tag st-" + si.cls + " srow-tag", si.glyph + " " + si.word));
    const del = el("button", "srow-del", "✕");
    del.title = "delete this session (remove from board)";
    del.onclick = (e) => { e.stopPropagation(); deleteSession(s.panel_id, s.name); };
    top.appendChild(del);
    r.appendChild(top);
    const sub = briefMap[s.panel_id] || s.last_message || "";
    if (sub) r.appendChild(el("div", "srow-sub", sub));
    const sug = suggestMap[s.panel_id];
    if (sug && sug.name && sug.name !== (s.name || "").toLowerCase()) r.appendChild(this.suggestChip(s.panel_id, sug));
    r.onclick = () => focus.open(s.panel_id, s);
    return r;
  }

  // S4: the secretary PROPOSES a name; the user accepts (✓) or dismisses (✕).
  suggestChip(pid, sug) {
    const chip = el("div", "suggest-chip");
    chip.appendChild(el("span", "eyebrow suggest-lead", "rename →"));
    chip.appendChild(el("span", "suggest-name", sug.name));
    const ok = el("button", "suggest-ok", "✓ accept");
    const no = el("button", "suggest-no", "✕");
    ok.title = "rename this session to the suggestion";
    no.title = "dismiss the suggestion";
    ok.onclick = (e) => { e.stopPropagation(); acceptSuggestion(pid, sug); };
    no.onclick = (e) => { e.stopPropagation(); dismissSuggestion(sug); };
    chip.appendChild(ok);
    chip.appendChild(no);
    return chip;
  }
}

// ============================================================= ADVISOR DOCK
// The right dock: the STREAMING advisor terminal (the live fix), plus the
// situation strip (secretary briefs). Named `Rail` per the layout split — the
// LEFT column is SessionRail; this keeps the advisor/secretary surface.
class Rail {
  constructor() {
    this.chat = $("advisor-chat");
    this.qInput = $("advisor-q");
    this.sendBtn = $("advisor-go");
    this.situation = $("situation");
    this.inFlight = false;
    this.liveEl = null;
    this.phaseEl = null;
    this.es = null;

    $("advisor-form").addEventListener("submit", (e) => { e.preventDefault(); this.ask(); });
    $("advisor-clear").addEventListener("click", () => this.clearChat());

    // Map (4A) floating console — same advisor backend, mirrored render.
    this.mapBody = $("map-console-body");
    this.mapInput = $("map-advisor-q");
    this._mYou = "";
    this._mBot = "";
    $("map-advisor-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const m = this.mapInput.value.trim();
      if (!m) return;
      this.mapInput.value = "";
      this.ask(m); // streams over the same persistent /advisor/stream; mirrored here + in the dock
    });

    this.openStream(); // persistent advisor SSE (live events only)
    this.loadChat(); // history (live-only stream never replays — /advisor/chat owns it)
    this.refreshBrief();
    setInterval(() => this.refreshBrief(), 2000);
  }

  // ---- situation strip (secretary briefs) ----
  async refreshBrief() {
    let sessions;
    try {
      sessions = (await (await fetch("/secretary/brief")).json()).sessions || [];
    } catch {
      return;
    }
    briefMap = {};
    for (const s of sessions) if (s.brief) briefMap[s.panel_id] = s.brief;
    this.renderSituation(sessions);
  }

  renderSituation(sessions) {
    this.situation.innerHTML = "";
    if (!sessions.length) {
      this.situation.appendChild(el("span", "situation-empty", "no live sessions."));
      return;
    }
    // what needs you, first
    for (const s of sessions.filter((x) => sinfo(x.state).cls === "need")) this.situation.appendChild(this.chip(s));
    for (const s of sessions.filter((x) => sinfo(x.state).cls === "err")) this.situation.appendChild(this.chip(s));
    const running = sessions.filter((x) => sinfo(x.state).cls === "run").length;
    if (running) {
      const c = el("span", "schip");
      c.appendChild(el("span", "dot bg-run"));
      c.appendChild(el("span", null, `${running} running clean`));
      this.situation.appendChild(c);
    }
    for (const s of sessions.filter((x) => sinfo(x.state).cls === "idle").slice(0, 2)) this.situation.appendChild(this.chip(s));
  }

  chip(s) {
    const si = sinfo(s.state);
    const cls = si.cls === "need" ? "need" : si.cls === "err" ? "err" : "idle";
    const c = el("span", "schip " + cls);
    c.appendChild(el("span", "dot bg-" + si.cls + (isLive(s.state) ? " live" : "")));
    let tail = s.brief || si.word;
    if (s.state === "awaiting-approval") tail = "needs approval";
    c.appendChild(el("span", null, `${s.name || "session " + s.panel_id} · ${tail}`));
    c.onclick = () => focus.open(s.panel_id, sessionRail.byPanel[s.panel_id] || null);
    return c;
  }

  // ---- streaming advisor (the live fix) ----
  openStream() {
    if (this.es) this.es.close();
    this.es = new EventSource("/advisor/stream"); // auto-reconnects on drop
    this.es.onmessage = (e) => this.onAdvisorEvent(JSON.parse(e.data));
  }

  onAdvisorEvent(ev) {
    switch (ev.kind) {
      case "phase": this.onPhase(ev.phase); break;
      case "delta": this.onDelta(ev.text); break;
      case "done": this.onDone(ev); break;
      case "error": this.onError(ev.text); break;
    }
  }

  onPhase(phase) {
    const n = Object.keys(sessionRail.byPanel || {}).length;
    const txt = phase === "thinking" ? `thinking…  scanning ${n} session${n === 1 ? "" : "s"}` : "answering…";
    if (!this.phaseEl) {
      this.phaseEl = el("div", "adv-phase", txt);
      this.chat.appendChild(this.phaseEl);
    } else {
      this.phaseEl.textContent = txt;
    }
    this.inFlight = true;
    this.setEnabled(false);
    this.paintMapConsole("phase", txt);
    this.scroll();
  }

  onDelta(text) {
    if (this.phaseEl) { this.phaseEl.remove(); this.phaseEl = null; } // answer is now visible
    if (!this.liveEl) {
      this.liveEl = el("div", "adv-line bot streaming");
      this.liveEl.appendChild(el("span", "mk", "● ")); // ●
      this.liveEl.appendChild(el("span", "tx", ""));
      this.chat.appendChild(this.liveEl);
    }
    this.liveEl.querySelector(".tx").textContent += text;
    this._mBot = this.liveEl.querySelector(".tx").textContent;
    this.paintMapConsole("stream");
    this.scroll();
  }

  onDone(ev) {
    if (this.phaseEl) { this.phaseEl.remove(); this.phaseEl = null; }
    if (this.liveEl) {
      this.liveEl.classList.remove("streaming");
      this.liveEl = null;
    } else {
      this.loadChat(); // no deltas streamed (rare) — pull the finalized answer
    }
    const bits = [];
    if (ev.cost != null) bits.push(`$${Number(ev.cost).toFixed(4)}`);
    if (ev.tokens != null) bits.push(`${ev.tokens} tok`);
    if (ev.duration_ms != null) bits.push(`${(ev.duration_ms / 1000).toFixed(1)}s`);
    if (bits.length) this.chat.appendChild(el("div", "adv-meta", bits.join(" · ")));
    this.inFlight = false;
    this.setEnabled(true);
    this.qInput.focus();
    this.paintMapConsole("done");
    this.scroll();
  }

  onError(text) {
    if (this.phaseEl) { this.phaseEl.remove(); this.phaseEl = null; }
    if (this.liveEl) { this.liveEl.classList.remove("streaming"); this.liveEl = null; }
    this.chat.appendChild(el("div", "adv-line err", "✕ " + (text || "advisor error")));
    this.inFlight = false;
    this.setEnabled(true);
    this._mBot = "✕ " + (text || "advisor error");
    this.paintMapConsole("done");
    this.scroll();
  }

  async ask(override) {
    const fromMap = override != null;
    const msg = (fromMap ? override : this.qInput.value).trim();
    if (!msg || this.inFlight) return;
    if (!fromMap) this.qInput.value = "";
    this.appendYou(msg);
    this._mYou = msg;
    this._mBot = "";
    this.paintMapConsole("phase", "sending…");
    this.inFlight = true;
    this.setEnabled(false);
    if (!this.phaseEl) {
      this.phaseEl = el("div", "adv-phase", "sending…");
      this.chat.appendChild(this.phaseEl);
      this.scroll();
    }
    let r;
    try {
      r = await fetch("/advisor/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });
    } catch {
      this.onError("advisor unreachable");
      return;
    }
    if (r.status === 409) {
      if (this.phaseEl) { this.phaseEl.remove(); this.phaseEl = null; }
      this.chat.appendChild(el("div", "adv-line err", "✕ advisor busy — wait for the current turn to finish"));
      this.inFlight = false;
      this.setEnabled(true);
      this.scroll();
      return;
    }
    if (!r.ok) { this.onError(`send failed (${r.status})`); return; }
    // success: phase -> delta(s) -> done arrive on the persistent stream
  }

  appendYou(text) {
    const d = el("div", "adv-line you");
    d.appendChild(el("span", "mk", "› ")); // ›
    d.appendChild(el("span", "tx", text));
    this.chat.appendChild(d);
    this.scroll();
  }

  setEnabled(on) {
    this.qInput.disabled = !on;
    this.sendBtn.disabled = !on;
    if (this.mapInput) this.mapInput.disabled = !on;
  }
  scroll() { this.chat.scrollTop = this.chat.scrollHeight; }
  scrollMap() { if (this.mapBody) this.mapBody.scrollTop = this.mapBody.scrollHeight; }

  // Mirror the latest advisor exchange into the Map's floating console. modes:
  // "phase" (a faint status line under the question) · "stream"/"done" (the
  // answer, with the streaming cursor only while "stream").
  paintMapConsole(mode, phaseText) {
    if (!this.mapBody) return;
    this.mapBody.classList.toggle("streaming", mode === "stream");
    this.mapBody.innerHTML = "";
    if (!this._mYou && !this._mBot && mode !== "phase") {
      this.mapBody.appendChild(el("span", "dock-empty", "Ask across the field, or click a node to read it in the Rail."));
      return;
    }
    if (this._mYou) {
      const y = el("div", "mc-you");
      y.appendChild(el("span", "mk", "› "));
      y.appendChild(el("span", "tx", this._mYou));
      this.mapBody.appendChild(y);
    }
    if (mode === "phase") {
      this.mapBody.appendChild(el("div", "mc-phase", phaseText || "thinking…"));
    } else {
      const b = el("div", "mc-bot");
      b.appendChild(el("span", "mk", "● "));
      b.appendChild(el("span", "tx", this._mBot));
      this.mapBody.appendChild(b);
    }
    this.scrollMap();
  }

  async loadChat() {
    try {
      const j = await (await fetch("/advisor/chat")).json();
      this.chat.innerHTML = "";
      for (const t of j.turns || []) {
        if (t.role === "you") this.appendYou(t.text);
        else {
          const d = el("div", "adv-line bot");
          d.appendChild(el("span", "mk", "● "));
          d.appendChild(el("span", "tx", t.text));
          this.chat.appendChild(d);
        }
      }
      if (!(j.turns || []).length) {
        this.chat.appendChild(el("div", "dock-empty", "Ask across every session — the advisor reasons over the whole board."));
      }
      // seed the Map console mirror with the most recent exchange
      const turns = j.turns || [];
      const lastYou = [...turns].reverse().find((t) => t.role === "you");
      const lastBot = [...turns].reverse().find((t) => t.role !== "you");
      this._mYou = lastYou ? lastYou.text : "";
      this._mBot = lastBot ? lastBot.text : "";
      this.paintMapConsole("done");
      this.scroll();
    } catch {
      /* hub down */
    }
  }

  clearChat() {
    const btn = $("advisor-clear");
    if (!this._confirm) {
      this._confirm = true;
      btn.textContent = "clear?";
      btn.classList.add("confirm");
      this._ct = setTimeout(() => this._resetClear(), 3000);
      return;
    }
    this._resetClear();
    fetch("/advisor/clear", { method: "POST" }).then(() => {
      this.liveEl = null;
      this.phaseEl = null;
      this.inFlight = false;
      this.setEnabled(true);
      this.loadChat();
    });
  }
  _resetClear() {
    const btn = $("advisor-clear");
    this._confirm = false;
    clearTimeout(this._ct);
    btn.textContent = "↻"; // ↻
    btn.classList.remove("confirm");
  }
}

// ============================================================ F-COORD: VIEWPORT
// World space = the existing 0..100 percent space. The viewport is a single CSS
// transform on #map-world (translate(panXpx,panYpx) scale(zoom), origin 0 0). Render
// sites keep their % math (they live inside the transformed group); pointer input
// converts screen px <-> world via toWorld/toScreen. Persisted to /viewport.
const ZMIN = 0.35, ZMAX = 3.0;
const Viewport = {
  panX: 0, panY: 0, zoom: 1,
  stage: null, world: null,
  init(stageEl, worldEl) { this.stage = stageEl; this.world = worldEl; },
  rect() { return this.stage.getBoundingClientRect(); },

  // screen px (relative to the stage top-left) -> world 0..100
  toWorld(sx, sy) {
    const r = this.rect();
    return { x: ((sx - this.panX) / this.zoom) / r.width * 100,
             y: ((sy - this.panY) / this.zoom) / r.height * 100 };
  },
  // world 0..100 -> screen px (relative to the stage top-left)
  toScreen(wx, wy) {
    const r = this.rect();
    return { x: (wx / 100 * r.width) * this.zoom + this.panX,
             y: (wy / 100 * r.height) * this.zoom + this.panY };
  },

  _settleT: null,
  applyTransform() {
    if (!this.world) return;
    this.world.style.transform =
      `translate(${this.panX}px, ${this.panY}px) scale(${this.zoom})`;
    // Sharpness fix: a persistent composited layer (will-change:transform) is rasterized
    // ONCE and bitmap-scaled, so zooming in pixelates the tiles until a re-raster (which
    // is why a Rail round-trip sharpened them). Hint the layer only while actively
    // transforming, then drop it on settle so the browser re-rasterizes crisply at the
    // resting zoom. Pan keeps scale constant, so it never blurs.
    this.world.style.willChange = "transform";
    clearTimeout(this._settleT);
    this._settleT = setTimeout(() => { if (this.world) this.world.style.willChange = "auto"; }, 200);
  },
  pan(dxScreen, dyScreen) { this.panX += dxScreen; this.panY += dyScreen; this.applyTransform(); },
  zoomAt(sx, sy, factor) {
    const z0 = this.zoom;
    const z1 = Math.max(ZMIN, Math.min(ZMAX, z0 * factor));
    if (z1 === z0) return;
    // keep the world point under the cursor fixed: solve panX so toScreen(world)==cursor
    this.panX = sx - ((sx - this.panX) / z0) * z1;
    this.panY = sy - ((sy - this.panY) / z0) * z1;
    this.zoom = z1;
    this.applyTransform();
  },
  reset() { this.panX = 0; this.panY = 0; this.zoom = 1; this.applyTransform(); },

  async load() {
    try {
      const v = await (await fetch("/viewport")).json();
      if (v && typeof v.zoom === "number") {
        this.panX = v.panX || 0; this.panY = v.panY || 0;
        this.zoom = Math.max(ZMIN, Math.min(ZMAX, v.zoom || 1));
      }
    } catch { /* hub down — home view */ }
    this.applyTransform();
  },
  _saveT: null,
  persist() {  // debounced — pan/zoom fire rapidly
    clearTimeout(this._saveT);
    this._saveT = setTimeout(() => {
      fetch("/viewport", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ panX: this.panX, panY: this.panY, zoom: this.zoom }) }).catch(() => {});
    }, 250);
  },
};

// ============================================================= MAP VIEW (4A)
// The Switchboard: one monitor tile per live session, grouped into workstream
// lanes; the secretary's typed relations drawn as bold ORTHOGONAL traces
// (solid = explicit/confirmed, dashed = secretary-inferred). Clicking a node
// opens it focused in the Rail. Sessions come from SessionRail's existing
// /sessions poll (no second poll); only /relations is fetched here.
class MapView {
  constructor() {
    this.view = $("map-view");
    this.stage = $("map-stage");
    this.svg = $("map-edges");
    this.lanesEl = $("map-lanes");
    this.nodesEl = $("map-nodes");
    this.labelsEl = $("map-labels");
    this.framesEl = $("map-frames"); // S5: group-frame regions (under the tiles)
    this.suggestsEl = $("map-suggests"); // S5: secretary frame-suggest chips (over the field)
    this.worldEl = $("map-world"); // F-COORD: the transformed group
    this.emptyEl = $("map-empty");
    this.countsEl = $("map-hud-counts");
    this.relations = [];
    this.frames = []; // S5: [{id,label,rect:{x,y,w,h}}]
    this.notes = []; // Slice 4: [{id,anchor,text,author,created}]
    this.notesEl = $("map-world-notes"); // Slice 4: notes layer (inside the transformed world)
    this.pos = {}; // panel_id -> {x,y} in 0..100 stage coords (tiles + edge routing)
    this.dragging = false; // S5: a tile is being dragged — pause re-renders
    this.framing = false; // S5: drawing a group-frame marquee
    $("map-autoarrange").addEventListener("click", () => this.autoArrange());
    $("map-frame").addEventListener("click", () => this.startFraming());
    setInterval(() => { if (this.visible()) this.refresh(); }, 1200);
    Viewport.init(this.stage, this.worldEl); // F-COORD
    this._bindPanZoom();
    $("map-reset").addEventListener("click", () => { Viewport.reset(); Viewport.persist(); });
  }

  visible() { return !this.view.classList.contains("hidden"); }

  async refresh() {
    if (this.dragging) return; // never refetch/rebuild under the user's hand
    if (!this._vpLoaded) { this._vpLoaded = true; await Viewport.load(); } // F-COORD: persisted pan/zoom on first open
    try {
      const [rel, fr, nt] = await Promise.all([
        (await fetch("/relations")).json(),
        (await fetch("/frames")).json(),
        (await fetch("/notes")).json(),
      ]);
      this.relations = rel.relations || [];
      this.frames = fr.frames || [];
      this.notes = nt.notes || [];
    } catch {
      /* hub down — keep the last edges/frames on screen */
    }
    this.render();
  }

  render() {
    if (inlineEditing || this.dragging) return; // editing / dragging — don't rebuild over the user
    const panels = Object.values(sessionRail.byPanel || {});
    this.updateCounts(panels);
    this.drawFrames(); // frames are position-independent notes — render even with no sessions
    if (!panels.length) {
      if (this.emptyEl) this.emptyEl.classList.remove("hidden");
      this.nodesEl.innerHTML = this.lanesEl.innerHTML = this.labelsEl.innerHTML = "";
      this.svg.innerHTML = "";
      this.pos = {};
      this.renderSuggests();
      this.drawNotes(); // Slice 4: free/frame notes still render with no sessions; node notes skip (empty pos)
      return;
    }
    if (this.emptyEl) this.emptyEl.classList.add("hidden");

    // deterministic lanes — reuse the rail's workstream grouping. These positions
    // are BOTH the default tidy layout AND the auto-arrange target (S5 §3): a node
    // with no pin sits in its lane slot; auto-arrange just clears the pins.
    const groups = groupSessions(panels);
    const lanes = Object.keys(groups).filter((n) => n !== "sessions").sort();
    if (groups.sessions) lanes.push("sessions");
    const N = lanes.length;

    const lanePos = {};
    this.lanesEl.innerHTML = "";
    lanes.forEach((lane, i) => {
      const x = ((i + 0.5) / N) * 100;
      const head = el("div", "map-lane-head");
      head.style.left = x + "%";
      head.appendChild(el("span", "eyebrow map-lane-name", "⌐ " + lane.toUpperCase()));
      head.appendChild(el("span", "map-lane-rule"));
      this.lanesEl.appendChild(head);

      const arr = groups[lane].slice().sort((a, b) => (parseInt(a.panel_id) || 0) - (parseInt(b.panel_id) || 0));
      const M = arr.length;
      arr.forEach((s, j) => {
        lanePos[s.panel_id] = { x, y: M === 1 ? 42 : 22 + j * (50 / (M - 1)) }; // band 22..72
      });
    });

    // a persisted pin (x/y from /sessions) overrides the lane slot — manual drag wins
    this.pos = {};
    this.nodesEl.innerHTML = "";
    for (const s of panels) {
      const pinned = typeof s.x === "number" && typeof s.y === "number";
      const p = pinned ? { x: s.x, y: s.y } : (lanePos[s.panel_id] || { x: 50, y: 42 });
      this.pos[s.panel_id] = p;
      this.nodesEl.appendChild(this.node(s, p.x, p.y, pinned));
    }

    this.drawEdges();
    this.renderSuggests();
    this.drawNotes(); // Slice 4: all anchors, with this.pos populated
  }

  node(s, x, y, pinned) {
    const si = sinfo(s.state);
    const t = el("div", "map-tile");
    t.dataset.panel = s.panel_id;
    t.style.left = x + "%";
    t.style.top = y + "%";
    // RAIL-TYPE: restore a manual SE-corner size; `.sized` lets the info block flex
    // to fill the extra height as a longer last-message preview.
    if (s.w != null && s.h != null) {
      t.style.width = s.w + "px";
      t.style.height = s.h + "px";
      t.classList.add("sized");
    }
    t.style.borderLeftColor = `var(${STATE_VAR[si.cls] || "--paper-mute"})`;
    if (s.panel_id === focus.id) t.classList.add("focused");
    if (pinned) t.classList.add("pinned");
    t.classList.add("alive-" + si.cls); // Slice 1: needs-you glows amber, running breathes
    t.appendChild(el("span", "map-tile-lane", groupOf(s.name).toUpperCase()));
    const head = el("div", "map-tile-head");
    head.appendChild(el("span", "dot bg-" + si.cls + (isLive(s.state) ? " live" : "")));
    head.appendChild(el("span", "map-tile-name", s.name || `session ${s.panel_id}`));
    if (s.pending) head.appendChild(queueBadge(s.pending)); // S7: queued-relay count badge
    head.appendChild(el("span", "map-tile-stat st-" + si.cls, si.glyph + " " + si.word));
    t.appendChild(head);
    const info = briefMap[s.panel_id] || s.last_message || "";
    if (info) t.appendChild(el("div", "map-tile-info", info));
    if (s.ctx_pct != null) { // S3: per-node context fill stripe
      const bar = el("div", "map-tile-ctx");
      bar.title = Math.round(s.ctx_pct * 100) + "% context";
      const fill = el("i", ctxHeat(s.ctx_pct, s.ctx_compact_pct));
      fill.style.width = Math.min(100, s.ctx_pct * 100) + "%";
      bar.appendChild(fill);
      t.appendChild(bar);
    }
    this._bindDrag(t, s); // S5: pointer-drag to pin; a tap (no drag) opens it in the Rail
    const wire = el("div", "map-tile-wire", "◦"); // Slice 3: drag this to draw an edge to another tile
    wire.title = "drag to another session to draw a relation";
    this._bindWireDraw(wire, s);
    t.appendChild(wire);
    const grip = el("div", "map-tile-resize"); // RAIL-TYPE: SE-corner resize handle
    grip.title = "drag to resize · double-click to reset";
    this._bindResize(grip, t, s);
    t.appendChild(grip);
    return t;
  }

  // RAIL-TYPE: SE-corner resize. The tile is center-anchored (translate(-50%,-50%))
  // so its map coordinate stays put; growing keeps the node centered on its point.
  // Each px the corner moves widens/heightens the box by 2px so the grabbed corner
  // tracks the cursor. Persists to /node/<id>/size; double-click clears it.
  _bindResize(grip, tile, s) {
    grip.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      e.stopPropagation(); // don't start a tile move (_bindDrag) or canvas pan
      const r = tile.getBoundingClientRect();
      const startW = r.width, startH = r.height, sx = e.clientX, sy = e.clientY;
      this.dragging = true; // pause re-renders so the tile isn't rebuilt mid-resize
      grip.setPointerCapture(e.pointerId);
      const MINW = 160, MAXW = 520, MINH = 64, MAXH = 420;
      const move = (ev) => {
        const w = Math.max(MINW, Math.min(MAXW, startW + (ev.clientX - sx) * 2));
        const h = Math.max(MINH, Math.min(MAXH, startH + (ev.clientY - sy) * 2));
        tile.style.width = w + "px";
        tile.style.height = h + "px";
        tile.classList.add("sized");
      };
      const up = () => {
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", up);
        this.dragging = false;
        const w = Math.round(parseFloat(tile.style.width));
        const h = Math.round(parseFloat(tile.style.height));
        fetch(`/node/${s.panel_id}/size`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ w, h }),
        }).catch(() => {});
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", up);
    });
    grip.addEventListener("dblclick", (e) => {
      e.preventDefault();
      e.stopPropagation();
      tile.style.width = "";
      tile.style.height = "";
      tile.classList.remove("sized");
      fetch(`/node/${s.panel_id}/size`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      }).catch(() => {});
    });
  }

  drawEdges() {
    this.svg.innerHTML = "";
    this.labelsEl.innerHTML = "";
    // F-IDENT: relations are keyed by node_id; this.pos stays PANEL-keyed. Resolve
    // node_id -> panel_id via the /sessions payload (each panel now carries node_id).
    const nodeToPanel = {};
    for (const p of Object.values(sessionRail.byPanel || {})) {
      if (p && p.node_id) nodeToPanel[p.node_id] = String(p.panel_id);
    }
    const fid = focus.id;
    // Slice 3: focus scope — the focused node's frame's effective members all stay lit,
    // so an initiative reads as a connected cluster, not just the single node's spokes.
    const focusCluster = new Set();
    if (fid != null) {
      focusCluster.add(String(fid));
      const here = this.pos[fid];
      for (const f of this.frames || []) {
        if (here && this._inRect(here, f.rect || {})) for (const m of (this.effectiveMembers ? this.effectiveMembers(f) : [])) focusCluster.add(String(m));
      }
    }
    for (const r of this.relations) {
      const pf = nodeToPanel[r.from], pt = nodeToPanel[r.to];
      const a = this.pos[pf];
      const b = this.pos[pt];
      if (!a || !b) continue; // an endpoint isn't on the board — skip (no dangling wire)
      // Slice 5: human edges read WARM (authoritative); secretary edges keep their type
      // color (FEEDS/REVIEWS already cool). solid-vs-dashed below still encodes trust.
      const mine = r.author === "human" || r.source === "explicit";
      const color = mine ? "var(--warm)" : (r.color || relColor(r.type));
      const explicit = r.source === "explicit";
      const touches = fid != null && (focusCluster.has(pf) && focusCluster.has(pt) || pf === String(fid) || pt === String(fid));
      const emph = fid == null || touches;
      const midx = (a.x + b.x) / 2;
      // orthogonal Z route (horizontal → vertical → horizontal); never a curve
      const pts = `${a.x},${a.y} ${midx},${a.y} ${midx},${b.y} ${b.x},${b.y}`;

      const wire = svgEl("polyline");
      wire.setAttribute("points", pts);
      wire.setAttribute("fill", "none");
      wire.setAttribute("stroke", color);
      wire.setAttribute("stroke-width", emph ? "2.6" : "1.6");
      wire.setAttribute("vector-effect", "non-scaling-stroke");
      if (!explicit) wire.setAttribute("stroke-dasharray", "3 4"); // inferred = dashed
      wire.style.opacity = emph ? "1" : "0.26";
      if (touches) wire.style.filter = `drop-shadow(0 0 4px ${color})`;
      this.svg.appendChild(wire);

      const chip = el("div", "map-relchip " + (mine ? "mine" : "its"), (r.type || "feeds").toLowerCase());
      chip.style.left = midx + "%";
      chip.style.top = (a.y + b.y) / 2 + "%";
      chip.style.color = color;
      if (explicit) chip.style.borderColor = color;
      chip.style.opacity = emph ? "1" : "0.3";
      this.labelsEl.appendChild(chip);
    }
  }

  updateCounts(panels) {
    if (!this.countsEl) return; // counts now live only in the right-side masthead tally
    let run = 0, need = 0, done = 0;
    for (const p of panels) {
      const c = sinfo(p.state).cls;
      if (c === "run") run++;
      else if (c === "need") need++;
      else if (c === "done") done++;
    }
    this.countsEl.innerHTML = "";
    this.countsEl.appendChild(this.countChip("run", `${run} running`));
    if (need) this.countsEl.appendChild(this.countChip("need", `${need} needs you`));
    if (done) this.countsEl.appendChild(this.countChip("done", `${done} done`));
    this.countsEl.appendChild(this.countChip("idle", `${panels.length} total`));
  }

  countChip(cls, text) {
    const c = el("span", "hud-count");
    c.appendChild(el("span", "dot bg-" + cls + (cls === "run" ? " live" : "")));
    c.appendChild(el("span", null, text));
    return c;
  }

  // ===================================================== S5: freeform canvas ==
  // The stage and tiles share a 0..100 percent space (tiles: left/top %, centered;
  // the edge SVG: viewBox 0 0 100 100). So a drop in pixels converts straight to %.
  stageRect() { return this.stage.getBoundingClientRect(); }

  // F-COORD: drag EMPTY canvas to pan; wheel to zoom about the cursor. A drag that
  // starts on a tile is handled by _bindDrag (tile move) — this only fires when the
  // pointerdown target is canvas background, not an interactive element.
  // NB: isBg is a NEGATIVE test (not the empty-element allow-list the plan sketched):
  // .map-nodes is inset:0 with pointer-events:auto, so it — not #map-stage/#map-world —
  // is the actual pointerdown target on empty canvas. Enumerating bg containers would
  // never match it and pan would never start. "background" = not a tile/frame-tab/chip/
  // control; everything else (the nodes/edges containers, world, stage) is pannable.
  _bindPanZoom() {
    let panning = false, lastX = 0, lastY = 0;
    const isBg = (t) => !t.closest(
      ".map-tile, .map-frame, .map-suggest-chip, .map-relay-chip, button, input, textarea");
    this.stage.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || this.framing || inlineEditing) return; // framing/edit own the stage
      if (!isBg(e.target)) return; // started on a tile/frame/chip — not a pan
      panning = true; lastX = e.clientX; lastY = e.clientY;
      this.stage.classList.add("panning");
      try { this.stage.setPointerCapture(e.pointerId); } catch { /* tracks without capture */ }
    });
    this.stage.addEventListener("pointermove", (e) => {
      if (!panning) return;
      Viewport.pan(e.clientX - lastX, e.clientY - lastY);
      lastX = e.clientX; lastY = e.clientY;
    });
    const endPan = (e) => {
      if (!panning) return;
      panning = false; this.stage.classList.remove("panning");
      try { this.stage.releasePointerCapture(e.pointerId); } catch { /* ok */ }
      Viewport.persist();
    };
    this.stage.addEventListener("pointerup", endPan);
    this.stage.addEventListener("pointercancel", endPan);
    this.stage.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = this.stageRect();
      Viewport.zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.1 : 1 / 1.1);
      Viewport.persist();
    }, { passive: false });
  }

  // keep a tile center on-canvas: half a 200px tile in % of the live stage width,
  // and a vertical band clear of the HUD bar (top) and the floating console (bottom).
  clampPct(x, y, rect) {
    const padX = rect && rect.width ? Math.min(20, (100 / rect.width) * 100) : 8;
    return [Math.max(padX, Math.min(100 - padX, x)), Math.max(13, Math.min(86, y))];
  }

  // snap a percent coord to the visible 46px blueprint grid (blueprint-clean feel)
  snapPct(x, y, rect) {
    const G = 46;
    const sx = Math.round((x / 100) * rect.width / G) * G;
    const sy = Math.round((y / 100) * rect.height / G) * G;
    return [(sx / rect.width) * 100, (sy / rect.height) * 100];
  }

  // pointer-drag a tile to pin it; a press without movement is a tap → open in Rail.
  _bindDrag(t, s) {
    const pid = s.panel_id;
    let startX = null, startY = null, base = null, moved = false;
    const begin = (e) => {
      if (e.button !== 0 || inlineEditing || this.framing) return; // L-button, not while editing/framing
      startX = e.clientX; startY = e.clientY; moved = false;
      base = this.pos[pid] ? { ...this.pos[pid] } : { x: parseFloat(t.style.left), y: parseFloat(t.style.top) };
      try { t.setPointerCapture(e.pointerId); } catch { /* no capture — drag still tracks */ }
    };
    const move = (e) => {
      if (startX == null) return;
      const dx = e.clientX - startX, dy = e.clientY - startY;
      if (!moved && Math.hypot(dx, dy) < 4) return; // threshold separates tap from drag
      moved = true; this.dragging = true;
      t.classList.add("dragging");
      const rect = this.stageRect();
      // F-COORD: a screen drag of dx px moves the tile dx/zoom world-px (the world group
      // is scaled by zoom). clampPct stays a SCREEN-space band (do NOT world-convert it).
      const z = Viewport.zoom;
      let [nx, ny] = this.clampPct(base.x + (dx / rect.width) * 100 / z, base.y + (dy / rect.height) * 100 / z, rect);
      t.style.left = nx + "%"; t.style.top = ny + "%";
      this.pos[pid] = { x: nx, y: ny };
      this.drawEdges(); // orthogonal traces re-route live as the node moves
    };
    const end = (e, allowTap) => {
      if (startX == null) return;
      try { t.releasePointerCapture(e.pointerId); } catch { /* already released */ }
      const wasDrag = moved;
      startX = startY = null;
      if (!wasDrag) {
        if (allowTap) { setView("rail"); focus.open(pid, sessionRail.byPanel[pid] || s); }
        return;
      }
      this.dragging = false;
      t.classList.remove("dragging");
      const rect = this.stageRect();
      let [sx, sy] = this.clampPct(...this.snapPct(this.pos[pid].x, this.pos[pid].y, rect), rect);
      t.style.left = sx + "%"; t.style.top = sy + "%";
      t.classList.add("pinned");
      this.pos[pid] = { x: sx, y: sy };
      const bp = sessionRail.byPanel[pid]; // optimistic: keep the pin between /sessions polls
      if (bp) { bp.x = sx; bp.y = sy; }
      this.drawEdges();
      this.persistPos(pid, sx, sy);
      // §7.3 re-entry: dropping a tile OUTSIDE a frame it was excluded from clears the
      // exclusion (so dragging back in re-auto-joins). Effective membership uses the live
      // position, so we only need to drop now-irrelevant exclusions.
      for (const f of this.frames || []) {
        if (this.isExcluded(f, pid) && !this._inRect({ x: sx, y: sy }, f.rect || {})) this.setFrameExclude(pid, f, false);
      }
    };
    t.addEventListener("pointerdown", begin);
    t.addEventListener("pointermove", move);
    t.addEventListener("pointerup", (e) => end(e, true));
    t.addEventListener("pointercancel", (e) => end(e, false));
  }

  async persistPos(pid, x, y) {
    try {
      await fetch(`/node/${pid}/pos`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x, y }),
      });
    } catch { /* hub down — the next poll restores the last persisted pin */ }
  }

  // Auto-arrange: clear every pin so all tiles fall back to the lane layout.
  async autoArrange() {
    try { await fetch("/positions/clear", { method: "POST" }); } catch { return; }
    for (const p of Object.values(sessionRail.byPanel || {})) { delete p.x; delete p.y; }
    this.render();
  }

  // per-node un-pin (right-click → Reset to lane)
  async resetNode(pid) {
    try {
      await fetch(`/node/${pid}/pos`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    } catch { return; }
    const bp = sessionRail.byPanel[pid]; if (bp) { delete bp.x; delete bp.y; }
    this.render();
  }

  // ---- Slice 3: connector nub drag-to-draw an edge ----
  // A temporary screen-space SVG overlay follows the cursor (drawn on the stage, ABOVE
  // #map-world so it isn't transformed by pan/zoom); dropping on a tile opens a 4-type
  // picker then POSTs /edge (node-keyed via the server boundary).
  _bindWireDraw(nub, s) {
    const pid = s.panel_id;
    let line = null;
    const tileCenterScreen = (panelId) => {
      const t = this.nodesEl.querySelector(`.map-tile[data-panel="${panelId}"]`);
      if (!t) return null;
      const tr = t.getBoundingClientRect(), sr = this.stageRect();
      return { x: tr.left + tr.width / 2 - sr.left, y: tr.top + tr.height / 2 - sr.top };
    };
    const down = (e) => {
      if (e.button !== 0) return;
      e.stopPropagation(); // not a tile-drag, not a pan
      this.dragging = true;
      const a = tileCenterScreen(pid);
      line = svgEl("line");
      line.setAttribute("class", "map-wire-draft");
      line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
      line.setAttribute("x2", a.x); line.setAttribute("y2", a.y);
      this.draftSvg().appendChild(line);
      try { nub.setPointerCapture(e.pointerId); } catch { /* ok */ }
    };
    const move = (e) => {
      if (!line) return;
      const sr = this.stageRect();
      line.setAttribute("x2", e.clientX - sr.left); line.setAttribute("y2", e.clientY - sr.top);
    };
    const up = (e) => {
      if (!line) return;
      line.remove(); line = null; this.dragging = false;
      try { nub.releasePointerCapture(e.pointerId); } catch { /* ok */ }
      const tgt = document.elementFromPoint(e.clientX, e.clientY);
      const tile = tgt && tgt.closest(".map-tile");
      const toPid = tile && tile.dataset.panel;
      if (!toPid || toPid === pid) return; // dropped on empty / self — no edge
      this._pickEdgeType(e.clientX, e.clientY, pid, toPid);
    };
    nub.addEventListener("pointerdown", down);
    nub.addEventListener("pointermove", move);
    nub.addEventListener("pointerup", up);
    nub.addEventListener("pointercancel", () => { if (line) { line.remove(); line = null; this.dragging = false; } });
  }
  // a transient screen-space SVG overlay for the draft wire (NOT inside #map-world)
  draftSvg() {
    if (!this._draftSvg) {
      this._draftSvg = svgEl("svg");
      this._draftSvg.setAttribute("class", "map-draft");
      this.stage.appendChild(this._draftSvg);
    }
    return this._draftSvg;
  }
  _pickEdgeType(x, y, fromPid, toPid) {
    openCtxMenu(x, y, ["FEEDS", "REVIEWS", "DEPENDS", "CONFLICT"].map((t) => ({
      label: t, hint: "edge", onClick: () => this._createEdge(fromPid, toPid, t),
    })));
  }
  async _createEdge(fromPid, toPid, type) {
    try {
      const r = await fetch("/edge", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_panel: fromPid, to_panel: toPid, rel_type: type }),
      });
      if (!r.ok) return;
    } catch { return; }
    await this.refresh(); // pull the new edge + redraw
  }

  // ---- group-frames (labeled regions; notes, not containers) ----
  // Slice 2: panel_id -> node_id, from the /sessions payload (F-IDENT put node_id on
  // each panel). Membership is node-keyed; positions are panel-keyed.
  _nodeOf(pid) { const s = sessionRail.byPanel[pid]; return s && s.node_id; }
  _inRect(p, r) { return p && r && p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h; }
  // §7.3 effective membership: panels whose CENTER is inside the rect AND whose node
  // is not excluded. Returns panel_ids.
  effectiveMembers(f) {
    const exc = new Set((f.excluded || []).map(String));
    const out = [];
    for (const pid of Object.keys(this.pos)) {
      if (this._inRect(this.pos[pid], f.rect || {}) && !exc.has(String(this._nodeOf(pid)))) out.push(pid);
    }
    return out;
  }
  isExcluded(f, pid) { return (f.excluded || []).map(String).includes(String(this._nodeOf(pid))); }

  drawFrames() {
    this.framesEl.innerHTML = "";
    for (const f of this.frames || []) {
      const r = f.rect || {};
      // S8: an auto-enabled frame renders distinctly (`.auto`). Slice 2: a human-authored
      // frame reads warm (`.mine`) — the human draws/accepts frames (S5 generalizes).
      const box = el("div", "map-frame" + (f.auto_relay ? " auto" : "") + (f.author === "human" ? " mine" : ""));
      box.dataset.frame = f.id;
      box.style.left = r.x + "%"; box.style.top = r.y + "%";
      box.style.width = r.w + "%"; box.style.height = r.h + "%";
      const tab = el("div", "map-frame-tab");
      tab.appendChild(el("span", "eyebrow map-frame-label", f.label));
      const mc = this.effectiveMembers(f).length; // §7.3: effective, not a stored list
      const auto = el("button", "map-frame-auto" + (f.auto_relay ? " on" : ""), "⚡");
      auto.title = f.auto_relay
        ? `auto-relay ON (${mc} member${mc === 1 ? "" : "s"}) — informational relays between members auto-fire; click to disable`
        : "auto-relay OFF — click to let informational (FEEDS/REVIEWS) relays between members auto-fire (active from Slice 6)";
      auto.onclick = (e) => { e.stopPropagation(); this.toggleFrameAuto(f); };
      tab.appendChild(auto);
      const lock = el("button", "map-frame-lock" + (f.promoted ? " on" : ""), f.promoted ? "🔒" : "🔓");
      lock.title = f.promoted
        ? "tracked initiative — the secretary holds a group image; click to stop tracking"
        : "promote to a tracked initiative — the secretary reasons over this group as a unit";
      lock.onclick = (e) => { e.stopPropagation(); this.togglePromote(f); };
      tab.appendChild(lock);
      const del = el("button", "map-frame-del", "✕");
      del.title = "remove this frame";
      del.onclick = (e) => { e.stopPropagation(); this.deleteFrame(f.id); };
      tab.appendChild(del);
      box.appendChild(tab);
      if (f.promoted && f.description) {
        const desc = el("div", "map-frame-desc its", f.description);
        box.appendChild(desc);
      } else if (f.promoted) {
        box.appendChild(el("div", "map-frame-desc its pending", "tracking… group image arrives next Pass B"));
      }
      const grip = el("div", "map-frame-resize"); // Slice 2: bottom-right resize handle
      box.appendChild(grip);
      this._bindFrameMove(tab, box, f);   // drag the header -> move the rect
      this._bindFrameResize(grip, box, f); // drag the grip -> resize the rect
      this.framesEl.appendChild(box);
    }
  }

  // Slice 2: drag the header to MOVE the whole frame; drag the grip to RESIZE. Both work
  // in WORLD coords — a screen drag of dx px changes the rect by dx/zoom world-px (the
  // frame lives inside #map-world). Persisted on drop; membership re-computes from the rect.
  _bindFrameMove(tab, box, f) {
    let sx, sy, base;
    const down = (e) => {
      if (e.button !== 0 || e.target.tagName === "BUTTON") return; // buttons own their clicks
      sx = e.clientX; sy = e.clientY; base = { ...f.rect };
      this.dragging = true;
      try { tab.setPointerCapture(e.pointerId); } catch { /* ok */ }
      e.stopPropagation(); // don't start a pan
    };
    const move = (e) => {
      if (base == null) return;
      const rect = this.stageRect(), z = Viewport.zoom;
      const dx = (e.clientX - sx) / rect.width * 100 / z, dy = (e.clientY - sy) / rect.height * 100 / z;
      f.rect = { ...base, x: Math.max(0, Math.min(100 - base.w, base.x + dx)), y: Math.max(0, Math.min(100 - base.h, base.y + dy)) };
      box.style.left = f.rect.x + "%"; box.style.top = f.rect.y + "%";
    };
    const up = (e) => {
      if (base == null) return;
      base = null; this.dragging = false;
      try { tab.releasePointerCapture(e.pointerId); } catch { /* ok */ }
      this.persistFrameRect(f);
    };
    tab.addEventListener("pointerdown", down);
    tab.addEventListener("pointermove", move);
    tab.addEventListener("pointerup", up);
    tab.addEventListener("pointercancel", up);
  }
  _bindFrameResize(grip, box, f) {
    let sx, sy, base;
    const down = (e) => {
      if (e.button !== 0) return;
      sx = e.clientX; sy = e.clientY; base = { ...f.rect };
      this.dragging = true;
      try { grip.setPointerCapture(e.pointerId); } catch { /* ok */ }
      e.stopPropagation();
    };
    const move = (e) => {
      if (base == null) return;
      const rect = this.stageRect(), z = Viewport.zoom;
      const dw = (e.clientX - sx) / rect.width * 100 / z, dh = (e.clientY - sy) / rect.height * 100 / z;
      f.rect = { ...base, w: Math.max(6, Math.min(100 - base.x, base.w + dw)), h: Math.max(6, Math.min(100 - base.y, base.h + dh)) };
      box.style.width = f.rect.w + "%"; box.style.height = f.rect.h + "%";
    };
    const up = (e) => {
      if (base == null) return;
      base = null; this.dragging = false;
      try { grip.releasePointerCapture(e.pointerId); } catch { /* ok */ }
      this.persistFrameRect(f);
    };
    grip.addEventListener("pointerdown", down);
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
    grip.addEventListener("pointercancel", up);
  }
  async persistFrameRect(f) {
    try {
      await fetch(`/frames/${f.id}/rect`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rect: f.rect }) });
    } catch { /* hub down — next refresh restores */ }
    this.drawEdges(); // members may have changed -> nothing edge-wise yet, but keep frame member counts fresh
    this.drawNotes(); // Slice 4: a frame-anchored note follows its moved frame without the 1.2s poll
  }

  // Slice 4: render each note as a small collapsed PIN at its anchor's world position.
  // Selective surfacing (§7 #8): the text card shows on hover, OR auto-expands when the
  // anchored node is the focused one — never all-at-once. Warm (human) / cool (secretary).
  _noteWorldPos(n) {
    const a = n.anchor || {};
    if (a.node_id) {
      const pid = this._panelOfNode(a.node_id);
      return pid && this.pos[pid];
    }
    if (a.frame_id) {
      const f = (this.frames || []).find((x) => String(x.id) === String(a.frame_id));
      return f && { x: f.rect.x + f.rect.w / 2, y: f.rect.y };
    }
    if (a.free) return { x: a.free.x, y: a.free.y };
    return null;
  }
  _panelOfNode(nid) {
    for (const p of Object.values(sessionRail.byPanel || {})) if (p && String(p.node_id) === String(nid)) return String(p.panel_id);
    return null;
  }
  drawNotes() {
    if (!this.notesEl) return;
    this.notesEl.innerHTML = "";
    for (const n of this.notes || []) {
      const p = this._noteWorldPos(n);
      if (!p) continue; // anchor not on the board (yet)
      const focused = n.anchor && n.anchor.node_id && this._panelOfNode(n.anchor.node_id) === String(focus.id);
      const card = el("div", "map-note " + (n.author === "secretary" ? "its" : "mine") + (focused ? " open" : ""));
      card.style.left = p.x + "%"; card.style.top = p.y + "%";
      card.appendChild(el("span", "map-note-pin", "✎"));
      const body = el("div", "map-note-body");
      body.appendChild(el("div", "map-note-text", n.text));
      const del = el("button", "map-note-del", "✕");
      del.title = "delete note";
      del.onclick = (e) => { e.stopPropagation(); this.deleteNote(n.id); };
      body.appendChild(del);
      card.appendChild(body);
      this.notesEl.appendChild(card);
    }
  }
  async addNote(anchor, text) {
    if (!text) return;
    try {
      await fetch("/notes", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ anchor, text }) });
    } catch { return; }
    await this.refresh();
  }
  async deleteNote(id) {
    try { await fetch(`/notes/${id}/delete`, { method: "POST" }); } catch { return; }
    this.notes = (this.notes || []).filter((n) => String(n.id) !== String(id));
    this.drawNotes();
  }
  // small inline composer at a screen point -> calls addNote with the given anchor
  _composeNote(screenX, screenY, anchor) {
    const inp = el("input", "rename-input map-note-input");
    inp.placeholder = "note…"; inp.spellcheck = false;
    inp.style.position = "fixed"; inp.style.left = screenX + "px"; inp.style.top = screenY + "px"; inp.style.zIndex = 30;
    document.body.appendChild(inp); inp.focus();
    let settled = false;
    const finish = (save) => { if (settled) return; settled = true; const t = inp.value.trim(); inp.remove(); if (save && t) this.addNote(anchor, t); };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); finish(true); } else if (e.key === "Escape") { e.preventDefault(); finish(false); } e.stopPropagation(); });
    inp.addEventListener("blur", () => finish(true));
  }

  // S8: flip a frame's per-group auto-relay switch (default OFF -> built for completeness,
  // turned on once the secretary's suggestions have proven good).
  async toggleFrameAuto(f) {
    const on = !f.auto_relay;
    try {
      const r = await fetch(`/frames/${f.id}/auto`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on }),
      });
      if (!r.ok) return;
    } catch { return; }
    f.auto_relay = on;
    this.drawFrames();
  }

  async togglePromote(f) {
    const on = !f.promoted;
    try {
      const r = await fetch(`/frames/${f.id}/promote`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on }) });
      if (!r.ok) return;
    } catch { return; }
    f.promoted = on; if (!on) f.description = "";
    this.drawFrames();
  }

  // Slice 2 (§7.3): the ONLY manual membership action — mark a positionally-inside tile
  // "not in the group" (exclude), or clear it (re-include). node-keyed server-side.
  async setFrameExclude(pid, f, exclude) {
    try {
      const r = await fetch(`/frames/${f.id}/exclude`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ panel_id: pid, op: exclude ? "add" : "remove" }),
      });
      if (!r.ok) return;
    } catch { return; }
    const nid = String(this._nodeOf(pid));
    const exc = (f.excluded || []).map(String).filter((m) => m !== nid);
    if (exclude) exc.push(nid);
    f.excluded = exc;
    this.drawFrames();
  }

  async deleteFrame(id) {
    try { await fetch(`/frames/${id}/delete`, { method: "POST" }); } catch { return; }
    this.frames = (this.frames || []).filter((f) => f.id !== id);
    this.drawFrames();
  }

  // Draw a labeled rectangle by dragging on the stage, then name it (manual frame).
  startFraming() {
    if (this.framing) { this._stopFraming(); return; } // toggle off if armed
    if (!this.visible()) setView("map");
    this.framing = true;
    this.view.classList.add("framing");
    let sx, sy, box = null, rc = null;
    const onDown = (e) => {
      if (e.button !== 0) return;
      rc = this.stageRect();
      sx = e.clientX; sy = e.clientY;
      box = el("div", "map-marquee");
      this.stage.appendChild(box);
      try { this.stage.setPointerCapture(e.pointerId); } catch { /* tracks without capture */ }
      e.preventDefault();
    };
    const onMove = (e) => {
      if (!box) return;
      const x1 = Math.min(sx, e.clientX) - rc.left, y1 = Math.min(sy, e.clientY) - rc.top;
      box.style.left = (x1 / rc.width) * 100 + "%"; box.style.top = (y1 / rc.height) * 100 + "%";
      box.style.width = (Math.abs(e.clientX - sx) / rc.width) * 100 + "%";
      box.style.height = (Math.abs(e.clientY - sy) / rc.height) * 100 + "%";
    };
    const onUp = (e) => {
      try { this.stage.releasePointerCapture(e.pointerId); } catch { /* ok */ }
      const handlers = { onDown, onMove, onUp };
      this._stopFraming(handlers);
      if (!box) return;
      const w = Math.abs(e.clientX - sx), h = Math.abs(e.clientY - sy);
      if (w < 30 || h < 30) { box.remove(); return; } // too small — treat as a mis-click
      // F-COORD: the marquee was drawn in SCREEN px; convert its corners to WORLD coords
      // so the frame lands correctly under pan/zoom (the frame renders inside #map-world).
      const sxl = Math.min(sx, e.clientX) - rc.left, syt = Math.min(sy, e.clientY) - rc.top;
      const p0 = Viewport.toWorld(sxl, syt);
      const p1 = Viewport.toWorld(sxl + w, syt + h);
      this._promptFrameLabel(
        { x: p0.x, y: p0.y, w: p1.x - p0.x, h: p1.y - p0.y },
        box,
      );
    };
    this._framingHandlers = { onDown, onMove, onUp };
    this.stage.addEventListener("pointerdown", onDown);
    this.stage.addEventListener("pointermove", onMove);
    this.stage.addEventListener("pointerup", onUp);
  }

  _stopFraming() {
    this.framing = false;
    this.view.classList.remove("framing");
    const h = this._framingHandlers;
    if (h) {
      this.stage.removeEventListener("pointerdown", h.onDown);
      this.stage.removeEventListener("pointermove", h.onMove);
      this.stage.removeEventListener("pointerup", h.onUp);
      this._framingHandlers = null;
    }
  }

  cancelFraming() {
    this._stopFraming();
    this.stage.querySelectorAll(".map-marquee").forEach((m) => m.remove());
  }

  // turn the drawn marquee into a named, persisted frame
  _promptFrameLabel(rect, box) {
    box.classList.add("naming");
    const input = el("input", "rename-input map-frame-input");
    input.placeholder = "label this group…";
    input.spellcheck = false;
    box.appendChild(input);
    input.focus();
    let settled = false;
    const finish = async (save) => {
      if (settled) return;
      settled = true;
      const label = input.value.trim();
      box.remove();
      if (save && label) {
        try {
          const j = await (await fetch("/frames", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ label, rect }),
          })).json();
          if (j && j.frame) this.frames.push(j.frame);
        } catch { /* hub down — frame not created */ }
      }
      this.drawFrames();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
      e.stopPropagation();
    });
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("pointerdown", (e) => e.stopPropagation());
  }

  // ---- secretary frame suggestions (accept → a frame bounding the members) ----
  renderSuggests() {
    if (!this.suggestsEl) return;
    this.suggestsEl.innerHTML = "";
    for (const fs of frameSuggests || []) {
      const pts = (fs.panels || []).map((p) => this.pos[p]).filter(Boolean);
      if (pts.length < 2) continue; // members not on the board (yet) — nothing to wrap
      const cx = pts.reduce((a, p) => a + p.x, 0) / pts.length;
      const topY = Math.min(...pts.map((p) => p.y));
      const chip = el("div", "map-suggest-chip");
      chip.style.left = cx + "%";
      chip.style.top = Math.max(12, topY - 10) + "%";
      chip.appendChild(el("span", "eyebrow map-suggest-lead", "frame →"));
      chip.appendChild(el("span", "map-suggest-label", `${fs.label} · ${pts.length}`));
      const ok = el("button", "map-suggest-ok", "✓ frame");
      const no = el("button", "map-suggest-no", "✕");
      ok.title = "draw a frame around this group";
      no.title = "dismiss the suggestion";
      ok.onclick = (e) => { e.stopPropagation(); this.acceptFrameSuggest(fs); };
      no.onclick = (e) => { e.stopPropagation(); dismissFrameSuggest(fs); };
      chip.appendChild(ok);
      chip.appendChild(no);
      this.suggestsEl.appendChild(chip);
    }
    // S1: secretary relay proposals — a ✓ Route chip floated at the from→to midpoint.
    const nm = (pid) => { const s = sessionRail.byPanel[pid]; return (s && s.name) || ("session " + pid); };
    for (const rs of relaySuggests || []) {
      const a = this.pos[rs.from], b = this.pos[rs.to];
      if (!a || !b) continue; // an endpoint isn't on the board — nothing to anchor to
      const color = relColor(rs.type);
      const chip = el("div", "map-relay-chip");
      chip.style.left = (a.x + b.x) / 2 + "%";
      chip.style.top = Math.max(8, (a.y + b.y) / 2 - 6) + "%";
      chip.style.borderColor = color;
      if (rs.reason) chip.title = rs.reason; // the secretary's WHY, on hover
      const lead = el("span", "eyebrow map-relay-lead", rs.type);
      lead.style.color = color;
      chip.appendChild(lead);
      chip.appendChild(el("span", "map-relay-label", `${nm(rs.from)} → ${nm(rs.to)}`));
      const ok = el("button", "map-relay-ok", "✓ route");
      const no = el("button", "map-relay-no", "✕");
      ok.title = "deliver the source's latest output into the target (verbatim)";
      no.title = "dismiss the suggestion";
      ok.onclick = (e) => { e.stopPropagation(); this.acceptRelaySuggest(rs); };
      no.onclick = (e) => { e.stopPropagation(); dismissRelaySuggest(rs); };
      chip.appendChild(ok);
      chip.appendChild(no);
      this.suggestsEl.appendChild(chip);
    }
  }

  // S1: accept a relay proposal -> POST the existing /route. We send {from_panel,
  // to_panel} + the chip's rel_type (relayfix MED-2: so the EXPLICIT edge keeps the
  // secretary's REVIEWS/DEPENDS/CONFLICT classification instead of being clobbered to
  // FEEDS, matching the auto-dispatch path). We deliberately send NO body `text`: the
  // server resolves the verbatim body from the source's CURRENT last_message (no
  // client-side copy / truncation). Then dismiss the item (mirrors the name/frame
  // accept flow) so the next 2.5s poll doesn't re-add it.
  async acceptRelaySuggest(rs) {
    try {
      const r = await fetch("/route", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_panel: rs.from, to_panel: rs.to, rel_type: rs.type }),
      });
      if (!r.ok) return; // target busy / no session / source empty — leave the chip up
      await fetch(`/secretary/dismiss/${rs.id}`, { method: "POST" });
    } catch { return; }
    relaySuggests = relaySuggests.filter((s) => s.id !== rs.id);
    this.renderSuggests();
  }

  async acceptFrameSuggest(fs) {
    const pts = (fs.panels || []).map((p) => this.pos[p]).filter(Boolean);
    if (pts.length < 2) return;
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const padX = 9, padY = 8; // % padding so the ~200px tiles sit inside the frame
    let x = Math.max(0, Math.min(...xs) - padX), y = Math.max(0, Math.min(...ys) - padY);
    const w = Math.min(100 - x, Math.max(...xs) - Math.min(...xs) + padX * 2);
    const h = Math.min(100 - y, Math.max(...ys) - Math.min(...ys) + padY * 2);
    try {
      const j = await (await fetch("/frames", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: fs.label, rect: { x, y, w, h } }),
      })).json();
      if (j && j.frame) this.frames.push(j.frame);
      await fetch(`/secretary/dismiss/${fs.id}`, { method: "POST" });
    } catch { return; }
    frameSuggests = frameSuggests.filter((s) => s.id !== fs.id);
    this.render();
  }
}

// ============================================================= S3: CONTEXT + QUOTA
// Per-session context heat (green→amber→red toward the auto-compact line) and the
// GLOBAL subscription quota readout (one HUD surface, never per node).
function ctxHeat(pct, compact) {
  const c = compact != null ? compact : 0.92; // default compact line if unknown (e.g. after restart)
  if (pct >= c) return "hot";
  if (pct >= c * 0.75) return "warm";
  return "";
}

function fmtReset(epoch) {
  if (!epoch) return "";
  const ms = epoch * 1000 - Date.now();
  if (ms <= 0) return "now";
  const m = Math.round(ms / 60000);
  if (m < 60) return m + "m";
  return Math.floor(m / 60) + "h" + (m % 60 ? (m % 60) + "m" : "");
}

let lastUsage = null;

async function pollUsage() {
  try {
    lastUsage = await (await fetch("/usage")).json();
  } catch {
    return; // hub down — conn dot handles it
  }
  renderQuota(lastUsage);
}

// The 5-hour window is the one the headless stream carries. Show its reset; add a
// utilization % ONLY if actually present (it isn't emitted below the warning line —
// so we never fabricate a Session/Weekly percentage).
function quotaText(u) {
  const s = u && u.session;
  if (!s) return null;
  let txt = "5h ";
  if (typeof s.utilization === "number") txt += Math.round(s.utilization * 100) + "% · ";
  const reset = fmtReset(s.resets_at);
  txt += reset ? "resets " + reset : s.status || "allowed";
  return txt;
}

function renderQuota(u) {
  const slot = $("map-hud-slot");
  const badge = $("quota-warn");
  const txt = quotaText(u);
  const warn = !!(u && u.warning);
  // Map HUD slot: quiet reset readout once a rate_limit_event lands; lit only at warning.
  if (slot && txt) {
    slot.innerHTML = "";
    slot.appendChild(el("span", "eyebrow", warn ? "⚠ quota" : "◷ quota"));
    slot.appendChild(el("span", "quota", txt));
    slot.classList.add("live");
    slot.classList.toggle("warn", warn);
  }
  // Masthead badge: hidden unless a near-limit warning has fired (surfaces it in
  // the daily-driver Rail view without switching to the Map).
  if (badge) {
    badge.classList.toggle("hidden", !warn);
    if (warn) badge.textContent = "⚠ " + (txt || "near limit");
  }
}

async function pingHealth() {
  const conn = $("conn");
  try {
    const j = await (await fetch("/health")).json();
    if (j.status === "ok") {
      conn.classList.add("up");
      conn.title = `hub up (step ${j.step})`;
      if (j.default_cwd) HUB_DEFAULT_CWD = j.default_cwd;
      return;
    }
  } catch {
    /* fallthrough */
  }
  conn.classList.remove("up");
  conn.title = "hub unreachable";
}

const focus = new FocusView();
const sessionRail = new SessionRail();
const rail = new Rail();
const mapView = new MapView();

// ---- view toggle: Rail (1A) <-> Map (4A), persisted in localStorage --------
const VIEW_KEY = "hub.view";
const RAIL_LEDE =
  "Every session on a grouped rail; focus one and read it like correspondence — the advisor consults from the right.";
const MAP_LEDE = "Sessions sit in workstream lanes; the secretary wires their relations as bold orthogonal traces.";

function setView(v) {
  const map = v === "map";
  $("map-view").classList.toggle("hidden", !map);
  document.querySelector(".frame").classList.toggle("hidden", map);
  document.body.classList.toggle("on-map", map); // reveal the masthead map controls
  $("view-map").classList.toggle("on", map);
  $("view-rail").classList.toggle("on", !map);
  $("masthead-tag").textContent = map ? "Switchboard" : "Reading Room";
  $("masthead-lede").textContent = map ? MAP_LEDE : RAIL_LEDE;
  try { localStorage.setItem(VIEW_KEY, v); } catch { /* private mode — non-fatal */ }
  if (map) mapView.refresh();
  if (map && lastUsage) renderQuota(lastUsage); // paint the HUD quota slot on switch
}

$("view-rail").addEventListener("click", () => setView("rail"));
$("view-map").addEventListener("click", () => setView("map"));
setView(localStorage.getItem(VIEW_KEY) || "rail");

pingHealth();
setInterval(pingHealth, 5000);
pollUsage(); // S3: global subscription quota (5h reset + near-limit warning)
setInterval(pollUsage, 5000);

// ---- S4: secretary board mode — Full runs the Sonnet cross-session pass; Quiet
// skips it to save cost. A glyph + word carry the state (never colour-alone); an
// amber tint is a redundant "throttled" cue. One toggle button, persisted server-side.
let boardMode = "full";
function renderBoardMode() {
  const btn = $("map-mode");
  if (!btn) return;
  const quiet = boardMode === "quiet";
  btn.textContent = quiet ? "◐ quiet" : "◑ full";
  btn.style.color = quiet ? "var(--amber)" : "";
  btn.style.borderColor = quiet ? "var(--amber)" : "";
  btn.title = quiet
    ? "secretary QUIET — skipping cross-session triage/relay/frame to save cost; click for Full"
    : "secretary FULL — running cross-session triage/relay/frame; click for Quiet (saves cost)";
}
async function loadBoardMode() {
  try { boardMode = (await (await fetch("/board/mode")).json()).mode || "full"; } catch { return; }
  renderBoardMode();
}
async function toggleBoardMode() {
  const next = boardMode === "full" ? "quiet" : "full";
  try {
    const j = await (await fetch("/board/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: next }),
    })).json();
    boardMode = j.mode || next;
  } catch { return; } // hub down — leave the button as-is
  renderBoardMode();
}
$("map-mode").addEventListener("click", toggleBoardMode);
loadBoardMode();

// ============================================================= S4/S5: ACTIONABLE CANVAS
// Right-click skill actions (/review · /design-partner), secretary-proposed rename
// the user accepts, and inline rename (S4) — plus the freeform-canvas controls the
// S5 engine now wires live: drag-to-pin, Auto-arrange, Frame a group, and per-node
// Reset to lane. (Free-floating "notes" remain a future nicety, deliberately out
// of this slice's success criteria.)

// ---- floating context menu (single reused element) ----
let ctxEl = null;
function closeCtxMenu() { if (ctxEl) { ctxEl.remove(); ctxEl = null; } }
function openCtxMenu(x, y, items) {
  closeCtxMenu();
  const menu = el("div", "ctx-menu");
  for (const it of items) {
    if (it.sep) { menu.appendChild(el("div", "ctx-sep")); continue; }
    const row = el("div", "ctx-item" + (it.disabled ? " disabled" : "") + (it.danger ? " danger" : ""));
    row.appendChild(el("span", "ctx-label", it.label));
    if (it.hint) row.appendChild(el("span", "ctx-hint", it.hint));
    if (!it.disabled) row.onclick = () => { closeCtxMenu(); it.onClick(); };
    menu.appendChild(row);
  }
  document.body.appendChild(menu);
  // keep the menu on-screen
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.min(x, window.innerWidth - r.width - 6) + "px";
  menu.style.top = Math.min(y, window.innerHeight - r.height - 6) + "px";
  ctxEl = menu;
}
document.addEventListener("click", closeCtxMenu);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeCtxMenu(); });
window.addEventListener("blur", closeCtxMenu);
document.addEventListener("scroll", closeCtxMenu, true);
// Escape cancels an in-progress group-frame draw (S5).
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && mapView.framing) mapView.cancelFraming(); });

// ---- spawn a /review or /design-partner session from a source node ----
async function act(pid, action) {
  let r;
  try {
    r = await fetch(`/act/${pid}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
  } catch {
    alert("hub unreachable");
    return;
  }
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    alert(j.error || `action failed (${r.status})`);
    return;
  }
  // refresh so the spawned node + its reviews edge appear immediately
  await sessionRail.refresh();
  if (mapView.visible()) await mapView.refresh();
}

// ---- inline rename (double-click a name, or the node menu) ----
function startInlineRename(nameEl, pid, currentName) {
  if (!nameEl || inlineEditing) return;
  inlineEditing = true;
  const input = el("input", "rename-input");
  input.value = currentName || nameEl.textContent || "";
  input.spellcheck = false;
  nameEl.replaceWith(input);
  input.focus();
  input.select();
  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    const name = input.value.trim();
    inlineEditing = false;
    if (save && name && name !== currentName) {
      try {
        await fetch(`/rename/${pid}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
      } catch { /* hub down — the next poll restores the old name */ }
    }
    await sessionRail.refresh();
    if (mapView.visible()) await mapView.refresh();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    e.stopPropagation();
  });
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("click", (e) => e.stopPropagation());
}

// ---- secretary name-suggestion accept / dismiss ----
async function acceptSuggestion(pid, sug) {
  try {
    await fetch(`/rename/${pid}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: sug.name }),
    });
    await fetch(`/secretary/dismiss/${sug.id}`, { method: "POST" });
  } catch { return; }
  delete suggestMap[pid];
  sessionRail.refresh();
}
async function dismissSuggestion(sug) {
  try { await fetch(`/secretary/dismiss/${sug.id}`, { method: "POST" }); } catch { return; }
  for (const k of Object.keys(suggestMap)) if (suggestMap[k].id === sug.id) delete suggestMap[k];
  sessionRail.refresh();
}

// S5: the secretary PROPOSES a frame for >=2 sessions on one plan; the user accepts
// (✓ frame) to draw it, or dismisses it. Mirrors the name-suggestion accept flow.
function dismissFrameSuggest(fs) {
  fetch(`/secretary/dismiss/${fs.id}`, { method: "POST" }).catch(() => {});
  frameSuggests = frameSuggests.filter((s) => s.id !== fs.id);
  if (mapView.visible()) mapView.renderSuggests();
}

// S1: dismiss a relay proposal (server-side, so the next poll can't re-add it).
function dismissRelaySuggest(rs) {
  fetch(`/secretary/dismiss/${rs.id}`, { method: "POST" }).catch(() => {});
  relaySuggests = relaySuggests.filter((s) => s.id !== rs.id);
  if (mapView.visible()) mapView.renderSuggests();
}

async function pollSuggestions() {
  let items;
  try { items = (await (await fetch("/secretary/feed")).json()).items || []; }
  catch { return; }
  const next = {};
  const fnext = [];
  const rnext = [];
  for (const it of items) {
    if (it.kind === "name_suggestion" && it.panel && it.name) next[it.panel] = { id: it.id, name: it.name };
    else if (it.kind === "frame_suggestion" && it.label && (it.panels || []).length >= 2)
      fnext.push({ id: it.id, label: it.label, panels: it.panels.map(String) });
    else if (it.kind === "relay" && it.from && it.to) // S1: a relay proposal -> a ✓ Route chip
      rnext.push({ id: it.id, from: String(it.from), to: String(it.to), type: it.type || "FEEDS", reason: it.reason || "" });
  }
  suggestMap = next;
  frameSuggests = fnext;
  relaySuggests = rnext;
  if (mapView.visible()) mapView.renderSuggests();
}
pollSuggestions();
setInterval(pollSuggestions, 2500);

// ---- delete a session (rail ✕ + map node menu) ----
// Removes the panel from the board: its tile, edges, frame-membership, notes,
// canvas pin, brief, and any running turn. The underlying claude conversation on
// disk is NOT deleted. The freed slot id becomes reusable.
async function deleteSession(pid, name) {
  const label = name || `session ${pid}`;
  if (!confirm(`Delete "${label}"?\n\nThis removes it from the board (tile, edges, notes, position) and stops any running turn. The claude conversation on disk is kept.`)) return;
  try {
    await fetch(`/session/${pid}/delete`, { method: "POST" });
  } catch { return; } // hub down — next poll reflects reality
  if (focus.id === pid) { focus.disconnect(); focus.id = null; focus.session = null; focus.show("empty"); }
  await sessionRail.refresh();
  if (mapView.visible()) await mapView.refresh();
}

// ---- node context menu (Map tile) ----
function nodeMenu(e, pid) {
  const s = sessionRail.byPanel[pid] || null;
  const pinned = !!(s && typeof s.x === "number" && typeof s.y === "number");
  const items = [
    { label: "Open in Rail", onClick: () => { setView("rail"); focus.open(pid, s); } },
    { label: "Route to…", onClick: () => { setView("rail"); focus.open(pid, s); focus.toggleRoute(true); } },
    { sep: true },
    { label: "Review", hint: "/review", onClick: () => act(pid, "review") },
    { label: "Discuss", hint: "/design-partner", onClick: () => act(pid, "discuss") },
    { sep: true },
    { label: "Rename", onClick: () => {
        const tile = mapView.nodesEl.querySelector(`.map-tile[data-panel="${pid}"]`);
        startInlineRename(tile && tile.querySelector(".map-tile-name"), pid, s && s.name);
      } },
    { label: "Add note", hint: "to this session", onClick: () => {
        const t = mapView.nodesEl.querySelector(`.map-tile[data-panel="${pid}"]`);
        const r = t ? t.getBoundingClientRect() : { left: e.clientX, top: e.clientY };
        mapView._composeNote(r.left, r.top, { panel_id: pid });
      } },
  ];
  // a pinned node can be sent back to its deterministic lane slot (per-node tidy)
  if (pinned) items.push({ label: "Reset to lane", hint: "unpin", onClick: () => mapView.resetNode(pid) });
  // Slice 2 (§7.3): membership is POSITIONAL — show the exclude toggle ONLY for frames
  // whose rect geometrically contains this tile. "Not in the group" excludes; "In the
  // group" re-includes an excluded tile.
  const frames = mapView.frames || [];
  const here = mapView.pos[pid];
  const containing = frames.filter((f) => mapView._inRect(here, f.rect || {}));
  if (containing.length) {
    items.push({ sep: true });
    for (const f of containing) {
      const excluded = mapView.isExcluded(f, pid);
      items.push({
        label: (excluded ? "In the group: " : "Not in the group: ") + f.label,
        hint: excluded ? "re-include" : "exclude",
        onClick: () => mapView.setFrameExclude(pid, f, !excluded),
      });
    }
  }
  // destructive — always last, behind a separator + a confirm() in deleteSession
  items.push({ sep: true });
  items.push({ label: "Delete session", hint: "remove from board", danger: true,
    onClick: () => deleteSession(pid, s && s.name) });
  openCtxMenu(e.clientX, e.clientY, items);
}

// ---- empty-canvas context menu (Map stage) ----
function canvasMenu(e) {
  openCtxMenu(e.clientX, e.clientY, [
    { label: "New session", onClick: () => { setView("rail"); focus.openLaunch(sessionRail.nextFreeId()); } },
    { sep: true },
    { label: "Frame a group", hint: "drag", onClick: () => mapView.startFraming() },
    { label: "Auto-arrange", hint: "tidy", onClick: () => mapView.autoArrange() },
    { label: "Add note here", hint: "note", onClick: () => {
        const sr = mapView.stageRect();
        const w = Viewport.toWorld(e.clientX - sr.left, e.clientY - sr.top);
        mapView._composeNote(e.clientX, e.clientY, { free: { x: w.x, y: w.y } });
      } },
  ]);
}

// ---- bind: contextmenu on the Map stage (delegated to survive node re-renders) ----
$("map-stage").addEventListener("contextmenu", (e) => {
  e.preventDefault();
  const tile = e.target.closest(".map-tile");
  if (tile && tile.dataset.panel != null) nodeMenu(e, tile.dataset.panel);
  else canvasMenu(e);
});

// ---- bind: double-click a name to rename (rail rows + map tiles) ----
$("session-rail").addEventListener("dblclick", (e) => {
  const nameEl = e.target.closest(".srow-name");
  if (!nameEl) return;
  const row = e.target.closest(".srow");
  if (!row) return;
  e.stopPropagation();
  startInlineRename(nameEl, row.dataset.panel, nameEl.textContent);
});
$("map-nodes").addEventListener("dblclick", (e) => {
  const nameEl = e.target.closest(".map-tile-name");
  if (!nameEl) return;
  const tile = e.target.closest(".map-tile");
  if (!tile) return;
  e.stopPropagation();
  startInlineRename(nameEl, tile.dataset.panel, nameEl.textContent);
});
