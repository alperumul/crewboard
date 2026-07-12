"""Verify S4 (right-click actions + rename + rehydrate + security): the backend
half of the actionable canvas.

S4 composes existing primitives into four new behaviours. This spike proves, in
process and WITHOUT spawning a real `claude -p` turn or spending a live call,
that:

  (1) POST /act/<id> {action:"review"}  spawns a fresh panel running /review,
      seeded with the source's last complete message, and records a typed
      REVIEWS edge (new -> source, source="explicit") on the Map.
  (2) POST /act/<id> {action:"discuss"} does the same with /design-partner.
  (3) /act rejects a bad action (400), a sourceless panel (409), and a source
      with no output to act on (400).
  (4) POST /rename/<id> {name} writes the hub-side name (durable + in memory)
      and broadcasts a `renamed` event; empty name -> 400, unknown panel -> 404.
  (5) the localhost Host guard rejects a non-local mutating request (403) while
      letting a loopback POST and ALL GETs through (the permission MCP bridge
      reaches /permission/* over urllib with Host=127.0.0.1:5055 — never broken).
  (6) the .jsonl re-hydrate parser turns durable session scrollback (user /
      assistant `message.content`) into replayable hub events, tolerating
      tool_result user lines + string-vs-list content.
  (7) the launch command carries `--name` (only on launch, never on --resume) so
      the real session gets a findable title (headless /rename is unavailable).

It redirects persistence to a throwaway state.json and stubs MANAGER.launch, so
it never touches the real board, needs no running hub, and spawns no subprocess.
Run from the hub root with the hub venv:
    .venv/Scripts/python.exe spikes/verify_act.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))  # so `from hub import ...` / `import app` work from any cwd

# ---- redirect ALL persistence to a throwaway file BEFORE sessions builds MANAGER ----
from hub import state  # noqa: E402

_tmp = tempfile.NamedTemporaryFile(prefix="hub_actspike_", suffix=".json", delete=False)
_tmp.close()
TMP = Path(_tmp.name)
state.STATE_FILE = TMP

# A source panel with real output + a sourceless/outputless panel for the error paths.
state.save(
    {
        "panels": {
            "1": {
                "name": "auth-oauth",
                "session_id": "sess-1",
                "cwd": "",
                "last_message": "OAuth handler shipped; all tests green.",
            },
            "2": {"name": "pay-stripe", "session_id": "sess-2", "cwd": "", "last_message": ""},
        },
        "relations": [],
    }
)

# Import sessions AFTER the redirect so MANAGER restores from TMP; then app binds
# that same singleton (`from hub.sessions import MANAGER`).
from hub import sessions  # noqa: E402
from app import app  # noqa: E402

client = app.test_client()
LOCAL = {"Host": "127.0.0.1:5055"}

# ---- stub MANAGER.launch: record the call + register an occupied panel, no subprocess ----
LAUNCHES: list[dict] = []


def fake_launch(panel_id, name, cwd, prompt):
    LAUNCHES.append({"panel_id": panel_id, "name": name, "cwd": cwd, "prompt": prompt})
    s = sessions.Session(panel_id, name, cwd)
    s.session_id = "spawned-" + panel_id
    s.sentinel = "<<<X>>>"
    sessions.MANAGER._sessions[panel_id] = s
    return s


sessions.MANAGER.launch = fake_launch  # type: ignore[assignment]

# ----------------------------------------------------------------- check harness
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def post(path, body=None, host=True):
    headers = dict(LOCAL) if host else {"Host": "evil.example.com"}
    return client.post(path, json=body or {}, headers=headers)


def edge(rels, frm, to):
    return next((r for r in rels if str(r.get("from")) == frm and str(r.get("to")) == to), None)


print("== S4 right-click actions + rename + rehydrate + security verification ==")

# ---- (1) /act review spawns a /review panel + a REVIEWS edge -----------------
print("\n(1) POST /act review -> /review panel + REVIEWS edge (new -> source, explicit)")
LAUNCHES.clear()
r = post("/act/1", {"action": "review"})
body = r.get_json() or {}
new_id = body.get("panel_id")
check("/act review returns 200", r.status_code == 200, f"status={r.status_code} body={body}")
check("a panel was launched", len(LAUNCHES) == 1, f"launches={LAUNCHES}")
check("spawned panel id differs from source", new_id not in (None, "1"), f"new_id={new_id}")
lp = LAUNCHES[0]["prompt"] if LAUNCHES else ""
check("launch prompt starts with /review", lp.lstrip().startswith("/review"), f"prompt={lp[:40]!r}")
check("launch prompt is slash-preserved (slash at very front)", lp.startswith("/review"), f"prompt={lp[:20]!r}")
check("launch prompt carries the source's last message", "OAuth handler shipped" in lp, f"prompt={lp[:120]!r}")
rels = state.get_relations()
e = edge(rels, str(new_id), "1")
check("REVIEWS edge new -> source persisted", e is not None, f"rels={rels}")
check("edge type == REVIEWS", bool(e) and e.get("type") == "REVIEWS", f"{e}")
check("edge source == explicit (human-triggered, solid wire)", bool(e) and e.get("source") == "explicit", f"{e}")
served = (client.get("/relations").get_json() or {}).get("relations", [])
check("/relations serves the new edge", edge(served, str(new_id), "1") is not None, f"served={served}")

# ---- (2) /act discuss spawns a /design-partner panel ------------------------
print("\n(2) POST /act discuss -> /design-partner panel")
LAUNCHES.clear()
r = post("/act/1", {"action": "discuss"})
body = r.get_json() or {}
check("/act discuss returns 200", r.status_code == 200, f"status={r.status_code} body={body}")
dp = LAUNCHES[0]["prompt"] if LAUNCHES else ""
check("launch prompt starts with /design-partner", dp.startswith("/design-partner"), f"prompt={dp[:40]!r}")
check(
    "discuss panel differs from review panel + source",
    body.get("panel_id") not in (None, "1", new_id),
    f"id={body.get('panel_id')}",
)

# ---- (3) /act error paths ---------------------------------------------------
print("\n(3) /act rejects bad action / sourceless / outputless")
check("bad action -> 400", post("/act/1", {"action": "frobnicate"}).status_code == 400)
check("unknown source panel -> 409", post("/act/99", {"action": "review"}).status_code == 409)
check("source with no output -> 400", post("/act/2", {"action": "review"}).status_code == 400)

# ---- (4) /rename writes the hub-side name -----------------------------------
print("\n(4) POST /rename writes the durable hub-side name + broadcasts")
# subscribe to the source's channel to observe the rename broadcast
q, _replay = sessions.MANAGER.get("1").subscribe()
r = post("/rename/1", {"name": "oauth-google-login"})
check("/rename returns 200", r.status_code == 200, f"status={r.status_code}")
check("in-memory name updated", sessions.MANAGER.get("1").name == "oauth-google-login")
persisted = state.load()["panels"]["1"].get("name")
check("durable name persisted to state.json", persisted == "oauth-google-login", f"persisted={persisted}")
got_rename = False
try:
    while not q.empty():
        ev = q.get_nowait()
        if ev.get("kind") == "renamed" and ev.get("name") == "oauth-google-login":
            got_rename = True
except Exception:
    pass
check("a `renamed` event was broadcast", got_rename)
check("empty name -> 400", post("/rename/1", {"name": "   "}).status_code == 400)
check("unknown panel -> 404", post("/rename/77", {"name": "x"}).status_code == 404)

# ---- (5) localhost Host guard ----------------------------------------------
print("\n(5) Host guard: non-local mutating request rejected; loopback + GET allowed")
check("cross-origin POST rejected (403)", post("/act/1", {"action": "review"}, host=False).status_code == 403)
check("loopback POST allowed (not 403)", post("/rename/1", {"name": "auth-oauth"}).status_code != 403)
# GETs are never guarded (the bridge's poll is a GET; and read-only is harmless)
check(
    "cross-origin GET still allowed", client.get("/relations", headers={"Host": "evil.example.com"}).status_code == 200
)
# the permission bridge POSTs with Host=127.0.0.1:5055 (urllib default) -> must pass the guard
bridge = client.post("/permission/request", json={"panel_id": "1", "tool_name": "Bash", "input": {}}, headers=LOCAL)
check(
    "permission bridge POST (loopback Host) not blocked by guard",
    bridge.status_code != 403,
    f"status={bridge.status_code}",
)

# ---- (6) .jsonl re-hydrate parser ------------------------------------------
print("\n(6) .jsonl re-hydrate parser -> replayable user/message events")
proj = Path(tempfile.mkdtemp(prefix="hub_proj_")) / "D--some-cwd"
proj.mkdir(parents=True)
sessions.PROJECTS_DIR = proj.parent  # point the parser's search root at the temp tree
jl = proj / "sess-9.jsonl"
lines = [
    {"type": "summary", "summary": "ignore me"},
    {"type": "user", "message": {"role": "user", "content": "build the login flow"}},
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "content": [{"type": "text", "text": "Done — wired the handler."}],
        },
    },
    {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    },  # tool_result -> skip
    {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Edit", "input": {}}]},
    },  # no text -> skip
]
jl.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
events = sessions.rehydrate_history("sess-9")
kinds = [e.get("kind") for e in events]
check("parser found the on-disk session by id (glob)", len(events) >= 2, f"events={events}")
check(
    "user prompt rehydrated",
    any(e.get("kind") == "user" and "login flow" in e.get("text", "") for e in events),
    f"{events}",
)
check(
    "assistant text rehydrated",
    any(e.get("kind") == "message" and "wired the handler" in e.get("text", "") for e in events),
    f"{events}",
)
check(
    "tool_result user line skipped (no empty user bubble)",
    all(e.get("text") for e in events if e.get("kind") == "user"),
    f"{events}",
)
check("tool_use-only assistant line skipped", kinds.count("message") == 1, f"kinds={kinds}")
check("missing session returns [] (no crash)", sessions.rehydrate_history("does-not-exist") == [])

# ---- (7) --name at launch, not on resume -----------------------------------
print("\n(7) launch command carries --name (launch only, never on --resume)")
s = sessions.Session("5", "my-session", str(HUB_ROOT))
s.session_id = "sess-5"
cmd_launch = s._build_cmd("/review hello", resume=False)
cmd_resume = s._build_cmd("more", resume=True)
check("--name present on launch cmd", "--name" in cmd_launch, f"cmd={cmd_launch}")
check("--name absent on resume cmd", "--name" not in cmd_resume, f"cmd={cmd_resume}")
check("--resume present on resume cmd", "--resume" in cmd_resume)
check("slash prompt is a positional arg on launch", "/review hello" in cmd_launch)

# ----------------------------------------------------------------------- cleanup
for p in (TMP, TMP.with_suffix(".json.tmp")):
    try:
        p.unlink()
    except OSError:
        pass

# ----------------------------------------------------------------------- verdict
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== S4 ACTIONS / RENAME / REHYDRATE / SECURITY ====================")
print(f"{passed}/{total} checks passed")
all_ok = passed == total
print(f"\nS4: {'PASS' if all_ok else 'FAIL'}")
raise SystemExit(0 if all_ok else 1)
