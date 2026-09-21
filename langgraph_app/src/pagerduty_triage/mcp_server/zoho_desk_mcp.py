"""A thin MCP server over Zoho Desk REST v1.

Written because no usable Zoho Desk MCP server exists — see the README for the
evidence. This is deliberately *thin*: it wraps `RestZohoDeskClient` and adds
nothing, so there is exactly one implementation of the Zoho API shapes in this
repo and the MCP layer is a transport choice rather than a second codebase.

## Scope: five tools, and no more

Community Desk servers expose 50–700 endpoints. This one exposes five,
because five is what triage needs:

    zoho_list_tickets      zoho_get_ticket      zoho_list_threads
    zoho_list_attachments  zoho_send_reply

Every additional tool is a way for a confused agent to mutate a customer's
ticket. `zoho_send_reply` is the only write, and it is the one the graph
already gates — note that **this server does not gate it**. The gate lives in
the LangGraph tool, above this layer. Exposing this server to anything other
than that graph would expose an ungated send.

Run it:

    python -m pagerduty_triage.mcp_server.zoho_desk_mcp        # stdio

Point the app at it with `ZOHO_TRANSPORT=mcp` and `ZOHO_MCP_URL`.
"""

from __future__ import annotations

import json
from typing import Any

from ..settings import load_settings
from ..zoho.rest import RestZohoDeskClient

SERVER_NAME = "zoho-desk"


def build_server() -> Any:
    """Construct the MCP server. Imports `mcp` lazily so the rest of the app
    does not depend on it."""
    from mcp.server.fastmcp import FastMCP

    settings = load_settings()
    client = RestZohoDeskClient(settings)
    server = FastMCP(SERVER_NAME)

    @server.tool()
    def zoho_list_tickets(modified_since: str = "", limit: int = 50) -> str:
        """List Desk tickets modified since an ISO-8601 timestamp."""
        rows = client.list_tickets(
            modified_since=modified_since or None,
            statuses=settings.zoho_poll_statuses,
            limit=limit,
        )
        return json.dumps([t.__dict__ for t in rows], indent=2)

    @server.tool()
    def zoho_get_ticket(ticket_id: str) -> str:
        """Fetch one ticket by id."""
        return json.dumps(client.get_ticket(ticket_id).__dict__, indent=2)

    @server.tool()
    def zoho_list_threads(ticket_id: str, limit: int = 50) -> str:
        """Full conversation for a ticket, oldest first, bodies included.

        Costs 1 + N upstream requests; Zoho's thread list carries no message
        bodies. Call it once per ticket, not per turn.
        """
        return json.dumps(
            [t.__dict__ for t in client.list_threads(ticket_id, limit=limit)], indent=2
        )

    @server.tool()
    def zoho_list_attachments(ticket_id: str) -> str:
        """Attachment metadata for a ticket. Does not download content."""
        return json.dumps(
            [a.__dict__ for a in client.list_attachments(ticket_id)], indent=2
        )

    @server.tool()
    def zoho_send_reply(ticket_id: str, content: str, to_address: str) -> str:
        """Send a public reply on a ticket.

        UNGATED AT THIS LAYER. The human approval gate is in the LangGraph
        tool that calls this. Do not expose this server to any other client.
        """
        result = client.send_reply(
            ticket_id, content=content, to_address=to_address, content_type="html"
        )
        return json.dumps(result.__dict__, indent=2, default=str)

    return server


def main() -> None:  # pragma: no cover - process entry point
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
