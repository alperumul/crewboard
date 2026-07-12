"""The ADVISOR: a persistent, resumable, STREAMING Opus session (S1).

Replaces secretary.advise()'s stateless one-shot (which faked memory by
re-injecting the last few turns into a template). A single durable claude
session-id gives the advisor REAL memory across turns; each turn streams live
(thinking -> answering -> deltas -> done) onto a per-advisor SSE channel that
subscribers (the terminal UI, S3) drain. The full board is re-sent every turn
so the advisor always reasons over current state even as panels change (the
de-risk spike confirmed resume carries memory AND tracks a changed board).

Resume is cwd-scoped, so the advisor pins itself to the hub root for every turn.
It runs pure-reasoning: no tools, no MCP (--strict-mcp-config, no --mcp-config).

Hub-events broadcast to subscribers:
  phase  {phase: "thinking"|"answering"}   block-level progress label
  delta  {text}                            live streaming answer fragment
  done   {cost, tokens, duration_ms}       turn finished (per-turn cost, L2)
  error  {text}                            spawn / stream failure
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

CLAUDE = shutil.which("claude")
_ROOT = Path(__file__).resolve().parent.parent  # hub root; resume is cwd-scoped
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

ADVISOR_MODEL = "opus"
_TRANSCRIPT_MAX = 40  # keep the last N entries (you/advisor turns)

# Sent ONCE, on the first turn of a session; resume turns ride the session's
# real memory instead. Mirrors the old _ADVISOR_PROMPT role framing (S0/S1).
_PREAMBLE = (
    "You are the ADVISOR for a hub of parallel Claude Code sessions -- the "
    "deep-reasoning brain, in an ongoing chat with the user. Give sharp, honest, "
    "cross-session answers. Name specific panels. If one session's output should "
    "be routed into another, say so. Reply in plain text, conversational but "
    "concise. No JSON, no code fences."
)

_DEFAULT_QUESTION = "What across these sessions needs my attention, and what should route where?"


class AdvisorSession:
    """Module-singleton persistent advisor: one claude session-id, many turns."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.cwd: str = str(_ROOT)
        self.transcript: list[dict[str, str]] = []  # [{role: "you"|"advisor", text}]
        self.busy: bool = False
        self.proc: subprocess.Popen | None = None
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    # --- subscriber fan-out -------------------------------------------------
    # Live-only: NO history replay here (M2). /advisor/chat owns history via
    # transcript_snapshot(); replaying in both would double-render the thread.
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def broadcast(self, ev: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            q.put(ev)

    # --- one streaming turn (blocking; call in a thread) --------------------
    def send(self, message: str, board_lines: str) -> None:
        """Run one advisor turn and stream it. Call in a daemon thread.

        [M1 busy-interlock] one turn at a time. The /advisor/send route already
        409s on busy; this lock-guarded check-and-set is the atomic backstop for
        the check-then-spawn race, and the finally guarantees the flag is freed.
        """
        with self._lock:
            if self.busy:
                return
            self.busy = True
        try:
            self._run_turn(message, board_lines)
        finally:
            with self._lock:
                self.busy = False
                self.proc = None

    def _run_turn(self, message: str, board_lines: str) -> None:
        if CLAUDE is None:
            self.broadcast({"kind": "error", "text": "claude not on PATH"})
            return

        message = (message or "").strip() or _DEFAULT_QUESTION
        resume = bool(self.session_id)
        if resume:
            prompt = f"Board now:\n{board_lines}\n\n{message}"
        else:
            prompt = f"{_PREAMBLE}\n\nBoard:\n{board_lines}\n\n{message}"

        cmd: list[str] = [CLAUDE, "-p"]
        if resume:
            cmd += ["--resume", self.session_id]  # type: ignore[list-item]
        cmd += [
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--strict-mcp-config",  # pure reasoning: ignore any project/global MCP
            "--model",
            ADVISOR_MODEL,
        ]

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
            return
        with self._lock:
            self.proc = proc

        stderr_buf: list[str] = []

        def drain_stderr():
            for line in proc.stderr:  # type: ignore[union-attr]
                stderr_buf.append(line.rstrip())

        et = threading.Thread(target=drain_stderr, daemon=True)
        et.start()

        answer_parts: list[str] = []
        result_text = ""
        done_payload: dict[str, Any] | None = None

        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            sid = ev.get("session_id")
            if sid and not self.session_id:
                self.session_id = sid  # capture on the launch turn so turn 2 resumes

            etype = ev.get("type")
            if etype == "stream_event":
                inner = ev.get("event", {})
                itype = inner.get("type")
                if itype == "content_block_start":
                    btype = (inner.get("content_block") or {}).get("type")
                    if btype == "thinking":
                        self.broadcast({"kind": "phase", "phase": "thinking"})
                    elif btype == "text":
                        self.broadcast({"kind": "phase", "phase": "answering"})
                elif itype == "content_block_delta":
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta":
                        txt = delta.get("text", "")
                        if txt:
                            answer_parts.append(txt)
                            self.broadcast({"kind": "delta", "text": txt})
                # thinking_delta (empty, verified) and other inner types: ignore
            elif etype == "result":
                result_text = ev.get("result", "") or ""
                done_payload = {
                    "kind": "done",
                    "cost": ev.get("total_cost_usd"),
                    "tokens": (ev.get("usage") or {}).get("output_tokens"),
                    "duration_ms": ev.get("duration_ms"),
                }
            # system / hook_* / rate_limit_event: ignore

        rc = proc.wait()
        et.join(timeout=1.0)

        if rc != 0 and done_payload is None:
            # Covers spawn-exit and a clear()-triggered terminate mid-turn: don't
            # commit a partial answer to the transcript on a hard failure.
            err = "\n".join(stderr_buf)[-1000:] or f"exit code {rc}"
            self.broadcast({"kind": "error", "text": err})
            return

        answer = (result_text or "".join(answer_parts)).strip() or "(no answer)"
        with self._lock:
            self.transcript.append({"role": "you", "text": message})
            self.transcript.append({"role": "advisor", "text": answer})
            del self.transcript[:-_TRANSCRIPT_MAX]
        # session_id rides the done event so a client can confirm both turns
        # share one session (real memory) and the UI (S3) can surface it.
        done = done_payload or {"kind": "done", "cost": None, "tokens": None, "duration_ms": None}
        self.broadcast({**done, "session_id": self.session_id})

    # --- history + lifecycle ------------------------------------------------
    def transcript_snapshot(self) -> list[dict[str, str]]:
        with self._lock:
            return list(self.transcript)

    def clear(self) -> None:
        """Reset to a fresh session. [L1] kill any in-flight turn first so its
        subprocess can't keep streaming into a thread we've abandoned."""
        with self._lock:
            p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
        with self._lock:
            self.session_id = None
            self.transcript = []
            self.busy = False
            self.proc = None


ADVISOR = AdvisorSession()
