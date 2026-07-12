"""Pending permission-approval registry (the 🟡 approve flow).

A session's permission MCP server (spawned by claude) bridges here over HTTP:
it POSTs a request, the hub surfaces it to the human, the MCP server long-polls
until the human decides, then returns the decision to claude — which continues
the SAME turn (continue-in-place, verified in step 5: tolerates >=180s wait).

This module is pure: registry + sync primitives. app.py wires it to the UI
(state -> awaiting-approval, broadcast, secretary rail).
"""

from __future__ import annotations

import secrets
import threading
from typing import Any


class PendingApproval:
    def __init__(self, req_id: str, panel_id: str, tool_name: str, tool_input: dict):
        self.id = req_id
        self.panel_id = panel_id
        self.tool_name = tool_name
        self.input = tool_input
        self.event = threading.Event()
        self.behavior: str | None = None  # "allow" | "deny"
        self.message: str | None = None
        self.resolved_input: dict = tool_input


_REGISTRY: dict[str, PendingApproval] = {}
_LOCK = threading.Lock()


def create(panel_id: str, tool_name: str, tool_input: dict) -> PendingApproval:
    req_id = secrets.token_hex(8)
    pa = PendingApproval(req_id, panel_id, tool_name, tool_input)
    with _LOCK:
        _REGISTRY[req_id] = pa
    return pa


def wait(req_id: str, timeout: float) -> dict[str, Any] | None:
    """Block up to `timeout`s for a decision. None = still undecided."""
    pa = _REGISTRY.get(req_id)
    if pa is None:
        return {"behavior": "deny", "message": "unknown approval request"}
    if pa.event.wait(timeout):
        return {"behavior": pa.behavior, "message": pa.message, "updatedInput": pa.resolved_input}
    return None


def decide(req_id: str, behavior: str, message: str | None = None) -> bool:
    pa = _REGISTRY.get(req_id)
    if pa is None or pa.event.is_set():
        return False
    pa.behavior = "allow" if behavior == "allow" else "deny"
    pa.message = message or ("approved by user" if behavior == "allow" else "denied by user")
    pa.event.set()
    return True


def get(req_id: str) -> PendingApproval | None:
    return _REGISTRY.get(req_id)


def pending_for(panel_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        return [
            {"id": pa.id, "tool_name": pa.tool_name, "input": pa.input}
            for pa in _REGISTRY.values()
            if pa.panel_id == panel_id and not pa.event.is_set()
        ]


def cleanup(req_id: str) -> None:
    with _LOCK:
        _REGISTRY.pop(req_id, None)
