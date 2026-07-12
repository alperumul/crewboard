"""Verify the Relay + Secretary-Cost build (plan: docs/plans/2026-06-22-relay-routing-plan.md).

This is the shared spike for slices S0-S8 — S0 authors it, each later slice APPENDS
its own section above the verdict block. It drives the secretary in-process with a
stubbed LLM and redirects ALL persistence (state.json + the cost log) to throwaway
temp files, so it never touches the real board / cost log and needs no running hub.

Run from the hub root with the hub venv (kill any hub on :5055 first):
    .venv/Scripts/python.exe spikes/verify_routing.py

------------------------------------------------------------------------------
S0 — per-turn secretary cost baseline. Proves, deterministically and WITHOUT a
live Sonnet call, that the EXISTING cost log (run/secretary_cost.log) is widened
so every LLM pass records ONE richly-tagged row:

  (1) one observe() pass records exactly ONE cost sample
  (2) the sample carries the pass cost + the LIVE-SESSION count + the board mode
      ("full" until S4 adds Full/Quiet) — the tags S4 needs to compare cost
      like-for-like (same session-count + mode bucket) instead of an aggregate
      that hides the frequency-gate
  (3) "live sessions" is the board's own definition (a launched panel: a
      session_id OR a non-"empty" state), so a never-launched placeholder tile is
      NOT counted
  (4) the running total_cost() still accumulates the pass cost
  (5) the new cost_samples() reader parses the log back into {model,cost,n_live,mode}

S0 stubs subprocess.run (NOT _run_json) so the REAL _run_json + _log_cost path
executes against a canned cost envelope — exercising the actual logging code, not
a stub of it. Later slices that test relay/triage PARSING stub _run_json directly
(cheaper) in their own sections; the two seams coexist.
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))  # so `from hub import ...` / `import app` work from any cwd

from hub import secretary, state  # noqa: E402

# ---- redirect ALL persistence to throwaway files (never touch the real board /
# ---- cost log; a fresh, empty cost log makes the sample-count assertion exact) ----
_tmp = tempfile.NamedTemporaryFile(prefix="hub_routespike_", suffix=".json", delete=False)
_tmp.close()
TMP_STATE = Path(_tmp.name)
state.STATE_FILE = TMP_STATE

_COST_DIR = Path(tempfile.mkdtemp(prefix="hub_costspike_"))
secretary._RUN_DIR = _COST_DIR  # type: ignore[attr-defined]
secretary._COST_LOG = _COST_DIR / "secretary_cost.log"  # type: ignore[attr-defined]
secretary._total_cost = 0.0  # type: ignore[attr-defined]  # deterministic total_cost()
secretary.CLAUDE = "claude"  # type: ignore[attr-defined]  # truthy so _run_json doesn't early-return

# S4 introduces a frequency-gate that DEBOUNCES Pass B — a burst of completions
# within a short window collapses to ONE Sonnet pass. This shared harness drives
# observe() many times back-to-back, so without this every S0-S3 call after the
# first would be debounced and skip Pass B, breaking their relay/triage/frame
# assertions. Disable the window here (each call runs both passes, as those sections
# assume); S4's own section re-arms a real window to PROVE the debounce. The
# attributes are created even pre-implementation — harmless until observe() reads them.
secretary._DEBOUNCE_WINDOW_S = 0.0  # type: ignore[attr-defined]
secretary._last_passb_ts = float("-inf")  # type: ignore[attr-defined]

# A board with THREE launched (live) sessions + ONE never-launched placeholder.
# Live count must be 3: panel "9" has no session_id and state "empty" (a tile that
# exists in the grid but was never launched) — the board's own snapshot() filter
# (`session_id is not None or state != "empty"`) excludes it, so n_live must too.
BOARD = [
    {
        "panel_id": "1",
        "name": "auth-oauth",
        "state": "done",
        "session_id": "s1",
        "task": "build oauth",
        "last_message": "tests green",
    },
    {
        "panel_id": "2",
        "name": "auth-jwt",
        "state": "running",
        "session_id": "s2",
        "task": "rotate jwt",
        "last_message": "",
    },
    {
        "panel_id": "3",
        "name": "pay-stripe",
        "state": "awaiting-input",
        "session_id": "s3",
        "task": "stripe webhooks",
        "last_message": "need creds",
    },
    {"panel_id": "9", "name": "session 9", "state": "empty", "session_id": None, "task": "", "last_message": ""},
]
EXPECTED_LIVE = 3

# ---- stub subprocess.run so the REAL _run_json runs (parses cost, logs the row) ----
KNOWN_COST = 0.0123
_CANNED_RESULT = json.dumps(
    {
        "brief": "Working toward the OAuth login flow; wired the handler and got its tests green this turn.",
        "name_suggestion": None,
        "frame_suggestion": None,
        "triage": [],
        "relays": [],
    }
)
_ENVELOPE = json.dumps({"result": _CANNED_RESULT, "total_cost_usd": KNOWN_COST})


def _fake_run(*_a, **_k):  # accepts _run_json's full subprocess.run(...) call shape
    return types.SimpleNamespace(stdout=_ENVELOPE, returncode=0)


secretary.subprocess.run = _fake_run  # type: ignore[assignment]

# Capture the REAL _run_json now — later sections (S1/S2) monkeypatch secretary._run_json
# to a stub, and the S3 section restores this original to prove BOTH passes log a
# per-pass cost row through the real _log_cost path.
_REAL_RUN_JSON = secretary._run_json  # type: ignore[attr-defined]


# ----------------------------------------------------------------- check harness
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


# ============================ S0 — cost baseline ============================
print("== S0 per-turn secretary cost baseline ==")

# ---- (3) live-session count = the board's own launched-panel definition
print("\n(3) live-session count excludes never-launched placeholders")
n_live = secretary._count_live_sessions(BOARD)  # type: ignore[attr-defined]
check("3 live sessions counted (placeholder panel 9 excluded)", n_live == EXPECTED_LIVE, f"n_live={n_live}")

# ---- drive ONE observe() pass through the REAL _run_json (stubbed subprocess)
before = len(secretary.cost_samples())
items, cost = secretary.observe("1", "auth-oauth", "tests green", BOARD)
after = secretary.cost_samples()

# ---- (1) the pass cost + sample count. S3 split observe() into TWO passes (Haiku
# brief+name, then Sonnet triage/relay/frame), so one observe() now records ONE row
# PER PASS and returns their SUMMED cost. The stubbed subprocess returns the same
# canned envelope to both passes, so each row costs KNOWN_COST -> total 2*KNOWN_COST.
print("\n(1) one observe() pass records ONE cost row PER LLM pass (S3: two passes)")
check("observe() returned the SUMMED pass cost (Haiku + Sonnet)", abs(cost - 2 * KNOWN_COST) < 1e-9, f"cost={cost}")
check("exactly two new samples recorded (one per pass)", len(after) - before == 2, f"before={before} after={len(after)}")

# ---- (2) the sample is tagged with cost + live-count + board mode (the LAST row is
# the Sonnet Pass B; S3 asserts the Haiku Pass A row's tags in its own section below)
print("\n(2) the last (Sonnet) sample carries cost + live-count + board mode")
s0 = after[-1] if after else {}
check("sample has all four fields", set(s0) >= {"model", "cost", "n_live", "mode"}, f"sample={s0}")
check("sample cost == pass cost", isinstance(s0.get("cost"), float) and abs(s0["cost"] - KNOWN_COST) < 1e-9, f"{s0}")
check("sample n_live == live count (3)", s0.get("n_live") == EXPECTED_LIVE, f"{s0}")
check('sample mode == "full" (hardcoded until S4)', s0.get("mode") == "full", f"{s0}")
check("sample model == the pass model", s0.get("model") == secretary.SECRETARY_MODEL, f"{s0}")

# ---- (4) the running total still accumulates both passes
print("\n(4) running total_cost() accumulates both passes")
check(
    "total_cost() == both pass costs summed",
    secretary.total_cost() == round(2 * KNOWN_COST, 4),
    f"total={secretary.total_cost()}",
)

# ---- (5) the reader parses the log back into the tagged shape
print("\n(5) cost_samples() reads the tagged rows back")
check(
    "cost_samples() returns a list of dicts",
    isinstance(after, list) and all(isinstance(r, dict) for r in after),
    f"{after}",
)
check(
    "every sample has model/cost/n_live/mode",
    all(set(("model", "cost", "n_live", "mode")).issubset(r) for r in after),
    f"{after}",
)

# ============================ S1 — relay chip -> /route ============================
# S1 makes the secretary's relay an ACTIONABLE chip. It proves, deterministically:
#   (S1.1) observe() surfaces a kind:"relay" FEED item (the chip's data source)
#          carrying its type + reason, with a dismissable id
#   (S1.2) the validity guard now gates the CHIP too — a self-loop / hallucinated
#          relay yields NO chip AND no edge (was edge-only before S1)
#   (S1.3) the relay's reason is persisted onto the durable Map edge
#   (S1.4) the chip's accept hits the EXISTING /route, which delivers the source's
#          CURRENT, FULL last_message (not the [:200]/[:500] snapshot copies) and
#          records an EXPLICIT edge. The client sends ONLY {from_panel,to_panel}.
print("\n== S1 relay chip -> /route ==")

import app as _appmod  # noqa: E402  (state already redirected to TMP_STATE above)
from hub import sessions as _sessions  # noqa: E402
from hub import nodes as _nodes  # noqa: E402  (F-IDENT: relations are node-keyed)

# F-IDENT: the routing spike drives observe()/route with synthetic boards whose panels
# were never launched, so mint a node per panel id used in relation assertions. The
# secretary/app edge-writes translate panel->node via node_id_for_panel, and the
# assertions resolve the same way through _rid().
for _pid in ("1", "2", "3", "10", "11", "12", "20", "21", "51", "52", "54", "55", "56"):
    state.update_panel(_pid, name=f"s{_pid}", session_id=f"s{_pid}")
    _nodes.mint_node_for_panel(_pid, f"s{_pid}")


def _rid(pid):  # resolve a panel id to its minted node_id
    return _nodes.node_id_for_panel(str(pid))


# stub the LLM for the relay-parse path (cheaper than the subprocess seam used by S0)
_S1_CANNED = {
    "brief": "",
    "name_suggestion": None,
    "frame_suggestion": None,
    "triage": [],
    "relays": [
        {"from": "1", "to": "2", "type": "REVIEWS", "reason": "oauth review applies to jwt"},
        {"from": "2", "to": "2", "type": "CONFLICT", "reason": "self loop -> dropped"},  # self-loop
        {"from": "1", "to": "404", "type": "FEEDS", "reason": "hallucinated -> dropped"},  # not live
    ],
}
secretary._run_json = lambda *a, **k: (json.dumps(_S1_CANNED), 0.0)  # type: ignore[assignment]

s1_items, _s1_cost = secretary.observe("1", "auth-oauth", "oauth tests green", BOARD)
feed_items = secretary.feed_snapshot()["items"]
relay_feed = [i for i in feed_items if i.get("kind") == "relay"]

# ---- (S1.1) a kind:"relay" item is surfaced in the feed (the chip's data source)
print("\n(S1.1) observe() surfaces a kind:'relay' feed item with type + reason + id")
r12 = next((i for i in relay_feed if (i.get("from"), i.get("to")) == ("1", "2")), None)
check("relay feed item 1->2 present", r12 is not None, f"relay_feed={relay_feed}")
check("relay feed item has a dismissable id", bool(r12 and r12.get("id")), f"{r12}")
check("relay feed item carries type REVIEWS", bool(r12) and r12.get("type") == "REVIEWS", f"{r12}")
check("relay feed item carries its reason", bool(r12) and r12.get("reason") == "oauth review applies to jwt", f"{r12}")

# ---- (S1.2) the guard gates the CHIP too: no chip + no edge for invalid relays
print("\n(S1.2) self-loop / hallucinated relay -> no chip AND no edge")
check("no relay chip for self-loop 2->2", not any((i.get("from"), i.get("to")) == ("2", "2") for i in relay_feed), f"{relay_feed}")
check("no relay chip for hallucinated 1->404", not any((i.get("from"), i.get("to")) == ("1", "404") for i in relay_feed), f"{relay_feed}")
_s1_rels = state.get_relations()
check("no edge for self-loop 2->2", not any((str(r.get("from")), str(r.get("to"))) == (_rid("2"), _rid("2")) for r in _s1_rels), f"{_s1_rels}")
check("no edge for hallucinated 1->404", not any((str(r.get("from")), str(r.get("to"))) == (_rid("1"), _rid("404")) for r in _s1_rels), f"{_s1_rels}")

# ---- (S1.3) the relay's reason is persisted onto the durable edge
print("\n(S1.3) the relay reason rides onto the durable edge")
_e12 = next((r for r in _s1_rels if (str(r.get("from")), str(r.get("to"))) == (_rid("1"), _rid("2"))), None)
check("edge 1->2 persisted with its reason", bool(_e12) and _e12.get("reason") == "oauth review applies to jwt", f"{_e12}")

# ---- (S1.4) /route delivers the source's CURRENT, FULL last_message (no truncation)
print("\n(S1.4) /route delivers the FULL current last_message, verbatim, + explicit edge")
_client = _appmod.app.test_client()
_src = _sessions.Session("1", "auth-oauth", "")
_src.session_id = "s1"
_src.state = "done"
_src.last_message = "X" * 1200  # longer than the 200/500-char snapshot copies
_dst = _sessions.Session("2", "auth-jwt", "")
_dst.session_id = "s2"
_dst.state = "idle"  # not in app.BUSY_STATES
_sessions.MANAGER._sessions["1"] = _src
_sessions.MANAGER._sessions["2"] = _dst
_delivered: dict[str, Any] = {}


def _capture_reply(panel_id, text):  # replaces MANAGER.reply so nothing spawns
    _delivered["panel"] = panel_id
    _delivered["text"] = text
    return _dst


_sessions.MANAGER.reply = _capture_reply  # type: ignore[assignment]
_resp = _client.post("/route", json={"from_panel": "1", "to_panel": "2"})
check("/route returns 200", _resp.status_code == 200, f"status={_resp.status_code} body={_resp.get_json()}")
check("/route delivered to the target", _delivered.get("panel") == "2", f"{_delivered}")
check(
    "/route delivered the FULL last_message (not the [:200]/[:500] copy)",
    _delivered.get("text") == "X" * 1200,
    f"len={len(_delivered.get('text', ''))}",
)
check(
    "/route recorded an EXPLICIT 1->2 edge",
    any((str(r.get("from")), str(r.get("to"))) == (_rid("1"), _rid("2")) and r.get("source") == "explicit" for r in state.get_relations()),
    f"{state.get_relations()}",
)
check(
    "the explicit upsert KEPT the secretary's reason (no blank)",
    bool(next((r for r in state.get_relations() if (str(r.get("from")), str(r.get("to"))) == (_rid("1"), _rid("2"))), {}).get("reason")),
    f"{state.get_relations()}",
)

# ============================ S2 — hygiene: text-aware key + persistent suppress + guard ============================
# S2 stops relay suggestions from nagging and makes "what am I about to send" knowable:
#   (S2.1) the relay key is TEXT-AWARE — same A->B content emitted twice -> ONE chip,
#          but a NEW A->B message (different source last_message) RE-suggests
#   (S2.2) execute/dismiss suppression is PERSISTENT (state.json) — a suppressed relay
#          stays gone across a FRESH state.json load (restart-proof; was in-memory FEED
#          removal that reset on restart). Both execute (the chip's ✓ route hits /route
#          THEN /secretary/dismiss) and ✕ dismiss funnel through secretary.dismiss().
#   (S2.3) a GUARD drops a relay whose source has nothing/failed to send — empty
#          last_message OR board state == "error" -> no chip AND no edge
print("\n== S2 hygiene: text-aware key + persistent suppress + empty/error guard ==")


def _s2_board(src_msg, src_state="done", dst="2"):
    """Minimal 2-panel board: source '1' (its content/state is what we vary) -> target.
    The hash + guard read last_message/state straight off THIS board snapshot."""
    return [
        {"panel_id": "1", "name": "src", "state": src_state, "session_id": "s1", "task": "t", "last_message": src_msg},
        {"panel_id": dst, "name": "dst", "state": "idle", "session_id": "s" + dst, "task": "t", "last_message": ""},
    ]


def _relays(frm="1", to="2"):
    return [
        i
        for i in secretary.feed_snapshot()["items"]
        if i.get("kind") == "relay" and (i.get("from"), i.get("to")) == (frm, to)
    ]


# stub the LLM to always propose a 1->2 FEEDS relay; the SOURCE TEXT we vary via the board
_S2_RELAY_12 = {
    "brief": "",
    "name_suggestion": None,
    "frame_suggestion": None,
    "triage": [],
    "relays": [{"from": "1", "to": "2", "type": "FEEDS", "reason": "feeds 2"}],
}
secretary._run_json = lambda *a, **k: (json.dumps(_S2_RELAY_12), 0.0)  # type: ignore[assignment]

# ---- (S2.1a) text-aware key: identical source content twice -> exactly ONE chip
print("\n(S2.1a) same A->B content emitted twice -> ONE chip")
secretary.FEED.clear()
secretary.observe("1", "src", "alpha", _s2_board("ALPHA output"))
secretary.observe("1", "src", "alpha", _s2_board("ALPHA output"))
check("same A->B content twice -> exactly one relay chip", len(_relays()) == 1, f"count={len(_relays())}")

# ---- (S2.2) execute/dismiss suppression persists to disk and survives a fresh load
print("\n(S2.2) executed/dismissed relay stays suppressed across a fresh state.json load")
_chip = _relays()[0]
secretary.dismiss(_chip["id"])  # the SAME call the client makes on BOTH ✓ route and ✕ dismiss
_disk = json.loads(TMP_STATE.read_text(encoding="utf-8"))  # prove it reached the file (restart-proof)
check("dismiss persisted the suppressed key to state.json on disk", bool(_disk.get("suppressed_keys")), f"{_disk.get('suppressed_keys')}")
# observe() re-reads the file via state.get_suppressed_keys(), so this IS the fresh-load path
secretary.FEED.clear()
secretary.observe("1", "src", "alpha", _s2_board("ALPHA output"))  # identical content
check("suppressed relay does NOT re-surface (same content, after persist)", len(_relays()) == 0, f"count={len(_relays())}")

# ---- (S2.1b) a NEW A->B message (different source last_message) re-suggests
print("\n(S2.1b) new A->B content (different last_message) re-suggests despite the suppressed old one")
secretary.FEED.clear()
secretary.observe("1", "src", "beta", _s2_board("BETA output"))  # different text -> different key
check("new source content re-suggests (not collapsed into / suppressed by the old key)", len(_relays()) == 1, f"count={len(_relays())}")

# ---- (S2.3) guard: empty last_message OR board state == "error" -> no chip, no edge
# Use a FRESH target panel '3' (no prior edge from S0/S1) so the no-edge assertion is exact.
print("\n(S2.3) blank or errored source -> no relay (chip AND edge)")
_S2_RELAY_13 = {
    "brief": "",
    "name_suggestion": None,
    "frame_suggestion": None,
    "triage": [],
    "relays": [{"from": "1", "to": "3", "type": "FEEDS", "reason": "feeds 3"}],
}
secretary._run_json = lambda *a, **k: (json.dumps(_S2_RELAY_13), 0.0)  # type: ignore[assignment]


def _has_edge(frm, to):
    nf, nt = _rid(frm), _rid(to)
    return any((str(r.get("from")), str(r.get("to"))) == (nf, nt) for r in state.get_relations())


secretary.FEED.clear()
secretary.observe("1", "src", "x", _s2_board("", dst="3"))  # empty source last_message
check("empty source last_message -> no relay chip", len(_relays("1", "3")) == 0, f"{_relays('1', '3')}")
check("empty source last_message -> no 1->3 edge", not _has_edge("1", "3"), f"{state.get_relations()}")

secretary.FEED.clear()
secretary.observe("1", "src", "x", _s2_board("has real output", src_state="error", dst="3"))  # errored source
check("errored source (board state=='error') -> no relay chip", len(_relays("1", "3")) == 0, f"{_relays('1', '3')}")
check("errored source -> no 1->3 edge", not _has_edge("1", "3"), f"{state.get_relations()}")

# ============================ S3 — decompose observe(): Haiku (brief+name) + Sonnet (triage+relay+frame) ============================
# S3 splits the single Sonnet observe() into Pass A (HAIKU, boardless: brief + name)
# and Pass B (SONNET, full board: triage + relay + frame) — cheap high-frequency
# upkeep on Haiku, cross-session judgment on Sonnet. It proves, deterministically:
#   (S3.1) observe() makes exactly TWO _run_json calls — Haiku FIRST, then Sonnet
#   (S3.2) Pass A's prompt is BOARDLESS (no other-panel lines); Pass B's carries the board
#   (S3.3) _BRIEF_MAX == 400 and a >400-char brief is stored truncated to exactly 400
#   (S3.4) the merged items match the old single-call shape (brief + name from A;
#          triage + relay from B all land in ONE items list)
#   (S3.5) each pass logs its OWN S0-schema cost row, and observe() returns the SUMMED cost
print("\n== S3 two-pass observe(): Haiku brief+name + Sonnet triage/relay/frame ==")

# recorder seam: capture (model, prompt) per _run_json call; return per-model canned JSON
_s3_calls: list[dict[str, Any]] = []
_S3_HAIKU = json.dumps({"brief": "B" * 500, "name_suggestion": "fresh-name"})  # >400 -> truncation test
_S3_SONNET = json.dumps(
    {
        "frame_suggestion": None,
        "triage": [{"severity": "info", "panel": "1", "text": "tri"}],
        "relays": [{"from": "1", "to": "3", "type": "FEEDS", "reason": "feeds 3"}],
    }
)


def _s3_record(prompt, model, *a, **k):  # matches _run_json(prompt, model, ...)
    _s3_calls.append({"model": model, "prompt": prompt})
    return (_S3_HAIKU if model == secretary.HAIKU_MODEL else _S3_SONNET), 0.0


secretary._run_json = _s3_record  # type: ignore[assignment]
secretary.FEED.clear()
secretary.clear_brief("1")
s3_items, s3_cost = secretary.observe("1", "session 1", "did work", BOARD)

# ---- (S3.1) two passes: Haiku FIRST, then Sonnet
print("\n(S3.1) observe() makes two passes - Haiku (brief+name) then Sonnet (triage/relay/frame)")
check("exactly two _run_json calls", len(_s3_calls) == 2, f"models={[c['model'] for c in _s3_calls]}")
check("first pass is Haiku", bool(_s3_calls) and _s3_calls[0]["model"] == secretary.HAIKU_MODEL, f"{[c['model'] for c in _s3_calls]}")
check("second pass is Sonnet", len(_s3_calls) > 1 and _s3_calls[1]["model"] == secretary.SECRETARY_MODEL, f"{[c['model'] for c in _s3_calls]}")

# ---- (S3.2) Pass A is boardless; Pass B carries the board (other panels' names appear)
print("\n(S3.2) Pass A (Haiku) prompt is boardless; Pass B (Sonnet) prompt carries the board")
_pa = _s3_calls[0]["prompt"] if _s3_calls else ""
_pb = _s3_calls[1]["prompt"] if len(_s3_calls) > 1 else ""
check("Pass A prompt has NO other-panel board lines", ("auth-jwt" not in _pa) and ("pay-stripe" not in _pa), f"other-panel names in Pass A: auth-jwt={'auth-jwt' in _pa} pay-stripe={'pay-stripe' in _pa}")
check("Pass B prompt DOES carry the board (other panels present)", ("auth-jwt" in _pb) and ("pay-stripe" in _pb), f"other-panel names in Pass B: auth-jwt={'auth-jwt' in _pb} pay-stripe={'pay-stripe' in _pb}")

# ---- (S3.3) cap is 400 and a >400-char brief truncates at exactly 400
print("\n(S3.3) _BRIEF_MAX == 400 and a >400-char brief truncates at exactly 400")
check("_BRIEF_MAX == 400", secretary._BRIEF_MAX == 400, f"{secretary._BRIEF_MAX}")
_b1 = secretary.brief_snapshot().get("1", "")
check("a 500-char brief is stored truncated to 400", len(_b1) == 400 and _b1 == "B" * 400, f"len={len(_b1)}")

# ---- (S3.4) merged items match the old single-call shape (A: brief+name; B: triage+relay)
print("\n(S3.4) merged items match the old single-call shape")
check("brief came from Pass A (Haiku)", _b1 == "B" * 400, f"brief len={len(_b1)}")
check(
    "name_suggestion (Pass A) surfaced in the merged items",
    any(i.get("kind") == "name_suggestion" and i.get("name") == "fresh-name" for i in s3_items),
    f"kinds={[i.get('kind') for i in s3_items]}",
)
check("triage (Pass B) surfaced in the merged items", any(i.get("kind") == "triage" for i in s3_items), f"kinds={[i.get('kind') for i in s3_items]}")
check(
    "relay (Pass B) surfaced in the merged items",
    any(i.get("kind") == "relay" and (i.get("from"), i.get("to")) == ("1", "3") for i in s3_items),
    f"kinds={[i.get('kind') for i in s3_items]}",
)

# ---- (S3.5) each pass logs its OWN per-pass cost row (S0 schema); observe() sums the cost.
# Restore the REAL _run_json + a per-model subprocess stub so BOTH passes log through
# the real _log_cost path (the recorder seam above bypassed it).
print("\n(S3.5) each pass logs its own S0-schema cost row; observe() returns the summed cost")
_S3_KA, _S3_KB = 0.001, 0.002


def _s3_per_model_run(cmd, *a, **k):  # inspect the real _run_json's cmd to pick the model
    model = cmd[cmd.index("--model") + 1]
    is_haiku = model == secretary.HAIKU_MODEL
    env = {"result": _S3_HAIKU if is_haiku else _S3_SONNET, "total_cost_usd": _S3_KA if is_haiku else _S3_KB}
    return types.SimpleNamespace(stdout=json.dumps(env), returncode=0)


secretary.subprocess.run = _s3_per_model_run  # type: ignore[assignment]
secretary._run_json = _REAL_RUN_JSON  # type: ignore[assignment]  # restore the genuine impl
_before5 = len(secretary.cost_samples())
_items5, _cost5 = secretary.observe("1", "session 1", "did work", BOARD)
_rows5 = secretary.cost_samples()[_before5:]
check("two cost rows logged (one per pass)", len(_rows5) == 2, f"rows={_rows5}")
check(
    "the two rows are a Haiku row + a Sonnet row",
    {r.get("model") for r in _rows5} == {secretary.HAIKU_MODEL, secretary.SECRETARY_MODEL},
    f"models={[r.get('model') for r in _rows5]}",
)
check(
    "each row carries the S0 schema (n_live + mode)",
    all(r.get("n_live") == EXPECTED_LIVE and r.get("mode") == "full" for r in _rows5),
    f"rows={_rows5}",
)
check("observe() returns the SUMMED cost of both passes", abs(_cost5 - (_S3_KA + _S3_KB)) < 1e-9, f"cost={_cost5}")

# ============================ S4 — frequency-gate (Pass B) + board mode Full/Quiet ============================
# S4 gates the EXPENSIVE Sonnet Pass B (cross-session triage/relay/frame) on
# frequency + a board mode, while the cheap Haiku Pass A (brief+name) ALWAYS runs.
# It proves, deterministically (stubbed _run_json counts calls per model):
#   (S4.5) state.get/set_board_mode: default "full", persists, coerces an unknown value
#   (S4.1) full + >=2 live -> BOTH passes run (Haiku then Sonnet)
#   (S4.2) <2 live -> Pass A only, Pass B SKIPPED (relays/frames are meaningless <2)
#   (S4.3) board mode "quiet" -> Pass A only, Pass B SKIPPED (even with >=2 live)
#   (S4.4) a burst of completions within the debounce window collapses to ONE Pass B
#   (S4.6) the cost row is tagged the LIVE board mode (S4 replaces S0's hardcoded "full")
#   (DONE-WHEN) in a MATCHED session-count bucket, S4's per-turn cost < the S0 baseline
#   (S4.7) GET/POST /board/mode (localhost-guarded) reports + sets the mode
print("\n== S4 frequency-gate + board mode Full/Quiet (Pass B only; Pass A always runs) ==")

_S4_HAIKU = json.dumps({"brief": "b", "name_suggestion": None})
_S4_SONNET = json.dumps({"frame_suggestion": None, "triage": [], "relays": []})
_s4_models: list[str] = []  # every _run_json model the current scenario saw


def _s4_run(prompt, model, *a, **k):  # matches _run_json(prompt, model, ...)
    _s4_models.append(model)
    return (_S4_HAIKU if model == secretary.HAIKU_MODEL else _S4_SONNET), 0.0


def _s4_board(n_live):
    """A board of exactly n_live launched (live) sessions — each has a session_id."""
    return [
        {"panel_id": str(i), "name": f"s{i}", "state": "done", "session_id": f"s{i}", "task": "t", "last_message": "out"}
        for i in range(1, n_live + 1)
    ]


def _reset_s4(window=0.0):
    """Fresh scenario: clear the call log + FEED, re-arm the debounce so the NEXT
    call always runs Pass B regardless of window, set the window, mode back to full."""
    _s4_models.clear()
    secretary.FEED.clear()
    secretary._run_json = _s4_run  # type: ignore[assignment]
    secretary._DEBOUNCE_WINDOW_S = window  # type: ignore[attr-defined]
    secretary._last_passb_ts = float("-inf")  # type: ignore[attr-defined]
    state.set_board_mode("full")


def _n(model):
    return sum(1 for m in _s4_models if m == model)


# ---- (S4.5) board-mode getter/setter: default, persist, coerce ----
print("\n(S4.5) board mode getter/setter: default full, persists, coerces an unknown value")
state.set_board_mode("full")
check("board mode defaults to 'full'", state.get_board_mode() == "full", f"{state.get_board_mode()}")
state.set_board_mode("quiet")
check("set_board_mode('quiet') persists", state.get_board_mode() == "quiet", f"{state.get_board_mode()}")
state.set_board_mode("bogus")
check("set_board_mode(unknown) coerces back to 'full'", state.get_board_mode() == "full", f"{state.get_board_mode()}")

# ---- (S4.1) full + >=2 live -> BOTH passes ----
print("\n(S4.1) full + >=2 live -> Pass A (Haiku) AND Pass B (Sonnet) both run")
_reset_s4()
secretary.observe("1", "s1", "did work", _s4_board(2))
check("Pass A (Haiku) ran", _n(secretary.HAIKU_MODEL) == 1, f"haiku={_n(secretary.HAIKU_MODEL)}")
check("Pass B (Sonnet) ran", _n(secretary.SECRETARY_MODEL) == 1, f"sonnet={_n(secretary.SECRETARY_MODEL)}")

# ---- (S4.2) <2 live -> Pass A only ----
print("\n(S4.2) <2 live -> Pass A only (Pass B skipped: relays/frames need >=2)")
_reset_s4()
secretary.observe("1", "s1", "did work", _s4_board(1))
check("Pass A still runs at <2 live", _n(secretary.HAIKU_MODEL) == 1, f"haiku={_n(secretary.HAIKU_MODEL)}")
check("Pass B SKIPPED at <2 live", _n(secretary.SECRETARY_MODEL) == 0, f"sonnet={_n(secretary.SECRETARY_MODEL)}")

# ---- (S4.3) quiet -> Pass A only (even with >=2 live) ----
print("\n(S4.3) board mode 'quiet' -> Pass A only (Pass B skipped even with >=2 live)")
_reset_s4()
state.set_board_mode("quiet")
secretary.observe("1", "s1", "did work", _s4_board(2))
check("Pass A still runs in quiet mode", _n(secretary.HAIKU_MODEL) == 1, f"haiku={_n(secretary.HAIKU_MODEL)}")
check("Pass B SKIPPED in quiet mode", _n(secretary.SECRETARY_MODEL) == 0, f"sonnet={_n(secretary.SECRETARY_MODEL)}")

# ---- (S4.4) debounce: a burst within the window collapses to ONE Pass B ----
print("\n(S4.4) a burst of completions within the debounce window -> ONE Pass B (Pass A every turn)")
_reset_s4(window=60.0)  # wide window so all three completions land inside it
secretary.observe("1", "s1", "burst", _s4_board(2))
secretary.observe("2", "s2", "burst", _s4_board(2))
secretary.observe("1", "s1", "burst", _s4_board(2))
check("Pass A ran every turn in the burst (3)", _n(secretary.HAIKU_MODEL) == 3, f"haiku={_n(secretary.HAIKU_MODEL)}")
check("Pass B collapsed to ONE for the burst", _n(secretary.SECRETARY_MODEL) == 1, f"sonnet={_n(secretary.SECRETARY_MODEL)}")
_reset_s4()  # restore window=0 for the real-cost sections below

# ---- (S4.6) the cost row is tagged the LIVE board mode (not S0's hardcoded "full") ----
# Restore the REAL _run_json + a subprocess stub so the row logs through _log_cost.
print("\n(S4.6) the cost row is tagged the LIVE board mode (S4 replaces S0's hardcoded 'full')")
_S4_KA, _S4_KB = 0.0011, 0.0090


def _s4_sub(cmd, *a, **k):  # pick the canned envelope by --model (real _run_json's cmd)
    model = cmd[cmd.index("--model") + 1]
    is_haiku = model == secretary.HAIKU_MODEL
    env = {"result": _S4_HAIKU if is_haiku else _S4_SONNET, "total_cost_usd": _S4_KA if is_haiku else _S4_KB}
    return types.SimpleNamespace(stdout=json.dumps(env), returncode=0)


secretary.subprocess.run = _s4_sub  # type: ignore[assignment]
secretary._run_json = _REAL_RUN_JSON  # type: ignore[assignment]
secretary._DEBOUNCE_WINDOW_S = 0.0  # type: ignore[attr-defined]
secretary._last_passb_ts = float("-inf")  # type: ignore[attr-defined]
secretary.FEED.clear()
state.set_board_mode("quiet")  # quiet -> only Pass A logs, so the single row's mode is testable
_b6 = len(secretary.cost_samples())
secretary.observe("1", "s1", "did work", _s4_board(2))
_rows6 = secretary.cost_samples()[_b6:]
check("quiet logs exactly ONE cost row (Pass A only)", len(_rows6) == 1, f"rows={_rows6}")
check(
    "the row is tagged mode='quiet' (the live mode, not the S0 hardcoded 'full')",
    bool(_rows6) and _rows6[0].get("mode") == "quiet",
    f"rows={_rows6}",
)
state.set_board_mode("full")

# ---- (DONE-WHEN) matched-bucket cost drop vs the S0 baseline ----
# S0's contract (verified above): EVERY turn logged BOTH passes -> baseline per-turn
# = KA+KB for ANY bucket. S4 gates Pass B, so in the SAME 1-live, full bucket a turn
# now logs ONLY Pass A (KA). KA < KA+KB -> a measured, like-for-like per-turn drop
# (this is the phase's payoff — not an aggregate that would hide the gate).
print("\n(DONE-WHEN) in a matched session-count bucket, S4 per-turn cost < the S0 baseline")
_S0_BASELINE_PER_TURN = _S4_KA + _S4_KB  # S0: Haiku + Sonnet every turn, regardless of bucket
secretary.subprocess.run = _s4_sub  # type: ignore[assignment]
secretary._run_json = _REAL_RUN_JSON  # type: ignore[assignment]
secretary._DEBOUNCE_WINDOW_S = 0.0  # type: ignore[attr-defined]
secretary._last_passb_ts = float("-inf")  # type: ignore[attr-defined]
secretary.FEED.clear()
state.set_board_mode("full")  # only the FREQUENCY gate differs from S0 in this bucket
_b7 = len(secretary.cost_samples())
_i7, _c7 = secretary.observe("1", "s1", "did work", _s4_board(1))  # the 1-live bucket
_rows7 = secretary.cost_samples()[_b7:]
_s4_per_turn = sum(r["cost"] for r in _rows7)
check(
    "S4 per-turn cost (1-live bucket) is LOWER than the S0 baseline",
    _s4_per_turn < _S0_BASELINE_PER_TURN,
    f"s4={_s4_per_turn} s0_baseline={_S0_BASELINE_PER_TURN}",
)
check(
    "the saving is exactly the skipped Sonnet Pass B (only the Haiku row logged)",
    len(_rows7) == 1 and abs(_s4_per_turn - _S4_KA) < 1e-9,
    f"rows={_rows7}",
)
check("observe() returns just the Pass A cost when Pass B is gated", abs(_c7 - _S4_KA) < 1e-9, f"cost={_c7}")

# ---- (S4.7) GET/POST /board/mode (localhost-guarded) ----
print("\n(S4.7) GET/POST /board/mode reports + sets the mode")
_mc = _appmod.app.test_client()
state.set_board_mode("full")
check("GET /board/mode reports the current mode", (_mc.get("/board/mode").get_json() or {}).get("mode") == "full", f"{_mc.get('/board/mode').get_json()}")
_pq = _mc.post("/board/mode", json={"mode": "quiet"})
check("POST /board/mode {quiet} -> 200 + mode quiet", _pq.status_code == 200 and (_pq.get_json() or {}).get("mode") == "quiet", f"status={_pq.status_code} body={_pq.get_json()}")
check("POST persisted: GET now reports quiet", (_mc.get("/board/mode").get_json() or {}).get("mode") == "quiet", f"{_mc.get('/board/mode').get_json()}")
_pbad = _mc.post("/board/mode", json={"mode": "bogus"})
check("POST /board/mode {invalid} -> 400", _pbad.status_code == 400, f"status={_pbad.status_code} body={_pbad.get_json()}")
state.set_board_mode("full")  # leave the (throwaway) board clean

# ============================ S5 — tailored relay prompt -> valid {from,to,type,reason} edges ============================
# S5 rewrites Pass B's relay GUIDANCE (prompt text only — relays are mostly review-
# relays passed within a group; suggest only what another session genuinely needs;
# PREFER SILENCE when unsure). The parse/persist path is UNCHANGED, so this section is
# the regression guard that the prompt edit kept the `relays` -> durable-EDGE contract
# intact. It is scoped to the EDGES (chip-suppression of these same relays is asserted
# in S1, not re-asserted here):
#   (S5.1) a well-formed relays output parses + persists as valid {from,to,type,reason} edges
#   (S5.2) a hallucinated/out-of-board target and a from==to self-loop NEVER become edges
# Uses a board of FRESH panel ids (10/11/12) + fresh source text so a persisted
# suppress-key from an earlier section can never collide with these relays.
print("\n== S5 tailored relay prompt -> valid {from,to,type,reason} edges ==")

_S5_BOARD = [
    {"panel_id": "10", "name": "rev", "state": "done", "session_id": "s10", "task": "review the diff", "last_message": "LGTM, but fix the retry path"},
    {"panel_id": "11", "name": "impl", "state": "running", "session_id": "s11", "task": "ship the feature", "last_message": "wip"},
    {"panel_id": "12", "name": "dep", "state": "idle", "session_id": "s12", "task": "downstream consumer", "last_message": "blocked on impl"},
]
_S5_CANNED = {
    "brief": "",
    "name_suggestion": None,
    "frame_suggestion": None,
    "triage": [],
    "relays": [
        {"from": "10", "to": "11", "type": "REVIEWS", "reason": "the review applies to the impl"},
        {"from": "11", "to": "12", "type": "DEPENDS", "reason": "downstream depends on the impl"},
        {"from": "10", "to": "404", "type": "FEEDS", "reason": "hallucinated target -> dropped"},  # 404 not live
        {"from": "11", "to": "11", "type": "CONFLICT", "reason": "self loop -> dropped"},  # from==to
    ],
}
secretary._run_json = lambda *a, **k: (json.dumps(_S5_CANNED), 0.0)  # type: ignore[assignment]
secretary._DEBOUNCE_WINDOW_S = 0.0  # type: ignore[attr-defined]  # no debounce -> Pass B runs this turn
secretary._last_passb_ts = float("-inf")  # type: ignore[attr-defined]
state.set_board_mode("full")  # full + 3 live -> Pass B (the relay leg) runs
secretary.FEED.clear()
secretary.observe("10", "rev", "LGTM, but fix the retry path", _S5_BOARD)
_s5_rels = state.get_relations()


def _s5_edge(frm, to):
    nf, nt = _rid(frm), _rid(to)
    return next((r for r in _s5_rels if (str(r.get("from")), str(r.get("to"))) == (nf, nt)), None)


# ---- (S5.1) well-formed relays persist as valid {from,to,type,reason} edges
print("\n(S5.1) well-formed relays persist as valid {from,to,type,reason} edges")
_e1011 = _s5_edge("10", "11")
check("edge 10->11 persisted", _e1011 is not None, f"{_s5_rels}")
check(
    "edge 10->11 is a valid {from,to,type,reason} edge (type REVIEWS)",
    bool(_e1011)
    and {"from", "to", "type", "reason"}.issubset(_e1011)
    and _e1011.get("type") == "REVIEWS"
    and bool(_e1011.get("reason")),
    f"{_e1011}",
)
_e1112 = _s5_edge("11", "12")
check(
    "edge 11->12 persisted as a valid {from,to,type,reason} edge (type DEPENDS)",
    bool(_e1112)
    and {"from", "to", "type", "reason"}.issubset(_e1112)
    and _e1112.get("type") == "DEPENDS"
    and bool(_e1112.get("reason")),
    f"{_e1112}",
)

# ---- (S5.2) hallucinated target + self-loop never become edges
print("\n(S5.2) hallucinated/out-of-board id and from==to never become edges")
check("no edge for hallucinated 10->404", _s5_edge("10", "404") is None, f"{_s5_rels}")
check("no edge for self-loop 11->11", _s5_edge("11", "11") is None, f"{_s5_rels}")

# ============================ S6 — augment (opt-in human section) + verbatim contract + UI-only provenance ============================
# S6 lets the human prepend an OPT-IN, human-authored `section` to a relay while the
# DELIVERED body stays VERBATIM and is resolved SERVER-SIDE from source.last_message.
# Hub provenance ([from <source>]) must live on the UI edge/feed, NEVER in the payload
# (the target processes it via --resume and would treat hub metadata as instruction --
# PLAN §3 derailment). It proves, deterministically (reusing S1's reply/broadcast seams):
#   (S6.1) NO section -> delivered text == the source's CURRENT full last_message, verbatim
#   (S6.2) WITH a section -> delivered text == section + verbatim, still carrying NO hub
#          provenance string ("[from .." / "relayed from ..") -> provenance is UI-only
#   (S6.3) a client-supplied `text` override is IGNORED -- the body is ALWAYS resolved
#          server-side from source.last_message (#13 verbatim-contract hardening)
#   (S6.4) provenance rides the UI edge/feed ONLY: the explicit edge persists AND the
#          target gets a [from <source>] feed marker carrying the source name, yet the
#          DELIVERED payload contains none of it
print("\n== S6 augment + verbatim contract + UI-only provenance ==")

# Fresh source/target panels (ids 20/21) so prior sections' edges/suppress-keys can't
# collide. last_message is full, multi-line, and far longer than the [:200]/[:500] copies.
_S6_VERBATIM = "RELEASE NOTES v2\n- fixed the retry path\n- " + "Z" * 800
_s6_src = _sessions.Session("20", "auth-oauth", "")
_s6_src.session_id = "s20"
_s6_src.state = "done"
_s6_src.last_message = _S6_VERBATIM
_s6_dst = _sessions.Session("21", "auth-jwt", "")
_s6_dst.session_id = "s21"
_s6_dst.state = "idle"  # not in app.BUSY_STATES
_sessions.MANAGER._sessions["20"] = _s6_src
_sessions.MANAGER._sessions["21"] = _s6_dst

# capture seams: the delivered payload (reply) + every UI broadcast, without spawning
_s6_delivered = {}


def _s6_capture_reply(panel_id, text):  # replaces MANAGER.reply so nothing spawns
    _s6_delivered["panel"] = panel_id
    _s6_delivered["text"] = text
    return _s6_dst


_sessions.MANAGER.reply = _s6_capture_reply  # type: ignore[assignment]
_s6_events = []
_s6_src.broadcast = lambda ev: _s6_events.append({"on": "20", **ev})  # type: ignore[assignment]
_s6_dst.broadcast = lambda ev: _s6_events.append({"on": "21", **ev})  # type: ignore[assignment]

_PROVENANCE_MARKERS = ("[from", "relayed from", "[relayed")


def _has_provenance(s):
    low = (s or "").lower()
    return any(m in low for m in _PROVENANCE_MARKERS)


_s6c = _appmod.app.test_client()

# ---- (S6.1) no section -> the source's full last_message, verbatim, server-resolved
print("\n(S6.1) no section -> delivered text is the source's full last_message, verbatim")
_s6_delivered.clear()
_s6_events.clear()
_r1 = _s6c.post("/route", json={"from_panel": "20", "to_panel": "21"})
check("/route (plain) returns 200", _r1.status_code == 200, f"status={_r1.status_code} body={_r1.get_json()}")
check("plain relay delivers the FULL verbatim last_message", _s6_delivered.get("text") == _S6_VERBATIM, f"len={len(_s6_delivered.get('text', ''))}")
check("plain relay payload has NO provenance string", not _has_provenance(_s6_delivered.get("text")), f"head={_s6_delivered.get('text', '')[:40]!r}")

# ---- (S6.2) with a section -> section + verbatim, still no hub provenance in the payload
print("\n(S6.2) with a human section -> section + verbatim, still no provenance in payload")
_s6_delivered.clear()
_s6_events.clear()
_SECTION = "Heads up: only the retry-path fix below is relevant to your work."
_r2 = _s6c.post("/route", json={"from_panel": "20", "to_panel": "21", "section": _SECTION})
check("/route (augmented) returns 200", _r2.status_code == 200, f"status={_r2.status_code} body={_r2.get_json()}")
_t2 = _s6_delivered.get("text", "")
check("augmented relay == section + verbatim", _t2 == f"{_SECTION}\n\n{_S6_VERBATIM}", f"head={_t2[:60]!r} tail-ok={_t2.endswith(_S6_VERBATIM)}")
check("the human section is at the FRONT and the verbatim body is intact after it", _t2.startswith(_SECTION) and _t2.endswith(_S6_VERBATIM), f"head={_t2[:60]!r}")
check("augmented payload still has NO hub provenance string", not _has_provenance(_t2), f"head={_t2[:60]!r}")

# ---- (S6.3) a client-supplied `text` override is IGNORED (body always server-resolved)
print("\n(S6.3) a client-supplied `text` override is ignored (verbatim contract #13)")
_s6_delivered.clear()
_s6_events.clear()
_r3 = _s6c.post("/route", json={"from_panel": "20", "to_panel": "21", "text": "SPOOFED CLIENT BODY -- must be ignored"})
check("/route (client text) returns 200", _r3.status_code == 200, f"status={_r3.status_code} body={_r3.get_json()}")
check("client `text` is ignored: delivered body is the server-resolved verbatim", _s6_delivered.get("text") == _S6_VERBATIM, f"head={_s6_delivered.get('text', '')[:40]!r}")
check("the spoofed client body did NOT ride into the payload", "SPOOFED" not in (_s6_delivered.get("text") or ""), f"head={_s6_delivered.get('text', '')[:40]!r}")

# ---- (S6.4) provenance is UI-only: explicit edge + [from <source>] feed marker, never payload
print("\n(S6.4) provenance rides the UI edge/feed only -- never the delivered payload")
_s6_delivered.clear()
_s6_events.clear()
_r4 = _s6c.post("/route", json={"from_panel": "20", "to_panel": "21", "section": _SECTION})
check(
    "/route recorded an EXPLICIT 20->21 edge (provenance on the edge)",
    any((str(r.get("from")), str(r.get("to"))) == (_rid("20"), _rid("21")) and r.get("source") == "explicit" for r in state.get_relations()),
    f"{state.get_relations()}",
)
_relay_in = next((e for e in _s6_events if e.get("on") == "21" and e.get("kind") == "relay_in"), None)
check("target got a relay_in feed marker (the [from <source>] provenance lives UI-side)", _relay_in is not None, f"events={_s6_events}")
check("the relay_in marker carries the SOURCE name so the UI can render [from <source>]", bool(_relay_in) and _relay_in.get("from_name") == "auth-oauth", f"{_relay_in}")
check("the source got a routed marker too", any(e.get("on") == "20" and e.get("kind") == "routed" for e in _s6_events), f"events={_s6_events}")
check("NONE of the provenance leaked into the delivered payload", not _has_provenance(_s6_delivered.get("text")), f"head={_s6_delivered.get('text', '')[:40]!r}")

# ============================ S7 — badge-queue: busy -> queue + badge -> re-surface on turn-complete ============================
# S7 turns the BUSY 409 on /route into a transient QUEUE + a tile badge, and re-surfaces
# the queued relay as a CONFIRM CHIP when the target next finishes a turn — never a silent
# auto-send. It proves, deterministically (reusing S1/S6's reply-capture seam):
#   (S7.1) a relay into a BUSY target is QUEUED (200, NOT 409), does NOT auto-send, and
#          increments that target's badge count (state.get_pending_counts())
#   (S7.2) a relay into a NO-SESSION target STILL 409s and is NEVER queued (the busy 409
#          is the ONLY one converted; a panel with no session can't hold a queue)
#   (S7.3) the pending queue is TRANSIENT — it never reaches state.json on disk
#   (S7.4) firing the server TURN_COMPLETE_HOOK (_on_turn_complete) for the target
#          RE-SURFACES the relay as a kind:"relay" chip, does NOT auto-send (no reply),
#          and clears the badge — and it fires with the target 'done' (NOT 'idle'), proving
#          the re-surface is NOT gated on state=='idle'
print("\n== S7 badge-queue: busy -> queued + badge; turn-complete re-surfaces (never auto-sent) ==")

# Fresh panels 30 (source) / 31 (busy target) / 32 (no-session) so prior edges/keys can't collide.
secretary.FEED.clear()
_s7_src = _sessions.Session("30", "src-30", "")
_s7_src.session_id = "s30"
_s7_src.state = "done"
_s7_src.last_message = "S7 PAYLOAD: queue me while the target is busy"
_s7_tgt = _sessions.Session("31", "tgt-31", "")
_s7_tgt.session_id = "s31"
_s7_tgt.state = "running"  # BUSY (in app.BUSY_STATES)
_s7_nosess = _sessions.Session("32", "tgt-32", "")  # a panel that exists but was never launched
_s7_nosess.session_id = None
_s7_nosess.state = "empty"
_sessions.MANAGER._sessions["30"] = _s7_src
_sessions.MANAGER._sessions["31"] = _s7_tgt
_sessions.MANAGER._sessions["32"] = _s7_nosess

# capture reply so we can PROVE neither the queue nor the re-surface ever auto-sends
_s7_replies: list[tuple[str, str]] = []
_sessions.MANAGER.reply = lambda pid, text: _s7_replies.append((pid, text))  # type: ignore[assignment]

_s7c = _appmod.app.test_client()

# ---- (S7.1) busy target -> queued (no 409), no auto-send, badge count increments
print("\n(S7.1) relay into a busy target -> queued (200, not 409) + badge count increments, no auto-send")
_before_cnt = state.get_pending_counts().get("31", 0)
_rq = _s7c.post("/route", json={"from_panel": "30", "to_panel": "31"})
check("/route into a busy target returns 200 (queued, not 409)", _rq.status_code == 200, f"status={_rq.status_code} body={_rq.get_json()}")
check("the response reports queued=True", bool((_rq.get_json() or {}).get("queued")), f"body={_rq.get_json()}")
check("the busy relay did NOT auto-send (no reply)", _s7_replies == [], f"replies={_s7_replies}")
check("the target's badge count incremented to 1", state.get_pending_counts().get("31", 0) == _before_cnt + 1, f"counts={state.get_pending_counts()}")

# ---- (S7.2) no-session target -> STILL a hard 409, never queued
print("\n(S7.2) relay into a no-session target -> still 409 (NOT queued)")
_rn = _s7c.post("/route", json={"from_panel": "30", "to_panel": "32"})
check("/route into a no-session target returns 409", _rn.status_code == 409, f"status={_rn.status_code} body={_rn.get_json()}")
check("the no-session target was NOT queued (no badge)", "32" not in state.get_pending_counts(), f"counts={state.get_pending_counts()}")

# ---- (S7.3) the pending queue is transient — never written to state.json
print("\n(S7.3) the pending queue is transient — never persisted to state.json")
_disk7 = json.loads(TMP_STATE.read_text(encoding="utf-8")) if TMP_STATE.exists() else {}
check(
    "pending relays are NOT in state.json on disk (transient, lost on restart)",
    "pending" not in _disk7 and "pending_relays" not in _disk7,
    f"top-level keys={list(_disk7)}",
)

# ---- (S7.4) turn-completion re-surfaces the relay as a confirm chip — not auto-sent,
#       not gated on state=='idle' (the just-completed target is 'done', never idle)
print("\n(S7.4) the target's turn-completion re-surfaces the queued relay as a confirm chip (never auto-sent)")
_s7_tgt.state = "done"  # a just-completed panel is done/awaiting-input — NEVER idle
# empty secretary stub so the observe() the hook also spawns adds no competing relay items
secretary._run_json = lambda *a, **k: (json.dumps({"brief": "", "name_suggestion": None, "frame_suggestion": None, "triage": [], "relays": []}), 0.0)  # type: ignore[assignment]
_appmod._on_turn_complete(_s7_tgt)  # fires the REAL server hook (resurface runs synchronously, before observe spawns)
_resurfaced = [i for i in secretary.feed_snapshot()["items"] if i.get("kind") == "relay" and (i.get("from"), i.get("to")) == ("30", "31")]
check("the queued relay RE-SURFACED as a kind:'relay' chip in the feed", len(_resurfaced) == 1, f"resurfaced={_resurfaced}")
check("the re-surfaced chip carries a dismissable id (the manual confirm path)", bool(_resurfaced and _resurfaced[0].get("id")), f"{_resurfaced}")
check("the re-surface did NOT auto-send (still no reply — zero silent sends)", _s7_replies == [], f"replies={_s7_replies}")
check("the target's pending queue is emptied after re-surface (the badge clears)", "31" not in state.get_pending_counts(), f"counts={state.get_pending_counts()}")

# ============================ S8 — auto-relay DORMANT (F-IDENT drops members[]) ====
# F-IDENT reverses S8's explicit-members model (plan §7.3) and drops members[]. Auto-
# relay therefore cannot fire until Slice 6 rewires _auto_eligible to positional
# (inside-rect minus excluded) membership. This block PROVES the dormancy is declared,
# not accidental. Slice 6 re-authors the full auto-dispatch assertions on the new model.
print("\n== S8 -> Slice 6: auto-relay is RESTORED on positional membership (full test in verify_group_image.py) ==")
_df = state.add_frame("grp", {"x": 0, "y": 0, "w": 50, "h": 50})
state.set_frame_auto_relay(_df["id"], True)
_auto_frames = [f for f in state.get_frames() if f.get("auto_relay")]
check("a FEEDS relay between panels with NO pins is NOT auto-eligible (needs effective membership)",
      not secretary._auto_eligible({"from": "1", "to": "2", "type": "FEEDS"}, _auto_frames, state.get_positions()),
      "should need positional membership")

# ============================ relayfix — review fixes (MED-1 verbatim post-restart; MED-2 edge type) ============================
# Two post-build review fixes, proven deterministically:
#   (RF1) MED-1: after a restart state.json holds only last_message[:500]; ensure_rehydrated
#         lifts the FULL last assistant message back from the durable transcript, sentinel-
#         stripped, so a verbatim relay delivers the complete text — never a 500-char clip,
#         never the done-sentinel. A length guard never downgrades a fresh in-memory message.
#   (RF2) MED-2: a typed ✓-route chip keeps its REVIEWS/DEPENDS/CONFLICT classification on the
#         EXPLICIT edge (was clobbered to FEEDS); a type-less route preserves an existing
#         inferred type; a fresh type-less route still inserts FEEDS; the busy-queue keeps the type.
print("\n== relayfix: MED-1 verbatim-from-transcript + MED-2 edge-type preservation ==")

# ---- (RF1) ensure_rehydrated restores the FULL, sentinel-cleaned last_message ----
print("\n(RF1) MED-1: ensure_rehydrated lifts the full verbatim from the transcript (sentinel-stripped)")
_rf_orig_rehydrate = _sessions.rehydrate_history
_rf_FULL = "RF FULL OUTPUT " + "Q" * 900  # the true (long) last message
_rf_sess = _sessions.Session("50", "rf-src", "")
_rf_sess.session_id = "s50"
_rf_sess.sentinel = "<<<DONE_rf01>>>"
_rf_sess.last_message = _rf_FULL[:500]  # what state.json's [:500] restored on restart (truncated)
# canned transcript: a user line + the full assistant message carrying a TRAILING sentinel
_sessions.rehydrate_history = (  # type: ignore[assignment]
    lambda sid: (
        [{"kind": "user", "text": "go"}, {"kind": "message", "role": "assistant", "text": _rf_FULL + "\n<<<DONE_rf01>>>", "model": "m"}]
        if sid == "s50"
        else []
    )
)
_rf_sess.ensure_rehydrated()
check("the truncated [:500] copy was upgraded to the FULL transcript message", _rf_sess.last_message == _rf_FULL, f"len={len(_rf_sess.last_message)} expected={len(_rf_FULL)}")
check("the done-sentinel was stripped (never rides into a relay)", "<<<DONE_rf01>>>" not in _rf_sess.last_message, f"tail={_rf_sess.last_message[-24:]!r}")
# a fresh, longer in-memory message is NEVER downgraded to a shorter transcript copy
_rf_fresh = _sessions.Session("50", "rf-src", "")
_rf_fresh.session_id = "s50b"
_rf_fresh.last_message = "FRESH FULL MESSAGE LIVE THIS PROCESS"  # longer than the canned 'stale'
_sessions.rehydrate_history = lambda sid: [{"kind": "message", "role": "assistant", "text": "stale", "model": "m"}]  # type: ignore[assignment]
_rf_fresh.ensure_rehydrated()
check("a fresh in-memory last_message is NOT downgraded to a shorter transcript copy", _rf_fresh.last_message == "FRESH FULL MESSAGE LIVE THIS PROCESS", f"{_rf_fresh.last_message!r}")
_sessions.rehydrate_history = _rf_orig_rehydrate  # restore

# ---- (RF2) MED-2: edge type preserved on a typed ✓-route; not clobbered to FEEDS ----
print("\n(RF2) MED-2: a typed route keeps its type; a type-less route preserves an existing type")
for _pid, _st, _msg in (("51", "done", "RF2 source output"), ("52", "done", ""), ("54", "done", ""), ("55", "done", ""), ("56", "running", "")):
    _s = _sessions.Session(_pid, "rf-" + _pid, "")
    _s.session_id = "s" + _pid
    _s.state = _st
    _s.last_message = _msg
    _sessions.MANAGER._sessions[_pid] = _s
_rf_replies: list[tuple[str, str]] = []
_sessions.MANAGER.reply = lambda pid, text: _rf_replies.append((pid, text))  # type: ignore[assignment]
_rfc = _appmod.app.test_client()


def _rf_edge(frm, to):
    nf, nt = _rid(frm), _rid(to)
    return next((r for r in state.get_relations() if (str(r.get("from")), str(r.get("to"))) == (nf, nt)), None)


# (a) an inferred REVIEWS edge exists -> a type-LESS /route upgrades to explicit WITHOUT clobbering the type
state.add_relation(_rid("51"), _rid("52"), "REVIEWS", "inferred", "rev applies")
_rfc.post("/route", json={"from_panel": "51", "to_panel": "52"})  # no rel_type
_ea = _rf_edge("51", "52")
check("type-less /route over an inferred REVIEWS edge KEEPS REVIEWS (not clobbered to FEEDS)", bool(_ea) and _ea.get("type") == "REVIEWS", f"{_ea}")
check("...and still upgrades the edge to explicit", bool(_ea) and _ea.get("source") == "explicit", f"{_ea}")
# (b) a typed /route on a FRESH pair records that exact type explicitly
_rfc.post("/route", json={"from_panel": "51", "to_panel": "54", "rel_type": "DEPENDS"})
_eb = _rf_edge("51", "54")
check("a typed /route on a fresh pair records the given type (DEPENDS, explicit)", bool(_eb) and _eb.get("type") == "DEPENDS" and _eb.get("source") == "explicit", f"{_eb}")
# (c) a type-less /route on a FRESH pair (no prior edge) still inserts FEEDS by default
_rfc.post("/route", json={"from_panel": "51", "to_panel": "55"})
_ec = _rf_edge("51", "55")
check("a type-less /route on a fresh pair inserts FEEDS by default", bool(_ec) and _ec.get("type") == "FEEDS", f"{_ec}")
# (d) the busy-target queue keeps the typed chip's type through to the re-surfaced chip
_before_q = state.get_pending_counts().get("56", 0)
_rfq = _rfc.post("/route", json={"from_panel": "51", "to_panel": "56", "rel_type": "REVIEWS"})
check("typed relay into a busy target -> queued (200)", _rfq.status_code == 200 and bool((_rfq.get_json() or {}).get("queued")), f"{_rfq.get_json()}")
_sessions.MANAGER._sessions["56"].state = "done"  # the target now completes a turn
secretary._run_json = lambda *a, **k: (json.dumps({"brief": "", "name_suggestion": None, "frame_suggestion": None, "triage": [], "relays": []}), 0.0)  # type: ignore[assignment]
_appmod._on_turn_complete(_sessions.MANAGER._sessions["56"])  # resurface runs synchronously
_rfd = [i for i in secretary.feed_snapshot()["items"] if i.get("kind") == "relay" and (i.get("from"), i.get("to")) == ("51", "56")]
check("the re-surfaced queued chip KEPT its REVIEWS type (not degraded to FEEDS)", len(_rfd) == 1 and _rfd[0].get("type") == "REVIEWS", f"{_rfd}")

# ---------------- (later slices S1-S8 APPEND their sections ABOVE this line) ----------------

# ----------------------------------------------------------------------- cleanup
for p in (TMP_STATE, TMP_STATE.with_suffix(".json.tmp"), secretary._COST_LOG):  # type: ignore[attr-defined]
    try:
        p.unlink()
    except OSError:
        pass
try:
    _COST_DIR.rmdir()
except OSError:
    pass

# ----------------------------------------------------------------------- verdict
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print("\n==================== RELAY + SECRETARY-COST ====================")
print(f"{passed}/{total} checks passed")
all_ok = passed == total
print(f"\nS0 SECRETARY-COST BASELINE: {'PASS' if all_ok else 'FAIL'}")
raise SystemExit(0 if all_ok else 1)
