"""The Zoho Desk seam.

Everything the triage agent knows about Zoho goes through ``ZohoDeskClient``.
There are three implementations:

* ``rest.RestZohoDeskClient``  -- direct Zoho Desk REST v1 (the fallback).
* ``mcp.McpZohoDeskClient``   -- talks to an MCP server (ours, or a
  third-party one if a trustworthy Desk server ever ships).
* ``fake.FakeZohoDeskClient`` -- in-memory, used by every test. No network.

The point of the seam is that swapping MCP in for REST is a one-line change in
``settings.py`` and touches no agent code.

Deliberately small. Only five operations, because only five are needed:
list tickets (polling), read one ticket, read its conversation, read its
attachments, and send a reply. Notably absent: anything that mutates a ticket
other than the single gated reply.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


def strip_html(raw: str) -> str:
    """Zoho ticket descriptions and thread bodies are HTML. Everything that
    reads them — the ``/ticket.md`` writer, the poller building the gate
    context — wants text.

    Lives here rather than in ``tools/`` because ``poller.py`` needs it too and
    must not import the LangChain tool stack to get at it.
    """
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text).strip()


@dataclass(frozen=True)
class Ticket:
    """A Zoho Desk ticket, reduced to what triage actually reads."""

    id: str
    ticket_number: str
    subject: str
    description: str
    status: str
    created_time: str
    modified_time: str
    web_url: str
    contact_name: str = ""
    account_name: str = ""
    priority: str = ""
    channel: str = ""
    # The email the customer wrote to / from; needed to address a reply.
    email: str = ""
    department_id: str = ""


@dataclass(frozen=True)
class Thread:
    """One message in a ticket's conversation."""

    id: str
    created_time: str
    direction: str  # "in" (customer) | "out" (agent)
    author: str
    content: str
    content_type: str = "html"
    from_address: str = ""
    to_address: str = ""


@dataclass(frozen=True)
class Attachment:
    id: str
    name: str
    size: int
    href: str = ""


@dataclass(frozen=True)
class ReplyResult:
    thread_id: str
    ticket_id: str
    sent_at: str = ""
    raw: dict = field(default_factory=dict)


@runtime_checkable
class ZohoDeskClient(Protocol):
    """The only Zoho surface the agent may use."""

    def list_tickets(
        self,
        *,
        modified_since: str | None = None,
        statuses: tuple[str, ...] = ("Open",),
        limit: int = 50,
        from_index: int = 1,
    ) -> list[Ticket]:
        """Tickets modified since ``modified_since`` (ISO-8601), newest first.

        ``modified_since`` is a *hint* for bandwidth, never a correctness
        mechanism -- dedupe is the ledger's job, not the query's.
        """

    def get_ticket(self, ticket_id: str) -> Ticket:
        ...

    def list_threads(self, ticket_id: str, *, limit: int = 50) -> list[Thread]:
        """Full conversation, oldest first, with content already fetched.

        Zoho's thread *list* endpoint omits message bodies; implementations
        must fetch each thread's detail so callers get real content.
        """

    def list_attachments(self, ticket_id: str) -> list[Attachment]:
        ...

    def send_reply(
        self,
        ticket_id: str,
        *,
        content: str,
        to_address: str,
        content_type: str = "html",
    ) -> ReplyResult:
        """Send a public reply. Only ever called after gate 1 approves."""
