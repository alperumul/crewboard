"""Verify Slice 6 (Canvas v2): group-image reasoning + RESTORED panel↔node auto-relay.

Drives observe() (stubbed _run_json) + _auto_eligible against a throwaway state.json.
Proves:
  (1) auto-relay RESTORED: _auto_eligible is True for two EFFECTIVE members of an auto-ON
      frame, False for an EXCLUDED member, False for a non-member (panel↔node hop works)
  (2) a PROMOTED frame gets a group description from Pass B over its effective members;
      an UNPROMOTED frame gets none
  (3) per-turn pass-count UNCHANGED: exactly 2 _run_json calls (Haiku + Sonnet), no third
  (4) gated: <2 live OR quiet -> Pass B skipped -> no group description written

Run: .venv/Scripts/python.exe spikes/verify_group_image.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))

from hub import nodes, secretary, state  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(prefix="hub_groupspike_", suffix=".json", delete=False)
_tmp.close()
TMP = Path(_tmp.name)
state.STATE_FILE = TMP
state.save({"panels": {}})
secretary.CLAUDE = "claude"  # truthy
secretary._DEBOUNCE_WINDOW_S = 0.0
secretary._last_passb_ts = float("-inf")

from hub import sessions as _sessions  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ---- two launched panels with nodes + pins inside a frame ----
for pid, nm in (("1", "rev"), ("2", "impl"), ("3", "other")):
    s = _sessions.Session(pid, nm, "C:/x"); s.reset_for_launch(nm, "C:/x")
    state.update_panel(pid, session_id="s" + pid, name=nm)
n1, n2, n3 = nodes.node_id_for_panel("1"), nodes.node_id_for_panel("2"), nodes.node_id_for_panel("3")
state.set_position("1", 20.0, 20.0)  # inside frame
state.set_position("2", 25.0, 25.0)  # inside frame
state.set_position("3", 90.0, 90.0)  # outside frame
fr = state.add_frame("the-initiative", {"x": 10, "y": 10, "w": 30, "h": 30})
state.set_frame_auto_relay(fr["id"], True)

BOARD = [
    {"panel_id": "1", "name": "rev", "state": "done", "session_id": "s1", "task": "review", "last_message": "LGTM fix retry"},
    {"panel_id": "2", "name": "impl", "state": "idle", "session_id": "s2", "task": "ship", "last_message": "wip"},
    {"panel_id": "3", "name": "other", "state": "done", "session_id": "s3", "task": "unrelated", "last_message": "done"},
]

print("== Slice 6 group-image + restored auto-relay verification ==")

# ---- (1) auto-relay restored on effective membership ----
print("\n(1) _auto_eligible: effective members eligible; excluded / non-member not")
positions = state.get_positions()
auto_frames = [f for f in state.get_frames() if f.get("auto_relay")]
check("FEEDS between two effective members IS auto-eligible", secretary._auto_eligible({"from": "1", "to": "2", "type": "FEEDS"}, auto_frames, positions), "should be eligible")
check("FEEDS to a NON-member (outside the rect) is NOT eligible", not secretary._auto_eligible({"from": "1", "to": "3", "type": "FEEDS"}, auto_frames, positions), "panel 3 is outside")
state.add_frame_exclude(fr["id"], n2)  # exclude member 2 (node-keyed)
auto_frames = [f for f in state.get_frames() if f.get("auto_relay")]
check("FEEDS to an EXCLUDED member is NOT eligible (panel<->node hop)", not secretary._auto_eligible({"from": "1", "to": "2", "type": "FEEDS"}, auto_frames, positions), "exclude should block")
check("a STEERING (CONFLICT) relay between members is NOT eligible", not secretary._auto_eligible({"from": "1", "to": "2", "type": "CONFLICT"}, auto_frames, positions), "steering never auto")
state.remove_frame_exclude(fr["id"], n2)  # restore for the group test

# ---- (2)+(3) promote -> group description from Pass B; pass-count unchanged ----
print("\n(2)+(3) promoted frame gets a group description; (3) exactly 2 passes (no new LLM call)")
state.set_frame_promoted(fr["id"], True)
_calls: list[str] = []
_HAIKU = json.dumps({"brief": "b", "name_suggestion": None})
_SONNET = json.dumps({
    "frame_suggestion": None, "triage": [], "relays": [],
    "group_images": [{"frame": fr["id"], "description": "Reviewer + impl shipping the retry-path fix; review is in, impl wiring it."}],
})


def _rec(prompt, model, *a, **k):
    _calls.append(model)
    return (_HAIKU if model == secretary.HAIKU_MODEL else _SONNET), 0.0


secretary._run_json = _rec
secretary._last_passb_ts = float("-inf")
secretary.FEED.clear()
secretary.observe("1", "rev", "LGTM fix retry", BOARD)
check("exactly TWO _run_json calls (Haiku + Sonnet) — no new LLM call", len(_calls) == 2, f"calls={_calls}")
desc = next((f for f in state.get_frames() if f["id"] == fr["id"]), {}).get("description", "")
check("the promoted frame got its group description", desc.startswith("Reviewer + impl"), f"desc={desc!r}")
# unpromoted frame gets no description
fr2 = state.add_frame("unpromoted", {"x": 60, "y": 60, "w": 20, "h": 20})
secretary._last_passb_ts = float("-inf")
secretary.observe("1", "rev", "again", BOARD)
check("an UNPROMOTED frame gets no description", not next((f for f in state.get_frames() if f["id"] == fr2["id"]), {}).get("description"), "unpromoted leaked a description")

# ---- (4) gated: quiet -> Pass B skipped -> no new description ----
print("\n(4) Quiet board mode skips Pass B -> the group image is absent (gated, not a bug)")
state.set_frame_description(fr["id"], "")  # clear
state.set_board_mode("quiet")
_calls.clear()
secretary._last_passb_ts = float("-inf")
secretary.observe("1", "rev", "quiet turn", BOARD)
check("quiet -> only Pass A ran (1 call)", len(_calls) == 1 and _calls[0] == secretary.HAIKU_MODEL, f"calls={_calls}")
check("quiet -> no group description written", not next((f for f in state.get_frames() if f["id"] == fr["id"]), {}).get("description"), "quiet leaked a description")
state.set_board_mode("full")

for q in (TMP, TMP.with_suffix(".json.tmp")):
    try:
        q.unlink()
    except OSError:
        pass

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== Slice 6 GROUP IMAGE + AUTO-RELAY ====================")
print(f"{passed}/{total} checks passed")
print(f"\nSlice 6 GROUP IMAGE + AUTO-RELAY: {'PASS' if passed == total else 'FAIL'}")
raise SystemExit(0 if passed == total else 1)
