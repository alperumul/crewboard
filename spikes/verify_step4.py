"""Verify step 4: sentinel -> done, no-sentinel -> awaiting-input, busy interlock."""

from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:5055"


def post(path: str, body: dict):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def snapshot(panel: str) -> dict:
    with urllib.request.urlopen(BASE + "/sessions") as r:
        panels = json.loads(r.read())["panels"]
    for p in panels:
        if p["panel_id"] == panel:
            return p
    return {}


def wait_settle(panel: str, timeout=60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = snapshot(panel).get("state")
        if st in ("done", "awaiting-input", "error"):
            return st
        time.sleep(0.5)
    return "TIMEOUT"


def wait_running(panel: str, timeout=20):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        p = snapshot(panel)
        if p.get("state") == "running" and p.get("session_id"):
            return True
        time.sleep(0.3)
    return False


print("health:", json.loads(urllib.request.urlopen(BASE + "/health").read()))

# --- Test A: task fully done -> sentinel -> state 'done' (green) ---
post("/launch/1", {"name": "doneTest", "prompt": "Reply with exactly the single word: READY"})
stA = wait_settle("1")
snapA = snapshot("1")
print(f"\n[A] task-done  -> state={stA!r}  last_message={snapA.get('last_message')!r}")
print(f"    PASS-done={stA == 'done'}  sentinel-stripped={'<<<DONE_' not in (snapA.get('last_message') or '')}")

# --- Test B: asks a question -> NO sentinel -> 'awaiting-input' (yellow) ---
post(
    "/launch/2",
    {
        "name": "askTest",
        "prompt": "Ask me what my single favorite color is. Ask only that one "
        "question, nothing else, and do not assume an answer.",
    },
)
# --- Test C: busy interlock — reply while running must be rejected ---
if wait_running("2"):
    code, resp = post("/reply/2", {"text": "blue"})
    print(f"\n[C] interlock  -> reply-during-run HTTP {code} resp={resp}")
    print(f"    PASS-interlock={code == 409}")
else:
    print("\n[C] interlock  -> SKIPPED (never observed running+session_id)")

stB = wait_settle("2")
snapB = snapshot("2")
print(f"\n[B] asks-question -> state={stB!r}")
print(f"    PASS-awaiting={stB == 'awaiting-input'}")
print(f"    (it asked: {snapB.get('last_message')!r})")
