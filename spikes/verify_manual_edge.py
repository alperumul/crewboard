"""Verify Slice 3 (Canvas v2): manual edges (the unified edge object, node-keyed).

Drives /edge + add_relation in-process against a throwaway state.json. Proves:
  (1) POST /edge records an EXPLICIT, author='human', node-keyed edge with the chosen type
  (2) a later secretary INFERRED upsert on the same pair does NOT downgrade it
      (source stays explicit, author stays human) — the no-downgrade contract
  (3) endpoints are node_ids (not panel ids); a self-loop / missing-node is rejected
Focus-scoped legibility is VISUAL (owner gate), not asserted here.

Run: .venv/Scripts/python.exe spikes/verify_manual_edge.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))

from hub import nodes, state  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(prefix="hub_edgespike_", suffix=".json", delete=False)
_tmp.close()
TMP = Path(_tmp.name)
state.STATE_FILE = TMP
state.save({"panels": {}})

from app import app  # noqa: E402
from hub import sessions as _sessions  # noqa: E402

# two launched panels with nodes
for pid, nm in (("0", "alpha"), ("1", "beta")):
    s = _sessions.Session(pid, nm, "C:/x"); s.reset_for_launch(nm, "C:/x")

client = app.test_client()
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def edge(frm_pid, to_pid):
    nf, nt = nodes.node_id_for_panel(frm_pid), nodes.node_id_for_panel(to_pid)
    return next((r for r in state.get_relations() if (str(r.get("from")), str(r.get("to"))) == (nf, nt)), None)


print("== Slice 3 manual edges verification ==")

# ---- (1) POST /edge records an explicit, human, node-keyed edge ----
print("\n(1) POST /edge -> explicit, author=human, node-keyed, chosen type")
r = client.post("/edge", json={"from_panel": "0", "to_panel": "1", "rel_type": "REVIEWS"})
check("/edge returns 200", r.status_code == 200, f"{r.get_json()}")
e = edge("0", "1")
check("edge persisted", e is not None, f"{state.get_relations()}")
check("endpoints are node_ids (8-hex), not panel ids", bool(e) and e["from"] == nodes.node_id_for_panel("0") and e["from"] not in ("0", "1"), f"{e}")
check("source is explicit", bool(e) and e.get("source") == "explicit", f"{e}")
check("author is human (warm)", bool(e) and e.get("author") == "human", f"{e}")
check("type is the chosen REVIEWS", bool(e) and e.get("type") == "REVIEWS", f"{e}")

# ---- (2) a later inferred upsert does NOT downgrade ----
print("\n(2) a later secretary inferred upsert preserves explicit + human (no-downgrade)")
state.add_relation(nodes.node_id_for_panel("0"), nodes.node_id_for_panel("1"), "FEEDS", "inferred", reason="secretary later")
e = edge("0", "1")
check("source still explicit after inferred upsert", bool(e) and e.get("source") == "explicit", f"{e}")
check("author still human after inferred upsert", bool(e) and e.get("author") == "human", f"{e}")
check("the inferred reason DID attach (additive, not a downgrade)", bool(e) and e.get("reason") == "secretary later", f"{e}")

# ---- (3) self-loop + missing node rejected ----
print("\n(3) self-loop and missing-node are rejected")
check("self-loop -> 400", client.post("/edge", json={"from_panel": "0", "to_panel": "0"}).status_code == 400, "")
check("unlaunched panel (no node) -> 409", client.post("/edge", json={"from_panel": "0", "to_panel": "9"}).status_code == 409, "")

for q in (TMP, TMP.with_suffix(".json.tmp")):
    try:
        q.unlink()
    except OSError:
        pass

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== Slice 3 MANUAL EDGES ====================")
print(f"{passed}/{total} checks passed")
print(f"\nSlice 3 MANUAL EDGES + FOCUS LEGIBILITY: {'PASS' if passed == total else 'FAIL'}")
raise SystemExit(0 if passed == total else 1)
