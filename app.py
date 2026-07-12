"""claude-hub — local web board coordinating headless `claude -p` sessions.

Routes:
  GET  /                     the board
  GET  /health               liveness
  GET  /sessions             snapshot of all panels (name/state/session_id/last)
  POST /launch/<panel_id>    {name, cwd, prompt} -> spawn a fresh session
  POST /reply/<panel_id>     {text} -> --resume the panel's session
  GET  /stream/<panel_id>    SSE: persistent per-panel event channel
  POST /permission/request           (from MCP bridge) surface a 🟡 approval
  GET  /permission/poll/<id>         (from MCP bridge) long-poll for the decision
  POST /permission/decide/<id>       (from UI) allow/deny
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from hub import advisor, nodes, permissions, secretary, sessions, state

# Import rate_limit_snapshot by name: the `/sessions` view function below rebinds
# the module global `sessions` to itself, so `sessions.rate_limit_snapshot` would
# break in any route defined after it.
from hub.sessions import MANAGER, rate_limit_snapshot

BUSY_STATES = {"running", "awaiting-approval"}


def _edge_nodes(src: str, dst: str) -> tuple[str | None, str | None]:
    """F-IDENT: resolve a panel-keyed (src,dst) to the node_id endpoints the relations
    store now uses. Returns (None, None)-bearing tuple if either has no node yet."""
    return nodes.node_id_for_panel(str(src)), nodes.node_id_for_panel(str(dst))


def _on_turn_complete(session) -> None:
    """Event-driven secretary pass — off-thread so it never blocks the turn."""
    # S7: re-surface any relays QUEUED while this panel was busy as confirm chips (the
    # manual ✓-route path) — NEVER auto-sent. Wired HERE, the server hook, NOT set_state:
    # a just-completed panel is done/awaiting-input, never idle, so a state=='idle' check
    # would never fire. Runs synchronously (a cheap dict-pop + FEED insert) and is guarded
    # so it can never stop the secretary pass below.
    try:
        secretary.resurface_pending_relays(str(session.panel_id))
    except Exception:  # noqa: BLE001 — re-surface must never break the turn-complete hook
        pass
    board = MANAGER.snapshot()
    threading.Thread(
        target=secretary.observe,
        args=(session.panel_id, session.name, session.last_message, board),
        daemon=True,
    ).start()


sessions.TURN_COMPLETE_HOOK = _on_turn_complete


def _auto_dispatch_relay(src: str, dst: str, rel_type: str, reason: str) -> bool:
    """S8 hub-as-executor for an AUTO-ELIGIBLE relay (the secretary already verified all
    four auto-conditions: informational type, non-augmented, shared auto-frame membership,
    target not busy). Reuses /route's contract: the body is re-resolved SERVER-SIDE from
    the source's CURRENT last_message (verbatim #13 — no stale copy rides in), an EXPLICIT
    edge is recorded (the hub delivered it, like a human /route), and source/target get the
    routed/relay_in markers PLUS an `auto_routed` marker so the UI can show 'auto-routed ·
    undo'. Auto relays are NEVER augmented, so no human section is ever prepended. Returns
    False (the caller falls back to the S7 queue) when the source vanished / has no body,
    or the target RACED to busy / lost its session between the check and here."""
    src, dst = str(src), str(dst)
    a = MANAGER.get(src)
    b = MANAGER.get(dst)
    if a is not None:
        a.ensure_rehydrated()  # relayfix MED-1: lift the FULL verbatim from the transcript post-restart
    verbatim = (a.last_message if a else "").strip()
    if not verbatim or b is None or not b.session_id or b.state in BUSY_STATES:
        return False
    MANAGER.reply(dst, verbatim)
    # F-IDENT: node-keyed edge. (This hook is DORMANT until Slice 6 restores auto-relay;
    # translating now keeps every relation writer coherent for when it returns.)
    nsrc, ndst = _edge_nodes(src, dst)
    if nsrc and ndst:
        state.add_relation(nsrc, ndst, rel_type, "explicit", reason)
    src_name = a.name if a is not None else src
    a.broadcast({"kind": "routed", "to": dst, "to_name": b.name, "chars": len(verbatim), "augmented": False, "auto": True})
    b.broadcast({"kind": "relay_in", "from": src, "from_name": src_name, "chars": len(verbatim), "augmented": False, "auto": True})
    # S8: the undoable auto-routed marker — UI-side only (the delivered payload above stays
    # clean; this marker is a separate hub-event, never sent to the model — PLAN §3).
    a.broadcast({"kind": "auto_routed", "from": src, "to": dst, "to_name": b.name, "type": rel_type})
    return True


secretary.AUTO_RELAY_DISPATCH_HOOK = _auto_dispatch_relay

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
PORT = 5055

# Sessions launch here unless a cwd is given. --resume is cwd-scoped, so this is
# the directory session work happens in. Point the hub at whatever project you
# want it to drive, in priority order:
#   1. the CLAUDE_HUB_CWD environment variable, else
#   2. a local `.hub_cwd` file next to app.py (one line, git-ignored), else
#   3. the hub's own root.
def _default_cwd() -> str:
    env = os.environ.get("CLAUDE_HUB_CWD")
    if env and Path(env).is_dir():
        return env
    local = ROOT / ".hub_cwd"
    if local.is_file():
        p = local.read_text(encoding="utf-8").strip()
        if p and Path(p).is_dir():
            return p
    return str(ROOT)

DEFAULT_CWD = _default_cwd()

app = Flask(__name__, static_folder=None)

# --- S4: localhost-only guard for mutating requests ---------------------------
# The actionable canvas makes spawning a one-click affair, so a cross-origin page
# must not be able to drive the hub. Pin to the HOST header (not Origin): the 🟡
# permission MCP bridge (hub/mcp_server.py) reaches /permission/* over urllib
# with NO Origin header — an Origin guard would kill the approve flow — but its
# Host is always 127.0.0.1:5055, so a Host check passes it cleanly. GET/HEAD are
# never guarded (read-only; the bridge's poll is a GET).
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_LOOPBACK_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def _is_local_host(host: str) -> bool:
    host = (host or "").strip()
    if not host:
        return False
    # strip a trailing :port, handling bracketed IPv6 ([::1]:5055)
    hostname = host[1:].split("]", 1)[0] if host.startswith("[") else host.split(":", 1)[0]
    return hostname in _LOOPBACK_HOSTNAMES


@app.before_request
def _localhost_only():
    if request.method in _SAFE_METHODS:
        return None
    if _is_local_host(request.headers.get("Host", "")):
        return None
    return jsonify(error="forbidden: non-local Host"), 403


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/health")
def health():
    return jsonify(status="ok", service="claude-hub", step=8, default_cwd=DEFAULT_CWD)


@app.route("/sessions")
def sessions():
    # snapshot now carries per-session ctx_pct / ctx_compact_pct (S3).
    # S5: merge each panel's freeform-canvas pin (x/y in 0..100 stage-percent) so
    # the Map restores manual positions on load. A panel with no pin omits x/y and
    # falls back to the deterministic lane layout client-side.
    panels = MANAGER.snapshot()
    pos = state.get_positions()
    sizes = state.get_sizes()  # RAIL-TYPE: per-node manual w/h (SE-corner resize)
    pending = state.get_pending_counts()  # S7: queued-relay count per target tile (badge)
    for p in panels:
        xy = pos.get(str(p["panel_id"]))
        if xy:
            p["x"], p["y"] = xy.get("x"), xy.get("y")
        wh = sizes.get(str(p["panel_id"]))
        if wh:
            p["w"], p["h"] = wh.get("w"), wh.get("h")
        c = pending.get(str(p["panel_id"]))
        if c:
            p["pending"] = c
    return jsonify(panels=panels)


@app.route("/usage")
def usage():
    """Global subscription quotas captured from rate_limit_event: 5-hour / 7-day
    reset + a near-limit warning flag. GLOBAL — one HUD readout, never per node.
    Empty until a rate_limit_event has been seen (no turn run yet)."""
    return jsonify(rate_limit_snapshot())


@app.route("/launch/<panel_id>", methods=["POST"])
def launch(panel_id: str):
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    name = (data.get("name") or f"session {panel_id}").strip()
    cwd = (data.get("cwd") or DEFAULT_CWD).strip()
    model = (data.get("model") or "").strip()  # "" = CLI/account default; else a --model alias/id
    if not prompt:
        return jsonify(error="prompt required"), 400
    if not Path(cwd).is_dir():
        return jsonify(error=f"cwd not a directory: {cwd}"), 400
    MANAGER.launch(panel_id, name, cwd, prompt, model)
    return jsonify(ok=True, panel_id=panel_id)


@app.route("/session/<panel_id>/delete", methods=["POST"])
def session_delete(panel_id: str):
    """Hard-delete a session/panel from the board (rail ✕ / map node menu). Kills
    any live turn and removes the panel + node + edges/frames/notes + position.
    The on-disk claude conversation is intentionally left untouched."""
    removed = MANAGER.delete(panel_id)
    return jsonify(ok=True, panel_id=panel_id, removed=removed)


@app.route("/reply/<panel_id>", methods=["POST"])
def reply(panel_id: str):
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="text required"), 400
    s = MANAGER.get(panel_id)
    if s is None or not s.session_id:
        return jsonify(error="no session to reply to"), 409
    if s.state in BUSY_STATES:
        return jsonify(error="session is busy"), 409
    MANAGER.reply(panel_id, text)
    return jsonify(ok=True, panel_id=panel_id)


@app.route("/fork/<panel_id>", methods=["POST"])
def fork(panel_id: str):
    """Slice 7: fork-with-handoff. Retire the panel's context-heavy session and continue
    the same work in a fresh session on the SAME tile/node/edges, seeded with a handoff
    summary. The node is PRESERVED (repoint), never re-minted."""
    s = MANAGER.fork_with_handoff(panel_id)
    if s is None:
        return jsonify(error="nothing to fork (no session, or busy)"), 409
    return jsonify(ok=True, panel_id=panel_id)


@app.route("/route", methods=["POST"])
def route():
    """Relay (Goal 1): deliver source's captured output into target via --resume.

    S6 VERBATIM CONTRACT: the delivered body is ALWAYS resolved server-side from the
    source's CURRENT last_message. A client `text` override is IGNORED (#13) so a
    stale/spoofed copy can't ride in as the payload. The ONLY client-supplied framing
    is an opt-in, human-authored `section` PREPENDED to the verbatim body. NO
    hub-generated provenance ever enters the payload — the target processes this via
    --resume and would treat hub metadata as instruction (PLAN §3 derailment).
    Provenance ([from <source>]) lives on the UI edge/feed only (see broadcasts below)."""
    data = request.get_json(silent=True) or {}
    src = str(data.get("from_panel", ""))
    dst = str(data.get("to_panel", ""))
    a = MANAGER.get(src)
    b = MANAGER.get(dst)
    if a is not None:
        a.ensure_rehydrated()  # relayfix MED-1: post-restart, restore the FULL verbatim from the transcript
    # relayfix MED-2: an OPTIONAL relay type carried by a typed ✓-route chip
    # (REVIEWS/DEPENDS/CONFLICT). Validated against the taxonomy; absent for the plain
    # route-bar / right-click route, in which case add_relation PRESERVES any existing
    # inferred type instead of clobbering it to a bare FEEDS default.
    rtype = str(data.get("rel_type") or "").upper()
    rel_type = rtype if rtype in secretary.RELATION_TYPES else None
    # Server-resolved verbatim body — NOT data["text"] (#13: client body override ignored).
    verbatim = (a.last_message if a else "").strip()
    if not verbatim:
        return jsonify(error="source has no output to route"), 400
    if b is None or not b.session_id:
        return jsonify(error="target has no session"), 409
    # S6: opt-in human-authored framing only (default empty). Prepended to the verbatim
    # body — this is the SANCTIONED channel for context (the human writes + approves it,
    # so it is not hub-injected metadata). Still no hub provenance string in the payload.
    section = (data.get("section") or "").strip()
    if b.state in BUSY_STATES:
        # S7: target BUSY -> QUEUE the relay (a count badge on its tile) instead of a 409.
        # It re-surfaces as a confirm chip when the target next COMPLETES a turn (the
        # TURN_COMPLETE_HOOK), and is NEVER auto-sent — the body is re-resolved server-side
        # on accept, so a now-stale approval can't ride in. ONLY the busy 409 becomes a
        # queue; the no-session 409 above stays HARD (a panel with no session can't hold a
        # queued relay). The queue is transient (state._PENDING_RELAYS) — lost on restart.
        state.add_pending_relay(
            dst,
            {
                "from": src,
                "to": dst,
                # relayfix MED-2: keep the typed chip's type through the queue so the
                # re-surfaced confirm chip + its eventual edge don't degrade to FEEDS.
                "type": rel_type or "FEEDS",
                "reason": "queued while the target was busy",
                "src_hash": secretary._hash_text(verbatim),
            },
        )
        if a is not None:
            a.broadcast({"kind": "queued", "to": dst, "to_name": b.name})
        return jsonify(ok=True, queued=True, to=dst)
    text = f"{section}\n\n{verbatim}" if section else verbatim
    MANAGER.reply(dst, text)
    # A human routed this — record it as an EXPLICIT (hard/solid) Map edge (S2). The edge
    # itself IS the provenance (from -> to) on the Map. relayfix MED-2: pass the chip's
    # type when it had one (else None -> add_relation keeps any inferred type, or inserts FEEDS).
    nsrc, ndst = _edge_nodes(src, dst)  # F-IDENT: relations are node-keyed
    if nsrc and ndst:
        state.add_relation(nsrc, ndst, rel_type, "explicit")
    src_name = a.name if a is not None else src
    if a is not None:
        a.broadcast({"kind": "routed", "to": dst, "to_name": b.name, "chars": len(text), "augmented": bool(section)})
    # S6: provenance on the TARGET's feed — [from <source>] — UI-side only. The delivered
    # payload (text above) stays clean; this marker is a separate hub-event, never sent to
    # the model. relay_in is not a _HISTORY_KIND, matching the ephemeral `routed` marker.
    b.broadcast({"kind": "relay_in", "from": src, "from_name": src_name, "chars": len(text), "augmented": bool(section)})
    return jsonify(ok=True, to=dst, chars=len(text))


# S4 right-click skill actions: a slash-launch (preserved by sessions.launch) of a
# skill that DOES expand in headless -p (verified: /review + /design-partner).
_SKILL_FOR_ACTION = {"review": "/review", "discuss": "/design-partner"}


def _next_free_panel() -> str:
    """Lowest id not held by a launched session — mirrors the front-end's slot
    model (an `ensure`-created empty placeholder, no session_id, is reusable)."""
    i = 0
    while True:
        s = MANAGER.get(str(i))
        if s is None or not s.session_id:
            return str(i)
        i += 1


@app.route("/act/<panel_id>", methods=["POST"])
def act(panel_id: str):
    """Right-click action (Goal: actionable canvas): spawn a NEW session running
    /review or /design-partner, seeded with the source's last complete message,
    and record a typed REVIEWS edge (new -> source) so it draws on the Map.
    Composes existing primitives: slash-launch + the relay content path."""
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).lower()
    skill = _SKILL_FOR_ACTION.get(action)
    if skill is None:
        return jsonify(error="action must be 'review' or 'discuss'"), 400
    src = MANAGER.get(panel_id)
    if src is None or not src.session_id:
        return jsonify(error="no source session to act on"), 409
    seed = (src.last_message or "").strip()
    if not seed:
        return jsonify(error="source has no output to act on"), 400
    new_id = _next_free_panel()
    verb = "review" if action == "review" else "discuss"
    label = f"{verb}: {src.name}"[:60]
    # last complete message only (no full-thread plumbing) — the boundary for S4.
    prompt = (
        f"{skill}\n\n"
        f'The text below is the latest complete message from a sibling hub session named "{src.name}". '
        f"{verb.capitalize()} it:\n\n{seed}"
    )
    # Spawn in the default cwd so the skill resolves (verified: /review expands there).
    MANAGER.launch(new_id, label, DEFAULT_CWD, prompt)
    # A human clicked this — record a hard (solid) explicit edge, new -> source.
    # F-IDENT: new_id was just launched (MANAGER.launch -> mint), so it has a node now.
    n_new, n_src = _edge_nodes(new_id, panel_id)
    if n_new and n_src:
        state.add_relation(n_new, n_src, "REVIEWS", "explicit")
    spawned = MANAGER.get(new_id)
    if spawned is not None:
        spawned.broadcast({"kind": "system", "subtype": "spawned", "action": action, "from": panel_id})
    return jsonify(ok=True, panel_id=new_id, action=action, name=label)


@app.route("/rename/<panel_id>", methods=["POST"])
def rename(panel_id: str):
    """Set the hub-side display name (the in-hub source of truth for findability;
    headless /rename can't rename the real session). The secretary PROPOSES a name
    and the user accepts it here — it is never auto-applied."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(error="name required"), 400
    s = MANAGER.get(panel_id)
    if s is None:
        return jsonify(error="unknown panel"), 404
    s.name = name
    state.update_panel(panel_id, name=name)
    s.broadcast({"kind": "renamed", "name": name})
    return jsonify(ok=True, panel_id=panel_id, name=name)


@app.route("/permission/request", methods=["POST"])
def permission_request():
    """Called by a session's MCP bridge when claude hits a gated tool."""
    data = request.get_json(silent=True) or {}
    panel_id = str(data.get("panel_id", ""))
    tool_name = data.get("tool_name", "?")
    tool_input = data.get("input") or {}
    pa = permissions.create(panel_id, tool_name, tool_input)
    s = MANAGER.get(panel_id)
    if s is not None:
        s.set_state("awaiting-approval")
        s.broadcast({"kind": "approval_request", "id": pa.id, "tool_name": tool_name, "input": tool_input})
    return jsonify(request_id=pa.id)


@app.route("/permission/poll/<req_id>")
def permission_poll(req_id: str):
    """Long-poll (default 25s) used by the MCP bridge to await the decision."""
    wait_s = min(float(request.args.get("wait", 25)), 50.0)
    decision = permissions.wait(req_id, wait_s)
    if decision is None:
        return jsonify(decided=False)
    return jsonify(decided=True, **decision)


@app.route("/permission/decide/<req_id>", methods=["POST"])
def permission_decide(req_id: str):
    """Called by the UI: allow or deny a pending approval."""
    data = request.get_json(silent=True) or {}
    behavior = "allow" if data.get("behavior") == "allow" else "deny"
    pa = permissions.get(req_id)
    if pa is None:
        return jsonify(error="unknown request"), 404
    permissions.decide(req_id, behavior, data.get("message"))
    s = MANAGER.get(pa.panel_id)
    if s is not None:
        # the turn resumes; back to running until it finishes or asks again
        s.set_state("running")
        s.broadcast({"kind": "approval_resolved", "id": req_id, "behavior": behavior})
    return jsonify(ok=True, behavior=behavior)


@app.route("/relations")
def relations():
    """Durable cross-session edges for the Map view: [{from,to,type,source}].
    source ∈ {explicit (human /route), inferred (secretary observe())}."""
    return jsonify(relations=state.get_relations())


# --- S5: freeform canvas — node pins + group-frames ---------------------------
@app.route("/node/<panel_id>/pos", methods=["POST"])
def node_pos(panel_id: str):
    """Pin / un-pin a node on the freeform canvas. Body {x, y} (numbers in 0..100
    stage-percent) PINS it; an empty or null-coord body CLEARS the pin so the node
    snaps back to its deterministic lane slot (per-node auto-arrange)."""
    data = request.get_json(silent=True) or {}
    x, y = data.get("x"), data.get("y")
    if x is None or y is None:
        state.clear_position(panel_id)
        return jsonify(ok=True, panel_id=panel_id, pinned=False)
    try:
        state.set_position(panel_id, float(x), float(y))
    except (TypeError, ValueError):
        return jsonify(error="x and y must be numbers"), 400
    return jsonify(ok=True, panel_id=panel_id, pinned=True)


@app.route("/node/<panel_id>/size", methods=["POST"])
def node_size(panel_id: str):
    """RAIL-TYPE: record / reset a node's manual SE-corner size. Body {w, h}
    (numbers, CSS px) SETS it; an empty or null-dim body CLEARS it so the tile
    returns to the default box. Sizes are stored apart from position pins, so a
    resize neither pins nor un-pins the node."""
    data = request.get_json(silent=True) or {}
    w, h = data.get("w"), data.get("h")
    if w is None or h is None:
        state.clear_size(panel_id)
        return jsonify(ok=True, panel_id=panel_id, sized=False)
    try:
        state.set_size(panel_id, float(w), float(h))
    except (TypeError, ValueError):
        return jsonify(error="w and h must be numbers"), 400
    return jsonify(ok=True, panel_id=panel_id, sized=True)


@app.route("/positions/clear", methods=["POST"])
def positions_clear():
    """Canvas-wide Auto-arrange: drop every pin so all tiles return to the
    deterministic S2 lane layout (the always-available tidy fallback)."""
    state.clear_all_positions()
    return jsonify(ok=True)


@app.route("/frames", methods=["GET", "POST"])
def frames():
    """Group-frames: labeled canvas regions (notes, not containers). GET lists
    [{id,label,rect}]; POST {label, rect:{x,y,w,h}} (0..100 percent) creates one."""
    if request.method == "GET":
        return jsonify(frames=state.get_frames())
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    rect = data.get("rect") or {}
    if not label:
        return jsonify(error="label required"), 400
    if not all(k in rect for k in ("x", "y", "w", "h")):
        return jsonify(error="rect requires x, y, w, h"), 400
    try:
        frame = state.add_frame(label, rect)
    except (TypeError, ValueError):
        return jsonify(error="rect values must be numbers"), 400
    return jsonify(ok=True, frame=frame)


@app.route("/frames/<frame_id>/delete", methods=["POST"])
def frames_delete(frame_id: str):
    if not state.remove_frame(frame_id):
        return jsonify(error="unknown frame"), 404
    return jsonify(ok=True, frame_id=frame_id)


@app.route("/frames/<frame_id>/rect", methods=["POST"])
def frames_rect(frame_id: str):
    """Slice 2: persist a moved/resized frame. Body {rect:{x,y,w,h}} (world 0..100).
    Header-drag moves, corner-grip resizes — x/y/w/h all change. Localhost-guarded."""
    data = request.get_json(silent=True) or {}
    rect = data.get("rect") or {}
    if not all(k in rect for k in ("x", "y", "w", "h")):
        return jsonify(error="rect requires x, y, w, h"), 400
    try:
        ok = state.update_frame_rect(frame_id, rect)
    except (TypeError, ValueError):
        return jsonify(error="rect values must be numbers"), 400
    if not ok:
        return jsonify(error="unknown frame"), 404
    return jsonify(ok=True, frame_id=frame_id)


# --- S8/Slice 2: per-group auto-relay switch + positional membership (exclude) -------
@app.route("/frames/<frame_id>/auto", methods=["POST"])
def frames_auto(frame_id: str):
    """Flip a frame's per-group auto-relay switch (default OFF). Body {on: bool}.
    The toggle is the ONLY user control (no per-session / per-type toggle) — auto
    eligibility is fixed-filtered by relay TYPE and by EXPLICIT membership server-side."""
    data = request.get_json(silent=True) or {}
    on = bool(data.get("on"))
    if not state.set_frame_auto_relay(frame_id, on):
        return jsonify(error="unknown frame"), 404
    return jsonify(ok=True, frame_id=frame_id, auto_relay=on)


@app.route("/frames/<frame_id>/exclude", methods=["POST"])
def frames_exclude(frame_id: str):
    """Slice 2 (§7.3): mark a panel 'not in the group' (op:add) or clear it (op:remove).
    Membership is positional; exclude is the ONLY manual membership action. The exclude
    set is node-keyed, so translate the panel_id to its node_id here."""
    data = request.get_json(silent=True) or {}
    pid = str(data.get("panel_id", ""))
    if not pid:
        return jsonify(error="panel_id required"), 400
    nid = nodes.node_id_for_panel(pid)
    if not nid:
        return jsonify(error="panel has no node"), 409
    op = str(data.get("op", "add")).lower()
    ok = state.remove_frame_exclude(frame_id, nid) if op == "remove" else state.add_frame_exclude(frame_id, nid)
    if not ok:
        return jsonify(error="unknown frame"), 404
    return jsonify(ok=True, frame_id=frame_id, panel_id=pid, op=op)


@app.route("/frames/<frame_id>/promote", methods=["POST"])
def frames_promote(frame_id: str):
    """Slice 6: promote/demote a frame to a tracked initiative (the secretary's Pass-B
    reasoning unit). Body {on: bool}. Demote clears the stale group description."""
    data = request.get_json(silent=True) or {}
    if not state.set_frame_promoted(frame_id, bool(data.get("on"))):
        return jsonify(error="unknown frame"), 404
    return jsonify(ok=True, frame_id=frame_id, promoted=bool(data.get("on")))


@app.route("/relations/remove", methods=["POST"])
def relations_remove():
    """S8 auto-route 'undo': retract a directed Map edge. The delivered message can't be
    recalled (it's already in the target's turn) and the hub never injects a 'disregard'
    instruction into the model (PLAN §3) — so undo is UI-side: it drops the edge so an
    auto-route the human didn't want doesn't linger as a relation. Body {from, to}."""
    data = request.get_json(silent=True) or {}
    frm, to = str(data.get("from", "")), str(data.get("to", ""))
    # F-IDENT: edges are node-keyed; the undo affordance still names panels -> translate.
    nfrm, nto = _edge_nodes(frm, to)
    removed = state.remove_relation(nfrm, nto) if nfrm and nto else False
    return jsonify(ok=True, removed=removed)


@app.route("/notes", methods=["GET", "POST"])
def notes():
    """Slice 4: free-text cards. GET lists all; POST {anchor, text, author?} creates one.
    A node anchor arrives as {anchor:{panel_id}} and is translated to node_id (the store
    is node-keyed). Other anchors: {frame_id} | {free:{x,y}} pass through."""
    if request.method == "GET":
        return jsonify(notes=state.get_notes())
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="text required"), 400
    raw = data.get("anchor") or {}
    anchor: dict = {}
    if raw.get("panel_id"):
        nid = nodes.node_id_for_panel(str(raw["panel_id"]))
        if not nid:
            return jsonify(error="panel has no node"), 409
        anchor = {"node_id": nid}
    elif raw.get("frame_id"):
        anchor = {"frame_id": str(raw["frame_id"])}
    elif isinstance(raw.get("free"), dict):
        anchor = {"free": {"x": float(raw["free"].get("x", 50)), "y": float(raw["free"].get("y", 50))}}
    else:
        return jsonify(error="anchor must be {panel_id} | {frame_id} | {free:{x,y}}"), 400
    author = "secretary" if data.get("author") == "secretary" else "human"
    note = state.add_note(anchor, text, author)
    if note is None:
        return jsonify(ok=True, deduped=True)
    return jsonify(ok=True, note=note)


@app.route("/notes/<note_id>/delete", methods=["POST"])
def notes_delete(note_id: str):
    if not state.remove_note(note_id):
        return jsonify(error="unknown note"), 404
    return jsonify(ok=True, note_id=note_id)


@app.route("/edge", methods=["POST"])
def edge():
    """Slice 3: a HUMAN-drawn Map edge. Body {from_panel, to_panel, rel_type?, label?}.
    Records the unified edge object (plan §3.3) as EXPLICIT (solid, no-downgrade), author
    'human' (warm), with node_id endpoints. No self-loop; both panels must have a node."""
    data = request.get_json(silent=True) or {}
    src, dst = str(data.get("from_panel", "")), str(data.get("to_panel", ""))
    if not src or not dst or src == dst:
        return jsonify(error="from_panel and to_panel required and must differ"), 400
    rtype = str(data.get("rel_type") or "").upper()
    rel_type = rtype if rtype in secretary.RELATION_TYPES else "FEEDS"
    label = (data.get("label") or "").strip()[:60] or None
    nsrc, ndst = _edge_nodes(src, dst)
    if not nsrc or not ndst:
        return jsonify(error="a panel has no node (not launched?)"), 409
    state.add_relation(nsrc, ndst, rel_type, "explicit", label=label, author="human")
    return jsonify(ok=True, from_panel=src, to_panel=dst, type=rel_type)


@app.route("/board/mode", methods=["GET", "POST"])
def board_mode():
    """S4 secretary throttle. 'full' runs the secretary's Sonnet Pass B (cross-session
    triage/relay/frame); 'quiet' skips it to cut cost (the cheap Haiku brief+name pass
    still runs). GET reports the current mode; POST {mode:'full'|'quiet'} sets it. The
    POST is localhost-guarded by the global before_request hook."""
    if request.method == "GET":
        return jsonify(mode=state.get_board_mode())
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip().lower()
    if mode not in ("full", "quiet"):
        return jsonify(error="mode must be 'full' or 'quiet'"), 400
    return jsonify(ok=True, mode=state.set_board_mode(mode))


@app.route("/viewport", methods=["GET", "POST"])
def viewport():
    """F-COORD: persist the Map's world viewport {panX, panY, zoom} so pan/zoom
    survive a reload. POST is localhost-guarded by the global before_request hook."""
    if request.method == "GET":
        return jsonify(state.get_viewport())
    data = request.get_json(silent=True) or {}
    try:
        vp = state.set_viewport(data.get("panX", 0), data.get("panY", 0), data.get("zoom", 1))
    except (TypeError, ValueError):
        return jsonify(error="panX, panY, zoom must be numbers"), 400
    return jsonify(ok=True, **vp)


@app.route("/secretary/feed")
def secretary_feed():
    return jsonify(secretary.feed_snapshot())


@app.route("/secretary/dismiss/<item_id>", methods=["POST"])
def secretary_dismiss(item_id: str):
    secretary.dismiss(item_id)
    return jsonify(ok=True)


@app.route("/secretary/brief")
def secretary_brief():
    """Situation-board strip: join BRIEF with the live board server-side and
    return ONLY live sessions [{panel_id, name, state, brief}]. Iterating the
    snapshot (not BRIEF) drops stale keys for deleted/relaunched panels."""
    briefs = secretary.brief_snapshot()
    out = [
        {
            "panel_id": b["panel_id"],
            "name": b.get("name", ""),
            "state": b.get("state", ""),
            "brief": briefs.get(str(b["panel_id"]), ""),
        }
        for b in MANAGER.snapshot()
    ]
    return jsonify(sessions=out)


@app.route("/advisor/send", methods=["POST"])
def advisor_send():
    """Kick off one streaming advisor (Opus) turn. Returns immediately; the
    answer streams over GET /advisor/stream and lands in the transcript.
    [M1] one turn at a time — a 2nd send while one is in flight gets 409."""
    if advisor.ADVISOR.busy:
        return jsonify(error="advisor busy"), 409
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    # MED-1 (DO NOT REGRESS S0): feed the advisor the brief-enriched board, NOT
    # the raw 200-char last_message snippet — otherwise S0's "both brains" lift
    # silently evaporates.
    board_lines = secretary._board_lines(MANAGER.snapshot())
    threading.Thread(target=advisor.ADVISOR.send, args=(message, board_lines), daemon=True).start()
    return jsonify(ok=True)


@app.route("/advisor/stream")
def advisor_stream():
    """SSE: the advisor's live event channel (phase / delta / done / error).
    [M2] LIVE events only — history replay is /advisor/chat's job; replaying
    here too would double-render the thread."""
    q = advisor.ADVISOR.subscribe()

    def gen():
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield _sse(ev)
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            advisor.ADVISOR.unsubscribe(q)

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/advisor/chat")
def advisor_chat():
    return jsonify(turns=advisor.ADVISOR.transcript_snapshot())


@app.route("/advisor/clear", methods=["POST"])
def advisor_clear():
    advisor.ADVISOR.clear()
    return jsonify(ok=True)


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev)}\n\n"


@app.route("/stream/<panel_id>")
def stream(panel_id: str):
    session = MANAGER.ensure(panel_id)
    session.ensure_rehydrated()  # S4: restore on-disk scrollback after a hub restart

    def gen():
        q, replay = session.subscribe()
        try:
            # snapshot the panel's current state + history so a late client
            # renders the existing thread before live events arrive
            yield _sse({"kind": "state", "state": session.state})
            for ev in replay:
                yield _sse(ev)
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield _sse(ev)
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            session.unsubscribe(q)

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


atexit.register(MANAGER.terminate_all)


if __name__ == "__main__":
    # threaded=True is required: each SSE channel + subprocess reader holds a thread.
    print(f"claude-hub on http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)
