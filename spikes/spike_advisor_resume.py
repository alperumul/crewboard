"""De-risk spike (S1 Prerequisites): a resumable streaming Opus session that
tracks a CHANGED board across turns.

Before wrapping the advisor in Flask, prove the raw mechanics against the live
binary:
  (1) RESUME works for a pure `-p ... --model opus` session (no tools / no MCP):
      turn 2 reuses turn 1's session_id.
  (2) MEMORY carries across the resume: turn 2 recalls a codeword given in turn 1.
  (3) The advisor TRACKS a board that CHANGED between turns: turn 2 sees a panel
      that only exists in board #2.
  (4) The stream-json event shapes the Flask reader will rely on actually arrive:
      content_block_start (thinking | text) phases, text_delta deltas, and a
      `result` carrying total_cost_usd + session_id.

If (1)-(3) are flaky, the S1 fallback is to send a compact "(board updated)\n..."
each turn instead of trusting memory for the board -- but note the design already
re-sends the full board every turn, so this spike mostly de-risks resume+stream.

Run with the hub venv from the hub root:
    .venv/Scripts/python.exe spikes/spike_advisor_resume.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parent.parent
RAW_LOG = Path(__file__).resolve().parent / "spike_advisor_resume_raw.jsonl"

CODEWORD = "PELICAN-7793"

PREAMBLE = (
    "You are the ADVISOR for a hub of parallel Claude Code sessions -- the "
    "deep-reasoning brain, in an ongoing chat with the user. Answer in plain "
    "text, conversational and concise. Name specific panels when relevant."
)

BOARD_1 = (
    '- panel 1 "alpha" [running]: refactoring the auth module to use JWT tokens\n'
    '- panel 2 "beta" [done]: wrote unit tests for the payment gateway'
)
# board #2 CHANGED: alpha finished, and a brand-new panel 3 "gamma" appeared.
BOARD_2 = (
    '- panel 1 "alpha" [done]: finished the JWT auth refactor\n'
    '- panel 2 "beta" [done]: wrote unit tests for the payment gateway\n'
    '- panel 3 "gamma" [running]: migrating the SEARCH index to Elasticsearch'
)

MSG_1 = f"Note this codeword for later, I will ask for it again: {CODEWORD}. Which single panel is mid-task right now?"
MSG_2 = (
    "What codeword did I give you earlier? Also: which NEW panel has appeared on "
    "the board since my first message, and what is it working on?"
)


def run_turn(claude: str, prompt: str, resume_sid: str | None, raw_sink: list[str]) -> dict:
    """Stream one `claude -p` turn; return observed facts about the stream."""
    cmd = [claude, "-p"]
    if resume_sid:
        cmd += ["--resume", resume_sid]
    cmd += [
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--strict-mcp-config",
        "--model",
        "opus",
    ]

    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(HUB_ROOT),
        text=True,
        encoding="utf-8",
        bufsize=1,
    )

    stderr_buf: list[str] = []

    def drain_stderr():
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_buf.append(line.rstrip())

    et = threading.Thread(target=drain_stderr, daemon=True)
    et.start()

    session_id: str | None = None
    phases: list[str] = []
    delta_count = 0
    answer_parts: list[str] = []
    result_text = ""
    cost = None
    duration_ms = None

    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.rstrip("\n")
        if not line:
            continue
        raw_sink.append(line)
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = ev.get("session_id")
        if sid and not session_id:
            session_id = sid
        etype = ev.get("type")
        if etype == "stream_event":
            inner = ev.get("event", {})
            itype = inner.get("type")
            if itype == "content_block_start":
                btype = (inner.get("content_block") or {}).get("type")
                if btype == "thinking":
                    phases.append("thinking")
                elif btype == "text":
                    phases.append("answering")
            elif itype == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    txt = delta.get("text", "")
                    if txt:
                        delta_count += 1
                        answer_parts.append(txt)
        elif etype == "result":
            result_text = ev.get("result", "") or ""
            cost = ev.get("total_cost_usd")
            duration_ms = ev.get("duration_ms")

    rc = proc.wait()
    et.join(timeout=1.0)
    elapsed = (time.monotonic() - t0) * 1000
    answer = result_text or "".join(answer_parts)
    return {
        "rc": rc,
        "session_id": session_id,
        "phases": phases,
        "delta_count": delta_count,
        "answer": answer,
        "cost": cost,
        "duration_ms": duration_ms,
        "wall_ms": elapsed,
        "stderr": "\n".join(stderr_buf),
    }


def main() -> int:
    claude = shutil.which("claude")
    if not claude:
        print("FATAL: claude not on PATH", file=sys.stderr)
        return 2
    print(f"claude = {claude}")
    print(f"cwd    = {HUB_ROOT}")

    raw: list[str] = []
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))

    # ---- turn 1 (launch) ----
    print(f"\n== TURN 1 (launch, board #1, codeword {CODEWORD}) ==")
    prompt1 = f"{PREAMBLE}\n\nBoard:\n{BOARD_1}\n\n{MSG_1}"
    t1 = run_turn(claude, prompt1, None, raw)
    print(
        f"  rc={t1['rc']} session_id={t1['session_id']} phases={t1['phases']} "
        f"deltas={t1['delta_count']} cost={t1['cost']} wall={t1['wall_ms']:.0f}ms"
    )
    print(f"  answer: {t1['answer'][:300]!r}")
    if t1["stderr"]:
        print(f"  stderr: {t1['stderr'][-300:]}")

    if not t1["session_id"]:
        check("turn 1 produced a session_id", False, "no session_id -> cannot resume")
        RAW_LOG.write_text("\n".join(raw), encoding="utf-8")
        return 1

    # ---- turn 2 (resume, CHANGED board #2) ----
    print(f"\n== TURN 2 (--resume {t1['session_id']}, board #2 with NEW panel gamma) ==")
    prompt2 = f"Board now:\n{BOARD_2}\n\n{MSG_2}"
    t2 = run_turn(claude, prompt2, t1["session_id"], raw)
    print(
        f"  rc={t2['rc']} session_id={t2['session_id']} phases={t2['phases']} "
        f"deltas={t2['delta_count']} cost={t2['cost']} wall={t2['wall_ms']:.0f}ms"
    )
    print(f"  answer: {t2['answer'][:400]!r}")
    if t2["stderr"]:
        print(f"  stderr: {t2['stderr'][-300:]}")

    RAW_LOG.write_text("\n".join(raw), encoding="utf-8")

    # ---- checks ----
    print("\n== CHECKS ==")
    a2 = (t2["answer"] or "").lower()

    # (1) resume reuses the same session_id
    check(
        "turn 2 reuses turn 1 session_id (resume works)",
        bool(t2["session_id"]) and t2["session_id"] == t1["session_id"],
        f"{t1['session_id']} vs {t2['session_id']}",
    )
    # (2) memory: codeword recalled across the resume
    recalled = "pelican" in a2 and "7793" in a2
    check("turn 2 recalls the turn-1 codeword (memory)", recalled, f"answer has codeword={recalled}")
    # (3) tracks the CHANGED board: gamma / search / elasticsearch only in board #2
    tracks = any(k in a2 for k in ("gamma", "elasticsearch", "search"))
    check("turn 2 references the NEW board panel (board tracking)", tracks)
    # (4) stream shapes the Flask reader needs
    check("answering phase observed (content_block_start type=text)", "answering" in t2["phases"])
    check("at least one text_delta streamed", t2["delta_count"] >= 1, f"deltas={t2['delta_count']}")
    check("result carried a cost", t2["cost"] is not None, f"cost={t2['cost']}")

    # informational: thinking phase (verified-fact: label-only, may be empty)
    saw_thinking = "thinking" in t1["phases"] or "thinking" in t2["phases"]
    print(f"\n  (info) thinking phase seen at least once: {saw_thinking}")
    print(f"  (info) total spike cost: ${(t1['cost'] or 0) + (t2['cost'] or 0):.4f}")
    print(f"  (info) raw events saved to {RAW_LOG}")

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n==================== ADVISOR RESUME SPIKE ====================")
    print(f"{passed}/{total} checks passed")
    all_ok = passed == total
    print(f"DE-RISK: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
