"""Verify S0: the situation-board brief — comprehension lift + cost measurement.

A persisted per-session BRIEF line is folded into the EXISTING event-driven
observe() pass (NO new LLM call). This spike proves, against a live hub:

  (1) /secretary/brief returns a <=400-char line per LIVE session (cap raised 350->400 in S3)
  (2) the line is TASK-GROUNDED even when the session's last_message is a terse
      done-token / confirmation (HIGH-1) — the brief reflects the launch task,
      not just the ~3 words the session actually emitted
  (3) _board_lines emits the brief, not the 200-char snippet, for acted sessions
      (the comprehension lift itself — tested directly on the function), and
      still falls back to the snippet for a never-briefed panel
  (4) the brief PERSISTS across a hub restart (state.json write + a COLD process
      re-seeding BRIEF via SessionManager._restore)
  (5) relaunching a panel with a NEW task DROPS the old brief immediately, then a
      fresh, differently-grounded brief replaces it after the new turn (HIGH-2)
  (6) the per-turn observe() cost delta is MEASURED, not assumed (MED-3)

Prereq: the hub is already running (`.venv/Scripts/python.exe app.py`).
Run from the hub root with the hub venv:
    .venv/Scripts/python.exe spikes/verify_brief.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:5055"
HUB_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = HUB_ROOT / "state.json"
COST_LOG = HUB_ROOT / "run" / "secretary_cost.log"
sys.path.insert(0, str(HUB_ROOT))  # so `from hub import ...` works regardless of cwd

PANEL_A, PANEL_B = "1", "2"

# Distinct, keyword-rich tasks; each replies with ONE terse word so the brief can
# only be rich if it is grounded in the TASK, not the output (HIGH-1).
TASK_A = (
    "You are assigned to build the ANTARCTIC ice-core temperature reconstruction "
    "pipeline. For THIS turn only, reply with exactly the single word ACKNOWLEDGED "
    "and nothing else."
)
KEYWORDS_A = ("antarctic", "ice", "core", "temperature")

TASK_B = (
    "You are assigned to migrate the BILLING service from REST to gRPC. For THIS "
    "turn only, reply with exactly the single word READY and nothing else."
)
KEYWORDS_B = ("billing", "grpc", "rest", "migrat")

# Relaunch A onto a totally different goal (HIGH-2): the old Antarctic brief must
# vanish and a revenue-grounded one take its place.
TASK_A2 = (
    "You are now assigned to write the QUARTERLY revenue FORECAST spreadsheet. For "
    "THIS turn only, reply with exactly the single word OK and nothing else."
)
KEYWORDS_A2 = ("quarterly", "revenue", "forecast", "spreadsheet")


# ---------------------------------------------------------------- http helpers
def post(path: str, body: dict):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


def snap(panel: str) -> dict:
    for p in get("/sessions")["panels"]:
        if p["panel_id"] == panel:
            return p
    return {}


def brief_row(panel: str) -> dict:
    for s in get("/secretary/brief")["sessions"]:
        if s["panel_id"] == panel:
            return s
    return {}


def feed_cost() -> float:
    return float(get("/secretary/feed").get("total_cost_usd") or 0.0)


def wait_settle(panel: str, timeout=120) -> str:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = snap(panel).get("state")
        if st in ("done", "awaiting-input", "error"):
            return st
        time.sleep(0.5)
    return "TIMEOUT"


def wait_brief(panel: str, timeout=90) -> str:
    """Poll until the off-thread observe() has written a brief for this panel."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        b = brief_row(panel).get("brief") or ""
        if b.strip():
            return b
        time.sleep(1.0)
    return ""


def cost_log_lines() -> list[str]:
    if not COST_LOG.exists():
        return []
    return COST_LOG.read_text(encoding="utf-8").splitlines()


# ----------------------------------------------------------------------- setup
print("== S0 brief verification ==")
try:
    health = get("/health")
except Exception as e:  # noqa: BLE001
    print(f"FAIL: hub not reachable at {BASE} ({e}). Start it: .venv/Scripts/python.exe app.py")
    raise SystemExit(1)
print(f"hub up: step {health.get('step')}, default_cwd={health.get('default_cwd')}")

cost_lines_before = cost_log_lines()
cost_total_before = feed_cost()

print(f"\nlaunching panel {PANEL_A} (Antarctic) and panel {PANEL_B} (billing/gRPC), terse one-word replies...")
post(f"/launch/{PANEL_A}", {"name": "ice-core", "prompt": TASK_A})
post(f"/launch/{PANEL_B}", {"name": "billing", "prompt": TASK_B})

stA, stB = wait_settle(PANEL_A), wait_settle(PANEL_B)
snapA, snapB = snap(PANEL_A), snap(PANEL_B)
print(f"panel {PANEL_A} settled: state={stA!r} last_message={snapA.get('last_message')!r}")
print(f"panel {PANEL_B} settled: state={stB!r} last_message={snapB.get('last_message')!r}")

print("waiting for the secretary's observe() to write the briefs (off-thread Sonnet call)...")
briefA = wait_brief(PANEL_A)
briefB = wait_brief(PANEL_B)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ---- (1) a bounded line per live session
print("\n(1) bounded brief line per live session")
check("panel A has a brief", bool(briefA.strip()), f"{len(briefA)} chars")
check("panel B has a brief", bool(briefB.strip()), f"{len(briefB)} chars")
check("panel A brief <= 400 chars", len(briefA) <= 400, f"len={len(briefA)}")
check("panel B brief <= 400 chars", len(briefB) <= 400, f"len={len(briefB)}")

# ---- (2) task-grounded even though the output was a single terse word
print("\n(2) task-grounded despite a terse last_message (HIGH-1)")
lmA = snapA.get("last_message") or ""
lmB = snapB.get("last_message") or ""
groundedA = any(k in briefA.lower() for k in KEYWORDS_A)
groundedB = any(k in briefB.lower() for k in KEYWORDS_B)
liftA = len(briefA) > len(lmA)
liftB = len(briefB) > len(lmB)
check("A brief references its launch task", groundedA, f"keywords {KEYWORDS_A} vs brief={briefA!r}")
check("B brief references its launch task", groundedB, f"keywords {KEYWORDS_B} vs brief={briefB!r}")
check("A brief is richer than its raw snippet", liftA, f"brief {len(briefA)}ch > last_message {len(lmA)}ch ({lmA!r})")
check("B brief is richer than its raw snippet", liftB, f"brief {len(briefB)}ch > last_message {len(lmB)}ch ({lmB!r})")

# ---- (3) the comprehension lift: _board_lines uses the brief, not the snippet
print("\n(3) _board_lines emits the brief, not the 200-char snippet (the lift)")
from hub import secretary  # noqa: E402 — imported here so the hub is confirmed up first

RICH = "Panel 99 is building the Antarctic ice-core pipeline; this turn it acknowledged the assignment."
SNIPPET = "ACKNOWLEDGED_terse_snippet_zzz"
secretary.seed_brief("99", RICH)
acted_line = secretary._board_lines([{"panel_id": "99", "name": "x", "state": "done", "last_message": SNIPPET}])
fallback_line = secretary._board_lines(
    [{"panel_id": "98", "name": "y", "state": "done", "last_message": "FALLBACK_snip_q"}]
)
check("acted session shows the rich brief", RICH in acted_line, acted_line.strip())
check("acted session hides the raw snippet", SNIPPET not in acted_line)
check("never-briefed session falls back to snippet", "FALLBACK_snip_q" in fallback_line, fallback_line.strip())

# ---- (4) persistence across a real restart (cold process re-seeds from state.json)
print("\n(4) brief persists across a hub restart")
disk = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("panels", {})
diskA = (disk.get(PANEL_A, {}).get("brief") or "").strip()
diskB = (disk.get(PANEL_B, {}).get("brief") or "").strip()
check("state.json persisted A's brief", bool(diskA), f"{diskA!r}")
check("state.json persisted B's brief", bool(diskB), f"{diskB!r}")

restore_code = (
    "import json,sys;"
    f"sys.path.insert(0, r'{HUB_ROOT}');"
    "from hub import sessions, secretary;"  # importing sessions runs _restore() -> seeds BRIEF
    "sys.stdout.write(json.dumps(secretary.brief_snapshot()))"
)
proc = subprocess.run(
    [sys.executable, "-c", restore_code], capture_output=True, text=True, cwd=str(HUB_ROOT), timeout=60
)
cold = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {}
check("cold restart re-seeds A's brief", bool((cold.get(PANEL_A) or "").strip()), f"{cold.get(PANEL_A)!r}")
check("cold restart re-seeds B's brief", bool((cold.get(PANEL_B) or "").strip()), f"{cold.get(PANEL_B)!r}")

# ---- (5) relaunch drops the old brief, then re-grounds on the new task (HIGH-2)
print("\n(5) relaunch with a NEW task drops the old brief (HIGH-2)")
old_briefA = briefA
post(f"/launch/{PANEL_A}", {"name": "revenue", "prompt": TASK_A2})
# reset_for_launch clears the brief synchronously, before the new turn runs.
dropped = ""
for _ in range(8):
    dropped = brief_row(PANEL_A).get("brief") or ""
    if not dropped.strip():
        break
    time.sleep(0.25)
check("old brief dropped immediately on relaunch", not dropped.strip(), f"saw {dropped!r}")

wait_settle(PANEL_A)
new_briefA = wait_brief(PANEL_A)
regrounded = any(k in new_briefA.lower() for k in KEYWORDS_A2)
stale_gone = not any(k in new_briefA.lower() for k in ("antarctic", "ice-core", "ice core"))
check("a fresh brief replaces it", bool(new_briefA.strip()) and new_briefA != old_briefA, f"{new_briefA!r}")
check("new brief is grounded in the NEW task", regrounded, f"keywords {KEYWORDS_A2}")
check("new brief no longer mentions the OLD task", stale_gone)

# ---- (6) cost: measure the per-turn observe() delta (MED-3)
print("\n(6) per-turn observe() cost measured (MED-3)")
new_cost_lines = cost_log_lines()[len(cost_lines_before) :]
observe_costs = []
for ln in new_cost_lines:
    parts = ln.split("\t")
    if len(parts) == 2 and parts[0] == secretary.SECRETARY_MODEL:
        try:
            observe_costs.append(float(parts[1]))
        except ValueError:
            pass
cost_total_after = feed_cost()
print(f"  observe() turns this run : {len(observe_costs)}  (sonnet)")
for i, c in enumerate(observe_costs, 1):
    print(f"    observe #{i} cost delta : ${c:.4f}")
if observe_costs:
    avg = sum(observe_costs) / len(observe_costs)
    print(f"  mean per-turn observe cost: ${avg:.4f}")
    print(f"  max  per-turn observe cost: ${max(observe_costs):.4f}")
print(
    f"  cumulative secretary spend : ${cost_total_before:.4f} -> ${cost_total_after:.4f} "
    f"(delta ${cost_total_after - cost_total_before:.4f})"
)
check("at least 3 observe() costs captured (2 launches + 1 relaunch)", len(observe_costs) >= 3)
check("per-turn cost is sane (< $0.20/turn)", all(c < 0.20 for c in observe_costs) if observe_costs else False)

# ----------------------------------------------------------------------- verdict
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== S0 BRIEF VERIFICATION ====================")
print("sample briefs:")
print(f"  A: {briefA!r}")
print(f"  B: {briefB!r}")
print(f"  A (relaunched): {new_briefA!r}")
print(f"\n{passed}/{total} checks passed")
all_ok = passed == total
print(f"\nS0 COMPREHENSION LIFT + COST MEASUREMENT: {'PASS' if all_ok else 'FAIL'}")
raise SystemExit(0 if all_ok else 1)
