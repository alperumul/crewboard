"""Verify Slice 4 (Canvas v2): sticky notes store + CRUD + dedup/ceiling.

Drives /notes + state.add_note against a throwaway state.json. Proves:
  (1) create/persist/list; anchor types node_id (translated from panel) / frame_id / free
  (2) dedup: same (anchor, text) twice -> ONE note
  (3) secretary ceiling: > ceiling secretary notes evicts the oldest; human notes uncapped
  (4) node-anchored notes are node-keyed AND drop_node cleans them (F-IDENT primitive)
  (5) frame/free notes survive drop_node (not node-keyed)
Selective surfacing is VISUAL (owner gate).

Run: .venv/Scripts/python.exe spikes/verify_notes.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))

from hub import nodes, state  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(prefix="hub_notespike_", suffix=".json", delete=False)
_tmp.close()
TMP = Path(_tmp.name)
state.STATE_FILE = TMP
state.save({"panels": {}})

from app import app  # noqa: E402
from hub import sessions as _sessions  # noqa: E402

s = _sessions.Session("0", "alpha", "C:/x"); s.reset_for_launch("alpha", "C:/x")
nA = s.node_id
client = app.test_client()
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


print("== Slice 4 sticky notes verification ==")

# ---- (1) create + anchor types ----
print("\n(1) create/persist/list; node/frame/free anchors")
r1 = client.post("/notes", json={"anchor": {"panel_id": "0"}, "text": "watch the retry path"})
check("node-anchored note created", r1.status_code == 200 and r1.get_json().get("note"), f"{r1.get_json()}")
note0 = r1.get_json()["note"]
check("node anchor stored as node_id (translated)", note0["anchor"].get("node_id") == nA, f"{note0}")
check("author defaults human (warm)", note0.get("author") == "human", f"{note0}")
client.post("/notes", json={"anchor": {"frame_id": "fr1"}, "text": "group note"})
client.post("/notes", json={"anchor": {"free": {"x": 40, "y": 60}}, "text": "floating thought"})
served = (client.get("/notes").get_json() or {}).get("notes", [])
check("GET /notes lists all three", len(served) == 3, f"{served}")

# ---- (2) dedup ----
print("\n(2) same (anchor,text) twice -> one note")
client.post("/notes", json={"anchor": {"panel_id": "0"}, "text": "watch the retry path"})
served = (client.get("/notes").get_json() or {}).get("notes", [])
check("duplicate node note deduped", sum(1 for n in served if n["text"] == "watch the retry path") == 1, f"{served}")

# ---- (3) secretary ceiling ----
print("\n(3) secretary notes capped at the ceiling; human uncapped")
for i in range(state._NOTE_SECRETARY_CEILING + 5):
    state.add_note({"free": {"x": i, "y": i}}, f"sec {i}", "secretary")
sec = [n for n in state.get_notes() if n.get("author") == "secretary"]
check("secretary notes capped at the ceiling", len(sec) == state._NOTE_SECRETARY_CEILING, f"count={len(sec)}")
check("the OLDEST secretary note was evicted (sec 0 gone)", not any(n["text"] == "sec 0" for n in sec), "FIFO evict")

# ---- (4) drop_node cleans node-anchored notes ----
print("\n(4) drop_node removes node-anchored notes; (5) frame/free survive")
before = len(state.get_notes())
state.drop_node(nA)
after = state.get_notes()
check("node-anchored note removed by drop_node", not any((n.get("anchor") or {}).get("node_id") == nA for n in after), f"{after}")
check("frame-anchored note survived drop_node", any((n.get("anchor") or {}).get("frame_id") == "fr1" for n in after), "frame note lost")
check("free note survived drop_node", any((n.get("anchor") or {}).get("free") for n in after), "free note lost")

for q in (TMP, TMP.with_suffix(".json.tmp")):
    try:
        q.unlink()
    except OSError:
        pass

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== Slice 4 STICKY NOTES ====================")
print(f"{passed}/{total} checks passed")
print(f"\nSlice 4 STICKY NOTES: {'PASS' if passed == total else 'FAIL'}")
raise SystemExit(0 if passed == total else 1)
