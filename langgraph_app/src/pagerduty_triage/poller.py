"""The cron entry point: find new Zoho tickets, open one thread each.

This is where "do not double-reply a paying customer" is won or lost. Read
`ledger.py` first for the four layers; this module implements layers 1–3.

## The shape of the loop

For each candidate ticket:

    thread_id = thread_id_for_ticket(ticket.id)   # pure function of the id
    try:
        threads.create(thread_id, if_exists="raise")
    except Conflict:
        continue                                   # someone else owns it
    ledger.record_seen(...)                        # audit, after the fact
    runs.create(thread_id, multitask_strategy="reject", ...)

The `try/except Conflict: continue` is the entire concurrency story. It is one
database insert against a primary key, so two pollers racing on one ticket
cannot both proceed — Postgres decides, not us. Everything else in this file
is bandwidth optimisation and bookkeeping.

## Why not a watermark

The obvious design — remember `last_seen_modified_time`, ask Zoho for
everything newer — is wrong here, and the research turned up why: **Zoho
Desk's search index lags.** Zoho's own documentation says newly added
resources "may require some time to be included in the index". A strict
watermark therefore silently drops tickets that were created before the
watermark advanced but indexed after.

So the query window deliberately **overlaps** (`OVERLAP_SECONDS`), and the
poller re-sees tickets it has already processed on every tick. That is fine,
and it is the point: re-seeing a ticket is free because thread creation is
idempotent, whereas missing one means a customer is ignored. We buy safety
with duplicate work that costs a 409.

Two consequences worth stating, because they look like bugs:

* the poller will attempt the same ticket many times over its lifetime;
* the `seen` ledger exists so those attempts usually skip before making an
  API call, not to make them *correct*. They were already correct.

## What is NOT handled

**Follow-up messages on a ticket we already replied to.** One thread per
ticket means the second customer message arrives on a ticket whose thread is
finished, and nothing wakes it. This is a real gap, not an oversight, and it
is listed in the README. Fixing it means either resuming the existing thread
with the new message or keying threads on ticket+thread-count; both need a
decision from the owner about what a second reply should even do.
"""

from __future__ import annotations

import os

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .ledger import TicketLedger, thread_id_for_ticket
from .settings import Settings
from .tools.deps import (
    MAX_CARRIED_CUSTOMER_MESSAGES,
    MAX_CARRIED_MESSAGE_CHARS,
    TICKET_CONTEXT_KEY,
    TicketContext,
)
from .zoho.client import Ticket, strip_html

#: How far back the query window reaches beyond the last tick. Covers Zoho's
#: search-index lag and any clock skew. Cheap: overlap costs 409s, not mail.
OVERLAP_SECONDS = 600

#: Hard cap on threads opened per tick, so a backlog or a bad query cannot
#: fan out into hundreds of concurrent runs and a surprise bill.
MAX_NEW_THREADS_PER_TICK = 25


class ThreadConflict(Exception):
    """Raised by a client when a thread id already exists (HTTP 409)."""


class LangGraphClientLike(Protocol):
    """The slice of `langgraph_sdk` this module uses.

    Narrow on purpose: it keeps the poller testable with a fake and makes the
    exact platform semantics we depend on explicit rather than implied.
    """

    def create_thread(
        self, thread_id: str, *, metadata: dict[str, Any], if_exists: str
    ) -> Any:
        """Create a thread. MUST raise `ThreadConflict` when `if_exists` is
        `"raise"` and the thread exists. That exception is the mutex."""

    def bind_ticket_context(
        self, thread_id: str, *, ticket_context: dict[str, Any]
    ) -> Any:
        """Persist the bound ticket on the *thread*, not just the run.

        Run-level ``configurable`` is not inherited by later runs, and a gate
        resume is a later run. Without this, every Slack approval re-enters
        the graph with no ticket bound and the tools refuse to act — which is
        exactly what happened to ticket #101: the gate was consumed and the
        run died with ``MissingTicketContext``.

        Written after the claim and before the run, so a thread that exists
        always ends up carrying the context a resume will need.
        """

    def create_run(
        self,
        thread_id: str,
        *,
        assistant_id: str,
        input: dict[str, Any],
        config: dict[str, Any],
        multitask_strategy: str,
    ) -> Any:
        """Start a run. `multitask_strategy` MUST be `"reject"` — the platform
        default is `enqueue`, which would queue a duplicate pass instead of
        refusing it."""


@dataclass
class PollReport:
    """What one tick did. Returned so the cron run leaves a readable trace."""

    considered: int = 0
    started: list[str] = field(default_factory=list)
    skipped_seen: list[str] = field(default_factory=list)
    skipped_conflict: list[str] = field(default_factory=list)
    skipped_replied: list[str] = field(default_factory=list)
    #: Created before ZOHO_MIN_CREATED_AT. Counted, never silent: a poller that
    #: drops most of what it sees looks identical to a broken query.
    skipped_old: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    capped: bool = False

    def summary(self) -> str:
        return (
            f"considered={self.considered} started={len(self.started)} "
            f"skipped_seen={len(self.skipped_seen)} "
            f"skipped_conflict={len(self.skipped_conflict)} "
            f"skipped_replied={len(self.skipped_replied)} "
            f"skipped_old={len(self.skipped_old)} "
            f"errors={len(self.errors)} capped={self.capped}"
        )


def _window_start(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(seconds=OVERLAP_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def poll_once(
    *,
    zoho: Any,
    ledger: TicketLedger,
    client: LangGraphClientLike,
    settings: Settings,
    assistant_id: str = "triage",
    now: datetime | None = None,
) -> PollReport:
    """One cron tick. Pure of global state; every dependency is injected.

    Never raises for a single bad ticket — one malformed ticket must not stop
    the other twenty-four from being picked up. Errors are collected into the
    report so the cron run records them.
    """
    report = PollReport()

    try:
        tickets: list[Ticket] = zoho.list_tickets(
            modified_since=_window_start(now),
            statuses=settings.zoho_poll_statuses,
            limit=settings.zoho_poll_limit,
        )
    except Exception as exc:  # noqa: BLE001 - a failed tick must not crash the cron
        # Zoho unreachable: skip this tick entirely. Tickets are picked up on
        # the next successful poll; nothing is lost because we never advanced
        # a watermark in the first place.
        report.errors.append(f"list_tickets failed: {exc!r}")
        return report

    report.considered = len(tickets)

    # Drawn once per tick, not per ticket.
    cutover = os.environ.get("ZOHO_MIN_CREATED_AT", "").strip()

    for ticket in tickets:
        if len(report.started) >= MAX_NEW_THREADS_PER_TICK:
            report.capped = True
            break

        # Pointing at an existing desk, the backlog is the problem, and the
        # `modifiedTimeRange` window does not solve it: a customer replying to a
        # three-day-old ticket bumps its modified time and it arrives looking
        # exactly like new work. This draws a line at a creation instant.
        #
        # A blank `created_time` is NOT filtered. Missing data must not silently
        # exclude a ticket — triaging one stale ticket is a cheaper mistake than
        # dropping real work and never knowing.
        if cutover and ticket.created_time and ticket.created_time < cutover:
            report.skipped_old.append(ticket.id)
            continue

        try:
            _start_one(
                ticket=ticket,
                zoho=zoho,
                ledger=ledger,
                client=client,
                assistant_id=assistant_id,
                report=report,
            )
        except ThreadConflict:
            # Another poller (or an earlier tick) owns this ticket.
            report.skipped_conflict.append(ticket.id)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"ticket {ticket.id}: {exc!r}")

    return report


def _clip(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_CARRIED_MESSAGE_CHARS:
        return text
    return (
        text[:MAX_CARRIED_MESSAGE_CHARS]
        + f"\n\n[… {len(text) - MAX_CARRIED_MESSAGE_CHARS} more characters — "
        "read the rest in the Zoho ticket]"
    )


def build_ticket_context(
    ticket: Ticket,
    *,
    thread_id: str,
    zoho: Any | None = None,
) -> TicketContext:
    """The context bound into the thread, including the customer's own words.

    The conversation read is **best effort**. A ticket whose threads cannot be
    listed still gets triaged — the description alone is the customer's
    original question, and `zoho_fetch_ticket` will read the conversation again
    inside the run anyway. Failing to start triage because a secondary read
    failed would be the wrong trade.

    Called from exactly two places — the poll loop and the manual
    `start_ticket` entry point — so both bind identical context.
    """
    later: list[str] = []
    truncated = False
    if zoho is not None:
        try:
            threads = zoho.list_threads(ticket.id)
        except Exception:  # noqa: BLE001 - a secondary read must not block triage
            threads = []
        inbound = [
            strip_html(t.content)
            for t in threads
            if getattr(t, "direction", "") == "in"
        ]
        inbound = [t for t in inbound if t]
        # The first inbound message is normally the description again; Zoho
        # populates both from the same mail. Drop it when it matches, so the
        # reviewer is not shown the same paragraph twice.
        description_text = strip_html(ticket.description)
        if inbound and inbound[0].strip() == description_text.strip():
            inbound = inbound[1:]
        if len(inbound) > MAX_CARRIED_CUSTOMER_MESSAGES:
            # Keep the most recent: those are what a reply has to answer.
            inbound = inbound[-MAX_CARRIED_CUSTOMER_MESSAGES:]
            truncated = True
        later = [_clip(t) for t in inbound]

    return TicketContext(
        ticket_id=ticket.id,
        ticket_number=ticket.ticket_number,
        subject=ticket.subject,
        web_url=ticket.web_url,
        email=ticket.email,
        thread_id=thread_id,
        description=_clip(strip_html(ticket.description)),
        customer_messages=tuple(later),
        customer_messages_truncated=truncated,
    )


def start_ticket(
    *,
    ticket_id: str,
    zoho: Any,
    ledger: TicketLedger,
    client: LangGraphClientLike,
    assistant_id: str = "triage",
) -> PollReport:
    """Start triage for one named ticket, by hand, with no cron and no query.

    This is the poll loop minus the query: **the same claim, the same ledger,
    the same deterministic thread id.** It exists because a local run has no
    hosted cron (that needs Plus/Enterprise), not because the guarantees are
    negotiable — a second call for the same ticket loses the
    `threads.create(if_exists="raise")` race exactly as a second poller would,
    and lands in `skipped_conflict`.
    """
    report = PollReport()
    try:
        ticket = zoho.get_ticket(ticket_id)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"get_ticket({ticket_id!r}) failed: {exc!r}")
        return report

    report.considered = 1
    try:
        _start_one(
            ticket=ticket,
            zoho=zoho,
            ledger=ledger,
            client=client,
            assistant_id=assistant_id,
            report=report,
        )
    except ThreadConflict:
        report.skipped_conflict.append(ticket.id)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"ticket {ticket.id}: {exc!r}")
    return report


def _start_one(
    *,
    ticket: Ticket,
    ledger: TicketLedger,
    client: LangGraphClientLike,
    assistant_id: str,
    report: PollReport,
    zoho: Any | None = None,
) -> None:
    thread_id = thread_id_for_ticket(ticket.id)

    # Cheap skips first. Neither is a correctness gate — both are here to
    # avoid a pointless API call on the overlap window's repeat sightings.
    if ledger.has_replied(ticket.id):
        report.skipped_replied.append(ticket.id)
        return
    if ledger.is_claimed(ticket.id):
        report.skipped_seen.append(ticket.id)
        return

    # THE CLAIM. First writer wins; everyone else gets ThreadConflict and is
    # caught by the caller. Do not replace this with a get-then-create.
    client.create_thread(
        thread_id,
        metadata={
            "zoho_ticket_id": ticket.id,
            "zoho_ticket_number": ticket.ticket_number,
            "subject": ticket.subject,
        },
        if_exists="raise",
    )

    # Only now, having actually won, record it. A record written before the
    # claim would describe work we might not own.
    ledger.record_seen(
        ticket.id, thread_id=thread_id, modified_time=ticket.modified_time
    )

    # Read the customer's own words ONCE, here, and bind them into the thread.
    # Gate 1 needs them in the payload and cannot fetch them itself: anything
    # above `interrupt()` runs before a human sees the request, and again on
    # every resume. See `tools/deps.TicketContext`.
    ctx = build_ticket_context(ticket, thread_id=thread_id, zoho=zoho)

    # Bind it to the thread before the run starts. The resume path reads it
    # back from here, so it must be durable and must not require another Zoho
    # call — a fetch on the resume path would be a side effect above
    # `interrupt()`, running again on every approval.
    client.bind_ticket_context(thread_id, ticket_context=ctx.to_configurable())

    client.create_run(
        thread_id,
        assistant_id=assistant_id,
        input={
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Triage Zoho Desk ticket #{ticket.ticket_number}: "
                        f"{ticket.subject!r}. Start by calling zoho_fetch_ticket."
                    ),
                }
            ]
        },
        # Ticket identity travels here, NOT as a model-visible tool argument.
        # See tools/deps.py for why.
        config={"configurable": {TICKET_CONTEXT_KEY: ctx.to_configurable()}},
        multitask_strategy="reject",
    )
    report.started.append(ticket.id)
