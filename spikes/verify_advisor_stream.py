"""Verify S1: the persistent, resumable, STREAMING advisor.

Drives the live hub over HTTP exactly as the S3 terminal UI will:
  - POST /advisor/send {message}   -> kicks off one streaming turn (async)
  - GET  /advisor/stream           -> SSE: phase / delta / done / error
  - GET  /advisor/chat             -> the persisted transcript

Asserts (per the S1 plan):
  (1) session_id is IDENTICAL across both turns (one resumed session = real memory)
  (2) turn-2's answer RECALLS a fact stated only in turn 1 (memory carries)
  (3) a `phase` + >=1 `delta` + a `done` event arrive on the SSE channel
  (4) the `done` event carries a cost
  (5) a 2nd /advisor/send issued WHILE the first is still streaming -> 409 (busy-interlock)

Prereq: the hub is running the S1 code (`.venv/Scripts/python.exe app.py`).
Run from the hub root with the hub venv:
    .venv/Scripts/python.exe spikes/verify_advisor_stream.py
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5055"

CODEWORD = "FALCON-4417"
MSG_1 = f"Remember this codeword for later, I will ask for it again: {CODEWORD}. Acknowledge in one short sentence."
MSG_2_BUSY = "This message should be REJECTED because a turn is already streaming."
MSG_3 = "What was the exact codeword I asked you to remember earlier?"


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
    """Drain GET /advisor/stream into a flat list of parsed hub-events."""

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


def segment_turns(events: list[dict]) -> list[list[dict]]:
    """Split the flat event stream into per-turn slices on `done` boundaries."""
    turns: list[list[dict]] = []
    cur: list[dict] = []
    for ev in events:
        cur.append(ev)
        if ev.get("kind") == "done":
            turns.append(cur)
            cur = []
    if cur:
        turns.append(cur)  # trailing (incomplete) turn, if any
    return turns


# ----------------------------------------------------------------------- setup
print("== S1 advisor streaming verification ==")
try:
    health = get("/health")
except Exception as e:  # noqa: BLE001
    print(f"FAIL: hub not reachable at {BASE} ({e}). Start it: .venv/Scripts/python.exe app.py")
    raise SystemExit(1)
print(f"hub up: step {health.get('step')}, default_cwd={health.get('default_cwd')}")

# fresh advisor: drop any prior session_id + transcript so turn 1 is a launch
post("/advisor/clear", {})
time.sleep(0.3)

reader = StreamReader()
reader.start()
time.sleep(1.0)  # let the SSE subscriber register server-side before we send

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ----------------------------------------------------- turn 1 (launch) + busy 409
print(f"\n-- turn 1: send codeword {CODEWORD} --")
st1, body1 = post("/advisor/send", {"message": MSG_1})
check("turn-1 /advisor/send accepted (200 ok)", st1 == 200 and body1.get("ok"), f"status={st1} body={body1}")

# wait until the turn is demonstrably live (a phase or delta has streamed)
live = wait_for(lambda: any(e.get("kind") in ("phase", "delta") for e in reader.events), timeout=60)
check("turn-1 started streaming (phase/delta observed)", live)

# (5) a second send WHILE streaming must be rejected with 409
st_busy, body_busy = post("/advisor/send", {"message": MSG_2_BUSY})
check(
    "(5) 2nd send during a live turn returns 409 busy-interlock",
    st_busy == 409 and "busy" in str(body_busy).lower(),
    f"status={st_busy} body={body_busy}",
)

# wait for turn 1 to finish (its `done` event)
done1 = wait_for(lambda: sum(1 for e in reader.events if e.get("kind") == "done") >= 1, timeout=150)
check("turn-1 reached `done`", done1)

# ------------------------------------------------------------- turn 2 (resume)
print("\n-- turn 2: ask for the codeword back --")
# busy clears in send()'s finally just after `done` broadcasts; retry briefly.
st3 = 0
body3: dict = {}
for _ in range(20):
    st3, body3 = post("/advisor/send", {"message": MSG_3})
    if st3 == 200:
        break
    time.sleep(0.25)
check(
    "turn-2 /advisor/send accepted after turn 1 settled", st3 == 200 and body3.get("ok"), f"status={st3} body={body3}"
)

done2 = wait_for(lambda: sum(1 for e in reader.events if e.get("kind") == "done") >= 2, timeout=150)
check("turn-2 reached `done`", done2)

time.sleep(0.5)
reader.stop()

# ------------------------------------------------------------------- analysis
turns = segment_turns(reader.events)
t1_events = turns[0] if len(turns) >= 1 else []
t2_events = turns[1] if len(turns) >= 2 else []


def kinds(evs: list[dict]) -> dict:
    out = {"phase": 0, "delta": 0, "done": 0, "error": 0}
    for e in evs:
        k = e.get("kind")
        if k in out:
            out[k] += 1
    return out


k1, k2 = kinds(t1_events), kinds(t2_events)
done1_ev = next((e for e in t1_events if e.get("kind") == "done"), {})
done2_ev = next((e for e in t2_events if e.get("kind") == "done"), {})
sid1 = done1_ev.get("session_id")
sid2 = done2_ev.get("session_id")

print("\n-- event tallies --")
print(f"  turn 1: phases={k1['phase']} deltas={k1['delta']} done={k1['done']} session_id={sid1}")
print(f"  turn 2: phases={k2['phase']} deltas={k2['delta']} done={k2['done']} session_id={sid2}")
print(f"  done#1 cost={done1_ev.get('cost')} tokens={done1_ev.get('tokens')} dur_ms={done1_ev.get('duration_ms')}")
print(f"  done#2 cost={done2_ev.get('cost')} tokens={done2_ev.get('tokens')} dur_ms={done2_ev.get('duration_ms')}")

# (3) phase + >=1 delta + done across the two turns
check("(3) at least one `phase` event arrived", (k1["phase"] + k2["phase"]) >= 1)
check("(3) at least one `delta` event arrived", (k1["delta"] + k2["delta"]) >= 1)
check("(3) both turns produced a `done` event", k1["done"] >= 1 and k2["done"] >= 1)

# (4) cost present in done
check("(4) done event carries a cost", done2_ev.get("cost") is not None, f"cost={done2_ev.get('cost')}")

# (1) identical session_id across turns
check(
    "(1) session_id identical across both turns",
    bool(sid1) and sid1 == sid2,
    f"{sid1} vs {sid2}",
)

# (2) memory: turn-2 transcript answer recalls the turn-1 codeword
chat = get("/advisor/chat").get("turns", [])
advisor_msgs = [t["text"] for t in chat if t.get("role") == "advisor"]
last_answer = advisor_msgs[-1] if advisor_msgs else ""
recalled = "falcon" in last_answer.lower() and "4417" in last_answer.lower()
check("(2) turn-2 answer recalls the turn-1 codeword (memory)", recalled, f"answer={last_answer[:160]!r}")

# transcript shape: the rejected (409) message must NOT be in the transcript
you_msgs = [t["text"] for t in chat if t.get("role") == "you"]
check(
    "rejected busy message never entered the transcript",
    all(MSG_2_BUSY not in m for m in you_msgs),
    f"you_turns={len(you_msgs)}",
)

# ----------------------------------------------------------------------- verdict
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== S1 ADVISOR STREAMING ====================")
print(f"transcript turns: {len(chat)} (expect 4: you/advisor x2; the 409 send is absent)")
print(f"{passed}/{total} checks passed")
all_ok = passed == total
print(f"\nS1 PERSISTENT STREAMING ADVISOR: {'PASS' if all_ok else 'FAIL'}")
raise SystemExit(0 if all_ok else 1)
