"""S3 spike-verify: per-session context% + global subscription rate-limits.

Convention: like the other spikes/, this is a standalone script (no pytest). It
replays the captured `run/spike_stream.jsonl` through the REAL Session._handle_event
so it guards the actual implementation, not just field names. No live hub, no
`claude` subprocess, no token spend.

What it asserts:
  1. `usage` carries input/cache_creation/cache_read/output (the ctx numerator).
  2. `result.modelUsage[<key>].contextWindow` exists (the denominator).
  3. _handle_event broadcasts a `ctx` event whose pct uses the 1M window — i.e.
     the model-key lookup tolerated the 'claude-opus-4-8' vs 'claude-opus-4-8[1m]'
     mismatch (a naive modelUsage[model] would miss and fall back to 200k).
  4. A synthetic MULTI-entry modelUsage still resolves via the prefix path.
  5. rate_limit_event is captured (no longer discarded). "No rate_limit_event at
     low utilization" is a PASS, not a failure — the warning only fires past ~0.9.

Run:  python spikes/verify_usage.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SPIKE = ROOT / "run" / "spike_stream.jsonl"

from hub import sessions, state  # noqa: E402
from hub.sessions import Session, _pick_model_usage, rate_limit_snapshot, record_rate_limit  # noqa: E402

_FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        _FAILS.append(msg)


def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


# ---- 0. load the captured stream ---------------------------------------------
if not SPIKE.exists():
    print(f"FAIL  spike stream missing: {SPIKE}")
    sys.exit(1)

raw_events = []
for line in SPIKE.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line.startswith("{"):
        try:
            raw_events.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # skip the stray non-JSON warning line at the top
print(f"loaded {len(raw_events)} JSON events from {SPIKE.name}\n")

# ---- 1 & 2. confirm the field names directly in the stream -------------------
result_ev = next((e for e in raw_events if e.get("type") == "result"), None)
check(result_ev is not None, "stream contains a `result` event")
usage = (result_ev or {}).get("usage") or {}
for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
    check(k in usage, f"usage.{k} present")
model_usage = (result_ev or {}).get("modelUsage") or {}
check(bool(model_usage), "result.modelUsage present")
some_md = next(iter(model_usage.values()), {})
check(bool(some_md.get("contextWindow")), "modelUsage entry has contextWindow")

# ---- 3. replay through the REAL _handle_event -------------------------------
state.update_panel = lambda *a, **k: None  # don't pollute state.json
sessions._RATE_LIMITS.clear()  # isolate the global from any prior import-time state

s = Session("_verify_usage", "verify", str(ROOT))
events: list[dict] = []
s.broadcast = lambda ev: events.append(ev)  # type: ignore[method-assign]
for ev in raw_events:
    s._handle_event(ev)

ctx_events = [e for e in events if e.get("kind") == "ctx"]
check(len(ctx_events) >= 1, "a `ctx` event was broadcast")
if ctx_events:
    c = ctx_events[-1]
    expected_tokens = sum(
        usage.get(k, 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    )
    check(c["tokens"] == expected_tokens, f"ctx.tokens == sum of usage ({expected_tokens})")
    check(c["window"] == 1_000_000, f"ctx.window resolved to the 1M [1m] window (got {c['window']})")
    check(
        approx(c["pct"], expected_tokens / 1_000_000),
        f"ctx.pct == tokens/1M (got {c['pct']:.6f}, NOT the 200k-fallback {expected_tokens / 200_000:.6f})",
    )
    check(0.0 < c["compact_pct"] < 1.0, f"ctx.compact_pct in (0,1): {c['compact_pct']}")

# ---- 4. synthetic guard: prefix lookup with a MULTI-entry modelUsage ---------
# This is the case the captured stream can't distinguish (it has one entry). It
# directly exercises the fix for the assistant-id vs modelUsage-key mismatch.
md = _pick_model_usage(
    "claude-opus-4-8",
    {"claude-sonnet-4-6": {"contextWindow": 200_000}, "claude-opus-4-8[1m]": {"contextWindow": 1_000_000}},
)
check(
    md.get("contextWindow") == 1_000_000, "multi-entry modelUsage: prefix lookup picks the [1m] window, not the decoy"
)

# ---- 5. rate_limit_event capture --------------------------------------------
snap = rate_limit_snapshot()
sess = snap.get("session")
check(sess is not None, "five_hour rate_limit captured (event no longer discarded)")
if sess:
    check(sess.get("resets_at") is not None, f"rate_limit.resets_at captured ({sess.get('resets_at')})")
    check(sess.get("status") is not None, f"rate_limit.status captured ({sess.get('status')!r})")
# At 'allowed' with no utilization there is NO warning — that is a PASS, not a fail.
check(snap.get("warning") is False, "no warning at low utilization (warning only fires past ~0.9) — PASS-on-absence")

# synthetic: a warning-level event DOES flip the flag
record_rate_limit({"rateLimitType": "five_hour", "status": "allowed_warning", "resetsAt": 1, "utilization": 0.97})
check(rate_limit_snapshot()["warning"] is True, "allowed_warning event flips warning -> True")

# ---- verdict -----------------------------------------------------------------
print()
if _FAILS:
    print(f"RESULT: {len(_FAILS)} FAILED")
    sys.exit(1)
print("RESULT: ALL PASS")
