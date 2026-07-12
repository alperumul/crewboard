"""The SECRETARY (event-driven): cross-session triage + the S0 situation board.

Runs when a session finishes a turn. S3 split the single Sonnet pass into TWO
`claude -p --output-format json` calls per turn: Pass A on HAIKU (boardless) writes
the per-session brief + name suggestion (cheap, high-frequency upkeep), and Pass B
on SONNET does the cross-session judgment — triage rail items, relay suggestions,
and the frame suggestion (the part that genuinely needs the whole board). Both
passes are stateless (no tools, no MCP); each logs its own total_cost_usd row
(Risk-1: measure, don't assume). Sessions never talk directly — the secretary is
the star-topology hub (Goal 4).

The ADVISOR (Opus, on-demand deep reasoning) used to live here too; it moved to
hub/advisor.py (S1) as a persistent, resumable, STREAMING session with real
memory. This module keeps the shared S0 board state both brains rely on:
BRIEF / brief_snapshot / _board_lines.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets as _secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from hub import nodes, state

CLAUDE = shutil.which("claude")
_ROOT = Path(__file__).resolve().parent.parent
_RUN_DIR = _ROOT / "run"
_COST_LOG = _RUN_DIR / "secretary_cost.log"
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

SECRETARY_MODEL = "sonnet"  # S3 Pass B: cross-session judgment (triage + relay + frame)
HAIKU_MODEL = "haiku"  # S3 Pass A: cheap, high-frequency brief + name upkeep (boardless)
_MAX_FEED = 40

# S2 relation taxonomy — a relay now carries one of these types (default FEEDS).
RELATION_TYPES = ("FEEDS", "REVIEWS", "DEPENDS", "CONFLICT")

# S8 per-group auto-relay (OFF by default; built for completeness, flipped on once the
# suggestions prove good). A relay only ever AUTO-fires when ALL of these hold:
#   * INFORMATIONAL type (#15): FEEDS/REVIEWS are read-and-ignore — B can absorb or skip.
#     DEPENDS/CONFLICT are STEERING (they change B's course) -> ALWAYS the manual chip.
#   * NON-AUGMENTED: no S6 human-authored section (augmented relays never auto-fire).
#   * EXPLICIT shared membership (#14): source AND target both in the SAME auto-enabled
#     frame's members[] list — never a tile's visual rect position.
#   * TARGET NOT BUSY NOW (#2): session_id present AND state not in _BUSY_STATES — the
#     EXACT /route predicate (app.py). A busy target falls back to the S7 queue, NEVER an
#     auto-fire on a later completion (that would be a silent stale send).
# Anything failing these falls through to S1's manual ✓-route chip.
_AUTO_RELAY_TYPES = frozenset({"FEEDS", "REVIEWS"})  # #15: informational only (auto); steering always asks
# Mirrors app.BUSY_STATES / the /route predicate (app.py:166-169). Duplicated (not
# imported) because app imports secretary — an `import app` here would be circular.
_BUSY_STATES = frozenset({"running", "awaiting-approval"})

# Set by app.py to the hub-as-executor for an auto-eligible relay (reuses /route's
# verbatim+edge+broadcast contract). Kept as a module hook — like TURN_COMPLETE_HOOK —
# so secretary needs no MANAGER back-ref. Signature: (src, dst, type, reason) -> bool
# (True if delivered; False -> the caller falls back to the S7 queue).
AUTO_RELAY_DISPATCH_HOOK = None

FEED: list[dict[str, Any]] = []
_FEED_LOCK = threading.Lock()
_total_cost = 0.0

# S4: the FREQUENCY-GATE on Pass B (the Sonnet cross-session pass). observe() fires
# every turn — so the cost driver is frequency × model, not board size — and Pass B
# is the expensive leg. We run it only when it can be useful AND not too often:
#   * board mode "quiet"        -> skip (the human's explicit throttle, state.py)
#   * < 2 live sessions         -> skip (relays/frames are meaningless with one panel)
#   * within the debounce window of the last Pass B -> skip (a burst of completions
#     collapses to ONE Sonnet pass; leading-edge — the first turn in the burst runs).
# Pass A (the cheap Haiku brief+name) ALWAYS runs — gates touch Pass B only. The
# board mode is now read live from state.get_board_mode() (S0 hardcoded "full"
# because the mode didn't exist yet) and used for BOTH the gate and the cost-log tag.
_DEBOUNCE_WINDOW_S = 8.0  # collapse completions within this many seconds into one Pass B
_passb_lock = threading.Lock()  # serialises the leading-edge debounce claim across turn threads
_last_passb_ts = float("-inf")  # monotonic time of the last Pass B run (-inf => the first ever runs)

# S0 situation-board: a persisted, distilled per-session brief line — "what this
# session is working toward + what it just did" (<=2 sentences, <=400 chars).
# S3 moved its production into the boardless HAIKU Pass A (it was folded into the
# single Sonnet call before); still read by BOTH brains via _board_lines, replacing
# the 200-char last_message snippet. Persisted into state.json (reuse the locked
# persistence) so it survives a hub restart. Cap raised 350->400 in S3 (it renders
# CSS-clamped, so a larger store is invisible anyway).
BRIEF: dict[str, str] = {}
_BRIEF_LOCK = threading.Lock()
_BRIEF_MAX = 400

# S3 Pass A (HAIKU, boardless): the per-session brief + name suggestion. Cheap,
# high-frequency upkeep — it only ever sees THIS one session (its task, prior brief,
# latest output), never the rest of the board, so it stays as small as possible.
_BRIEF_PROMPT = """You are the SECRETARY's note-taker for a local hub of parallel Claude Code \
sessions. You maintain a one-line brief on ONE session and, when its name no longer fits, suggest a \
better one. You only see this single session — not the rest of the board.

Session "{name}" (panel {panel}) just finished a turn.
What it was launched to work toward (its task): {task}
Its previous brief (what it had already done, before this turn): {prior_brief}
Its latest output this turn:
---
{output}
---

Do TWO things:

1. BRIEF — write a fresh brief line for panel {panel}: at most 2 sentences and at most 400 characters, \
capturing what this session is working toward AND what it just did. Ground "working toward" in its launch \
task even when the latest output is terse (e.g. just a confirmation or a done-token) — the task is the \
source of truth for the goal, the output is only the latest step. Carry the previous brief forward, \
updating it with what changed this turn.

2. NAME — if session "{name}"'s current name is a generic placeholder (e.g. "session 0") or no longer fits \
what panel {panel} is actually doing, propose a short kebab-case label for it (lowercase, <=4 words, e.g. \
"jwt-token-rotation"); otherwise set name_suggestion to null. You only ever name panel {panel} — never \
another session. This is a SUGGESTION the human accepts or ignores; do not assume it is applied.

Respond with ONLY a JSON object, no prose, no code fence:
{{"brief": "<=2 sentences, <=400 chars: what panel {panel} is working toward + what it just did>",
 "name_suggestion": "<short kebab-case label for panel {panel}, or null if the current name already fits>"}}"""


# S3 Pass B (SONNET): the cross-session judgment — triage, relays, and the frame
# suggestion. This is the part that genuinely needs the whole board; the brief + name
# moved to Pass A (Haiku). S5 authored the tailored relay guidance below (relays are
# mostly review-relays passed WITHIN a group; suggest only what another session
# genuinely needs; PREFER SILENCE when unsure) — the frame wording and the JSON shape
# are unchanged. NOTE: verify_relations.py / verify_pos.py assert this constant still
# mentions "type", the 4 RELATION_TYPES, and "frame_suggestion".
_SECRETARY_PROMPT = """You are the SECRETARY of a local hub coordinating several parallel Claude Code \
sessions. You do cross-session triage so the human is not the relay: you decide what the human needs to \
know and whether one session's output should be routed into another. Sessions never talk to each other \
directly — only you connect them.

Session "{name}" (panel {panel}) just finished a turn.
Its latest output this turn:
---
{output}
---

Full board right now:
{board}

Promoted initiatives (groups you TRACK as a unit — reason over each as a whole):
{groups}

Do THREE things:

1. TRIAGE/RELAY — decide what (if anything) the human needs to know, and whether this session's \
just-finished output should be ROUTED VERBATIM into another session that genuinely needs it (so the human \
stops being the copy-paste relay between sessions). For TRIAGE be conservative — only surface genuinely \
useful items; silence is fine. For RELAY: most relays are REVIEWS passed within a group — sessions on the \
same initiative (the kind you would group under a frame below), where one has produced a review, critique, \
or result another must act on. Suggest a relay ONLY when a finished output is something another session \
genuinely needs right now: a review/critique that applies to its work, an output it DEPENDS on, or a \
CONFLICT it must reconcile. When unsure, PREFER SILENCE — the human copies the rare, complex cases by hand, \
and a spurious relay costs more than a missed one. TYPE each relay: FEEDS (one's output feeds another's \
work), REVIEWS (a review/critique of one applies to another), DEPENDS (one depends on or is blocked by \
another), CONFLICT (their work collides). The `reason` is WHY that specific output applies to that specific \
target — not a generic restatement. Only relay between panels that exist on the board above; never invent an \
id and never relay a session to itself.

2. FRAME — if TWO OR MORE sessions on the board are clearly part of the SAME broader initiative or plan \
(e.g. several sessions all building one feature, or all working the same redesign/migration), propose ONE \
group: a short label for that initiative and the list of those panel ids. Judge from their tasks and briefs, \
not their names. Only propose a frame when the shared plan is genuinely clear; otherwise set \
frame_suggestion to null. Group only EXISTING panels from the board above — never invent an id. This is a \
SUGGESTION the human accepts or ignores.

3. GROUP IMAGES — for each PROMOTED initiative listed above (and ONLY those), write a 1-2 \
sentence "group image": what the initiative is and its current state across its member sessions, \
judged from their briefs. This is intra-group reasoning — never mix two groups. If no initiative is \
promoted, return an empty group_images list.

Respond with ONLY a JSON object, no prose, no code fence:
{{"frame_suggestion": {{"label": "<short initiative label>", "panels": ["<id>", "<id>"]}},
 "triage": [{{"severity": "info|attention|urgent", "panel": "<id>", "text": "<short note>"}}],
 "relays": [{{"from": "<id>", "to": "<id>", "type": "FEEDS|REVIEWS|DEPENDS|CONFLICT", "reason": "<why that output applies to that session>"}}],
 "group_images": [{{"frame": "<promoted frame id or label>", "description": "<1-2 sentence group image>"}}]}}
Set frame_suggestion to null when no clear multi-session initiative spans the board."""


def _log_cost(model: str, cost: float, n_live: int = 0, mode: str = "full") -> None:
    """Append ONE row per LLM pass: model, the pass cost, the count of LIVE sessions
    on the board at the time, and the board mode. The n_live + mode tags let S4
    compare per-turn cost like-for-like (same session-count + mode bucket) rather
    than against an aggregate that would hide the frequency-gate's gain (plan §7).
    One row per pass means S3's Haiku+Sonnet split logs two rows automatically.
    Schema: model\\tcost\\tn_live\\tmode."""
    try:
        _RUN_DIR.mkdir(exist_ok=True)
        with open(_COST_LOG, "a", encoding="utf-8") as f:
            f.write(f"{model}\t{cost}\t{n_live}\t{mode}\n")
    except OSError:
        pass


def total_cost() -> float:
    return round(_total_cost, 4)


def cost_samples() -> list[dict[str, Any]]:
    """Read the per-pass cost log back into [{model, cost, n_live, mode}] so S4 can
    compare per-turn cost like-for-like. Tolerates pre-S0 two-column rows
    (model\\tcost) so an existing log isn't a parse error — a row missing the new
    tags reports n_live=None, mode="" (clearly 'unknown'). Rows with an unparseable
    cost are skipped."""
    rows: list[dict[str, Any]] = []
    try:
        with open(_COST_LOG, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return rows
    for ln in lines:
        parts = ln.rstrip("\n").split("\t")
        if len(parts) < 2:
            continue
        try:
            cost = float(parts[1])
        except ValueError:
            continue
        n_live_raw = parts[2] if len(parts) > 2 else ""
        rows.append(
            {
                "model": parts[0],
                "cost": cost,
                "n_live": int(n_live_raw) if n_live_raw.lstrip("-").isdigit() else None,
                "mode": parts[3] if len(parts) > 3 else "",
            }
        )
    return rows


def _run_json(
    prompt: str, model: str, timeout: float = 150.0, n_live: int = 0, mode: str = "full"
) -> tuple[str | None, float]:
    global _total_cost
    if CLAUDE is None:
        return None, 0.0
    cmd = [CLAUDE, "-p", prompt, "--model", model, "--output-format", "json", "--strict-mcp-config"]
    try:
        out = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(_ROOT),
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, 0.0
    try:
        env = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None, 0.0
    cost = float(env.get("total_cost_usd") or 0.0)
    _total_cost += cost
    _log_cost(model, cost, n_live, mode)
    return (env.get("result") or "").strip(), cost


def _parse_json_block(text: str | None) -> dict | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _add_items(items: list[dict[str, Any]]) -> None:
    with _FEED_LOCK:
        existing = {_key(i) for i in FEED}
        for it in items:
            if _key(it) in existing:
                continue
            it["id"] = _secrets.token_hex(4)
            FEED.insert(0, it)
        del FEED[_MAX_FEED:]


def _slug_name(raw: str) -> str:
    """Normalise a proposed session name to a safe kebab slug, <=4 words."""
    return "-".join(re.findall(r"[A-Za-z0-9]+", raw.lower())[:4])


def _hash_text(text: str) -> str:
    """Stable, cross-restart short digest of a source's last_message. Python's
    builtin hash() is salted per-process, which would break the persisted
    suppress-set (S2) — its keys must mean the same thing after a hub restart."""
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:12]


def _key(it: dict[str, Any]) -> str:
    kind = it.get("kind")
    if kind == "relay":
        # S2: TEXT-AWARE key. Was pair-only (relay:{from}->{to}), which is text-blind
        # — a NEW A->B message wrongly collapsed into the prior one (and, once the
        # suppress-set keys off this, suppression would have killed the new message).
        # src_hash is the digest of the source's last_message AT SUGGEST-TIME (carried
        # on the item by observe), so identical content -> one chip, fresh output ->
        # new key -> re-suggests, and an executed/dismissed relay only stays suppressed
        # for THAT content.
        return f"relay:{it.get('from')}->{it.get('to')}:{it.get('src_hash', '')}"
    if kind == "name_suggestion":
        # keyed by the proposed name so a NEW suggestion for the same panel isn't
        # deduped away by a prior one (text is None for this kind).
        return f"name_suggestion:{it.get('panel')}:{it.get('name')}"
    if kind == "frame_suggestion":
        # keyed by the member SET — re-proposing the same group dedupes, but a
        # changed membership (a session joined/left the plan) is a fresh suggestion.
        return f"frame_suggestion:{':'.join(sorted(it.get('panels') or []))}"
    return f"{kind}:{it.get('panel')}:{it.get('text')}"


def feed_snapshot() -> dict[str, Any]:
    with _FEED_LOCK:
        return {"items": list(FEED), "total_cost_usd": total_cost()}


def resurface_pending_relays(target_panel: str) -> list[dict[str, Any]]:
    """S7: when a busy target finishes a turn, RE-SURFACE any relays that were QUEUED
    while it was busy (state.add_pending_relay) as confirm chips — the SAME kind:"relay"
    FEED item the secretary's own suggestions use, so the existing ✓-route accept path
    handles them. NEVER auto-sent: the chip's accept re-resolves the body server-side at
    click time (the verbatim contract), so a now-stale approval can never ride in. Wired
    to the server TURN_COMPLETE_HOOK (a just-completed panel is done/awaiting-input, never
    idle — so a state=='idle' check would never fire), NOT set_state. Popping the queue
    also clears the tile badge. Returns the re-surfaced items (the verify spike asserts on
    them). Bypasses the suppress-set on purpose: a queued relay is an EXPLICIT human intent
    awaiting confirmation, not a fresh secretary guess to be silenced."""
    pending = state.pop_pending_relays(str(target_panel))
    if not pending:
        return []
    items: list[dict[str, Any]] = []
    for rel in pending:
        frm, to = str(rel.get("from")), str(rel.get("to"))
        if not frm or not to or frm == to:
            continue  # mirror observe()'s validity guard — never a self-loop / blank id
        rtype = str(rel.get("type") or "FEEDS").upper()
        if rtype not in RELATION_TYPES:
            rtype = "FEEDS"
        items.append(
            {
                "kind": "relay",
                "from": frm,
                "to": to,
                "type": rtype,
                "reason": rel.get("reason") or "queued while the target was busy",
                # carry the enqueue-time source digest so _key stays text-aware (a fresh
                # source output later is a different key); the body is re-resolved on accept.
                "src_hash": rel.get("src_hash", ""),
            }
        )
    _add_items(items)
    return items


def dismiss(item_id: str) -> None:
    with _FEED_LOCK:
        item = next((i for i in FEED if i.get("id") == item_id), None)
        FEED[:] = [i for i in FEED if i.get("id") != item_id]
    # S2: a dismissed OR executed relay is suppressed PERSISTENTLY by its text-aware
    # key, so it can't re-nag after a hub restart (the FEED removal alone reset on
    # restart). Both flows reach here: the chip's ✓ route hits /route THEN
    # /secretary/dismiss (app.js acceptRelaySuggest); a plain ✕ hits /secretary/dismiss
    # directly. A NEW source output yields a new key (see _key) and re-suggests.
    if item is not None and item.get("kind") == "relay":
        state.add_suppressed_key(_key(item))


# --- per-session brief (S0 situation-board) -----------------------------------
def seed_brief(panel_id: str, line: str) -> None:
    """Restore a persisted brief into memory on hub start (SessionManager._restore)."""
    if not line:
        return
    with _BRIEF_LOCK:
        BRIEF[str(panel_id)] = line[:_BRIEF_MAX]


def clear_brief(panel_id: str) -> None:
    """Drop a panel's brief — called on relaunch so a new task starts blank (HIGH-2)."""
    with _BRIEF_LOCK:
        BRIEF.pop(str(panel_id), None)


def brief_snapshot() -> dict[str, str]:
    with _BRIEF_LOCK:
        return dict(BRIEF)


def _board_lines(board: list[dict[str, Any]]) -> str:
    """One line per session for both brains. Uses the rich brief when present;
    falls back to the 200-char snippet only for never-acted sessions (relaunch
    clears the brief, so the fallback is reachable again after a fresh launch)."""
    briefs = brief_snapshot()
    return "\n".join(
        f'- panel {b["panel_id"]} "{b.get("name", "")}" [{b.get("state", "")}]: '
        + (briefs.get(str(b["panel_id"])) or (b.get("last_message") or "")[:200])
        for b in board
    )


def _promoted_groups_block(promoted: list[dict[str, Any]], board: list[dict[str, Any]], positions: dict[str, Any]) -> str:
    """Slice 6: one block per promoted frame — its id, label, and EFFECTIVE member briefs —
    so Pass B can reason over each initiative as a unit. '(none promoted)' when empty."""
    if not promoted:
        return "(none promoted)"
    briefs = brief_snapshot()
    live = {str(b.get("panel_id")): b for b in board}
    out = []
    for f in promoted:
        members = [pid for pid in live if _effective_member(f, pid, positions)]
        lines = [f'  - panel {pid} "{live[pid].get("name","")}": ' + (briefs.get(pid) or (live[pid].get("last_message") or "")[:120]) for pid in members]
        out.append(f'GROUP "{f.get("label","")}" (id {f.get("id")}):\n' + ("\n".join(lines) if lines else "  (no effective members yet)"))
    return "\n".join(out)


def _count_live_sessions(board: list[dict[str, Any]]) -> int:
    """Number of LIVE sessions on the board — the board's own definition of a
    launched panel, mirroring SessionManager.snapshot()'s filter: a session_id OR a
    non-"empty" state. A never-launched placeholder tile (no session_id, state
    "empty") is NOT live. This is exactly the bucket S4's frequency-gate keys on
    (<2 live -> skip the Sonnet pass), so logging it per pass lets S4's cost
    comparison be like-for-like (plan §7)."""
    return sum(1 for b in board if b.get("session_id") or (b.get("state") not in (None, "", "empty")))


def _should_run_passb(n_live: int, mode: str) -> bool:
    """S4 frequency-gate: decide whether THIS turn runs the Sonnet Pass B. Order is
    cheap-checks-first — mode, then live-count — then the debounce CLAIM last (so a
    quiet/too-few turn never disturbs the debounce clock). The claim is leading-edge
    and atomic under _passb_lock: the first turn past the window stamps the clock and
    runs; concurrent/rapid turns inside the window see the fresh stamp and skip, so a
    burst collapses to one Pass B. Gates Pass B ONLY — Pass A always runs (caller)."""
    if mode == "quiet":
        return False
    if n_live < 2:
        return False
    global _last_passb_ts
    now = time.monotonic()
    with _passb_lock:
        if now - _last_passb_ts < _DEBOUNCE_WINDOW_S:
            return False  # within the window of the last run — debounced (clock untouched)
        _last_passb_ts = now  # claim this turn as the burst's leading edge
        return True


def _effective_member(frame: dict[str, Any], panel_id: str, positions: dict[str, Any]) -> bool:
    """Slice 6 (§7.3): a panel is an EFFECTIVE member iff its pin is inside the frame's
    rect AND its node is NOT excluded. positions = state.get_positions() (panel-keyed);
    excluded[] is node-keyed, so translate via nodes.node_id_for_panel — THIS is the
    panel↔node hop the plan (§9) puts in Slice 6."""
    pos = positions.get(str(panel_id))
    if not pos:
        return False
    r = frame.get("rect") or {}
    try:
        x, y = float(pos.get("x")), float(pos.get("y"))
        rx, ry, rw, rh = float(r.get("x", 0)), float(r.get("y", 0)), float(r.get("w", 0)), float(r.get("h", 0))
    except (TypeError, ValueError):
        return False
    if not (rx <= x <= rx + rw and ry <= y <= ry + rh):
        return False
    nid = nodes.node_id_for_panel(panel_id)
    return nid is not None and nid not in (frame.get("excluded") or [])


def _auto_members_share(auto_frames: list[dict[str, Any]], frm: str, to: str, positions: dict[str, Any]) -> bool:
    """Slice 6: True iff some AUTO-ENABLED frame has BOTH frm and to as EFFECTIVE members
    (positional inside-rect minus excluded). Replaces the S8 explicit members[] read —
    this ENDS the F-IDENT→Slice 6 auto-relay dormant window."""
    frm, to = str(frm), str(to)
    for f in auto_frames:
        if _effective_member(f, frm, positions) and _effective_member(f, to, positions):
            return True
    return False


def _auto_eligible(item: dict[str, Any], auto_frames: list[dict[str, Any]], positions: dict[str, Any]) -> bool:
    """S8 type/augment + Slice 6 effective-membership half of auto-eligibility (the target
    not-busy check is applied at dispatch). INFORMATIONAL type (#15), NON-AUGMENTED (#6),
    and source+target both EFFECTIVE members of one auto-enabled frame."""
    return (
        str(item.get("type")) in _AUTO_RELAY_TYPES
        and not item.get("section")
        and _auto_members_share(auto_frames, item.get("from"), item.get("to"), positions)
    )


def observe(trigger_panel: str, trigger_name: str, trigger_output: str, board: list[dict[str, Any]]):
    """Event-driven secretary pass after a session finishes a turn.

    S3 splits this into TWO `claude -p` passes — SAME external item/persistence
    contract as the old single Sonnet call:
      * Pass A — HAIKU, boardless: the trigger's brief + name suggestion, grounded
        in its launch `task` (read off the board) and its prior brief, so it stays
        meaningful even when the latest output is a bare sentinel.
      * Pass B — SONNET, full board: triage rail items, relay suggestions, and the
        frame suggestion (the cross-session judgment that actually needs the board).
        S4-GATED: skipped on quiet mode / <2 live / a debounced burst — on a skip
        we return just the Pass A items + cost (a measurably cheaper turn).
    Returns (merged items, SUMMED cost); each pass that RUNS logs its own cost row.
    """
    task = next((b.get("task", "") for b in board if str(b.get("panel_id")) == str(trigger_panel)), "")
    prior = brief_snapshot().get(str(trigger_panel), "")
    n_live = _count_live_sessions(board)
    mode = state.get_board_mode()  # S4: drives the Pass B gate AND tags the cost log (was hardcoded "full")

    # --- Pass A (HAIKU, boardless): brief + name -------------------------------
    prompt_a = _BRIEF_PROMPT.format(
        name=trigger_name,
        panel=trigger_panel,
        task=(task or "(not recorded)")[:600],
        prior_brief=prior or "(none yet — this is its first turn)",
        output=(trigger_output or "")[:1500],
    )
    text_a, cost_a = _run_json(prompt_a, HAIKU_MODEL, n_live=n_live, mode=mode)
    data_a = _parse_json_block(text_a) or {}

    items: list[dict[str, Any]] = []
    brief = data_a.get("brief")
    if isinstance(brief, str) and brief.strip():
        line = brief.strip()[:_BRIEF_MAX]
        with _BRIEF_LOCK:
            BRIEF[str(trigger_panel)] = line
        state.update_panel(str(trigger_panel), brief=line)  # MED-2: reuse locked persistence
    # S4: a NAME proposal — surfaced as an acceptable feed item, never auto-applied.
    name_sug = data_a.get("name_suggestion")
    if isinstance(name_sug, str) and name_sug.strip():
        slug = _slug_name(name_sug)
        if slug and slug != trigger_name.strip().lower():
            items.append({"kind": "name_suggestion", "panel": str(trigger_panel), "name": slug})

    # --- S4 frequency-gate: run the expensive Sonnet Pass B only when it's useful
    # and not too often (quiet mode / <2 live / a debounced burst -> skip). Pass A
    # above has already maintained the brief + name; on a skip we surface those and
    # return JUST the Pass A cost — so a gated turn is measurably cheaper than the S0
    # baseline (which ran both passes every turn). Gates touch Pass B only.
    if not _should_run_passb(n_live, mode):
        _add_items(items)
        return items, cost_a

    # --- Pass B (SONNET, full board): triage + relay + frame -------------------
    # Rendered AFTER Pass A wrote the brief, so the board the Sonnet pass reasons
    # over carries the trigger's FRESH brief (an incidental gain; no test depends on it).
    frames_all = state.get_frames()
    positions = state.get_positions()         # Slice 6: effective-membership needs the pins
    promoted = [f for f in frames_all if f.get("promoted")]
    prompt_b = _SECRETARY_PROMPT.format(
        name=trigger_name,
        panel=trigger_panel,
        output=(trigger_output or "")[:1500],
        board=_board_lines(board),
        groups=_promoted_groups_block(promoted, board, positions),
    )
    text_b, cost_b = _run_json(prompt_b, SECRETARY_MODEL, n_live=n_live, mode=mode)
    data_b = _parse_json_block(text_b) or {}
    cost = cost_a + cost_b  # summed across both passes (each already logged its own row)

    for t in data_b.get("triage", []) or []:
        if not t.get("text"):
            continue
        items.append(
            {
                "kind": "triage",
                "severity": t.get("severity", "info"),
                "panel": str(t.get("panel", "")),
                "text": t.get("text", ""),
            }
        )
    live_ids = {str(b.get("panel_id")) for b in board}
    # S5: a FRAME proposal — >=2 sessions clearly on one plan. Surfaced as an
    # acceptable feed item (sibling of name_suggestion); the user accepts and the
    # client computes the rectangle from the members' live positions. Validate
    # against the board so a hallucinated id can't bloat or fake the group.
    frame_sug = data_b.get("frame_suggestion")
    if isinstance(frame_sug, dict):
        flabel = str(frame_sug.get("label") or "").strip()
        fpanels = list(dict.fromkeys(str(p) for p in (frame_sug.get("panels") or []) if str(p) in live_ids))
        if flabel and len(fpanels) >= 2:
            items.append({"kind": "frame_suggestion", "label": flabel[:60], "panels": fpanels})
    # S2: read the persisted suppress-set + a board index ONCE for the relay loop.
    suppressed = set(state.get_suppressed_keys())
    board_by_id = {str(b.get("panel_id")): b for b in board}
    # S8: the AUTO-ENABLED frames (the per-group switch is OFF by default, so this is
    # usually empty -> no relay is auto-eligible and everything stays a manual chip).
    auto_frames = [f for f in frames_all if f.get("auto_relay")]
    for r in data_b.get("relays", []) or []:
        if not (r.get("from") and r.get("to")):
            continue
        frm, to = str(r.get("from")), str(r.get("to"))
        # S1: validity guard now gates the FEED ITEM too (was edge-only). The model
        # can hallucinate a panel id or emit a self-loop; dropping it here means a
        # bogus relay never even surfaces as a clickable chip, not just as a Map edge.
        if frm == to or frm not in live_ids or to not in live_ids:
            continue
        # S2 GUARD: read the SOURCE panel straight off the board (frm is in live_ids,
        # so it exists). `state` is the canonical per-panel status and `last_message`
        # the content — both already on the board, no MANAGER/is_error plumbing. Drop
        # the relay when the source has nothing to send (empty last_message) or errored:
        # routing a blank/failed turn into another session is pure noise (no chip, no edge).
        src = board_by_id.get(frm, {})
        src_msg = src.get("last_message") or ""
        if not src_msg.strip() or src.get("state") == "error":
            continue
        rtype = str(r.get("type") or "FEEDS").upper()
        if rtype not in RELATION_TYPES:
            rtype = "FEEDS"
        reason = r.get("reason", "")
        item = {
            "kind": "relay",
            "from": frm,
            "to": to,
            "type": rtype,
            "reason": reason,
            # S2: digest of the source text AT SUGGEST-TIME so _key is text-aware —
            # both the FEED dedup and the suppress-set key off identical content.
            "src_hash": _hash_text(src_msg),
        }
        # S2: an already executed/dismissed identical relay stays suppressed across
        # restarts — drop it WHOLE (no chip, no edge). A NEW source output gives a new
        # src_hash -> new key, which isn't in the set, so it re-suggests.
        if _key(item) in suppressed:
            continue
        # S8: per-group AUTO-RELAY. Eligible only when ALL hold: INFORMATIONAL type
        # (#15 — FEEDS/REVIEWS; DEPENDS/CONFLICT are steering -> always the manual chip),
        # NON-AUGMENTED (a secretary suggestion carries no human section, but assert it),
        # and source+target BOTH explicit members of the SAME auto-enabled frame (#14 —
        # the members[] list, never the rect). When eligible we either auto-dispatch (a
        # not-busy target) or QUEUE (a busy one), and in BOTH cases SUPPRESS this content
        # so a later completion can't silently re-fire it (#2) — mirroring the human
        # ✓-route -> /route -> dismiss suppression. Everything else falls through to the
        # S1 manual chip below.
        if _auto_eligible(item, auto_frames, positions):
            tgt = board_by_id.get(to, {})
            # (#2) the EXACT /route not-busy predicate: a session_id AND state not busy.
            target_free = bool(tgt.get("session_id")) and tgt.get("state") not in _BUSY_STATES
            if target_free and AUTO_RELAY_DISPATCH_HOOK is not None and AUTO_RELAY_DISPATCH_HOOK(frm, to, rtype, reason):
                # Auto-dispatched (the hook recorded an EXPLICIT edge, like a human /route).
                # Suppress so observe() on a LATER completion can't re-fire the same content.
                suppressed.add(_key(item))
                state.add_suppressed_key(_key(item))
                continue
            if not target_free:
                # Busy target -> S7 queue + badge; it re-surfaces as a MANUAL confirm chip
                # on the target's next completion, NEVER an auto-fire (the silent stale send
                # #2 forbids). Suppress so the observe() that ALSO runs on that completion
                # can't auto-fire the same content in parallel with the re-surface.
                state.add_pending_relay(to, {"from": frm, "to": to, "type": rtype, "reason": reason, "src_hash": item["src_hash"]})
                suppressed.add(_key(item))
                state.add_suppressed_key(_key(item))
                continue
            # else: eligible but the hook is unset / declined while the target was free
            # -> fall through to the manual chip (the safe default).
        items.append(item)
        # Persist as a durable Map edge (S2), carrying the human-readable reason (S1).
        # F-IDENT: relations ride the node_id spine, so translate the panel-keyed
        # endpoints to node_ids for the EDGE only. The FEED item / chip / suppress-key
        # path above stays panel-keyed by design (plan §3.1). A panel with no node yet
        # (shouldn't happen post-launch) -> skip the edge rather than write a panel key.
        nfrm, nto = nodes.node_id_for_panel(frm), nodes.node_id_for_panel(to)
        if nfrm and nto:
            state.add_relation(nfrm, nto, rtype, "inferred", reason)
    # Slice 6: write each promoted group's image as its frame description (intra-group).
    for g in data_b.get("group_images", []) or []:
        desc = str(g.get("description", "")).strip()
        if not desc:
            continue
        fkey = str(g.get("frame", ""))
        match = next((f for f in promoted if str(f.get("id")) == fkey or str(f.get("label")) == fkey), None)
        if match:
            state.set_frame_description(str(match["id"]), desc)
    _add_items(items)
    return items, cost
