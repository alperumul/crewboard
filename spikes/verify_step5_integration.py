"""Integration test of the real UI-mediated approve flow through the hub.

Launch -> gated Bash op -> hub surfaces awaiting-approval -> approve via API ->
turn continues in place and completes. Exercises mcp_server bridge + permissions
registry + routes end to end.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:5055"
MARKER = "HUB_APPROVED_MARKER"
events: queue.Queue = queue.Queue()


def post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def snap(panel):
    with urllib.request.urlopen(BASE + "/sessions") as r:
        for p in json.loads(r.read())["panels"]:
            if p["panel_id"] == panel:
                return p
    return {}


def sse_reader():
    with urllib.request.urlopen(BASE + "/stream/7", timeout=400) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data: "):
                events.put(json.loads(line[6:]))


threading.Thread(target=sse_reader, daemon=True).start()
time.sleep(1.0)

post(
    "/launch/7",
    {
        "name": "permTest",
        "prompt": f"Use the Bash tool to run exactly this command: echo {MARKER} > _hub_marker.txt "
        f"&& cat _hub_marker.txt. Then report the exact text it printed.",
    },
)
print("launched panel 7 with a gated Bash op")

# 1) wait for the approval request to surface
approval_id = None
deadline = time.monotonic() + 60
while time.monotonic() < deadline and not approval_id:
    try:
        ev = events.get(timeout=2)
    except queue.Empty:
        continue
    if ev.get("kind") == "approval_request":
        approval_id = ev["id"]
        print(f"[APPROVAL SURFACED] tool={ev['tool_name']} input={json.dumps(ev['input'])[:90]}")

if not approval_id:
    print("FAIL: no approval surfaced (Bash may have auto-approved)")
    raise SystemExit(1)

print(f"state while awaiting = {snap('7').get('state')!r}  (expect 'awaiting-approval')")

# 2) interlock: a reply while awaiting-approval must be rejected
try:
    post("/reply/7", {"text": "hello"})
    print("interlock: reply ACCEPTED (BAD)")
except urllib.error.HTTPError as e:
    print(f"interlock: reply rejected HTTP {e.code} (good)")

# 3) approve it
post(f"/permission/decide/{approval_id}", {"behavior": "allow"})
print("approved -> turn should continue in place")

# 4) wait for the turn to finish and confirm the marker came back
got_marker = False
result_text = None
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        ev = events.get(timeout=2)
    except queue.Empty:
        if snap("7").get("state") in ("done", "awaiting-input", "error"):
            break
        continue
    if ev.get("kind") == "approval_resolved":
        print(f"[RESOLVED] behavior={ev.get('behavior')}")
    if ev.get("kind") == "message" and MARKER in (ev.get("text") or ""):
        got_marker = True
    if ev.get("kind") == "result":
        result_text = ev.get("text")
        break

final = snap("7")
print("\n==== STEP 5 INTEGRATION ====")
print("approval surfaced       : True")
print(f"continue-in-place marker: {got_marker}  (or in result: {MARKER in (result_text or '')})")
print(f"final state             : {final.get('state')!r}")
print(f"final last_message      : {final.get('last_message')!r}")
ok = (got_marker or (MARKER in (result_text or ""))) and final.get("state") in ("done", "awaiting-input")
print(f"\nAPPROVE-AND-CONTINUE THROUGH HUB: {ok}")
