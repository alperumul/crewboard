"""S3 Haiku quality gate: a Haiku-vs-Sonnet brief-quality diff.

S3 decomposed observe() so the per-session BRIEF (+ name) is produced by the
BOARDLESS HAIKU Pass A instead of the single Sonnet call. Decomposing brief->Haiku
is a QUALITY BET, not free: this gate runs the SAME `_BRIEF_PROMPT` on Haiku AND on
Sonnet over a handful of real-board fixtures and records both briefs side-by-side,
so a human can eyeball whether Haiku reads as well as Sonnet. Keep Haiku ONLY if
quality holds; otherwise flip Pass A's model back to Sonnet (use SECRETARY_MODEL in
observe()'s Pass A call) and record why in the VERDICT below.

The fixtures span the cases the brief must handle — the S3 risk is the terse
done-token / one-word output, where a good brief MUST lean on the launch task, not
the ~1-3 words actually emitted.

This SPENDS a small amount on the Pro/Max subscription (a few cheap `claude -p`
--output-format json calls). Needs `claude` on PATH; no running hub required.
Run from the hub root with the hub venv:
    .venv/Scripts/python.exe spikes/verify_brief_quality.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_ROOT))  # so `from hub import ...` works from any cwd

from hub import secretary  # noqa: E402

# keep the real cost log clean — redirect to a throwaway dir (spike convention)
_COST_DIR = Path(tempfile.mkdtemp(prefix="hub_briefqual_"))
secretary._RUN_DIR = _COST_DIR  # type: ignore[attr-defined]
secretary._COST_LOG = _COST_DIR / "secretary_cost.log"  # type: ignore[attr-defined]

RECORD = HUB_ROOT / "docs" / "plans" / "2026-06-22-relay-routing-s3-brief-quality.md"

# A handful of real-board fixtures spanning the cases the brief must handle:
#   - a terse done-token (the S3 risk: the brief must lean on the TASK, not the output)
#   - a one-word confirmation with a prior brief (carry-forward under terseness)
#   - rich multi-line output on a first turn
#   - a carry-forward where real incremental progress should update the prior brief
FIXTURES = [
    {
        "label": "terse done-token, first turn (task-grounding stress)",
        "name": "session 3",
        "panel": "3",
        "task": (
            "Build the ANTARCTIC ice-core temperature reconstruction pipeline: ingest the raw "
            "core CSVs, calibrate each core against the 1850 baseline, and emit a yearly anomaly series."
        ),
        "prior_brief": "(none yet — this is its first turn)",
        "output": "<<<DONE_a1b2>>>",
    },
    {
        "label": "one-word confirmation, with a prior brief (carry-forward under terseness)",
        "name": "rank-stream",
        "panel": "5",
        "task": "Refactor the matchup-state ranker into a streaming pipeline.",
        "prior_brief": "Refactoring the matchup-state ranker into a streaming pipeline; extracted the scoring core last turn.",
        "output": "ok",
    },
    {
        "label": "rich multi-line output, first turn",
        "name": "auth",
        "panel": "1",
        "task": "Add OAuth2 (Google) login to the Flask app.",
        "prior_brief": "(none yet — this is its first turn)",
        "output": (
            "Wired the /auth/google route and its callback handler, added the token-exchange helper, "
            "and moved the client secret into the environment. All 7 auth unit tests pass; next is "
            "issuing the session cookie and a logout route."
        ),
    },
    {
        "label": "carry-forward, real incremental progress",
        "name": "billing-grpc",
        "panel": "2",
        "task": "Migrate the billing service from REST to gRPC.",
        "prior_brief": "Migrating billing from REST to gRPC; defined the proto schema and generated the stubs last turn.",
        "output": (
            "Ported the InvoiceService.Create handler to gRPC and added an auth interceptor; the REST "
            "shim still fronts ListInvoices until its handler is ported next."
        ),
    },
]


def brief_for(model: str, fx: dict):
    prompt = secretary._BRIEF_PROMPT.format(  # type: ignore[attr-defined]
        name=fx["name"],
        panel=fx["panel"],
        task=fx["task"][:600],
        prior_brief=fx["prior_brief"],
        output=fx["output"][:1500],
    )
    text, cost = secretary._run_json(prompt, model)  # type: ignore[attr-defined]
    data = secretary._parse_json_block(text) or {}  # type: ignore[attr-defined]
    return (data.get("brief") or "").strip(), data.get("name_suggestion"), cost


if secretary.CLAUDE is None:
    print("FAIL: `claude` is not on PATH — cannot run the live quality gate.")
    raise SystemExit(2)

print(f"== S3 Haiku quality gate: {secretary.HAIKU_MODEL} vs {secretary.SECRETARY_MODEL} on _BRIEF_PROMPT ==")
rows: list[dict] = []
tot_h = tot_s = 0.0
for fx in FIXTURES:
    bh, nh, ch = brief_for(secretary.HAIKU_MODEL, fx)
    bs, ns, cs = brief_for(secretary.SECRETARY_MODEL, fx)
    tot_h += ch
    tot_s += cs
    rows.append({**fx, "haiku": bh, "haiku_name": nh, "sonnet": bs, "sonnet_name": ns, "ch": ch, "cs": cs})
    print(f"\n### {fx['label']} (panel {fx['panel']})")
    print(f"  task   : {fx['task'][:140]}")
    print(f"  output : {fx['output'][:120]!r}")
    print(f"  HAIKU  ({len(bh)}c ${ch:.4f}) name={nh!r}: {bh}")
    print(f"  SONNET ({len(bs)}c ${cs:.4f}) name={ns!r}: {bs}")

lines = [
    "# S3 Haiku quality gate — brief diff (Haiku Pass A vs Sonnet)",
    "",
    f"Same `_BRIEF_PROMPT` run on `{secretary.HAIKU_MODEL}` vs `{secretary.SECRETARY_MODEL}` over "
    f"{len(FIXTURES)} real-board fixtures (`spikes/verify_brief_quality.py`).",
    f"Cost this run: Haiku ${tot_h:.4f}, Sonnet ${tot_s:.4f}.",
    "",
    "**VERDICT:** _<fill in after eyeballing — KEEP HAIKU (quality holds) or REVERT Pass A to Sonnet + why>_",
    "",
]
for r in rows:
    lines += [
        f"## {r['label']} (panel {r['panel']})",
        "",
        f"- **task**: {r['task']}",
        f"- **prior brief**: {r['prior_brief']}",
        f"- **output**: `{r['output']}`",
        f"- **HAIKU** ({len(r['haiku'])} chars, name={r['haiku_name']!r}): {r['haiku']}",
        f"- **SONNET** ({len(r['sonnet'])} chars, name={r['sonnet_name']!r}): {r['sonnet']}",
        "",
    ]
RECORD.write_text("\n".join(lines), encoding="utf-8")
print(f"\nRecord written: {RECORD}")
print(f"Cost this run: Haiku ${tot_h:.4f}  Sonnet ${tot_s:.4f}")
print("\nEYEBALL the briefs above (and the record). If Haiku reads as well as Sonnet, KEEP Haiku.")
print("If Haiku degrades, set Pass A's model to SECRETARY_MODEL in observe() and note why in the record.")
