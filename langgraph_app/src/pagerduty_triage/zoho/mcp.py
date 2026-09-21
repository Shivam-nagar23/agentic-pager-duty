"""`ZohoDeskClient` backed by an MCP server.

Talks to the thin wrapper in `mcp_server/zoho_desk_mcp.py` (or, in principle,
to any server exposing the same five tools). Selected with
`ZOHO_TRANSPORT=mcp`.

**Not implemented yet, and deliberately not stubbed into silence.** Writing an
MCP client that quietly returns empty ticket lists would make a
misconfiguration look like a quiet day in the support queue. It raises
instead.

The REST path is complete and is what the first live run should use. Add this
once the MCP wrapper has been exercised end to end — the interface it must
satisfy is `ZohoDeskClient` in `client.py`, nothing more.
"""

from __future__ import annotations

from ..settings import Settings


class McpZohoDeskClient:
    def __init__(self, settings: Settings):
        raise NotImplementedError(
            "ZOHO_TRANSPORT=mcp is not implemented yet. Use ZOHO_TRANSPORT=rest "
            "(complete) or 'fake' (offline). The MCP wrapper server itself is in "
            "pagerduty_triage.mcp_server.zoho_desk_mcp; what is missing is the "
            "client that speaks to it."
        )
