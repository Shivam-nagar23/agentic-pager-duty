"""In-memory Zoho Desk stub. Every test uses this; it never touches a network.

It also records every ``send_reply`` call, which is what lets the gate tests
assert the strong property: *the side effect did not happen*. A test that only
checks "the tool returned an interrupt" would still pass if the tool sent the
mail first and interrupted afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import Attachment, ReplyResult, Thread, Ticket


@dataclass
class FakeZohoDeskClient:
    tickets: dict[str, Ticket] = field(default_factory=dict)
    threads: dict[str, list[Thread]] = field(default_factory=dict)
    attachments: dict[str, list[Attachment]] = field(default_factory=dict)

    #: Every reply actually sent. Tests assert this stays empty across a gate.
    sent_replies: list[dict] = field(default_factory=list)
    #: Raised by the next call, for failure-path tests.
    fail_next: Exception | None = None

    # -- construction helpers ------------------------------------------------

    def add_ticket(self, ticket: Ticket, *, threads: list[Thread] | None = None) -> None:
        self.tickets[ticket.id] = ticket
        self.threads[ticket.id] = threads or []
        self.attachments.setdefault(ticket.id, [])

    # -- ZohoDeskClient ------------------------------------------------------

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            err, self.fail_next = self.fail_next, None
            raise err

    def list_tickets(
        self,
        *,
        modified_since: str | None = None,
        statuses: tuple[str, ...] = ("Open",),
        limit: int = 50,
        from_index: int = 1,
    ) -> list[Ticket]:
        self._maybe_fail()
        rows = [
            t
            for t in self.tickets.values()
            if (not statuses or t.status in statuses)
            and (modified_since is None or t.modified_time >= modified_since)
        ]
        rows.sort(key=lambda t: t.modified_time, reverse=True)
        return rows[from_index - 1 : from_index - 1 + limit]

    def get_ticket(self, ticket_id: str) -> Ticket:
        self._maybe_fail()
        return self.tickets[ticket_id]

    def list_threads(self, ticket_id: str, *, limit: int = 50) -> list[Thread]:
        self._maybe_fail()
        return self.threads.get(ticket_id, [])[:limit]

    def list_attachments(self, ticket_id: str) -> list[Attachment]:
        self._maybe_fail()
        return self.attachments.get(ticket_id, [])

    def send_reply(
        self,
        ticket_id: str,
        *,
        content: str,
        to_address: str,
        content_type: str = "html",
    ) -> ReplyResult:
        self._maybe_fail()
        self.sent_replies.append(
            {
                "ticket_id": ticket_id,
                "content": content,
                "to_address": to_address,
                "content_type": content_type,
            }
        )
        return ReplyResult(
            thread_id=f"fake-thread-{len(self.sent_replies)}",
            ticket_id=ticket_id,
            sent_at="2026-09-16T00:00:00.000Z",
        )
