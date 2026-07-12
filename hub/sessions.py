"""Session management: launch / stream / resume headless `claude -p` turns.

A "session" is a durable claude session-id on disk; a "turn" is an ephemeral
subprocess. Each panel keeps a persistent SSE channel (survives between turns)
that subscribers drain. A per-turn reader thread parses stream-json stdout and
broadcasts a simplified hub-event protocol to all subscribers + a replay history.

Hub-event kinds emitted to the browser:
  turn_start                                  a turn began
  user      {text}                            the prompt/reply we sent
  delta     {text}                            live streaming text fragment
  message   {role, text, model}               a finalized assistant message
  tool_use  {name, preview}                   assistant invoked a tool
  state     {state}                            idle | running | awaiting-input | error
  result    {is_error, cost, duration_ms, text}
  system    {subtype, session_id}             init etc. (capture moment)
  error     {text}                             stderr / spawn failure
"""

from __future__ import annotations

import json
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from hub import nodes, secretary, state

CLAUDE = shutil.which("claude")

# Windows: suppress a console window per spawned turn (9 of them in step 6).
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Finalized event kinds worth replaying to a client that connects later.
# `ctx` (S3) rides along so a late/reconnecting client paints the last ctx% gauge.
_HISTORY_KINDS = {"user", "message", "tool_use", "result", "state", "system", "ctx"}

# --- permission wiring (step 5) -----------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_RUN_DIR = _ROOT / "run"
_MCP_SERVER = str(_ROOT / "hub" / "mcp_server.py")
HUB_URL = "http://127.0.0.1:5055"
# AUTO MODE (user choice): bypassPermissions runs every launched session fully
# unattended — NOTHING routes to the 🟡 approve flow, including risky Bash, Write,
# web, and git push. This is a deliberate override of the original step-5 "default"
# mode (which gated those behind mcp__hub__approve). The approve-flow wiring below
# (--permission-prompt-tool / --mcp-config) is left INTACT but now dormant: it never
# fires under bypass, so flipping back to a guarded mode is just changing this one
# constant (e.g. "default" or "acceptEdits"). NB: acceptEdits also auto-approves
# file-writing Bash (verified step 5) — it is NOT a middle ground for "edits only".
PERMISSION_MODE = "bypassPermissions"
ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Edit"]

# Set by app.py to the event-driven secretary trigger; called(session) after each
# successful turn. Kept as a module hook so Session needs no manager back-ref.
TURN_COMPLETE_HOOK = None


# --- S3: per-session context% + global subscription rate-limits ---------------
# Per-session ctx% = the LATEST single request's prompt size (input_tokens +
# cache_creation_input_tokens + cache_read_input_tokens, from the streamed
# `assistant` message's own usage) ÷ the model's contextWindow (from
# result.modelUsage). It MUST be a single request's usage, NOT the `result`
# event's `usage`: that field is CUMULATIVE over every internal tool-loop request
# in the turn, so cache_read re-counts the cached prefix once per request and the
# ratio runs past 100% (observed 375%). A single request's prompt can never exceed
# the window (verified across 1645 real requests: max occupancy 996k ≤ 1M).
# Spike caveat: the model id on the assistant message ("claude-opus-4-8") does NOT
# match the modelUsage KEY ("claude-opus-4-8[1m]"), so the window lookup is exact →
# prefix → single-entry → id-derived fallback (guarded by spikes/verify_usage.py).
_CW_1M = 1_000_000
_CW_DEFAULT = 200_000


def _window_from_id(model: str) -> int | None:
    if not model:
        return None
    return _CW_1M if "[1m]" in model else _CW_DEFAULT


def _pick_model_usage(model: str, model_usage: dict[str, Any]) -> dict[str, Any]:
    """Choose the modelUsage entry for the turn's model, tolerant of the
    assistant-id vs modelUsage-key mismatch ('…-4-8' vs '…-4-8[1m]')."""
    if not model_usage:
        return {}
    if model and model in model_usage:
        return model_usage[model]
    if model:
        for key, md in model_usage.items():
            if key.startswith(model):
                return md
    if len(model_usage) == 1:
        return next(iter(model_usage.values()))
    return {}


# Global subscription quotas — the LATEST rate_limit_event per rateLimitType
# (five_hour / seven_day), one type per event. The headless `-p` stream carries
# only status/resetsAt (NO used_percentage below the warning line), so this is a
# NEAR-LIMIT warning source, not an always-on Session/Weekly gauge.
_RATE_LIMITS: dict[str, dict[str, Any]] = {}
_RATE_LOCK = threading.Lock()
_WARN_STATUSES = {"allowed_warning", "warning", "limited", "rejected"}


def record_rate_limit(info: dict[str, Any]) -> None:
    """Accumulate one rate_limit_info, keyed by its rateLimitType. Previously the
    whole rate_limit_event was discarded (the point of S3 is to stop doing that)."""
    rlt = info.get("rateLimitType")
    if not rlt:
        return
    with _RATE_LOCK:
        _RATE_LIMITS[str(rlt)] = {
            "rate_limit_type": str(rlt),
            "status": info.get("status"),
            "resets_at": info.get("resetsAt"),
            # utilization is absent at 'allowed'; present only near the warning line.
            "utilization": info.get("utilization"),
        }


def _is_warning(t: dict[str, Any]) -> bool:
    if t.get("status") in _WARN_STATUSES:
        return True
    u = t.get("utilization")
    return isinstance(u, (int, float)) and u >= 0.9


def rate_limit_snapshot() -> dict[str, Any]:
    """The global quota readout backing GET /usage. `warning` is true only when a
    rate_limit_event has fired at the near-limit threshold (≈>0.9 utilization)."""
    with _RATE_LOCK:
        types = {k: dict(v) for k, v in _RATE_LIMITS.items()}
    return {
        "rate_limits": types,
        "session": types.get("five_hour"),  # 5-hour window
        "weekly": types.get("seven_day"),  # 7-day window (not emitted headless yet)
        "warning": any(_is_warning(t) for t in types.values()),
    }


def _ensure_mcp_config(panel_id: str) -> str:
    """Write/return this panel's mcp-config pointing at the hub approve bridge."""
    _RUN_DIR.mkdir(exist_ok=True)
    cfg = {
        "mcpServers": {
            "hub": {
                "command": sys.executable,
                "args": [_MCP_SERVER],
                "env": {"HUB_PANEL": panel_id, "HUB_URL": HUB_URL},
            }
        }
    }
    path = _RUN_DIR / f"mcp_{panel_id}.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return str(path)


def new_sentinel() -> str:
    """A collision-resistant per-session task-done token."""
    return f"<<<DONE_{secrets.token_hex(4)}>>>"


def build_launch_prompt(user_prompt: str, sentinel: str) -> str:
    """Wrap a user prompt with the headless-survival + sentinel contract.

    The sentinel is the ONLY signal that earns a green light; its absence keeps
    the panel yellow (needs-you), which is the safe failure direction.
    """
    headless = (
        "You run headless inside a coordination hub - ask any questions as plain "
        "text in your reply; do not use interactive question tools."
    )
    contract = (
        "TASK-DONE CONTRACT: when the WHOLE task is fully complete with nothing "
        f"left to do, make the very last line of your reply exactly: {sentinel}\n"
        "Do NOT emit that line if you still have work pending, or if you are asking "
        "a question / need input - in those cases just reply normally without it."
    )
    return f"{headless}\n\n{contract}\n\n{user_prompt}"


FORK_SUMMARY_PROMPT = (
    "You are about to be FORKED to a fresh session to free up context — your conversation "
    "history will NOT carry over. Write a CONCISE handoff summary for your successor: the "
    "current task state, the key decisions made so far, and the exact next steps. Be complete "
    "but brief — your successor sees ONLY this summary, not our conversation. Do NOT do any new "
    "work; just write the summary."
)


def build_fork_prompt(summary: str) -> str:
    """The new session's launch task: continue the predecessor's work from the handoff."""
    return (
        "You are CONTINUING prior work that was handed off to you to free up context. Below is "
        "the handoff summary from your predecessor session — pick up exactly where it left off.\n\n"
        f"--- HANDOFF SUMMARY ---\n{summary}\n--- END SUMMARY ---"
    )


def _safe_session_name(name: str) -> str:
    """A conservative, --name-safe slug of a hub label, for `claude --name` at
    launch. --name accepts a plain title (verified exit 0), but a label like
    'review: auth-oauth' carries a colon + spaces; slug to [A-Za-z0-9._-] so an
    exotic char can never crash the launch. The full label still lives hub-side."""
    if not name:
        return ""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-._")[:60]


# --- S4: lazy .jsonl re-hydrate -----------------------------------------------
# A hub restart rebuilds Session objects from state.json but NOT their in-memory
# thread history (turns are ephemeral; _history isn't persisted). Durable
# scrollback lives on disk in ~/.claude/projects/<encoded-cwd>/<session_id>.jsonl.
# On first focus after a restart we lazily read that file's tail and rebuild
# user/assistant events so the thread isn't blank. Isolated + format-tolerant:
# the .jsonl shape is external (confirmed by the S4 spike) — never let it throw.
PROJECTS_DIR = Path.home() / ".claude" / "projects"
_REHYDRATE_TAIL = 200  # cap the replay; older turns rarely matter on re-focus


def _session_jsonl_path(session_id: str) -> Path | None:
    """Locate a session's transcript by id, independent of the cwd-encoding scheme
    (glob the unique <session_id>.jsonl filename under every project dir)."""
    if not session_id:
        return None
    try:
        matches = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    return matches[0] if matches else None


def _text_from_content(content: Any) -> str:
    """Pull prose out of a message.content (a plain string OR a block list);
    tool_use / tool_result blocks contribute no text (skipped upstream)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        return "\n".join(parts).strip()
    return ""


def rehydrate_history(session_id: str) -> list[dict[str, Any]]:
    """Read the tail of a durable session's .jsonl and rebuild replayable hub
    events (user / assistant message text). Returns [] for an unknown id or an
    unreadable/garbled file — re-hydrate is best-effort, never fatal."""
    path = _session_jsonl_path(session_id)
    if path is None:
        return []
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in raw[-_REHYDRATE_TAIL:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue
        msg = rec.get("message") or {}
        text = _text_from_content(msg.get("content"))
        if not text:
            continue  # tool_result-only user lines / tool_use-only assistant lines
        if rec["type"] == "user":
            events.append({"kind": "user", "text": text})
        else:
            events.append({"kind": "message", "role": "assistant", "text": text, "model": msg.get("model", "")})
    return events


class Session:
    def __init__(self, panel_id: str, name: str, cwd: str):
        self.panel_id = panel_id
        self.name = name
        self.cwd = cwd
        self.session_id: str | None = None
        self.node_id: str | None = None  # F-IDENT: canvas identity (mirror of panels[pid].node_id, synced at mint/restore)
        # empty | idle | running | awaiting-input | done | error
        self.state: str = "empty"
        self.task: str = ""  # the raw launch prompt — grounds the secretary's brief (S0)
        self.sentinel: str | None = None
        self.proc: subprocess.Popen | None = None
        self.last_message: str = ""
        self.model: str = ""  # last assistant model id — the ctx-window lookup hint (S3)
        self.launch_model: str = ""  # user-chosen --model for THIS launch ("" = CLI/account default)
        self.ctx_pct: float | None = None  # context-window fill 0..1 (S3)
        self.ctx_tokens: int | None = None
        self._last_usage: dict[str, Any] | None = None  # latest assistant msg's per-request usage (S3 ctx source)
        self.ctx_compact_pct: float | None = None  # auto-compact line as a fraction
        self._subs: list[queue.Queue] = []
        self._history: list[dict[str, Any]] = []
        self._rehydrated = False  # S4: disk scrollback lazily restored on first focus
        self._pending_fork = False  # Slice 7: capture should repoint (not bind) on a fork
        self._lock = threading.Lock()

    # --- subscriber fan-out -------------------------------------------------
    def subscribe(self) -> tuple[queue.Queue, list[dict[str, Any]]]:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
            replay = list(self._history)
        return q, replay

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def ensure_rehydrated(self) -> None:
        """S4: lazily restore on-disk scrollback into _history once, on first focus
        after a hub restart (_history isn't persisted, but the .jsonl is). Idempotent;
        a no-op for a panel with live history already or no session yet. File IO runs
        outside the lock so it never stalls a broadcast.

        Also restores the FULL verbatim last_message (relayfix MED-1): state.json only
        persists last_message[:500] (a bounded board snippet), so after a restart the
        in-memory copy is truncated — and the verbatim relay (/route, auto-dispatch)
        would then deliver a 500-char clip with no marker. The transcript is the
        untruncated source of truth, so when we read it here we lift the full last
        assistant message back into last_message (cleaned of the done-sentinel so it
        can never ride into a relay). Done in the SAME single transcript read as the
        scrollback restore, on whichever path (focus or relay) reaches it first."""
        if self._rehydrated or not self.session_id:
            return
        with self._lock:
            # Live history this process implies a fresh, full last_message already —
            # nothing to restore (and no truncation to undo). Just mark done.
            if self._rehydrated or self._history:
                self._rehydrated = True
                return
        events = rehydrate_history(self.session_id)
        with self._lock:
            if not self._history:
                self._history = events
            # Lift the full last assistant message back over the [:500] restored copy.
            # _clean strips a trailing sentinel (restored from state.json) so a relay
            # never carries it; the length guard never DOWNGRADES a fresh in-memory msg.
            last = next((e.get("text", "") for e in reversed(events) if e.get("kind") == "message"), "")
            last = self._clean(last)
            if last and len(last) > len(self.last_message):
                self.last_message = last
            self._rehydrated = True

    def broadcast(self, ev: dict[str, Any]) -> None:
        with self._lock:
            if ev.get("kind") in _HISTORY_KINDS:
                self._history.append(ev)
            subs = list(self._subs)
        for q in subs:
            q.put(ev)

    def set_state(self, st: str) -> None:
        self.state = st
        self.broadcast({"kind": "state", "state": st})

    def reset_for_launch(self, name: str, cwd: str) -> None:
        """Re-arm this persistent panel for a fresh launch, keeping subscribers.

        Open SSE connections survive (channel is per-panel); they receive a
        `cleared` event so the browser wipes the old thread.
        """
        # A relaunched panel is a NEW task; its old brief must not linger, or
        # _board_lines' brief-or-snippet fallback stays suppressed by a stale
        # truthy line (S0 HIGH-2). Clear it from memory and from state.json.
        secretary.clear_brief(self.panel_id)
        state.update_panel(self.panel_id, brief=None)
        # F-IDENT: THE single node mint (mint_node_for_panel is called ONCE per launch,
        # here — MANAGER.launch always calls reset_for_launch, so there is no second mint
        # path). It drop_node's the OLD node's SEMANTIC traces (edges/membership/notes —
        # replacing the old clear_relations_for + clear_frame_membership_for, now
        # node-keyed) — a no-op on first launch — then mints + binds a NEW node. The
        # position pin is panel-keyed and deliberately survives (state.py:266).
        self.node_id = nodes.mint_node_for_panel(self.panel_id, name)
        with self._lock:
            self.name = name
            self.cwd = cwd
            self.session_id = None
            self.last_message = ""
            self._history = []
            subs = list(self._subs)
        for q in subs:
            q.put({"kind": "cleared"})

    def _reset_for_fork(self) -> None:
        """Slice 7: reset the OPERATIONAL session fields for a fork — WITHOUT touching the
        node (preserve its node_id + all edges/frames/notes). Unlike reset_for_launch this
        does NOT mint a new node / drop_node the semantics; it only re-arms the panel so the
        new session captures a fresh sid. Sets _pending_fork so the capture path repoints."""
        with self._lock:
            self.session_id = None
            self.last_message = ""
            self._history = []
            self._rehydrated = True  # a brand-new session has no disk scrollback to restore
            subs = list(self._subs)
        self.sentinel = new_sentinel()
        self._pending_fork = True
        state.update_panel(self.panel_id, session_id=None, sentinel=self.sentinel)
        for q in subs:
            q.put({"kind": "cleared"})

    def _fork_handoff(self) -> None:
        """Slice 7 (runs in a thread): (1) summary turn on the OLD session, (2) reset the
        operational fields (preserve the node), (3) spawn the NEW session seeded with the
        summary. repoint(node_id, new_sid) then runs in the CAPTURE path (1b)."""
        if not self.session_id:
            self.broadcast({"kind": "error", "text": "fork: no session to fork"})
            return
        # (1) summary turn (a --resume reply); run_turn fills self.last_message with the result
        self.run_turn(FORK_SUMMARY_PROMPT, resume=True, display="↪ writing a handoff summary before forking…")
        summary = (self.last_message or "").strip()
        if not summary:
            self.broadcast({"kind": "error", "text": "fork: empty handoff summary — not forking"})
            return
        # (2) reset operational fields, preserve the node
        self._reset_for_fork()
        # (3) spawn the NEW session in the SAME panel, seeded with the summary
        full = build_launch_prompt(build_fork_prompt(summary), self.sentinel)
        self.run_turn(full, resume=False, display="↪ forked — continuing from the handoff summary")

    def _build_cmd(self, prompt: str, *, resume: bool) -> list[str]:
        """Assemble the `claude -p` argv for one turn — isolated + pure so it's
        unit-testable. On LAUNCH (not resume) it titles the real session via
        --name (a --name-safe slug of the hub label): the ONLY way to set the
        real-session name now that headless /rename is unavailable. The hub
        name-map (s.name) stays the in-hub source of truth, so --name is
        best-effort external findability — slugged so an exotic label (a colon
        from an 'review: x' action name, spaces) can't crash the launch."""
        cmd: list[str] = [CLAUDE, "-p"]
        if resume:
            cmd += ["--resume", self.session_id or ""]
        else:
            safe = _safe_session_name(self.name)
            if safe:
                cmd += ["--name", safe]
            # --model is launch-only: --resume re-uses the session's recorded model,
            # so we never re-pass it (which would override mid-conversation). "" = CLI default.
            if self.launch_model:
                cmd += ["--model", self.launch_model]
        cmd += [
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            PERMISSION_MODE,
            "--allowedTools",
            *ALLOWED_TOOLS,
        ]
        # The 🟡 approve-flow wiring is GUARDED-MODE ONLY. Under bypassPermissions
        # (auto mode) nothing is ever gated, so the approve tool is dead weight — and
        # `claude` rejects --permission-prompt-tool alongside a full bypass on some
        # versions. Omitting it here also skips spawning a useless mcp_server.py per
        # session. Flip PERMISSION_MODE back to "default"/"acceptEdits" to re-arm it.
        if PERMISSION_MODE != "bypassPermissions":
            cmd += [
                "--permission-prompt-tool",
                "mcp__hub__approve",
                "--mcp-config",
                _ensure_mcp_config(self.panel_id),
                "--strict-mcp-config",
            ]
        return cmd

    # --- turn execution -----------------------------------------------------
    def run_turn(
        self, prompt: str, *, resume: bool, display: str | None = None, on_done: Callable | None = None
    ) -> None:
        """Spawn a turn and stream it. Blocking — call in a thread.

        `display` is what the thread shows as the user message (the original
        prompt); `prompt` is what claude actually receives (wrapped with the
        headless + sentinel contract on launch). They differ only on launch.
        """
        if CLAUDE is None:
            self.broadcast({"kind": "error", "text": "claude not on PATH"})
            self.set_state("error")
            return

        if resume and not self.session_id:
            self.broadcast({"kind": "error", "text": "resume requested but no session_id yet"})
            self.set_state("error")
            return

        cmd = self._build_cmd(prompt, resume=resume)

        self._last_usage = None  # S3: this turn's ctx fill comes from THIS turn's messages
        self.broadcast({"kind": "turn_start"})
        self.broadcast({"kind": "user", "text": display or prompt})
        self.set_state("running")

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as e:
            self.broadcast({"kind": "error", "text": f"spawn failed: {e}"})
            self.set_state("error")
            return

        self.proc = proc
        stderr_buf: list[str] = []

        def drain_stderr():
            for line in proc.stderr:  # type: ignore[union-attr]
                stderr_buf.append(line.rstrip())

        et = threading.Thread(target=drain_stderr, daemon=True)
        et.start()

        result_payload: dict[str, Any] | None = None
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = self._handle_event(ev)
            if payload is not None:
                result_payload = payload

        rc = proc.wait()
        et.join(timeout=1.0)
        self.proc = None

        if rc != 0 and result_payload is None:
            err = "\n".join(stderr_buf)[-1000:] or f"exit code {rc}"
            self.broadcast({"kind": "error", "text": err})
            self.set_state("error")
        else:
            # The sentinel is the ONLY thing that earns a green light. Its
            # absence keeps the panel yellow (it answered / asked / needs you),
            # which is the safe failure direction per the status model.
            final_text = (result_payload or {}).get("text") or self.last_message or ""
            self.set_state("done" if self._has_sentinel(final_text) else "awaiting-input")

        if on_done:
            on_done(self, result_payload, rc)
        if TURN_COMPLETE_HOOK is not None and rc == 0:
            try:
                TURN_COMPLETE_HOOK(self)
            except Exception:  # noqa: BLE001 — the secretary must never break a turn
                pass

    def _has_sentinel(self, text: str) -> bool:
        if not self.sentinel:
            return False
        # exact-final-line; the token is distinctive enough that endswith on the
        # rstripped text is collision-safe and tolerant of a trailing newline.
        return text.rstrip().endswith(self.sentinel)

    def _clean(self, text: str) -> str:
        """Strip a trailing sentinel line so it never shows in the thread."""
        if self.sentinel and text.rstrip().endswith(self.sentinel):
            return text.rstrip()[: -len(self.sentinel)].rstrip()
        return text

    def _capture_ctx(self, ev: dict[str, Any]) -> None:
        """S3: compute + broadcast per-session context-window fill. Tokens come from
        the LATEST single request's usage (`self._last_usage`, captured off the
        streamed assistant message) — input + cache_creation + cache_read = the
        prompt that was sent = current window occupancy. The result event is used
        ONLY for the model's contextWindow (its own `usage` is cumulative over the
        whole turn and must NOT be summed — that is the 375% bug)."""
        usage = self._last_usage or {}
        tokens = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
        )
        if tokens <= 0:
            return
        md = _pick_model_usage(self.model, ev.get("modelUsage") or {})
        window = md.get("contextWindow") or _window_from_id(self.model)
        if not window:
            return
        # the auto-compact line: Claude Code reserves the next reply's output plus
        # a safety buffer; the gauge reddens toward this fraction, not 100%.
        max_out = md.get("maxOutputTokens") or 64_000
        compact = window - max(max_out, 20_000) - 13_000
        pct = min(tokens / window, 1.0)  # a single prompt can't exceed the window — clamp as a display guard
        compact_pct = compact / window if compact > 0 else 1.0
        self.ctx_tokens = tokens
        self.ctx_pct = pct
        self.ctx_compact_pct = compact_pct
        state.update_panel(self.panel_id, ctx_pct=pct, ctx_tokens=tokens)
        self.broadcast({"kind": "ctx", "pct": pct, "tokens": tokens, "window": window, "compact_pct": compact_pct})

    def _handle_event(self, ev: dict[str, Any]) -> dict[str, Any] | None:
        """Translate one claude stream-json event into hub broadcasts.

        Returns the result payload dict when the turn's `result` event arrives.
        """
        etype = ev.get("type")
        sid = ev.get("session_id")
        if sid and not self.session_id:
            self.session_id = sid
            state.update_panel(self.panel_id, session_id=sid, cwd=self.cwd, name=self.name)
            if self._pending_fork and self.node_id:
                # Slice 7: a FORK — preserve the node, push the retired session to
                # prev_session_ids, point the node at the new session. NOT bind_session
                # (which would just overwrite current_session_id, losing the handoff lineage).
                nodes.repoint(self.node_id, sid)
                self._pending_fork = False
            elif self.node_id:  # F-IDENT: normal launch — bind the launch-minted node
                nodes.bind_session(self.node_id, sid)
            self.broadcast({"kind": "system", "subtype": "session_captured", "session_id": sid})

        if etype == "stream_event":
            inner = ev.get("event", {})
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        self.broadcast({"kind": "delta", "text": text})
            return None

        if etype == "assistant":
            msg = ev.get("message", {})
            model = msg.get("model", "")
            if model:
                self.model = model  # hint for the result event's modelUsage lookup (S3)
            # S3: stash THIS request's usage (input+cache side = current context fill).
            # The latest one wins — within a turn the context grows request by request,
            # so the last assistant message reflects the most-grown occupancy.
            u = msg.get("usage")
            if u and ((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                      + (u.get("cache_creation_input_tokens") or 0)) > 0:
                self._last_usage = u
            for block in msg.get("content", []):
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    clean = self._clean(block["text"])
                    self.last_message = clean
                    self.broadcast({"kind": "message", "role": "assistant", "text": clean, "model": model})
                elif btype == "tool_use":
                    preview = json.dumps(block.get("input", {}))[:160]
                    self.broadcast({"kind": "tool_use", "name": block.get("name", "?"), "preview": preview})
            return None

        if etype == "result":
            self._capture_ctx(ev)  # S3: per-session context-window fill
            raw = ev.get("result", "")  # raw (may carry sentinel) for done-detection
            payload = {
                "kind": "result",
                "is_error": bool(ev.get("is_error")),
                "cost": ev.get("total_cost_usd"),
                "duration_ms": ev.get("duration_ms"),
                "text": raw,
            }
            if raw:
                clean = self._clean(raw)
                self.last_message = clean
                state.update_panel(self.panel_id, last_message=clean[:500])
            self.broadcast({**payload, "text": self._clean(raw)})  # display: no sentinel
            return payload  # detection: raw

        if etype == "rate_limit_event":
            # S3: stop discarding — accumulate the global subscription quota.
            record_rate_limit(ev.get("rate_limit_info") or {})
            return None

        return None  # system / hook_* / system status -> ignore


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        state.migrate_to_node_ids()  # F-IDENT: one-time panel_id->node_id upgrade (idempotent)
        self._restore()

    def _restore(self) -> None:
        data = state.load()
        for panel_id, rec in data.get("panels", {}).items():
            s = Session(panel_id, rec.get("name", f"session {panel_id}"), rec.get("cwd", ""))
            s.session_id = rec.get("session_id")
            s.node_id = rec.get("node_id")  # F-IDENT: restore the canvas identity mirror
            s.last_message = rec.get("last_message", "")
            s.ctx_pct = rec.get("ctx_pct")  # S3: last context-window fill survives restart
            s.ctx_tokens = rec.get("ctx_tokens")
            s.sentinel = rec.get("sentinel")
            s.state = "idle" if s.session_id else "empty"
            secretary.seed_brief(panel_id, rec.get("brief") or "")  # S0: restore brief -> survives restart
            self._sessions[panel_id] = s

    def get(self, panel_id: str) -> Session | None:
        return self._sessions.get(panel_id)

    def ensure(self, panel_id: str) -> Session:
        """Get the panel's persistent Session, creating an empty one if needed.

        The SSE channel binds here so it survives across launches/turns.
        """
        with self._lock:
            s = self._sessions.get(panel_id)
            if s is None:
                s = Session(panel_id, f"session {panel_id}", "")
                self._sessions[panel_id] = s
            return s

    def launch(self, panel_id: str, name: str, cwd: str, prompt: str, model: str = "") -> Session:
        s = self.ensure(panel_id)
        s.reset_for_launch(name, cwd)
        s.launch_model = model  # "" = CLI/account default; else passed as --model on the launch turn
        s.task = prompt  # the raw launch prompt grounds the brief (was discarded before — passed only as display)
        s.sentinel = new_sentinel()
        state.update_panel(panel_id, name=name, cwd=cwd, session_id=None, sentinel=s.sentinel)
        # A slash-command / skill launch (e.g. "/fbi-design") must keep the "/" at
        # the very start, or Claude Code treats it as plain text. Don't wrap those
        # with the headless+sentinel preamble (it would push the slash off the front).
        full = prompt if prompt.lstrip().startswith("/") else build_launch_prompt(prompt, s.sentinel)
        t = threading.Thread(target=s.run_turn, args=(full,), kwargs={"resume": False, "display": prompt}, daemon=True)
        t.start()
        return s

    def delete(self, panel_id: str) -> bool:
        """Hard-delete a session/panel from the board. Kills any live turn, tells
        open SSE clients to tear down, drops the node's semantic traces + record,
        clears the brief, and removes the durable panel record + position pin so
        the slot id frees up. Does NOT touch the on-disk claude `.jsonl` (the hub
        doesn't own it, and --resume is cwd-scoped — that's the user's history)."""
        s = self._sessions.get(panel_id)
        if s is not None:
            if s.proc and s.proc.poll() is None:
                try:
                    s.proc.terminate()
                except Exception:
                    pass
            for q in list(s._subs):
                q.put({"kind": "deleted"})  # browser closes the stream + drops the tile
        # durable teardown — order mirrors mint_node_for_panel's recycle path
        nid = nodes.node_id_for_panel(panel_id)
        if nid:
            state.drop_node(nid)          # relations / frame-excluded / note anchors
            nodes.drop_node_record(nid)   # the node record itself
        secretary.clear_brief(panel_id)
        state.update_panel(panel_id, brief=None)
        removed = state.remove_panel(panel_id)  # panel record + position pin
        self._sessions.pop(panel_id, None)
        return removed or s is not None

    def fork_with_handoff(self, panel_id: str) -> Session | None:
        """Slice 7: fork the panel's near-full session — summarize, retire it, and continue
        the work in a FRESH session on the SAME tile/node/edges. Returns the Session, or None
        if there's nothing to fork. Runs off-thread (the summary turn blocks)."""
        s = self._sessions.get(panel_id)
        if s is None or not s.session_id or not s.node_id:
            return None
        if s.state in ("running", "awaiting-approval"):
            return None  # don't fork mid-turn
        threading.Thread(target=s._fork_handoff, daemon=True).start()
        return s

    def reply(self, panel_id: str, text: str) -> Session | None:
        s = self._sessions.get(panel_id)
        if s is None or not s.session_id:
            return None
        t = threading.Thread(target=s.run_turn, args=(text,), kwargs={"resume": True}, daemon=True)
        t.start()
        return s

    def snapshot(self) -> list[dict[str, Any]]:
        # Only panels the user actually launched count as sessions. SSE-channel
        # placeholders (state "empty", no session_id) are invisible until launch,
        # so nothing is ever auto-created on the board.
        return [
            {
                "panel_id": s.panel_id,
                "name": s.name,
                "cwd": s.cwd,
                "session_id": s.session_id,
                "state": s.state,
                "task": s.task,
                "last_message": s.last_message[:200],
                "node_id": s.node_id,  # F-IDENT: lets the Map resolve node-keyed edges -> panel-keyed positions
                "ctx_pct": s.ctx_pct,  # S3: per-session context-window fill (0..1)
                "ctx_compact_pct": s.ctx_compact_pct,
            }
            for s in self._sessions.values()
            if s.session_id is not None or s.state != "empty"
        ]

    def terminate_all(self) -> None:
        for s in self._sessions.values():
            if s.proc and s.proc.poll() is None:
                try:
                    s.proc.terminate()
                except OSError:
                    pass


MANAGER = SessionManager()
