"""Durable hub state: panel <-> claude session_id <-> launch cwd <-> last message.

The hub itself is restart-proof: turns are ephemeral subprocesses, sessions live
on disk in ~/.claude/projects. On restart we reload this map and `--resume` still
works. A single lock guards the file (step 6 relies on this under the 9-grid).
"""

from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "state.json"
_LOCK = threading.RLock()


def load() -> dict[str, Any]:
    with _LOCK:
        if not STATE_FILE.exists():
            return {"panels": {}}
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"panels": {}}


def save(data: dict[str, Any]) -> None:
    with _LOCK:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)  # atomic on Windows when target on same volume


def update_panel(panel_id: str, **fields: Any) -> None:
    """Merge fields into one panel's record and persist."""
    with _LOCK:
        data = load()
        panels = data.setdefault("panels", {})
        rec = panels.setdefault(panel_id, {})
        rec.update(fields)
        save(data)


def remove_panel(panel_id: str) -> bool:
    """Hard-delete a panel from the durable map: its panel record AND its
    panel-keyed position pin (so the freed slot id leaves no off-canvas ghost).
    Node-keyed semantics (relations/frames/notes) are dropped separately via
    drop_node, and the node RECORD is popped by the caller (nodes.drop_node_record).
    Returns True if anything was removed."""
    pid = str(panel_id)
    with _LOCK:
        data = load()
        changed = data.get("panels", {}).pop(pid, None) is not None
        pos = data.get("positions")
        if pos and pid in pos:
            del pos[pid]
            changed = True
        sz = data.get("sizes")  # RAIL-TYPE: drop the freed slot's manual size too
        if sz and pid in sz:
            del sz[pid]
            changed = True
        if changed:
            save(data)
    return changed


# --- board mode (S4): Full vs Quiet — the secretary's coarse throttle -----------
# The secretary's cost driver is frequency × model: observe() fires every turn and
# its expensive leg is the SONNET Pass B (cross-session triage/relay/frame). Board
# mode is the human's throttle on THAT pass: "full" runs it, "quiet" SKIPS it — the
# cheap HAIKU Pass A (brief + name) always runs regardless. Persisted in state.json
# so the choice survives a hub restart (like every other setting here). Scoped mode
# (frame-aware) is deliberately out of scope (S4 boundary).
_BOARD_MODES = ("full", "quiet")


def get_board_mode() -> str:
    """The current board mode — defaults to "full" when unset or stored corrupt."""
    with _LOCK:
        mode = load().get("board_mode", "full")
    return mode if mode in _BOARD_MODES else "full"


def set_board_mode(mode: str) -> str:
    """Persist the board mode, coercing anything not in {full,quiet} to "full".
    Returns the stored (effective) mode so a caller can echo it back."""
    mode = mode if mode in _BOARD_MODES else "full"
    with _LOCK:
        data = load()
        data["board_mode"] = mode
        save(data)
    return mode


# --- F-COORD: Map viewport (coordinate contract) — {panX, panY, zoom} ----------
def get_viewport() -> dict[str, float]:
    with _LOCK:
        v = load().get("viewport") or {}
    return {"panX": float(v.get("panX", 0.0)), "panY": float(v.get("panY", 0.0)), "zoom": float(v.get("zoom", 1.0))}


def set_viewport(pan_x: float, pan_y: float, zoom: float) -> dict[str, float]:
    z = max(0.35, min(3.0, float(zoom)))  # mirror the client ZMIN/ZMAX clamp
    vp = {"panX": float(pan_x), "panY": float(pan_y), "zoom": z}
    with _LOCK:
        data = load()
        data["viewport"] = vp
        save(data)
    return vp


# --- relay suppress-set (S2): persistent executed/dismissed relay keys ----------
# When the human executes (the chip's ✓ route hits /route THEN /secretary/dismiss)
# or dismisses a secretary relay suggestion, we record its TEXT-AWARE key here so it
# can't nag again — and crucially so the suppression SURVIVES a hub restart (the old
# in-memory FEED removal reset on every restart). Stored as a JSON list (sets aren't
# JSON-serialisable), membership-deduped on add. The key is per-content, not per-pair
# (see secretary._key), so a NEW source output yields a new key and re-suggests.
def get_suppressed_keys() -> list[str]:
    with _LOCK:
        return load().get("suppressed_keys", [])


def add_suppressed_key(key: str) -> None:
    """Persist one suppressed relay key (idempotent — no duplicate rows)."""
    if not key:
        return
    with _LOCK:
        data = load()
        keys = data.setdefault("suppressed_keys", [])
        if key not in keys:
            keys.append(key)
            save(data)


# --- relations (S2): typed, persisted cross-session edges ----------------------
# A relation is a durable edge {from, to, type, source}. `type` ∈ FEEDS/REVIEWS/
# DEPENDS/CONFLICT; `source` ∈ {"explicit" (a human /route), "inferred" (the
# secretary's observe() suggestion). These were transient feed items before S2;
# now they persist as a top-level `relations` list and feed the Map view.
def get_relations() -> list[dict[str, Any]]:
    with _LOCK:
        return load().get("relations", [])


def set_relations(relations: list[dict[str, Any]]) -> None:
    with _LOCK:
        data = load()
        data["relations"] = relations
        save(data)


def add_relation(frm: str, to: str, rel_type: str | None, source: str,
                 reason: str | None = None, label: str | None = None,
                 color: str | None = None, author: str | None = None) -> None:
    """Upsert one edge, deduped by (from, to). The single unified edge object (plan §3.3).

    `source` never DOWNGRADES: once explicit (a human routed/drew it → solid), a later
    secretary inference must not flip it to inferred/dashed. `author` tracks the FINAL
    source unless explicitly given: explicit→"human" (warm), inferred→"secretary" (cool)
    — so S5's warm/cool encoding needs no per-caller wiring. `rel_type`/`reason`/`label`/
    `color` are applied on upsert ONLY when truthy (a type/label-less upsert preserves the
    existing richer value). On insert with no type, default "FEEDS".
    """
    frm, to = str(frm), str(to)
    with _LOCK:
        data = load()
        rels = data.setdefault("relations", [])
        for r in rels:
            if str(r.get("from")) == frm and str(r.get("to")) == to:
                if rel_type:
                    r["type"] = rel_type
                if source == "explicit" or r.get("source") == "explicit":
                    r["source"] = "explicit"
                if reason:
                    r["reason"] = reason
                if label:
                    r["label"] = label
                if color:
                    r["color"] = color
                # author follows the FINAL source (no-downgrade), unless explicitly set
                r["author"] = author or ("human" if r.get("source") == "explicit" else r.get("author") or "secretary")
                save(data)
                return
        rels.append({
            "from": frm, "to": to, "type": rel_type or "FEEDS", "source": source,
            "reason": reason or "", "label": label or "", "color": color or "",
            "author": author or ("human" if source == "explicit" else "secretary"),
        })
        save(data)


def clear_relations_for(panel_id: str) -> None:
    """Drop every edge touching this panel — called on relaunch so a reused
    panel_id can't keep a prior task's edges as phantom Map relations (mirrors
    clear_brief / HIGH-2)."""
    pid = str(panel_id)
    with _LOCK:
        data = load()
        rels = data.get("relations")
        if not rels:
            return
        kept = [r for r in rels if str(r.get("from")) != pid and str(r.get("to")) != pid]
        if len(kept) != len(rels):
            data["relations"] = kept
            save(data)


def remove_relation(frm: str, to: str) -> bool:
    """S8: drop one directed edge (the auto-route 'undo' affordance). The delivered
    message itself can't be recalled — the hub never injects a 'disregard' instruction
    into the model (PLAN §3) — so undo is UI-side: it retracts the edge so the auto-route
    doesn't linger as a Map relation. Returns True if an edge was removed."""
    frm, to = str(frm), str(to)
    with _LOCK:
        data = load()
        rels = data.get("relations")
        if not rels:
            return False
        kept = [r for r in rels if not (str(r.get("from")) == frm and str(r.get("to")) == to)]
        if len(kept) == len(rels):
            return False
        data["relations"] = kept
        save(data)
        return True


# --- notes (Slice 4): free-text cards, anchored or free, on the understanding-map ----
# A note = {id, anchor, text, author, created}. anchor is ONE of:
#   {"node_id": <id>}  (node-keyed — drop_node/rekey clean it, F-IDENT)
#   {"frame_id": <id>} | {"free": {"x", "y"}}   (not node-keyed — survive a recycle)
# Persisted liberally; the UI surfaces selectively (hover/focus). Secretary-authored
# notes are deduped by (anchor,text) and capped at a ceiling; human notes are uncapped
# (the user's own marks). edge_id anchoring is deferred (edges are (from,to)-keyed).
_NOTE_SECRETARY_CEILING = 12


def get_notes() -> list[dict[str, Any]]:
    with _LOCK:
        return load().get("notes", [])


def _anchor_key(anchor: dict[str, Any]) -> str:
    a = anchor or {}
    if "node_id" in a: return f"node:{a['node_id']}"
    if "frame_id" in a: return f"frame:{a['frame_id']}"
    if "free" in a: return f"free:{round(float((a['free'] or {}).get('x', 0)), 1)},{round(float((a['free'] or {}).get('y', 0)), 1)}"
    return "free:?"


def add_note(anchor: dict[str, Any], text: str, author: str = "human") -> dict[str, Any] | None:
    """Add a note. Dedup by (anchor, text). Secretary-authored notes are capped at the
    ceiling (evict the oldest secretary note). Returns the stored note, or None if it was
    a dedup hit. Human notes are never capped."""
    text = (text or "").strip()[:280]
    if not text:
        return None
    import time as _time
    note = {
        "id": secrets.token_hex(4),
        "anchor": anchor or {"free": {"x": 50, "y": 50}},
        "text": text,
        "author": str(author or "human"),
        "created": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
    }
    nkey = (_anchor_key(note["anchor"]), text)
    with _LOCK:
        data = load()
        notes = data.setdefault("notes", [])
        if any((_anchor_key(n.get("anchor")), n.get("text")) == nkey for n in notes):
            return None  # dedup
        if note["author"] == "secretary":
            sec = [n for n in notes if n.get("author") == "secretary"]
            if len(sec) >= _NOTE_SECRETARY_CEILING:
                oldest = sec[0]  # FIFO — list is append-ordered
                data["notes"] = [n for n in notes if n is not oldest]
                notes = data["notes"]
        notes.append(note)
        save(data)
    return note


def remove_note(note_id: str) -> bool:
    nid = str(note_id)
    with _LOCK:
        data = load()
        notes = data.get("notes")
        if not notes:
            return False
        kept = [n for n in notes if str(n.get("id")) != nid]
        if len(kept) == len(notes):
            return False
        data["notes"] = kept
        save(data)
        return True


# --- F-IDENT: shared node-keyed store primitive (composed by the SEMANTIC stores) -----
# These centralize node_id handling for relations + frame-membership + notes ONLY.
# They are DEFENSIVE: stores that do not exist yet (frames[].excluded[] -> Slice 2,
# notes -> Slice 4) are simply absent, so the loops over them are no-ops. They NEVER
# touch positions (panel-keyed slot layout) or suppressed_keys (panel-keyed secretary
# dedup) — those are operational and stay panel-keyed (plan §3.1).
def drop_node(node_id: str) -> None:
    """Remove this node's SEMANTIC traces (relations, frame.excluded, note anchors).
    Does NOT remove the node record itself (mint_node_for_panel does that on recycle),
    and does NOT touch positions / suppressed_keys."""
    nid = str(node_id)
    with _LOCK:
        data = load()
        changed = False
        rels = data.get("relations")
        if rels:
            kept = [r for r in rels if str(r.get("from")) != nid and str(r.get("to")) != nid]
            if len(kept) != len(rels):
                data["relations"] = kept
                changed = True
        for f in data.get("frames", []) or []:          # Slice 2 adds excluded[]; absent now -> no-op
            exc = f.get("excluded")
            if exc and nid in exc:
                f["excluded"] = [m for m in exc if str(m) != nid]
                changed = True
        notes = data.get("notes")                        # Slice 4 adds notes; absent now -> no-op
        if notes:
            kept_n = [n for n in notes if str((n.get("anchor") or {}).get("node_id")) != nid]
            if len(kept_n) != len(notes):
                data["notes"] = kept_n
                changed = True
        if changed:
            save(data)


def rekey(mapping: dict[str, str]) -> None:
    """Rewrite old_node_id -> new_node_id across relations + frame.excluded + note
    anchors ONLY (NOT positions, NOT suppressed_keys). DEFENSIVE — no-op for stores
    not present yet. Used by the one-time migration and any future bulk re-key."""
    if not mapping:
        return
    m = {str(k): str(v) for k, v in mapping.items()}
    with _LOCK:
        data = load()
        for r in data.get("relations", []) or []:
            if str(r.get("from")) in m:
                r["from"] = m[str(r["from"])]
            if str(r.get("to")) in m:
                r["to"] = m[str(r["to"])]
        for f in data.get("frames", []) or []:
            exc = f.get("excluded")
            if exc:
                f["excluded"] = [m.get(str(x), x) for x in exc]
        for n in data.get("notes", []) or []:
            anc = n.get("anchor") or {}
            if str(anc.get("node_id")) in m:
                anc["node_id"] = m[str(anc["node_id"])]
        save(data)


# --- F-IDENT one-time migration: panel_id -> node_id for the SEMANTIC stores --------
# Mints a node per existing panel (bound to its current session_id), rekeys relations'
# endpoints panel->node, and drops the legacy S8 frames[].members[]. IDEMPOTENT: a panel
# that already has node_id is skipped, and node-keyed relations (whose endpoints are not
# panel-ids) are not re-rekeyed. Does NOT touch positions or suppressed_keys (plan §5).
def migrate_to_node_ids() -> None:
    import secrets as _secrets
    import time as _time
    with _LOCK:
        data = load()
        panels = data.get("panels", {})
        if not panels:
            return
        nodes = data.setdefault("nodes", {})
        mapping: dict[str, str] = {}  # panel_id -> node_id, for rekeying relations/frames
        for pid, rec in panels.items():
            nid = rec.get("node_id")
            if not nid:
                nid = _secrets.token_hex(4)
                rec["node_id"] = nid
                nodes[nid] = {
                    "current_session_id": rec.get("session_id"),
                    "panel_id": str(pid),
                    "name": rec.get("name", ""),
                    "prev_session_ids": [],
                    "created": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
                }
            mapping[str(pid)] = nid
        # rekey relations endpoints that are still panel-ids (idempotent: node-keyed
        # endpoints are 8-hex tokens, never a panel-id key, so a 2nd run is a no-op).
        for r in data.get("relations", []) or []:
            if str(r.get("from")) in mapping:
                r["from"] = mapping[str(r["from"])]
            if str(r.get("to")) in mapping:
                r["to"] = mapping[str(r["to"])]
        # drop legacy S8 members[] (membership is positional now)
        for f in data.get("frames", []) or []:
            f.pop("members", None)
        save(data)


# --- pending-relay queue (S7): TRANSIENT, in-memory, keyed by TARGET panel -------
# A relay into a BUSY target is QUEUED here instead of 409'd, and a small count badge
# shows on the target tile. When the target next COMPLETES a turn (the server
# TURN_COMPLETE_HOOK, not set_state), its queued relays RE-SURFACE as confirm chips —
# NEVER auto-sent. DELIBERATELY in-memory (a plain dict + its own lock), NOT in
# state.json: a queued relay is an ephemeral, about-to-confirm intent that must NOT
# survive a hub restart — a stale cross-session approval re-appearing (and being sent)
# after a restart is exactly the silent-send hazard S7 forbids. Separate from _LOCK
# (the file lock) since it touches no file.
_PENDING_RELAYS: dict[str, list[dict[str, Any]]] = {}
_PENDING_LOCK = threading.Lock()


def add_pending_relay(target: str, relay: dict[str, Any]) -> None:
    """Queue one relay for a busy target (transient; lost on a hub restart by design)."""
    tgt = str(target)
    with _PENDING_LOCK:
        _PENDING_RELAYS.setdefault(tgt, []).append(dict(relay))


def pop_pending_relays(target: str) -> list[dict[str, Any]]:
    """Remove + return a target's queued relays ([] if none). Called on the target's
    turn-completion to re-surface them — popping clears the tile badge in the same step."""
    tgt = str(target)
    with _PENDING_LOCK:
        return _PENDING_RELAYS.pop(tgt, [])


def get_pending_counts() -> dict[str, int]:
    """Per-target queued-relay counts backing the tile badges — only nonzero targets."""
    with _PENDING_LOCK:
        return {k: len(v) for k, v in _PENDING_RELAYS.items() if v}


# --- node positions (S5): freeform-canvas pins ---------------------------------
# A pin is a per-panel {x, y} in 0..100 STAGE-PERCENT coords (the same space the
# Map's tiles and SVG edges already use: tiles sit at left:x% / top:y% centered,
# the edge SVG is viewBox 0 0 100 100). A panel with NO pin falls back to the
# deterministic lane layout — so "auto-arrange" is simply "clear the pins".
def get_positions() -> dict[str, dict[str, float]]:
    with _LOCK:
        return load().get("positions", {})


def set_position(panel_id: str, x: float, y: float) -> None:
    """Pin one node. Coords are clamped to the 0..100 stage so a bad client
    value can't park a tile off-canvas where it's unreachable."""
    pid = str(panel_id)
    x = max(0.0, min(100.0, float(x)))
    y = max(0.0, min(100.0, float(y)))
    with _LOCK:
        data = load()
        data.setdefault("positions", {})[pid] = {"x": x, "y": y}
        save(data)


def clear_position(panel_id: str) -> None:
    """Un-pin one node so it returns to its lane slot (per-node auto-arrange)."""
    pid = str(panel_id)
    with _LOCK:
        data = load()
        pos = data.get("positions")
        if pos and pid in pos:
            del pos[pid]
            save(data)


def clear_all_positions() -> None:
    """Un-pin everything — the canvas-wide Auto-arrange back to the lane layout."""
    with _LOCK:
        data = load()
        if data.get("positions"):
            data["positions"] = {}
            save(data)


# --- node sizes (RAIL-TYPE): manual bottom-right resize -------------------------
# A size is a per-panel {w, h} in CSS px — the explicit width/height a user dragged
# the tile to via its SE corner handle. Kept in a SEPARATE map from `positions` so a
# node can be resized WITHOUT being pinned (and vice-versa). A panel with no size
# falls back to the default tile box (200px wide, auto height). Clamped so a bad
# client value can't make a tile unusably small or swallow the canvas.
_SIZE_MIN_W, _SIZE_MAX_W = 160.0, 520.0
_SIZE_MIN_H, _SIZE_MAX_H = 64.0, 420.0


def get_sizes() -> dict[str, dict[str, float]]:
    with _LOCK:
        return load().get("sizes", {})


def set_size(panel_id: str, w: float, h: float) -> None:
    """Record one node's manual size. w/h are clamped to a sane tile range."""
    pid = str(panel_id)
    w = max(_SIZE_MIN_W, min(_SIZE_MAX_W, float(w)))
    h = max(_SIZE_MIN_H, min(_SIZE_MAX_H, float(h)))
    with _LOCK:
        data = load()
        data.setdefault("sizes", {})[pid] = {"w": w, "h": h}
        save(data)


def clear_size(panel_id: str) -> None:
    """Reset one node back to the default tile box."""
    pid = str(panel_id)
    with _LOCK:
        data = load()
        sz = data.get("sizes")
        if sz and pid in sz:
            del sz[pid]
            save(data)


# NB: a node's pin is deliberately NOT cleared on relaunch (unlike clear_brief /
# clear_relations_for). Brief + relations are task CONTENT and must reset; a pin
# is LAYOUT the user chose, so a panel reused for a new task keeps its place on
# the canvas — auto-arrange is the explicit way to undo a pin.


# --- group-frames (S5): labeled canvas regions ---------------------------------
# A frame is a durable note {id, label, rect} where rect = {x, y, w, h} in the
# same 0..100 stage-percent space. For RENDERING, frames are the Obsidian-canvas
# model: notes, NOT containers — visual containment is purely positional, nothing
# is bound to a frame's rect. The secretary may PROPOSE one (a frame_suggestion
# feed item); the user accepts and the client computes the rect from the members'
# positions. Membership (§7.3, Slice 2) is POSITIONAL: a tile whose center sits
# inside the rect IS a member unless its node is in the frame's `excluded[]` (the
# only manual membership action). Effective membership is computed each render,
# never stored; `auto_relay` (Slice 6) reads that effective set.
def get_frames() -> list[dict[str, Any]]:
    with _LOCK:
        return load().get("frames", [])


def add_frame(label: str, rect: dict[str, float], author: str = "human") -> dict[str, Any]:
    """Create one frame, returning the stored record (with its new id).

    S8: a frame carries `auto_relay` (the per-group auto-fire switch, default OFF —
    build the feature now, flip it on once the suggestions prove good). Membership is
    POSITIONAL now (plan §7.3, reverses S8's explicit members[]): a tile whose center
    sits inside the rect IS a member unless its node is in `excluded[]` (the only manual
    membership action). Slice 2 adds `excluded[]` (node_ids — semantic layer, F-IDENT)
    and `author` ('human' frames read warm; S5 generalizes)."""
    frame = {
        "id": secrets.token_hex(4),
        "label": str(label)[:60],
        "rect": {
            "x": max(0.0, min(100.0, float(rect.get("x", 0)))),
            "y": max(0.0, min(100.0, float(rect.get("y", 0)))),
            "w": max(0.0, min(100.0, float(rect.get("w", 0)))),
            "h": max(0.0, min(100.0, float(rect.get("h", 0)))),
        },
        "auto_relay": False,           # S8: per-group auto-fire — OFF by default (restored in Slice 6)
        "excluded": [],                # Slice 2: node_ids positionally inside but marked "not in the group" (§7.3)
        "author": str(author or "human"),  # Slice 2: the human draws/accepts frames -> warm (S5 generalizes)
        "promoted": False,             # Slice 6: lock = promote to a tracked initiative (Pass-B reasoning unit)
        "description": "",             # Slice 6: the secretary's group image (cool/advisory)
    }
    with _LOCK:
        data = load()
        data.setdefault("frames", []).append(frame)
        save(data)
    return frame


def set_frame_auto_relay(frame_id: str, on: bool) -> bool:
    """S8: flip one frame's per-group auto-relay switch. Returns True if a frame matched."""
    fid = str(frame_id)
    with _LOCK:
        data = load()
        for f in data.get("frames", []) or []:
            if str(f.get("id")) == fid:
                f["auto_relay"] = bool(on)
                save(data)
                return True
    return False


def update_frame_rect(frame_id: str, rect: dict[str, float]) -> bool:
    """Slice 2: persist a moved/resized frame (world coords, clamped 0..100). Returns
    True if a frame matched. Membership is computed from the rect, so a move/resize
    silently re-computes effective membership next render — no stored list to update.
    (Supersedes S8's move-only move_frame: the header drags the rect, the corner grip
    resizes it, so x/y/w/h all change.)"""
    fid = str(frame_id)
    with _LOCK:
        data = load()
        for f in data.get("frames", []) or []:
            if str(f.get("id")) == fid:
                f["rect"] = {
                    "x": max(0.0, min(100.0, float(rect.get("x", 0)))),
                    "y": max(0.0, min(100.0, float(rect.get("y", 0)))),
                    "w": max(0.0, min(100.0, float(rect.get("w", 0)))),
                    "h": max(0.0, min(100.0, float(rect.get("h", 0)))),
                }
                save(data)
                return True
    return False


def add_frame_exclude(frame_id: str, node_id: str) -> bool:
    """Slice 2 (§7.3): mark a node 'not in the group' — sticky exclusion. node_id-keyed
    (the canvas semantic layer). Idempotent. Returns True if a frame matched."""
    fid, nid = str(frame_id), str(node_id)
    with _LOCK:
        data = load()
        for f in data.get("frames", []) or []:
            if str(f.get("id")) == fid:
                exc = f.setdefault("excluded", [])
                if nid not in exc:
                    exc.append(nid)
                    save(data)
                return True
    return False


def remove_frame_exclude(frame_id: str, node_id: str) -> bool:
    """Slice 2 (§7.3): clear an exclusion (re-include, or drag-out-and-back). Returns
    True if a frame matched."""
    fid, nid = str(frame_id), str(node_id)
    with _LOCK:
        data = load()
        for f in data.get("frames", []) or []:
            if str(f.get("id")) == fid:
                exc = f.get("excluded") or []
                if nid in exc:
                    f["excluded"] = [m for m in exc if str(m) != nid]
                    save(data)
                return True
    return False


def set_frame_promoted(frame_id: str, on: bool) -> bool:
    """Slice 6: promote/demote a frame to a tracked initiative. A promoted frame is the
    secretary's Pass-B reasoning unit; demoting stops the group reasoning."""
    fid = str(frame_id)
    with _LOCK:
        data = load()
        for f in data.get("frames", []) or []:
            if str(f.get("id")) == fid:
                f["promoted"] = bool(on)
                if not on:
                    f["description"] = ""  # drop the stale group image on demote
                save(data)
                return True
    return False


def set_frame_description(frame_id: str, text: str) -> bool:
    """Slice 6: store/replace the secretary's group image for a promoted frame (replaces,
    never accumulates — note lifecycle: one live description per group)."""
    fid = str(frame_id)
    with _LOCK:
        data = load()
        for f in data.get("frames", []) or []:
            if str(f.get("id")) == fid:
                f["description"] = str(text or "")[:300]
                save(data)
                return True
    return False


def exclude_node_from_containing_frames(node_id: str, x: float, y: float) -> int:
    """Slice 2 (§7.3 recycle-safety): auto-exclude node_id from EVERY frame whose rect
    contains the point (x, y) in world coords. Called by mint_node_for_panel on relaunch,
    where the surviving pin would otherwise auto-join the new node to a frame it never
    belonged to. Returns the number of frames it was excluded from."""
    nid = str(node_id)
    n = 0
    with _LOCK:
        data = load()
        for f in data.get("frames", []) or []:
            r = f.get("rect") or {}
            rx, ry, rw, rh = float(r.get("x", 0)), float(r.get("y", 0)), float(r.get("w", 0)), float(r.get("h", 0))
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                exc = f.setdefault("excluded", [])
                if nid not in exc:
                    exc.append(nid)
                    n += 1
        if n:
            save(data)
    return n


def remove_frame(frame_id: str) -> bool:
    """Delete one frame by id. Returns True if something was removed."""
    fid = str(frame_id)
    with _LOCK:
        data = load()
        frames = data.get("frames")
        if not frames:
            return False
        kept = [f for f in frames if str(f.get("id")) != fid]
        if len(kept) == len(frames):
            return False
        data["frames"] = kept
        save(data)
        return True
