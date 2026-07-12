"""Verify Slice 2 (Canvas v2): frames first-class + the §7.3 hybrid membership model.

Drives hub.state + hub.nodes in-process against a throwaway state.json. Proves:
  (1) move/resize persist (world coords)
  (2) effective membership = inside-rect AND NOT excluded (computed, no stored members[])
  (3) exclude is sticky; remove clears it
  (4) the recycle-safety guard: mint_node_for_panel auto-excludes a NEW node from any
      frame whose rect contains the SURVIVING pin (§7.3)
  (5) legacy S8 members[] is absent on freshly created frames
  (6) drop_node clears a node from frames[].excluded[] (F-IDENT primitive now has data)
Does NOT assert auto-relay (Slice 6 — secretary untouched here).

Run: .venv/Scripts/python.exe spikes/verify_frames2.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))

from hub import nodes, state  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(prefix="hub_frames2spike_", suffix=".json", delete=False)
_tmp.close()
TMP = Path(_tmp.name)
state.STATE_FILE = TMP
state.save({"panels": {}})

from hub import sessions as _sessions  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def emembers(frame, pos_by_node):
    """python mirror of MapView.effectiveMembers: nodes whose point is inside the rect
    and not excluded. Here pos_by_node maps node_id -> {x,y}."""
    r = frame["rect"]; exc = set(map(str, frame.get("excluded", [])))
    return [nid for nid, p in pos_by_node.items()
            if r["x"] <= p["x"] <= r["x"] + r["w"] and r["y"] <= p["y"] <= r["y"] + r["h"] and str(nid) not in exc]


print("== Slice 2 frames first-class + §7.3 membership verification ==")

# ---- setup: two launched panels with nodes + pins ----
sA = _sessions.Session("0", "alpha", "C:/x"); sA.reset_for_launch("alpha", "C:/x")
sB = _sessions.Session("1", "beta", "C:/x"); sB.reset_for_launch("beta", "C:/x")
nA, nB = sA.node_id, sB.node_id
state.set_position("0", 20.0, 20.0)  # inside the frame below
state.set_position("1", 80.0, 80.0)  # outside
fr = state.add_frame("grp", {"x": 10, "y": 10, "w": 30, "h": 30})  # contains (20,20), not (80,80)

# ---- (5) fresh frame has no members[]; has excluded[] + author ----
print("\n(5) a fresh frame carries excluded[]+author and NO legacy members[]")
check("no members[] on a new frame", "members" not in fr, f"{fr}")
check("has excluded[] (empty)", fr.get("excluded") == [], f"{fr}")
check("author defaults to 'human'", fr.get("author") == "human", f"{fr}")

pos_by_node = {nA: {"x": 20.0, "y": 20.0}, nB: {"x": 80.0, "y": 80.0}}

# ---- (2) effective membership = inside AND not excluded ----
print("\n(2) effective membership = inside-rect AND not excluded")
check("A (inside) is an effective member", nA in emembers(state.get_frames()[0], pos_by_node), "A should be in")
check("B (outside) is NOT a member", nB not in emembers(state.get_frames()[0], pos_by_node), "B should be out")

# ---- (3) exclude is sticky; remove clears ----
print("\n(3) exclude is sticky; remove clears it")
state.add_frame_exclude(fr["id"], nA)
check("A excluded -> not an effective member despite being inside", nA not in emembers(state.get_frames()[0], pos_by_node), "exclude failed")
check("exclude is sticky (persisted)", nA in (state.get_frames()[0].get("excluded") or []), f"{state.get_frames()[0]}")
state.remove_frame_exclude(fr["id"], nA)
check("remove exclude -> A is an effective member again", nA in emembers(state.get_frames()[0], pos_by_node), "re-include failed")

# ---- (1) move/resize persist ----
print("\n(1) move/resize persist (world coords)")
state.update_frame_rect(fr["id"], {"x": 50, "y": 50, "w": 40, "h": 40})
moved = state.get_frames()[0]["rect"]
check("rect persisted moved/resized", moved == {"x": 50.0, "y": 50.0, "w": 40.0, "h": 40.0}, f"{moved}")
# now A(20,20) is outside, B(80,80) inside
check("membership recomputes from the new rect (B now in, A out)", emembers(state.get_frames()[0], pos_by_node) == [nB], f"{emembers(state.get_frames()[0], pos_by_node)}")

# ---- (4) recycle-safety: relaunch auto-excludes the NEW node from a containing frame ----
print("\n(4) recycle guard: relaunch auto-excludes the new node from frames containing the surviving pin")
# move the frame back over panel 0's pin (20,20), then relaunch panel 0
state.update_frame_rect(fr["id"], {"x": 10, "y": 10, "w": 30, "h": 30})
old_node_0 = nodes.node_id_for_panel("0")
sA.reset_for_launch("alpha-2", "C:/x")  # RELAUNCH: pin (20,20) survives; new node minted
new_node_0 = nodes.node_id_for_panel("0")
check("relaunch minted a new node for panel 0", new_node_0 and new_node_0 != old_node_0, f"old={old_node_0} new={new_node_0}")
check("the surviving pin is unchanged", state.get_positions().get("0") == {"x": 20.0, "y": 20.0}, f"{state.get_positions().get('0')}")
check("the NEW node is auto-excluded from the containing frame (recycle-safe)", new_node_0 in (state.get_frames()[0].get("excluded") or []), f"{state.get_frames()[0]}")
check("so the recycled tile is NOT an effective member until re-included",
      new_node_0 not in emembers(state.get_frames()[0], {new_node_0: {"x": 20.0, "y": 20.0}}), "recycle guard failed")

# ---- (6) drop_node clears the node from excluded[] ----
print("\n(6) drop_node clears a node from frames[].excluded[]")
state.add_frame_exclude(fr["id"], nB)
state.drop_node(nB)
check("drop_node removed nB from excluded[]", nB not in (state.get_frames()[0].get("excluded") or []), f"{state.get_frames()[0]}")

for q in (TMP, TMP.with_suffix(".json.tmp")):
    try:
        q.unlink()
    except OSError:
        pass

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== Slice 2 FRAMES FIRST-CLASS ====================")
print(f"{passed}/{total} checks passed")
print(f"\nSlice 2 FRAMES + §7.3 MEMBERSHIP: {'PASS' if passed == total else 'FAIL'}")
raise SystemExit(0 if passed == total else 1)
