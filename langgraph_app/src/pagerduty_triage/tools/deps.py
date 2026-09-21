"""Everything the tools need, and how a tool learns which ticket it is on.

## Ticket identity comes from config, never from the model

A tool like `zoho_send_reply` has to know which ticket it is replying to. The
obvious design is to let the model pass `ticket_id` as an argument. We do not
do that, and the reason is worth stating plainly:

**the model must not be able to choose who receives mail.** If `ticket_id` is
a model-supplied argument then a confused agent — or a prompt-injected one,
and ticket text is attacker-influenced input by definition — can address a
reply to a different customer's ticket. The gate would still fire, but it
would show a reviewer the *right* text next to the *wrong* recipient, which
is precisely the kind of thing a reviewer approves at 2am.

So ticket identity is bound at invocation time, into `configurable`, by the
poller — the component that already knows which ticket this thread is for —
and the tools read it from `RunnableConfig`. The model has no say. Its tools
take only content arguments: the reply text, the issue fields.

This also means a tool can assert `thread_id == thread_id_for_ticket(id)`,
which catches a whole class of wiring mistakes at the moment they matter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..ledger import TicketLedger
from ..settings import Settings
from ..sprint_tasks import SprintTasksClient
from ..zoho.client import ZohoDeskClient

#: Key under ``config["configurable"]`` holding the bound ticket context.
TICKET_CONTEXT_KEY = "ticket_context"


#: How many of the customer's messages ride in the bound context. The first
#: (the ticket description) is always carried; this caps the *later* ones, so
#: a 200-message ticket cannot turn a `configurable` blob into a payload no
#: reviewer will read and no checkpoint wants to store. The most recent are
#: kept, because they are the ones a reply has to answer.
MAX_CARRIED_CUSTOMER_MESSAGES = 6

#: Per-message character cap, applied when the context is built. A single
#: pasted 2MB log must not travel in every checkpoint of the thread.
MAX_CARRIED_MESSAGE_CHARS = 8000


@dataclass(frozen=True)
class TicketContext:
    """Which ticket this thread is about. Bound by the poller, read by tools.

    It also carries **the customer's own words**. That is not decoration: gate
    1 asks a human to approve a reply, and a reviewer cannot judge a reply
    without seeing what was asked. The alternative — having `zoho_send_reply`
    fetch the description itself — would put a network call *above*
    `interrupt()`, where it runs before anyone sees the request and runs again
    on every resume. `test_interrupt_precedes_every_side_effect` rejects that,
    correctly. So the read happens once, in the poller, and travels here.
    """

    ticket_id: str
    ticket_number: str
    subject: str
    web_url: str
    email: str
    thread_id: str = ""
    #: The customer's original question: the ticket description, plain text,
    #: verbatim. Untrusted input — redact before it reaches a channel.
    description: str = ""
    #: Later customer messages on the ticket, oldest first, plain text. Empty
    #: when the conversation could not be read; that is not an error.
    customer_messages: tuple[str, ...] = ()
    #: True when `customer_messages` was capped, so a renderer can say so
    #: rather than quietly showing a partial thread.
    customer_messages_truncated: bool = False

    def to_configurable(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "ticket_number": self.ticket_number,
            "subject": self.subject,
            "web_url": self.web_url,
            "email": self.email,
            "thread_id": self.thread_id,
            "description": self.description,
            "customer_messages": list(self.customer_messages),
            "customer_messages_truncated": self.customer_messages_truncated,
        }

    @classmethod
    def from_configurable(cls, raw: dict[str, Any]) -> "TicketContext":
        return cls(
            ticket_id=raw.get("ticket_id", ""),
            ticket_number=raw.get("ticket_number", ""),
            subject=raw.get("subject", ""),
            web_url=raw.get("web_url", ""),
            email=raw.get("email", ""),
            thread_id=raw.get("thread_id", ""),
            description=str(raw.get("description", "") or ""),
            customer_messages=tuple(
                str(m) for m in (raw.get("customer_messages") or [])
            ),
            customer_messages_truncated=bool(
                raw.get("customer_messages_truncated", False)
            ),
        )


class MissingTicketContext(RuntimeError):
    """Raised when a tool runs on a thread with no bound ticket.

    This is a wiring bug, and it fails loudly rather than defaulting to
    anything. There is no sensible default for "which customer to email".
    """


def ticket_context_from_config(config: Any) -> TicketContext:
    """Pull the bound ticket out of a ``RunnableConfig``.

    LangGraph injects ``config`` into any tool whose signature declares it,
    and hides it from the schema the model sees.
    """
    configurable = (config or {}).get("configurable") or {}
    raw = configurable.get(TICKET_CONTEXT_KEY)
    if not raw or not raw.get("ticket_id"):
        raise MissingTicketContext(
            "No ticket is bound to this thread. The graph must be invoked with "
            f"configurable[{TICKET_CONTEXT_KEY!r}] set by the poller. Refusing to "
            "act without knowing which ticket this is."
        )
    return TicketContext.from_configurable(raw)


@dataclass
class ToolDeps:
    """Wiring handed to the tool factory. One instance per process."""

    settings: Settings
    zoho: ZohoDeskClient
    sprint_tasks: SprintTasksClient
    ledger: TicketLedger
