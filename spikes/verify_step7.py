"""Verify step 7: relay session A's output into session B, verbatim, no copy-paste."""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:5055"
SRC, DST = "1", "3"
events: queue.Queue = queue.Queue()


def post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, json.loads(r.read())


def snap(panel):
    with urllib.request.urlopen(BASE + "/sessions") as r:
        for p in json.loads(r.read())["panels"]:
            if p["panel_id"] == panel:
                return p
    return {}


def sse_reader(panel):
    with urllib.request.urlopen(BASE + f"/stream/{panel}", timeout=200) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data: "):
                events.put(json.loads(line[6:]))


src_output = snap(SRC).get("last_message", "")
print(f"source (panel {SRC}) output to relay: {src_output!r}")
assert src_output, "source has no output"

threading.Thread(target=sse_reader, args=(DST,), daemon=True).start()
time.sleep(1.0)

code, resp = post("/route", {"from_panel": SRC, "to_panel": DST})
print(f"POST /route -> HTTP {code} {resp}")

# 1) target must receive the EXACT source text as its next user message
relayed_verbatim = False
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        ev = events.get(timeout=2)
    except queue.Empty:
        continue
    if ev.get("kind") == "user":
        print(f"[target got user msg] {ev['text']!r}")
        relayed_verbatim = ev["text"].strip() == src_output.strip()
        break

# 2) target processes it and produces a response
got_response = False
deadline = time.monotonic() + 40
while time.monotonic() < deadline:
    try:
        ev = events.get(timeout=2)
    except queue.Empty:
        if snap(DST).get("state") in ("done", "awaiting-input", "error"):
            break
        continue
    if ev.get("kind") == "message":
        print(f"[target responded] {ev['text'][:80]!r}")
        got_response = True
        break

print("\n==== STEP 7 RELAY ====")
print(f"route accepted          : {code == 200}")
print(f"delivered VERBATIM to B : {relayed_verbatim}")
print(f"B processed the relay    : {got_response}")
print(f"\nRELAY A->B WITHOUT COPY-PASTE: {code == 200 and relayed_verbatim and got_response}")
