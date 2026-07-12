"""Identity contract (Canvas v2 F-IDENT): the durable `node_id` spine for the
SEMANTIC canvas stores (relations, frame-membership, notes).

A `node_id` is a fresh, non-recyclable token minted at LAUNCH (so a launching ⚪ /
errored-before-capture 🔴 panel already has a canvas identity). It is the stable key
the semantic stores ride, so a fork can swap the live session underneath a tile
without losing its edges/frames/notes, and a relaunch deliberately mints a NEW node
(wiping the old node's semantics — the pin, which is panel-keyed, survives).

Layer boundary (plan §3.1): `positions` and `suppressed_keys` are OPERATIONAL,
panel-keyed, and are NOT touched here. Only relations / frame-membership / notes are
node-keyed. Canonical reader of a panel's node = `panels[pid].node_id`
(`node_id_for_panel`); `nodes[id].panel_id` is the back-pointer kept in sync by
`mint_node_for_panel`.

Storage: state.json["nodes"] = {
    <node_id>: {current_session_id, panel_id, name, prev_session_ids:[], created}
}
"""

from __future__ import annotations

import secrets
import time

from hub import state


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mint_node_id() -> str:
    """Fresh 8-hex id (matches add_frame's secrets.token_hex(4) scheme)."""
    return secrets.token_hex(4)


def node_id_for_panel(panel_id: str) -> str | None:
    """CANONICAL reader: the panel's current node_id (single source of truth)."""
    with state._LOCK:
        return (state.load().get("panels", {}).get(str(panel_id), {}) or {}).get("node_id")


def get_node(node_id: str) -> dict | None:
    with state._LOCK:
        return state.load().get("nodes", {}).get(str(node_id))


def all_nodes() -> dict[str, dict]:
    with state._LOCK:
        return dict(state.load().get("nodes", {}))


def current_session(node_id: str) -> str | None:
    node = get_node(node_id)
    return node.get("current_session_id") if node else None


def mint_node_for_panel(panel_id: str, name: str) -> str:
    """THE SINGLE mint, called ONCE per launch from reset_for_launch (which
    MANAGER.launch always calls — there is no separate first-launch/relaunch path).

    Steps IN ORDER:
      (1) if a node is already bound to this panel, drop_node(old) to archive its
          SEMANTIC traces (edges/membership/notes) — a harmless no-op on first launch —
          and remove the retired node record;
      (2) mint a new node_id;
      (3) bind it to the panel with current_session_id=None, updating BOTH
          panels[panel_id].node_id AND nodes[new_id].panel_id (keep the two in sync).

    Returns the new node_id. positions[panel] is panel-keyed + untouched (the pin
    survives). Fork (Slice 7) does NOT call this — it uses repoint.

    Slice 2 (§7.3 recycle-safety): the position pin is panel-keyed and survives the
    relaunch, so a NEW node lands at the old tile's spot — inside whatever frame contained
    it — and positional membership would silently auto-join it. After minting, this
    auto-excludes the new node from every frame whose rect contains the surviving pin;
    the user re-includes deliberately (or drags out-and-back, which clears the exclusion).
    """
    panel_id = str(panel_id)
    old = node_id_for_panel(panel_id)
    if old:
        state.drop_node(old)  # archive the old node's SEMANTIC traces (relations; +excluded/notes later)
    nid = mint_node_id()
    with state._LOCK:
        data = state.load()
        nodes = data.setdefault("nodes", {})
        if old:
            nodes.pop(old, None)  # retire the old node record (no orphan)
        nodes[nid] = {
            "current_session_id": None,
            "panel_id": panel_id,
            "name": name,
            "prev_session_ids": [],
            "created": _now(),
        }
        data.setdefault("panels", {}).setdefault(panel_id, {})["node_id"] = nid
        state.save(data)
    # Slice 2 (§7.3 recycle-safety): auto-exclude the new node from every frame whose
    # rect contains the surviving pin, so the recycled tile is NOT silently a member.
    pin = state.get_positions().get(panel_id)
    if pin:
        state.exclude_node_from_containing_frames(nid, pin.get("x", -1), pin.get("y", -1))
    return nid


def drop_node_record(node_id: str) -> None:
    """Pop the node RECORD itself (call AFTER state.drop_node has archived its
    semantic traces). Mirrors the mint-time `nodes.pop(old, None)` recycle, but
    for an outright delete with no replacement node. No-op if absent."""
    with state._LOCK:
        data = state.load()
        if data.get("nodes", {}).pop(str(node_id), None) is not None:
            state.save(data)


def bind_session(node_id: str, session_id: str) -> None:
    """Called at session-capture: set the launch-minted node's current_session_id."""
    with state._LOCK:
        data = state.load()
        node = data.get("nodes", {}).get(str(node_id))
        if node is not None:
            node["current_session_id"] = session_id
            state.save(data)


def repoint(node_id: str, new_session_id: str) -> None:
    """FORK-WITH-HANDOFF (Slice 7): push the old current_session_id onto
    prev_session_ids and set the new one. Canvas semantic stores are NOT touched
    (they key on node_id, which is unchanged). Fork uses THIS — never
    mint_node_for_panel (which would mint a fresh node + drop_node the semantics)."""
    with state._LOCK:
        data = state.load()
        node = data.get("nodes", {}).get(str(node_id))
        if node is None:
            return
        old = node.get("current_session_id")
        if old:
            node.setdefault("prev_session_ids", []).append(old)
        node["current_session_id"] = new_session_id
        state.save(data)
