"""Verify F-IDENT (Canvas v2): the identity contract `node_id` spine.

Drives hub.nodes + hub.state in-process against a throwaway state.json — no LLM, no
running hub. Proves (plan §4.1/§4.3/§5/§7.3):
  (1) EXACTLY ONE mint per launch: one live node bound to the panel, zero orphans,
      and the back-pointers (panels[pid].node_id <-> nodes[id].panel_id) agree
  (2) relaunch: positions[panel] UNCHANGED (pin survives); the OLD node's edges +
      (future) membership are dropped; a NEW node_id is minted
  (3) fork-readiness: repoint keeps all node-keyed edges valid (node_id unchanged)
  (4) migration: maps a panel-id board's relations + S8 members[] to node_ids cleanly,
      is IDEMPOTENT (twice == once), and leaves positions + suppressed_keys panel-keyed
  (5) edge-resolution invariant: every surviving edge connects two live nodes, each
      resolving to a current panel
  (6) drop_node / rekey are DEFENSIVE no-ops for excluded[] / notes (not present yet)

Run from the hub root with the hub venv (kill any hub on :5055 first):
    .venv/Scripts/python.exe spikes/verify_node_id.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))

from hub import nodes, state  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(prefix="hub_nodespike_", suffix=".json", delete=False)
_tmp.close()
TMP = Path(_tmp.name)
state.STATE_FILE = TMP

# import sessions AFTER the redirect so MANAGER restores from TMP
from hub import sessions as _sessions  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


print("== F-IDENT identity contract verification ==")

# ---- (1) exactly one mint per launch -----------------------------------------
print("\n(1) reset_for_launch mints exactly one live node, zero orphans, pointers agree")
state.save({"panels": {}})
s = _sessions.Session("0", "alpha", "C:/x")
s.reset_for_launch("alpha", "C:/x")  # the single mint path (no subprocess spawned)
nid1 = s.node_id
data = state.load()
check("a node_id was minted onto the Session", bool(nid1), f"node_id={nid1}")
check("panels[0].node_id == minted id (canonical reader)", nodes.node_id_for_panel("0") == nid1, f"{data.get('panels')}")
check("nodes[id].panel_id back-pointer == '0'", (data.get("nodes", {}).get(nid1) or {}).get("panel_id") == "0", f"{data.get('nodes')}")
check("exactly ONE node exists (no orphans)", len(data.get("nodes", {})) == 1, f"nodes={list(data.get('nodes', {}))}")
check("new node's current_session_id is None (bound at capture, not mint)", nodes.current_session(nid1) is None, f"{nodes.get_node(nid1)}")

# ---- bind at capture ----
nodes.bind_session(nid1, "sess-A")
check("bind_session sets current_session_id", nodes.current_session(nid1) == "sess-A", f"{nodes.get_node(nid1)}")

# ---- (2) relaunch: pin survives; old node's edges dropped; new node minted -----
print("\n(2) relaunch: positions[panel] survives; old node edges dropped; new node minted")
state.set_position("0", 33.0, 44.0)                       # pin the slot (panel-keyed)
state.add_relation(nid1, "ffffffff", "FEEDS", "inferred")  # an edge on the OLD node
s.reset_for_launch("alpha-2", "C:/x")                      # RELAUNCH -> mint_node_for_panel again
nid2 = s.node_id
data = state.load()
check("relaunch minted a NEW node_id", nid2 and nid2 != nid1, f"old={nid1} new={nid2}")
check("the old node record is gone (zero orphans)", nid1 not in data.get("nodes", {}), f"nodes={list(data.get('nodes', {}))}")
check("exactly one live node after relaunch", len(data.get("nodes", {})) == 1 and nid2 in data["nodes"], f"nodes={list(data.get('nodes', {}))}")
check("the OLD node's edge was dropped (drop_node)", not any(str(r.get("from")) == nid1 for r in data.get("relations", [])), f"rels={data.get('relations')}")
check("positions['0'] UNCHANGED across relaunch (pin survives)", data.get("positions", {}).get("0") == {"x": 33.0, "y": 44.0}, f"positions={data.get('positions')}")

# ---- (3) fork-readiness: repoint preserves node + its edges --------------------
print("\n(3) repoint keeps the node_id (and its edges) — fork-readiness")
nodes.bind_session(nid2, "sess-B")
state.add_relation(nid2, nid2, "FEEDS", "explicit") if False else None  # (no self-loop; real edge below)
# give nid2 a real partner edge
s2 = _sessions.Session("1", "beta", "C:/x")
s2.reset_for_launch("beta", "C:/x")
nid_b = s2.node_id
state.add_relation(nid2, nid_b, "REVIEWS", "explicit")
nodes.repoint(nid2, "sess-B2")
data = state.load()
check("repoint preserved the node_id", nid2 in data.get("nodes", {}), f"nodes={list(data.get('nodes', {}))}")
check("repoint swapped current_session_id", nodes.current_session(nid2) == "sess-B2", f"{nodes.get_node(nid2)}")
check("repoint pushed the old session to prev_session_ids", "sess-B" in (nodes.get_node(nid2) or {}).get("prev_session_ids", []), f"{nodes.get_node(nid2)}")
check("the node's edge is still valid (node_id unchanged)", any((str(r.get("from")), str(r.get("to"))) == (nid2, nid_b) for r in data.get("relations", [])), f"rels={data.get('relations')}")

# ---- (4) migration: panel-id board -> node-ids; idempotent; ops untouched ------
print("\n(4) migrate_to_node_ids maps a panel-id board cleanly + is idempotent + leaves ops alone")
FIXTURE = {
    "panels": {"7": {"name": "g", "session_id": "s7"}, "8": {"name": "h", "session_id": "s8"}},
    "relations": [{"from": "7", "to": "8", "type": "FEEDS", "source": "inferred", "reason": "x"}],
    "frames": [{"id": "fr1", "label": "grp", "rect": {"x": 0, "y": 0, "w": 9, "h": 9}, "auto_relay": False, "members": ["7", "8"]}],
    "positions": {"7": {"x": 10.0, "y": 20.0}},
    "suppressed_keys": ["relay:7->8:abc"],
}
state.save(FIXTURE)
state.migrate_to_node_ids()
mig = state.load()
n7 = nodes.node_id_for_panel("7")
n8 = nodes.node_id_for_panel("8")
check("each panel got a node_id", bool(n7) and bool(n8) and n7 != n8, f"n7={n7} n8={n8}")
check("migrated node bound to the panel's session_id", nodes.current_session(n7) == "s7", f"{nodes.get_node(n7)}")
check("relations endpoints rekeyed panel->node", any((str(r.get("from")), str(r.get("to"))) == (n7, n8) for r in mig.get("relations", [])), f"rels={mig.get('relations')}")
check("legacy frames[].members[] dropped", "members" not in mig["frames"][0], f"frame={mig['frames'][0]}")
check("positions LEFT panel-keyed + unchanged (NOT migrated)", mig.get("positions", {}).get("7") == {"x": 10.0, "y": 20.0}, f"positions={mig.get('positions')}")
check("suppressed_keys LEFT panel-keyed + unchanged (NOT migrated)", mig.get("suppressed_keys") == ["relay:7->8:abc"], f"suppressed={mig.get('suppressed_keys')}")
# idempotent: a 2nd run changes nothing
state.migrate_to_node_ids()
mig2 = state.load()
check("migration is IDEMPOTENT (twice == once)", json.dumps(mig, sort_keys=True) == json.dumps(mig2, sort_keys=True), "second run mutated state")

# ---- (5) edge-resolution invariant -------------------------------------------
print("\n(5) every surviving edge connects two live nodes that resolve to a panel")
mig = state.load()
live = mig.get("nodes", {})
panels = mig.get("panels", {})
ok_inv = all(
    str(r.get("from")) in live and str(r.get("to")) in live
    and str(live[str(r["from"])].get("panel_id")) in panels
    and str(live[str(r["to"])].get("panel_id")) in panels
    for r in mig.get("relations", [])
)
check("all edges resolve node->live node->panel", ok_inv, f"rels={mig.get('relations')}")

# ---- (6) drop_node / rekey defensive for not-yet-existing stores --------------
print("\n(6) drop_node / rekey are defensive no-ops for excluded[] / notes (absent here)")
before = state.load()
nodes_before = json.dumps(before.get("nodes", {}), sort_keys=True)
state.drop_node("does-not-exist")           # no excluded[]/notes present -> must not throw
state.rekey({"does-not-exist": "also-not"})  # likewise
after = state.load()
check("drop_node on absent stores did not throw / corrupt nodes", json.dumps(after.get("nodes", {}), sort_keys=True) == nodes_before, "nodes changed")

# ----------------------------------------------------------------------- cleanup
for p in (TMP, TMP.with_suffix(".json.tmp")):
    try:
        p.unlink()
    except OSError:
        pass

# ----------------------------------------------------------------------- verdict
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== F-IDENT IDENTITY CONTRACT ====================")
print(f"{passed}/{total} checks passed")
all_ok = passed == total
print(f"\nF-IDENT IDENTITY CONTRACT: {'PASS' if all_ok else 'FAIL'}")
raise SystemExit(0 if all_ok else 1)
