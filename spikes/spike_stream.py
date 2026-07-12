"""Spike: observe real `claude -p` stream-json output shapes.

Verifies the riskiest integration (tracer step 2) against the live binary
BEFORE building the Flask streaming layer on top of assumed event shapes.

Run:  D:/Downloads/claude-hub/.venv/Scripts/python.exe D:/Downloads/claude-hub/spikes/spike_stream.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

CWD = str(Path(__file__).resolve().parent.parent)  # claude-hub dir
RAW_LOG = Path(__file__).resolve().parent / "spike_stream_raw.jsonl"
PROMPT = "Reply with exactly these three words and nothing else: hello from spike"


def main() -> int:
    claude = shutil.which("claude")
    if not claude:
        print("FATAL: claude not on PATH", file=sys.stderr)
        return 2
    print(f"claude = {claude}")
    print(f"cwd    = {CWD}")

    cmd = [
        claude,
        "-p",
        PROMPT,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--allowedTools",
        "",  # no tools needed for a hello; keeps the turn fast/cheap
    ]
    print("launching:", " ".join(repr(c) for c in cmd))

    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,  # Windows-native detach; NOT POSIX < /dev/null
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=CWD,
        text=True,
        encoding="utf-8",
        bufsize=1,  # line-buffered
    )

    session_id = None
    event_types: list[str] = []
    raw_lines: list[str] = []

    def drain_stderr():
        for line in proc.stderr:
            print(f"[stderr] {line.rstrip()}")

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()

    for line in proc.stdout:
        elapsed = (time.monotonic() - t0) * 1000
        line = line.rstrip("\n")
        if not line:
            continue
        raw_lines.append(line)
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            print(f"[{elapsed:7.0f}ms] NON-JSON: {line[:200]}")
            continue
        etype = ev.get("type", "?")
        sub = ev.get("subtype", "")
        event_types.append(f"{etype}/{sub}" if sub else etype)
        sid = ev.get("session_id")
        if sid and not session_id:
            session_id = sid
        # compact, informative per-event summary
        summary = f"type={etype}"
        if sub:
            summary += f" subtype={sub}"
        if sid:
            summary += f" session_id={sid}"
        if etype == "result":
            summary += f" cost=${ev.get('total_cost_usd')} is_error={ev.get('is_error')} result={ev.get('result')!r}"
        if etype == "stream_event":
            inner = ev.get("event", {})
            summary += f" inner={inner.get('type')}"
        print(f"[{elapsed:7.0f}ms] {summary}")

    rc = proc.wait()
    total = (time.monotonic() - t0) * 1000
    RAW_LOG.write_text("\n".join(raw_lines), encoding="utf-8")

    print("\n==== SUMMARY ====")
    print(f"exit code      : {rc}")
    print(f"total time     : {total:.0f}ms")
    print(f"session_id     : {session_id}")
    print(f"event types    : {event_types}")
    print(f"raw lines      : {len(raw_lines)} (saved to {RAW_LOG})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
