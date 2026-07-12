"""Spike: --permission-prompt-tool continue-in-place + approval-wait timeout.

Launches a turn that must use Bash (NOT in the allowlist) so the permission
decision routes to our MCP approve tool. Observes whether the same turn then
executes Bash and finishes (continue-in-place). Set APPROVE_SLEEP to probe the
paused-turn timeout.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
MCP_CFG = str(HERE / "hub_mcp.json")
MCP_LOG = HERE / "mcp_spike_log.txt"
CWD = str(HERE.parent)
MARKER = "APPROVED_BASH_RAN"
SLEEP = os.environ.get("APPROVE_SLEEP", "0")


def main() -> int:
    claude = shutil.which("claude")
    if MCP_LOG.exists():
        MCP_LOG.unlink()

    cmd = [
        claude,
        "-p",
        f"Use the Bash tool to run exactly this command: echo {MARKER} > _spike_marker.txt "
        f"&& cat _spike_marker.txt. Then tell me the exact text it printed.",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode",
        "default",  # override user's defaultMode:auto so the prompt actually fires
        "--allowedTools",
        "Read",  # Bash excluded -> needs permission
        "--permission-prompt-tool",
        "mcp__hub__approve",
        "--mcp-config",
        MCP_CFG,
        "--strict-mcp-config",  # only the hub server, not the user's claude.ai MCPs
    ]
    print(f"APPROVE_SLEEP={SLEEP}")
    print("launching with permission-prompt-tool mcp__hub__approve ...")

    env = dict(os.environ, APPROVE_SLEEP=SLEEP)
    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=CWD,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=env,
    )

    def drain_err():
        for ln in proc.stderr:
            s = ln.rstrip()
            if s:
                print(f"[stderr] {s[:200]}")

    threading.Thread(target=drain_err, daemon=True).start()

    saw = {
        "mcp_hub_init": False,
        "bash_tool_use": False,
        "marker_in_output": False,
        "approve_called": False,
        "result": None,
        "num_turns": None,
        "is_error": None,
    }

    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        el = (time.monotonic() - t0) * 1000
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            servers = ev.get("mcp_servers", [])
            names = [s.get("name") for s in servers]
            saw["mcp_hub_init"] = "hub" in names
            print(f"[{el:7.0f}ms] init  mcp_servers={names} hub_status={[s.get('status') for s in servers]}")
        elif t == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "tool_use":
                    print(f"[{el:7.0f}ms] tool_use {b.get('name')} input={json.dumps(b.get('input'))[:120]}")
                    if b.get("name") == "Bash":
                        saw["bash_tool_use"] = True
        elif t == "user":
            # tool_result comes back as a user event
            content = ev.get("message", {}).get("content", [])
            txt = json.dumps(content)
            if MARKER in txt:
                saw["marker_in_output"] = True
                print(f"[{el:7.0f}ms] tool_result contains {MARKER}")
        elif t == "result":
            saw["result"] = ev.get("result")
            saw["num_turns"] = ev.get("num_turns")
            saw["is_error"] = ev.get("is_error")
            print(
                f"[{el:7.0f}ms] RESULT is_error={ev.get('is_error')} num_turns={ev.get('num_turns')} "
                f"dur={ev.get('duration_ms')}ms result={ev.get('result')!r}"
            )

    rc = proc.wait()
    total = (time.monotonic() - t0) * 1000

    if MCP_LOG.exists():
        log = MCP_LOG.read_text(encoding="utf-8")
        saw["approve_called"] = "approve CALLED" in log
        print("\n--- MCP server log ---")
        print(log.strip())

    print("\n==== SPIKE SUMMARY ====")
    print(f"exit code              : {rc}  total {total:.0f}ms")
    print(f"mcp 'hub' recognized   : {saw['mcp_hub_init']}")
    print(f"approve tool CALLED    : {saw['approve_called']}")
    print(f"Bash tool_use happened : {saw['bash_tool_use']}")
    print(f"marker in tool output  : {saw['marker_in_output']}")
    print(f"num_turns (internal)   : {saw['num_turns']}")
    print(f"is_error               : {saw['is_error']}")
    print(f"final result           : {saw['result']!r}")
    # continue-in-place = ONE subprocess gets approval, executes the tool, finishes.
    verdict = (
        saw["approve_called"] and saw["bash_tool_use"] and saw["marker_in_output"] and not saw["is_error"] and rc == 0
    )
    print(f"\nCONTINUE-IN-PLACE CONFIRMED: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
