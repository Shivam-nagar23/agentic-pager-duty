"""Cross-thread bookkeeping: which tickets we have seen, and which we replied to.

**Double-replying to a paying customer is the worst thing this system can do.**
It is worse than missing a ticket, worse than a wrong classification, and
worse than a bad draft, because the other three are recoverable by a human and
this one is already in the customer's inbox twice.

So it is defended in four layers:

1. **One thread per ticket, deterministically named.** The thread id is
   `uuid5(NAMESPACE, "zoho-ticket:<id>")` — a pure function of the ticket id.
   Two overlapping cron ticks that both see ticket 12345 compute the *same*
   thread id, so they converge on one thread instead of racing to open two.

2. **`threads.create(..., if_exists="raise")` is the atomic claim.** This is
   the mutex, and it is the only real one in the system. Thread creation is a
   single database insert against a primary key: the first caller wins, the
   second gets HTTP 409. Two pollers racing on the same ticket cannot both
   win, because Postgres will not let them. The poller treats 409 as "someone
   else has this ticket" and moves on.

3. **`multitask_strategy="reject"` on run creation.** Belt to the braces
   above: if a run is already in flight on that thread, creating a second is
   rejected rather than — as the platform default `enqueue` would do —
   silently queued to run afterwards. The default is actively wrong for this
   workload and must be overridden explicitly.

4. **This ledger's reply log, at the send site.** Layers 1–3 all live in the
   orchestration layer and all guard against *concurrency*. They do nothing
   about *replay*, which is the other way a second mail gets sent. The reply
   guard lives inside `zoho_send_reply`, immediately before the network call:
   if a reply for this ticket is already recorded, the tool refuses and says
   so.

## What this ledger is NOT

**It is not the claim mechanism, because it cannot be.** `BaseStore.put` is
last-write-wins: there is no compare-and-set, no conditional put, no
`if_not_exists`, and `batch()` carries no atomicity guarantee. A
read-then-write claim built on it (`get` → absent → `put`) is racy, and under
a 2-minute cron with retries and overlap two pollers would both read "unseen"
and both proceed. Building the claim here would have produced a mutex that
looks correct in tests and fails in production. Layer 2 does the claiming.

What this ledger stores is therefore either (a) a *record* of something that
already happened exactly once — the reply log, the issue log — where
last-write-wins is harmless because the second write is identical, or (b) an
observability breadcrumb. Never a lock.

## The failure this is really aimed at

Not concurrency — layers 1 and 2 handle that. The dangerous case is
*sequential replay*: the tool sends the reply, and then the process dies
before LangGraph commits the checkpoint recording that it sent. On restart
LangGraph faithfully re-runs the tool from the top, the human's approval is
already in the resume value, and the tool would send the mail a second time.

The reply record is written **immediately after** the send returns and is
keyed on ticket id, so the replayed execution finds it and refuses. The
window between "Zoho accepted the mail" and "we wrote the record" is the
residual risk, and it is small and honest. It is not zero. See the README.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

#: Stable namespace so thread ids are reproducible across processes, restarts
#: and deployments. Must never change: changing it re-opens every ticket.
TICKET_THREAD_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

#: Store namespaces. Tuples, per the BaseStore convention.
NS_SEEN = ("pagerduty", "seen_tickets")
NS_REPLIES = ("pagerduty", "replies")
NS_ISSUES = ("pagerduty", "issues")


def thread_id_for_ticket(ticket_id: str) -> str:
    """The one and only thread id for a Zoho ticket.

    Deterministic on purpose: it is what makes "open a thread for this ticket"
    idempotent without needing a lookup first. LangGraph thread ids must be
    UUIDs, so uuid5 rather than a readable string.
    """
    if not ticket_id:
        raise ValueError("ticket_id is required to derive a thread id")
    return str(uuid.uuid5(TICKET_THREAD_NAMESPACE, f"zoho-ticket:{ticket_id}"))


def content_fingerprint(text: str) -> str:
    """Short stable hash of reply text, recorded alongside a sent reply.

    Lets a human answer "is this the same mail we already sent, or a
    different one?" when investigating, without storing the full body.
    """
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StoreLike(Protocol):
    """The slice of ``langgraph.store.base.BaseStore`` this module uses.

    Narrowed to three methods so tests can substitute a dict and so the code
    does not quietly depend on store features the managed runtime may not
    provide.
    """

    def get(self, namespace: tuple[str, ...], key: str) -> Any: ...

    def put(
        self, namespace: tuple[str, ...], key: str, value: dict[str, Any]
    ) -> None: ...

    def delete(self, namespace: tuple[str, ...], key: str) -> None: ...


@dataclass
class InMemoryStore:
    """A dict-backed ``StoreLike`` for tests and local runs. Never networked."""

    data: dict[tuple[tuple[str, ...], str], dict[str, Any]] = field(
        default_factory=dict
    )

    def get(self, namespace: tuple[str, ...], key: str) -> Any:
        value = self.data.get((namespace, key))
        if value is None:
            return None
        # Mimic BaseStore.get, which returns an Item with a .value attribute.
        return _Item(value)

    def put(self, namespace: tuple[str, ...], key: str, value: dict[str, Any]) -> None:
        self.data[(namespace, key)] = dict(value)

    def delete(self, namespace: tuple[str, ...], key: str) -> None:
        self.data.pop((namespace, key), None)


@dataclass
class _Item:
    value: dict[str, Any]


def _unwrap(item: Any) -> dict[str, Any] | None:
    """BaseStore.get returns an Item; tests may hand back a bare dict."""
    if item is None:
        return None
    return getattr(item, "value", item)


class TicketLedger:
    """Durable per-ticket bookkeeping, shared across every thread."""

    def __init__(self, store: StoreLike):
        self._store = store

    # -- seen log (observability, NOT a lock) --------------------------------

    def is_claimed(self, ticket_id: str) -> bool:
        """Have we recorded a thread for this ticket?

        An **optimization and an audit trail, not a gate.** A `True` here lets
        the poller skip an API call; a `False` costs one wasted
        `threads.create` that returns 409. Correctness never depends on this
        answer — see the module docstring. Do not add a code path that treats
        `False` as permission to act.
        """
        return _unwrap(self._store.get(NS_SEEN, ticket_id)) is not None

    def record_seen(
        self, ticket_id: str, *, thread_id: str, modified_time: str = ""
    ) -> None:
        """Note that a thread exists for this ticket.

        Written *after* `threads.create` has actually won the claim, so this
        record can only ever describe something that really happened. Written
        before the run is created, so a crash in between leaves a record with
        no run — the ticket looks started when it is not.

        That is the deliberate trade. The recovery for a stalled ticket is a
        human noticing an unanswered customer; the recovery for a double-reply
        is apologising to one. We bias toward the failure a human can see.
        `seen_at` exists so a sweeper can surface threads that never ran —
        that sweeper is not built (README TODOs).
        """
        self._store.put(
            NS_SEEN,
            ticket_id,
            {
                "ticket_id": ticket_id,
                "thread_id": thread_id,
                "seen_at": _now(),
                "modified_time": modified_time,
            },
        )

    def seen_info(self, ticket_id: str) -> dict[str, Any] | None:
        return _unwrap(self._store.get(NS_SEEN, ticket_id))

    def forget(self, ticket_id: str) -> None:
        """Drop the seen record. For operator recovery only; nothing calls this.

        Note this does NOT let the ticket be reprocessed — the thread still
        exists, so `threads.create` still 409s. Deleting the thread is the
        real reset, and the reply log still guards the send.
        """
        self._store.delete(NS_SEEN, ticket_id)

    # -- reply log (layer 4 — the load-bearing one) --------------------------

    def reply_record(self, ticket_id: str) -> dict[str, Any] | None:
        """The recorded reply for this ticket, or None."""
        return _unwrap(self._store.get(NS_REPLIES, ticket_id))

    def has_replied(self, ticket_id: str) -> bool:
        return self.reply_record(ticket_id) is not None

    def record_reply(
        self, ticket_id: str, *, thread_id: str, content: str, zoho_thread_id: str = ""
    ) -> None:
        """Record that a reply went out. Call immediately after the send."""
        self._store.put(
            NS_REPLIES,
            ticket_id,
            {
                "ticket_id": ticket_id,
                "thread_id": thread_id,
                "zoho_thread_id": zoho_thread_id,
                "fingerprint": content_fingerprint(content),
                "sent_at": _now(),
            },
        )

    # -- issue log (same idea, lower stakes) ---------------------------------

    def issue_record(self, ticket_id: str) -> dict[str, Any] | None:
        return _unwrap(self._store.get(NS_ISSUES, ticket_id))

    def has_issue(self, ticket_id: str) -> bool:
        return self.issue_record(ticket_id) is not None

    def record_issue(
        self, ticket_id: str, *, thread_id: str, issue_number: int, issue_url: str
    ) -> None:
        """Record that a sprint-tasks issue exists for this ticket.

        A duplicate issue is far less costly than a duplicate reply — but it
        does trigger the GitHub Action twice, which means two agents localizing
        and two draft PRs on the same bug. Worth preventing.
        """
        self._store.put(
            NS_ISSUES,
            ticket_id,
            {
                "ticket_id": ticket_id,
                "thread_id": thread_id,
                "issue_number": issue_number,
                "issue_url": issue_url,
                "created_at": _now(),
            },
        )
