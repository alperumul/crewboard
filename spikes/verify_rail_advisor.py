"""Verify S1 (Rail port): a live session on the board + the STREAMING advisor.

Drives the live hub over HTTP exactly as the new Reading-Room UI does:
  - POST /launch/<id>   {name, cwd, prompt}  -> put one real session on the board
  - POST /advisor/send  {message}            -> kick off one streaming advisor turn
  - GET  /advisor/stream                     -> SSE: phase / delta / done / error

Asserts (per the S1 plan / success criteria):
  (1) the launch is accepted (a session exists for the advisor to reason over)
  (2) /advisor/send is accepted (200 ok)
  (3) the stream yields a `phase`, then >=1 `delta`, then a `done` -- IN THAT ORDER
  (4) the `done` event carries a cost
  (5) the persisted transcript gained the you/advisor turn (/advisor/chat)

Safety: the launched session runs in the HUB ROOT (never fantasy_analyzer) with a
read-only, no-tools prompt, so the spike can't mutate any project.

Prereq: the hub is running the S1 code:  .venv/Scripts/python.exe app.py
Run from the hub root with the hub venv:
    .venv/Scripts/python.exe spikes/verify_rail_advisor.py
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
import threading

BASE = "http://127.0.0.1:5055"
HUB_ROOT = str(Path(__file__).resolve().parent.parent)  # never fantasy_analyzer
PANEL = "97"  # a high slot id, to avoid clobbering real sessions in state.json

LAUNCH_PROMPT = "Reply with exactly the single word: ack. Do not use any tools or read any files."
ADVISOR_MSG = "In one short sentence, what's the state of the board right now?"


# ---------------------------------------------------------------- http helpers
def post(path: str, body: dict):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, {}


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- SSE reader
class StreamReader(threading.Thread):
    """Drain GET /advisor/stream into a flat, ordered list of parsed hub-events."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.events: list[dict] = []
        self._stop = False

    def run(self) -> None:
        try:
            resp = urllib.request.urlopen(BASE + "/advisor/stream", timeout=None)
        except Exception as e:  # noqa: BLE001
            self.events.append({"kind": "_reader_error", "text": str(e)})
            return
        for raw in resp:
            if self._stop:
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line.startswith("data: "):
                continue  # heartbeat / comment
            try:
                self.events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                continue

    def stop(self) -> None:
        self._stop = True


def wait_for(pred, timeout: float, poll: float = 0.25) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(poll)
    return False


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ----------------------------------------------------------------------- setup
print("== S1 rail + advisor verification ==")
try:
    health = get("/health")
except Exception as e:  # noqa: BLE001
    print(f"FAIL: hub not reachable at {BASE} ({e}). Start it: .venv/Scripts/python.exe app.py")
    raise SystemExit(1)
print(f"hub up: step {health.get('step')}, default_cwd={health.get('default_cwd')}")

# fresh advisor so this turn is a clean launch turn
post("/advisor/clear", {})
time.sleep(0.3)

# ---------------------------------------------------- (1) a real session exists
print("\n-- launch a read-only session on the board --")
st_l, body_l = post("/launch/" + PANEL, {"name": "spike-probe", "cwd": HUB_ROOT, "prompt": LAUNCH_PROMPT})
check(
    "(1) /launch accepted (a session is on the board)", st_l == 200 and body_l.get("ok"), f"status={st_l} body={body_l}"
)
# give the panel a moment to register in the snapshot the advisor reads
on_board = wait_for(lambda: any(p.get("panel_id") == PANEL for p in get("/sessions").get("panels", [])), timeout=20)
check("    session appears in /sessions", on_board)

# ------------------------------------------------- subscribe, then send a turn
reader = StreamReader()
reader.start()
time.sleep(1.0)  # let the SSE subscriber register server-side before we send

print("\n-- advisor: one streaming turn --")
st_s, body_s = post("/advisor/send", {"message": ADVISOR_MSG})
check("(2) /advisor/send accepted (200 ok)", st_s == 200 and body_s.get("ok"), f"status={st_s} body={body_s}")

# wait for the full turn to complete (its `done`)
done = wait_for(lambda: any(e.get("kind") == "done" for e in reader.events), timeout=180)
check("    advisor turn reached `done`", done)

time.sleep(0.4)
reader.stop()

# ------------------------------------------------------------------- analysis
evs = reader.events
kinds = [e.get("kind") for e in evs]
i_phase = next((i for i, k in enumerate(kinds) if k == "phase"), None)
i_delta = next((i for i, k in enumerate(kinds) if k == "delta"), None)
i_done = next((i for i, k in enumerate(kinds) if k == "done"), None)
n_delta = sum(1 for k in kinds if k == "delta")
done_ev = next((e for e in evs if e.get("kind") == "done"), {})

print("\n-- event stream --")
print(f"  kinds: {kinds}")
print(f"  phases={kinds.count('phase')} deltas={n_delta} done={kinds.count('done')}")
print(f"  done: cost={done_ev.get('cost')} tokens={done_ev.get('tokens')} dur_ms={done_ev.get('duration_ms')}")

check("(3a) a `phase` event arrived", i_phase is not None)
check("(3b) at least one `delta` event arrived", n_delta >= 1)
check("(3c) a `done` event arrived", i_done is not None)
check(
    "(3d) order is phase -> delta -> done",
    None not in (i_phase, i_delta, i_done) and i_phase <= i_delta < i_done,
    f"phase@{i_phase} delta@{i_delta} done@{i_done}",
)
check("(4) the `done` event carries a cost", done_ev.get("cost") is not None, f"cost={done_ev.get('cost')}")

# (5) the turn persisted to the transcript
chat = get("/advisor/chat").get("turns", [])
you = [t for t in chat if t.get("role") == "you"]
adv = [t for t in chat if t.get("role") == "advisor"]
check("(5) transcript gained a you+advisor turn", len(you) >= 1 and len(adv) >= 1, f"turns={len(chat)}")

# ----------------------------------------------------------------------- verdict
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== S1 RAIL + ADVISOR ====================")
print(f"{passed}/{total} checks passed")
all_ok = passed == total
print(f"\nS1 RAIL (1A) + STREAMING ADVISOR: {'PASS' if all_ok else 'FAIL'}")
raise SystemExit(0 if all_ok else 1)
