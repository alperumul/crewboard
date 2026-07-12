"""Verify Slice 7 (Canvas v2): fork-with-handoff preserves the node (repoint, NOT relaunch).

Drives the fork's deterministic pieces in-process (no subprocess): the operational
field-reset, then a simulated capture of the new sid. Proves:
  (1) _reset_for_fork clears session_id (so capture fires) and sets _pending_fork,
      WITHOUT minting a new node / dropping semantics
  (2) on capturing the new sid, the capture path REPOINTS: node_id preserved,
      current_session_id swapped, old sid pushed to prev_session_ids
  (3) all node-keyed semantics (edges, frame-membership/exclude, notes) + the panel-keyed
      pin survive the fork unchanged
  (4) fork does NOT call drop_node or mint_node_for_panel (the preserve-vs-relaunch guard)
  (5) the new session is --resume-able (session_id set to the captured sid)

Run: .venv/Scripts/python.exe spikes/verify_fork.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))

from hub import nodes, state  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(prefix="hub_forkspike_", suffix=".json", delete=False)
_tmp.close()
TMP = Path(_tmp.name)
state.STATE_FILE = TMP
state.save({"panels": {}})

from hub import sessions as _sessions  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


print("== Slice 7 fork-with-handoff verification ==")

# ---- setup: a launched session with a node + edges + frame-exclude + note + pin ----
s = _sessions.Session("0", "alpha", "C:/x"); s.reset_for_launch("alpha", "C:/x")
nid = s.node_id
s._handle_event({"type": "system", "session_id": "OLD-sid"})  # capture old sid -> bind_session
check("setup: node bound to the old session", nodes.current_session(nid) == "OLD-sid", f"{nodes.get_node(nid)}")

partner = _sessions.Session("1", "beta", "C:/x"); partner.reset_for_launch("beta", "C:/x")
np = partner.node_id
state.add_relation(nid, np, "FEEDS", "explicit")          # an edge ON the node
state.set_position("0", 30.0, 40.0)                        # the pin (panel-keyed)
fr = state.add_frame("grp", {"x": 20, "y": 20, "w": 40, "h": 40})
state.add_frame_exclude(fr["id"], nid)                     # frame-membership trace (node-keyed)
state.add_note({"node_id": nid}, "remember the retry path", "human")  # a note anchored to the node

# ---- install spies on the relaunch-only ops; they must NOT be called during a fork ----
calls = {"drop": 0, "mint": 0}
_orig_drop, _orig_mint = state.drop_node, nodes.mint_node_for_panel
state.drop_node = lambda *a, **k: (calls.__setitem__("drop", calls["drop"] + 1), _orig_drop(*a, **k))[1]
nodes.mint_node_for_panel = lambda *a, **k: (calls.__setitem__("mint", calls["mint"] + 1), _orig_mint(*a, **k))[1]

# ---- (1) fork field-reset (the exact op _fork_handoff runs in Phase 2) ----
print("\n(1) _reset_for_fork clears session_id + sets _pending_fork; no mint/drop")
old_sid = s.session_id
s._reset_for_fork()
check("session_id cleared (so the new sid can be captured)", s.session_id is None, f"{s.session_id}")
check("_pending_fork set (capture will repoint)", s._pending_fork is True, "")
check("node_id preserved by the reset", s.node_id == nid, f"{s.node_id}")
check("_reset_for_fork did NOT mint a new node", calls["mint"] == 0, f"mint={calls['mint']}")
check("_reset_for_fork did NOT drop_node the semantics", calls["drop"] == 0, f"drop={calls['drop']}")

# ---- (2) capture the new sid -> repoint ----
print("\n(2) capturing the new sid REPOINTS the node (preserve identity, swap session)")
s._handle_event({"type": "system", "session_id": "NEW-sid"})  # the new session's capture
check("node_id STILL preserved after capture", s.node_id == nid, f"{s.node_id}")
check("current_session_id swapped to the new sid", nodes.current_session(nid) == "NEW-sid", f"{nodes.get_node(nid)}")
check("old sid pushed to prev_session_ids", "OLD-sid" in (nodes.get_node(nid) or {}).get("prev_session_ids", []), f"{nodes.get_node(nid)}")
check("_pending_fork cleared after repoint", s._pending_fork is False, "")
check("the new session is --resume-able (session_id set)", s.session_id == "NEW-sid", f"{s.session_id}")

# ---- (3) all semantics + the pin survived ----
print("\n(3) edges, frame-exclude, note, and the pin all survive the fork")
check("the edge ON the node survived", any((str(r.get("from")), str(r.get("to"))) == (nid, np) for r in state.get_relations()), f"{state.get_relations()}")
check("the frame-exclude trace survived", nid in (state.get_frames()[0].get("excluded") or []), f"{state.get_frames()[0]}")
check("the node-anchored note survived", any((n.get("anchor") or {}).get("node_id") == nid for n in state.get_notes()), f"{state.get_notes()}")
check("the pin (panel-keyed) is unchanged", state.get_positions().get("0") == {"x": 30.0, "y": 40.0}, f"{state.get_positions().get('0')}")

# ---- (4) the whole fork never called drop_node / mint_node_for_panel ----
print("\n(4) fork preserved the node — drop_node + mint_node_for_panel NEVER called")
check("drop_node NOT called across the fork", calls["drop"] == 0, f"drop={calls['drop']}")
check("mint_node_for_panel NOT called across the fork", calls["mint"] == 0, f"mint={calls['mint']}")

# restore patched fns
state.drop_node, nodes.mint_node_for_panel = _orig_drop, _orig_mint

for q in (TMP, TMP.with_suffix(".json.tmp")):
    try:
        q.unlink()
    except OSError:
        pass

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== Slice 7 FORK-WITH-HANDOFF ====================")
print(f"{passed}/{total} checks passed")
print(f"\nSlice 7 FORK-WITH-HANDOFF: {'PASS' if passed == total else 'FAIL'}")
raise SystemExit(0 if passed == total else 1)
