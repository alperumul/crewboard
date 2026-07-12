"""Isolated spike: a permission-prompt-tool MCP server.

Auto-allows every request (this spike only proves the mechanism: recognition,
continue-in-place, and the approval-wait timeout). It logs exactly what claude
passes so we learn the real input schema. APPROVE_SLEEP env var delays the
decision to probe how long a paused turn tolerates an unanswered permission call.
"""

from __future__ import annotations

import json
import os
import time

from mcp.server.fastmcp import FastMCP

LOG = os.path.join(os.path.dirname(__file__), "mcp_spike_log.txt")
SLEEP = float(os.environ.get("APPROVE_SLEEP", "0"))


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


mcp = FastMCP("hub")


@mcp.tool()
def approve(tool_name: str, input: dict | None = None):
    """Permission decision tool. Returns the Claude Code permission-result JSON.

    No return annotation on purpose: a `-> str` makes FastMCP attach an
    outputSchema and emit structuredContent, which Claude Code's permission
    parser rejects ("expected a single text block"). Returning a bare str
    yields exactly one text content block.
    """
    log(f"approve CALLED tool_name={tool_name!r} input={json.dumps(input)} sleep={SLEEP}")
    if SLEEP:
        time.sleep(SLEEP)
    decision = {"behavior": "allow", "updatedInput": input or {}}
    log(f"approve RETURN {json.dumps(decision)}")
    return json.dumps(decision)


if __name__ == "__main__":
    log(f"=== server boot pid={os.getpid()} sleep={SLEEP} ===")
    mcp.run()
