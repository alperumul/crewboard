"""Verify S5 (freeform canvas): the backend half — node pins + group-frames.

S5 turns the deterministic-lane Map into an actionable canvas: tiles can be
dragged to a pinned x/y that PERSISTS, auto-arrange clears the pins back to the
lane layout, and labeled group-frames are stored as notes. This spike proves,
deterministically and WITHOUT spending a live Sonnet call, that:

  (1) POST /node/<id>/pos pins a node; the pin PERSISTS and surfaces as x/y on
      GET /sessions, and survives a "reload" (a fresh state.load() off disk)
  (2) coords are clamped to the 0..100 stage (a bad client value can't park a
      tile off-canvas)
  (3) POST /node/<id>/pos with null/empty coords UN-pins one node; the canvas-
      wide POST /positions/clear (Auto-arrange) drops every pin
  (4) GET/POST /frames + POST /frames/<id>/delete CRUD labeled regions, and the
      POST validates label + a full {x,y,w,h} rect
  (5) the secretary prompt ASKS for a frame_suggestion, and observe() emits a
      validated {kind:frame_suggestion,label,panels} feed item — dropping a
      hallucinated panel id and requiring >=2 real members

It drives observe() in-process with a stubbed LLM and redirects persistence to a
throwaway state.json, so it never touches the real board and needs no running hub
or API key. Run from the hub root with the hub venv:
    .venv/Scripts/python.exe spikes/verify_pos.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))  # so `from hub import ...` / `import app` work from any cwd

from hub import secretary, state  # noqa: E402

# ---- redirect ALL persistence to a throwaway file (never touch the real board) ----
_tmp = tempfile.NamedTemporaryFile(prefix="hub_posspike_", suffix=".json", delete=False)
_tmp.close()
TMP = Path(_tmp.name)
state.STATE_FILE = TMP
BOARD = [
    {"panel_id": "1", "name": "stream-rank", "state": "running", "task": "matchup-state ranker", "last_message": ""},
    {"panel_id": "2", "name": "stream-card", "state": "running", "task": "player-film card port", "last_message": ""},
    {"panel_id": "3", "name": "infra-deploy", "state": "done", "task": "docker deploy", "last_message": "ok"},
]
state.save(
    {"panels": {b["panel_id"]: {"name": b["name"], "session_id": "s" + b["panel_id"]} for b in BOARD}, "relations": []}
)

# importing app AFTER the redirect means MANAGER restores from TMP, and every
# route reads TMP too (same module singleton).
from app import app  # noqa: E402

client = app.test_client()


# ----------------------------------------------------------------- check harness
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def panel(pid: str) -> dict:
    panels = (client.get("/sessions").get_json() or {}).get("panels", [])
    return next((p for p in panels if str(p.get("panel_id")) == pid), {})


print("== S5 freeform-canvas backend verification ==")

# ---- (1) pin persists + surfaces on /sessions + survives a reload
print("\n(1) POST /node/<id>/pos pins; persists; surfaces on /sessions; survives reload")
r = client.post("/node/1/pos", json={"x": 33.3, "y": 41.0})
check(
    "POST /node/1/pos returns 200 + pinned",
    r.status_code == 200 and r.get_json().get("pinned") is True,
    f"{r.get_json()}",
)
p1 = panel("1")
check("/sessions reflects x for pinned node", p1.get("x") == 33.3, f"x={p1.get('x')}")
check("/sessions reflects y for pinned node", p1.get("y") == 41.0, f"y={p1.get('y')}")
check("an un-pinned node omits x/y (falls back to lane layout)", "x" not in panel("2"), f"panel2={panel('2')}")
# "reload": a fresh read off disk (simulates a hub restart reading state.json)
reloaded = state.load().get("positions", {})
check("pin persisted to disk under `positions`", reloaded.get("1") == {"x": 33.3, "y": 41.0}, f"positions={reloaded}")

# ---- (2) coords clamp to the 0..100 stage
print("\n(2) coords clamp to 0..100")
client.post("/node/2/pos", json={"x": 150, "y": -10})
p2 = panel("2")
check("x clamped to 100", p2.get("x") == 100.0, f"x={p2.get('x')}")
check("y clamped to 0", p2.get("y") == 0.0, f"y={p2.get('y')}")
rbad = client.post("/node/2/pos", json={"x": "nope", "y": 5})
check("non-numeric coords rejected (400)", rbad.status_code == 400, f"status={rbad.status_code}")

# ---- (3) un-pin one node + canvas-wide auto-arrange
print("\n(3) un-pin one node; Auto-arrange clears all pins")
client.post("/node/1/pos", json={})  # empty body -> clear that pin
check("empty-body POST un-pins node 1", "x" not in panel("1"), f"panel1={panel('1')}")
check("node 2 still pinned after clearing node 1", "x" in panel("2"), f"panel2={panel('2')}")
client.post("/node/3/pos", json={"x": 80, "y": 25})  # pin a 3rd so >1 pin exists
check(
    ">1 pin exists before Auto-arrange", len(state.load().get("positions", {})) >= 2, f"{state.load().get('positions')}"
)
rc = client.post("/positions/clear")
check("POST /positions/clear returns 200", rc.status_code == 200)
check("Auto-arrange dropped every pin", state.load().get("positions", {}) == {}, f"{state.load().get('positions')}")
check("no node reports x/y after Auto-arrange", all("x" not in panel(p) for p in ("1", "2", "3")))

# ---- (4) frames CRUD + validation
print("\n(4) frames CRUD + validation")
rf = client.post("/frames", json={"label": "streaming-redesign", "rect": {"x": 10, "y": 15, "w": 40, "h": 55}})
fj = rf.get_json()
fid = (fj or {}).get("frame", {}).get("id")
check("POST /frames returns a frame with an id", rf.status_code == 200 and bool(fid), f"{fj}")
check(
    "created frame carries label + rect",
    (fj or {}).get("frame", {}).get("label") == "streaming-redesign"
    and (fj or {}).get("frame", {}).get("rect", {}).get("w") == 40.0,
    f"{fj}",
)
served = (client.get("/frames").get_json() or {}).get("frames", [])
check("GET /frames lists the created frame", any(f.get("id") == fid for f in served), f"frames={served}")
check(
    "POST /frames without a label -> 400",
    client.post("/frames", json={"rect": {"x": 1, "y": 1, "w": 1, "h": 1}}).status_code == 400,
)
check(
    "POST /frames with an incomplete rect -> 400",
    client.post("/frames", json={"label": "x", "rect": {"x": 1, "y": 1}}).status_code == 400,
)
rd = client.post(f"/frames/{fid}/delete")
check("POST /frames/<id>/delete returns 200", rd.status_code == 200, f"{rd.get_json()}")
check(
    "deleted frame is gone",
    not any(f.get("id") == fid for f in (client.get("/frames").get_json() or {}).get("frames", [])),
)
check("deleting an unknown frame -> 404", client.post("/frames/deadbeef/delete").status_code == 404)

# ---- (5) secretary frame_suggestion: prompt asks + observe() emits a validated item
print("\n(5) secretary frame_suggestion (prompt + parse)")
check('prompt schema mentions "frame_suggestion"', "frame_suggestion" in secretary._SECRETARY_PROMPT)
CANNED = {
    "brief": "Wiring the matchup-state ranker for the streaming redesign.",
    "name_suggestion": None,
    "frame_suggestion": {"label": "streaming-redesign", "panels": ["1", "2", "999"]},  # 999 hallucinated -> dropped
    "triage": [],
    "relays": [],
}
# S4: this section drives observe() twice back-to-back; disable the new Pass B
# frequency-gate debounce so the SECOND call also runs Pass B — otherwise it would
# be collapsed (no frame produced) and the <2-member-drop check below would pass
# VACUOUSLY instead of exercising the membership guard it means to test.
secretary._DEBOUNCE_WINDOW_S = 0.0  # type: ignore[attr-defined]
secretary._last_passb_ts = float("-inf")  # type: ignore[attr-defined]
secretary._run_json = lambda *a, **k: (json.dumps(CANNED), 0.0)  # type: ignore[assignment]
items, _cost = secretary.observe("1", "stream-rank", "ranker wired", BOARD)
fs = next((it for it in items if it.get("kind") == "frame_suggestion"), None)
check("observe() emits a frame_suggestion item", fs is not None, f"items={items}")
check("frame_suggestion carries the label", bool(fs) and fs.get("label") == "streaming-redesign", f"{fs}")
check("hallucinated panel 999 dropped; only real members kept", bool(fs) and fs.get("panels") == ["1", "2"], f"{fs}")

# a single-real-member proposal is NOT surfaced (needs >=2)
CANNED2 = {**CANNED, "frame_suggestion": {"label": "solo", "panels": ["1", "999"]}}
secretary._run_json = lambda *a, **k: (json.dumps(CANNED2), 0.0)  # type: ignore[assignment]
items2, _ = secretary.observe("2", "stream-card", "card ported", BOARD)
check(
    "a <2-real-member frame_suggestion is dropped",
    not any(it.get("kind") == "frame_suggestion" for it in items2),
    f"items2={[it.get('kind') for it in items2]}",
)

# ----------------------------------------------------------------------- cleanup
for p in (TMP, TMP.with_suffix(".json.tmp")):
    try:
        p.unlink()
    except OSError:
        pass

# ----------------------------------------------------------------------- verdict
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== S5 FREEFORM CANVAS (backend) ====================")
print(f"{passed}/{total} checks passed")
all_ok = passed == total
print(f"\nS5 FREEFORM-CANVAS BACKEND: {'PASS' if all_ok else 'FAIL'}")
raise SystemExit(0 if all_ok else 1)
