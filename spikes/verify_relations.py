"""Verify S2 (typed, persisted relations): the backend half of the Map view.

Relations used to be transient feed items; S2 promotes the secretary's existing
`{from,to,reason}` relays to durable, TYPED, validated edges and serves them from
GET /relations. This spike proves, deterministically and WITHOUT spending a live
Sonnet call, that:

  (1) the secretary prompt now ASKS for a relation `type` (the 4-type taxonomy)
  (2) observe() reads the type and PERSISTS the relay as an edge
      {from,to,type,source:"inferred"} — keeping its S0 brief + triage intact
  (3) a missing type defaults to FEEDS; a junk type is coerced to FEEDS;
      a lowercase type is normalised (DEPENDS)
  (4) edges naming a non-existent panel, or a self-loop, are DROPPED before
      persisting (the LLM can hallucinate a panel id → dangling Map edge)
  (5) a human /route records an EXPLICIT edge, and `source` never downgrades:
      a later secretary inference can't flip a confirmed (solid) edge to dashed
  (6) drop_node drops every edge touching the node (mirrors clear_brief)
  (7) GET /relations serves [{from,to,type,source}]

It drives observe() in-process with a stubbed LLM and redirects persistence to a
throwaway state.json, so it never touches the real board and needs no running hub
or API key. Run from the hub root with the hub venv:
    .venv/Scripts/python.exe spikes/verify_relations.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))  # so `from hub import ...` / `import app` work from any cwd

from hub import state  # noqa: E402

# ---- redirect ALL persistence to a throwaway file (never touch the real board) ----
# F-IDENT: redirect BEFORE importing sessions. `MANAGER = SessionManager()` now runs
# migrate_to_node_ids() at import time, which would otherwise WRITE node_ids into the
# REAL state.json (this spike's hermetic contract forbids that). Pointing STATE_FILE at
# TMP first means the startup migration hits TMP (empty -> no-op) instead.
_tmp = tempfile.NamedTemporaryFile(prefix="hub_relspike_", suffix=".json", delete=False)
_tmp.close()
TMP = Path(_tmp.name)
state.STATE_FILE = TMP

from hub import secretary, sessions  # noqa: E402  (after the redirect: MANAGER's startup migration hits TMP)
# Every source panel needs a non-empty last_message: the relay-routing S2 empty-source
# guard (verify_routing.py S2.3) drops any relay whose SOURCE has nothing to send, so
# panels 2 and 3 (the sources of the 2->3 and 3->1 relays in CANNED below) must carry
# content or those edges never persist. They were last_message="" — a stale fixture
# predating that guard, which silently failed checks (3)/(4)/(6)/(7) regardless of S3.
BOARD = [
    {"panel_id": "1", "name": "auth-oauth", "state": "done", "task": "build oauth", "last_message": "ok"},
    {"panel_id": "2", "name": "auth-jwt", "state": "running", "task": "rotate jwt", "last_message": "jwt rotated"},
    {"panel_id": "3", "name": "pay-stripe", "state": "running", "task": "stripe webhooks", "last_message": "stripe wired"},
]
state.save(
    {"panels": {b["panel_id"]: {"name": b["name"], "session_id": "s" + b["panel_id"]} for b in BOARD}, "relations": []}
)
# F-IDENT: relations are node-keyed. `sessions` (hence MANAGER) was imported ABOVE,
# BEFORE the STATE_FILE redirect, so the startup migrate_to_node_ids() ran on the REAL
# board, not TMP — the later `from app import app` doesn't rebuild MANAGER. Mint nodes
# for THIS spike's panels explicitly so observe()/route translate panel->node. Idempotent.
state.migrate_to_node_ids()

# importing app AFTER the redirect means MANAGER restores from TMP, and the
# /relations route reads TMP too (same module singleton).
from app import app  # noqa: E402

from hub import nodes  # noqa: E402  (F-IDENT: relations are node-keyed)


def nid(pid):  # F-IDENT: a test panel's node_id (minted by the startup migration)
    return nodes.node_id_for_panel(pid)


client = app.test_client()

# ---- stub the LLM: observe() will parse exactly this JSON, no network/spend ----
CANNED = {
    "brief": "Working toward the OAuth google login flow; wired the handler and got its tests green this turn.",
    "triage": [{"severity": "info", "panel": "1", "text": "oauth tests passing"}],
    "relays": [
        {"from": "1", "to": "2", "type": "REVIEWS", "reason": "oauth review applies to jwt"},
        {"from": "2", "to": "3", "reason": "jwt output feeds stripe"},  # no type   -> FEEDS
        {"from": "3", "to": "1", "type": "depends", "reason": "stripe depends on oauth"},  # lower -> DEPENDS
        {"from": "1", "to": "999", "type": "FEEDS", "reason": "hallucinated target"},  # 999 not live -> DROP
        {"from": "2", "to": "2", "type": "CONFLICT", "reason": "self loop"},  # from==to     -> DROP
        {"from": "1", "to": "3", "type": "BOGUS", "reason": "invalid type"},  # junk type    -> FEEDS
    ],
}
secretary._run_json = lambda *a, **k: (json.dumps(CANNED), 0.0)  # type: ignore[assignment]


# ----------------------------------------------------------------- check harness
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def edge(rels, frm, to):
    nf, nt = nid(frm), nid(to)
    return next((r for r in rels if str(r.get("from")) == nf and str(r.get("to")) == nt), None)


print("== S2 typed, persisted relations verification ==")

# ---- (1) the prompt now asks for a type
print("\n(1) secretary prompt asks for a relation type")
prompt = secretary._SECRETARY_PROMPT
check('prompt schema mentions "type"', '"type"' in prompt)
check(
    "prompt lists all 4 relation types",
    all(t in prompt for t in secretary.RELATION_TYPES),
    f"types={secretary.RELATION_TYPES}",
)

# ---- drive one observe() pass with the stubbed LLM
items, _cost = secretary.observe("1", "auth-oauth", "tests green", BOARD)
rels = state.get_relations()

# ---- (2) it persisted typed inferred edges + kept brief/triage intact
print("\n(2) observe() persists typed inferred edges; S0 brief + triage intact")
e12 = edge(rels, "1", "2")
check("edge 1->2 persisted", e12 is not None, f"{e12}")
check("edge 1->2 type=REVIEWS", bool(e12) and e12.get("type") == "REVIEWS", f"{e12}")
check("edge 1->2 source=inferred", bool(e12) and e12.get("source") == "inferred", f"{e12}")
check(
    "relay feed item carries a type",
    any(it.get("kind") == "relay" and it.get("type") for it in items),
    f"relays={[it for it in items if it.get('kind') == 'relay']}",
)
# S1: the relay's human-readable reason rides onto BOTH the edge and the feed chip.
check("S1: edge 1->2 carries the relay reason", bool(e12) and e12.get("reason") == "oauth review applies to jwt", f"{e12}")
check(
    "S1: relay feed item carries its reason",
    any(
        it.get("kind") == "relay" and (it.get("from"), it.get("to")) == ("1", "2")
        and it.get("reason") == "oauth review applies to jwt"
        for it in items
    ),
    f"relays={[it for it in items if it.get('kind') == 'relay']}",
)
check(
    "S0 brief still written",
    secretary.brief_snapshot().get("1", "").startswith("Working toward the OAuth"),
    secretary.brief_snapshot().get("1", "")[:40],
)
check("S0 triage still flows", any(it.get("kind") == "triage" for it in items))

# ---- (3) type defaulting / normalisation
print("\n(3) type defaulting + normalisation")
e23 = edge(rels, "2", "3")
e31 = edge(rels, "3", "1")
e13 = edge(rels, "1", "3")
check("missing type defaults to FEEDS", bool(e23) and e23.get("type") == "FEEDS", f"{e23}")
check("lowercase 'depends' -> DEPENDS", bool(e31) and e31.get("type") == "DEPENDS", f"{e31}")
check("junk type 'BOGUS' -> FEEDS", bool(e13) and e13.get("type") == "FEEDS", f"{e13}")

# ---- (4) validation drops hallucinated / self edges
print("\n(4) hallucinated-panel + self-loop edges dropped before persisting")
check("edge to non-existent panel 999 dropped", edge(rels, "1", "999") is None)
check("self-loop 2->2 dropped", edge(rels, "2", "2") is None)
check("exactly 4 edges persisted (6 relays - 2 invalid)", len(rels) == 4, f"count={len(rels)} rels={rels}")
# S1: the validity guard now gates the FEED ITEM too — a hallucinated/self-loop
# relay must produce NO chip, not just no edge.
relay_items = [it for it in items if it.get("kind") == "relay"]
check(
    "S1: invalid relays produce NO feed chip either",
    not any((it.get("from"), it.get("to")) in {("1", "999"), ("2", "2")} for it in relay_items),
    f"relay_items={relay_items}",
)
check("S1: exactly 4 relay chips (one per valid edge)", len(relay_items) == 4, f"count={len(relay_items)}")

# ---- (5) explicit edges + no source downgrade
print("\n(5) explicit /route edge wins; inference can't downgrade it")
state.add_relation(nid("1"), nid("2"), "FEEDS", "explicit")  # what /route does (node-keyed)
rels = state.get_relations()
e12 = edge(rels, "1", "2")
dupes = [r for r in rels if str(r.get("from")) == nid("1") and str(r.get("to")) == nid("2")]
check("explicit upsert did NOT duplicate the (1,2) edge", len(dupes) == 1, f"dupes={dupes}")
check("(1,2) source upgraded to explicit", bool(e12) and e12.get("source") == "explicit", f"{e12}")
# S1: that reason-less explicit upsert (a human /route over the inferred edge) must
# UPGRADE source WITHOUT blanking the secretary's reason.
check(
    "S1: reason-less explicit upsert KEEPS the prior reason",
    bool(e12) and e12.get("reason") == "oauth review applies to jwt",
    f"{e12}",
)
state.add_relation(nid("1"), nid("2"), "REVIEWS", "inferred")  # a later secretary inference
e12 = edge(state.get_relations(), "1", "2")
check("later inference does NOT downgrade source to inferred", bool(e12) and e12.get("source") == "explicit", f"{e12}")
check(
    "S1: later reason-less inference also preserves the reason",
    bool(e12) and e12.get("reason") == "oauth review applies to jwt",
    f"{e12}",
)

# ---- (6) relaunch / drop_node clears every edge touching the node
print("\n(6) drop_node drops every edge touching the node (mirrors clear_brief)")
before = state.get_relations()
n1 = nid("1")
touching_1 = [r for r in before if n1 in (str(r.get("from")), str(r.get("to")))]
state.drop_node(n1)   # F-IDENT: relaunch archives the old node's semantics this way
after = state.get_relations()
check("had >1 edge touching node 1 before", len(touching_1) >= 2, f"{touching_1}")
check("no edge touches node 1 after drop_node", not any(n1 in (str(r.get("from")), str(r.get("to"))) for r in after), f"after={after}")
check("the untouched 2->3 edge survived", edge(after, "2", "3") is not None, f"after={after}")

# ---- (7) GET /relations serves the [{from,to,type,source}] shape
print("\n(7) GET /relations serves the edge shape")
resp = client.get("/relations")
body = resp.get_json()
served = (body or {}).get("relations", [])
check("/relations returns 200", resp.status_code == 200, f"status={resp.status_code}")
check("/relations payload is a list under `relations`", isinstance(served, list), f"body={body}")
check(
    "every served edge has from/to/type/source",
    all(set(("from", "to", "type", "source")).issubset(r) for r in served) if served else False,
    f"served={served}",
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
print("\n==================== S2 TYPED RELATIONS ====================")
print(f"{passed}/{total} checks passed")
all_ok = passed == total
print(f"\nS2 TYPED, PERSISTED RELATIONS: {'PASS' if all_ok else 'FAIL'}")
raise SystemExit(0 if all_ok else 1)
