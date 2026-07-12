"""Per-session permission MCP server (the `mcp__hub__approve` tool).

Spawned by each claude session via --mcp-config. Bridges a permission decision
to the hub over localhost HTTP: POST the request, long-poll until the human
decides, return the Claude Code permission-result JSON as a BARE string (a
return annotation would make FastMCP emit structuredContent, which the
permission parser rejects — learned in the step-5 spike).

Env (set per session in the generated mcp-config):
  HUB_URL    hub base url (default http://127.0.0.1:5055)
  HUB_PANEL  which panel this session belongs to
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from mcp.server.fastmcp import FastMCP

HUB_URL = os.environ.get("HUB_URL", "http://127.0.0.1:5055")
PANEL = os.environ.get("HUB_PANEL", "?")
OVERALL_TIMEOUT = 170.0  # stay under the verified >=180s paused-turn tolerance

mcp = FastMCP("hub")


def _post(path: str, body: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        HUB_URL + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(path: str, timeout: float) -> dict:
    with urllib.request.urlopen(HUB_URL + path, timeout=timeout) as r:
        return json.loads(r.read())


@mcp.tool()
def approve(tool_name: str, input: dict | None = None):
    """Ask the human (via the hub) to allow/deny a tool call; block until decided."""
    tool_input = input or {}
    try:
        resp = _post(
            "/permission/request",
            {"panel_id": PANEL, "tool_name": tool_name, "input": tool_input},
        )
        req_id = resp["request_id"]
    except Exception as e:  # noqa: BLE001 — fail safe
        return json.dumps({"behavior": "deny", "message": f"hub unreachable: {e}"})

    deadline = time.monotonic() + OVERALL_TIMEOUT
    while time.monotonic() < deadline:
        try:
            d = _get(f"/permission/poll/{req_id}?wait=25", timeout=40.0)
        except Exception:  # noqa: BLE001 — transient; retry until deadline
            continue
        if d.get("decided"):
            if d.get("behavior") == "allow":
                return json.dumps({"behavior": "allow", "updatedInput": d.get("updatedInput") or tool_input})
            return json.dumps({"behavior": "deny", "message": d.get("message") or "denied"})

    return json.dumps({"behavior": "deny", "message": "approval timed out"})


if __name__ == "__main__":
    mcp.run()
