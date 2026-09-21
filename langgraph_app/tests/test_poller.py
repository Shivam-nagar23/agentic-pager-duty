"""Proof that polling is idempotent and survives overlap, restart and failure.

The scenarios here are the ones that actually happen in production with a
2-minute cron: ticks overlap, a pod restarts mid-loop, Zoho times out, two
pollers run at once because a deploy briefly doubled the replica count.

The fake LangGraph client models the one platform behaviour everything rests
on: `create_thread(..., if_exists="raise")` raises on a duplicate id. If that
behaviour is ever wrong, these tests are worthless — which is why it is
asserted explicitly in `test_fake_client_models_the_conflict`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pagerduty_triage.ledger import (
    InMemoryStore,
    TicketLedger,
    thread_id_for_ticket,
)
from pagerduty_triage.poller import (
    MAX_NEW_THREADS_PER_TICK,
    ThreadConflict,
    poll_once,
)
from pagerduty_triage.settings import Settings
from pagerduty_triage.tools.deps import TICKET_CONTEXT_KEY
from pagerduty_triage.zoho.client import Ticket
from pagerduty_triage.zoho.fake import FakeZohoDeskClient


@dataclass
class FakeLangGraphClient:
    """In-memory stand-in for the Platform API. Models 409 on duplicate id."""

    threads: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)
    #: Shared between instances to simulate two pollers on one backend.
    _shared: dict[str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self._shared is not None:
            self.threads = self._shared

    def create_thread(
        self, thread_id: str, *, metadata: dict[str, Any], if_exists: str
    ) -> dict[str, Any]:
        if thread_id in self.threads:
            if if_exists == "raise":
                raise ThreadConflict(f"thread {thread_id} already exists")
            return self.threads[thread_id]
        self.threads[thread_id] = {"thread_id": thread_id, "metadata": metadata}
        return self.threads[thread_id]

    def bind_ticket_context(
        self, thread_id: str, *, ticket_context: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge onto the thread's metadata, as `threads.update` does.

        Merging rather than replacing matters: the claim metadata written by
        `create_thread` has to survive, and a fake that replaced it would hide
        a real regression.
        """
        thread = self.threads[thread_id]
        thread["metadata"] = {**thread.get("metadata", {}), "ticket_context": ticket_context}
        return thread

    def create_run(
        self,
        thread_id: str,
        *,
        assistant_id: str,
        input: dict[str, Any],
        config: dict[str, Any],
        multitask_strategy: str,
    ) -> dict[str, Any]:
        run = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "input": input,
            "config": config,
            "multitask_strategy": multitask_strategy,
        }
        self.runs.append(run)
        return run


def _recent(offset_seconds: int = 0) -> str:
    """A timestamp inside the poller's overlap window.

    The fake honours `modified_since`, so fixtures must use live timestamps
    or every poll legitimately returns nothing. Using a fixed date here is
    what made the first draft of this suite fail — correctly.
    """
    ts = datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_ticket(n: int) -> Ticket:
    return Ticket(
        id=f"ticket-{n}",
        ticket_number=str(4000 + n),
        subject=f"Issue {n}",
        description="",
        status="Open",
        created_time=_recent(120),
        modified_time=_recent(60),
        web_url=f"https://desk.zoho.com/t/{n}",
        email=f"user{n}@example.com",
    )


@pytest.fixture
def poll_settings() -> Settings:
    return Settings(zoho_transport="fake")


@pytest.fixture
def zoho_with(request):
    def _make(count: int) -> FakeZohoDeskClient:
        client = FakeZohoDeskClient()
        for i in range(count):
            client.add_ticket(make_ticket(i))
        return client

    return _make


def run_poll(zoho, ledger, client, settings):
    return poll_once(zoho=zoho, ledger=ledger, client=client, settings=settings)


# ---------------------------------------------------------------------------
# The fake must model the platform, or none of this means anything
# ---------------------------------------------------------------------------


def test_fake_client_models_the_conflict():
    client = FakeLangGraphClient()
    client.create_thread("t1", metadata={}, if_exists="raise")
    with pytest.raises(ThreadConflict):
        client.create_thread("t1", metadata={}, if_exists="raise")


def test_thread_id_is_a_pure_function_of_the_ticket_id():
    """Two pollers must compute the same id without coordinating."""
    assert thread_id_for_ticket("abc") == thread_id_for_ticket("abc")
    assert thread_id_for_ticket("abc") != thread_id_for_ticket("abd")
    # Must be a UUID: the platform rejects anything else.
    import uuid

    uuid.UUID(thread_id_for_ticket("abc"))


def test_thread_id_requires_a_ticket_id():
    with pytest.raises(ValueError):
        thread_id_for_ticket("")


# ---------------------------------------------------------------------------
# Overlapping ticks
# ---------------------------------------------------------------------------


def test_second_tick_starts_nothing_new(zoho_with, poll_settings):
    """The overlap window re-shows every ticket. Nothing may run twice."""
    zoho = zoho_with(3)
    ledger = TicketLedger(InMemoryStore())
    client = FakeLangGraphClient()

    first = run_poll(zoho, ledger, client, poll_settings)
    second = run_poll(zoho, ledger, client, poll_settings)

    assert len(first.started) == 3
    assert second.started == []
    assert len(second.skipped_seen) == 3
    assert len(client.runs) == 3, "a second run was started for an already-seen ticket"


def test_ten_consecutive_ticks_start_each_ticket_once(zoho_with, poll_settings):
    zoho = zoho_with(5)
    ledger = TicketLedger(InMemoryStore())
    client = FakeLangGraphClient()

    for _ in range(10):
        run_poll(zoho, ledger, client, poll_settings)

    assert len(client.runs) == 5
    assert len(client.threads) == 5


# ---------------------------------------------------------------------------
# Concurrent pollers — the case the ledger alone cannot handle
# ---------------------------------------------------------------------------


def test_two_pollers_sharing_a_backend_start_each_ticket_once(
    zoho_with, poll_settings
):
    """Two replicas, two independent ledgers, one platform backend.

    Each poller's ledger says "unseen", so both attempt the claim. Only one
    can win, because thread creation is a primary-key insert. This is the
    scenario that a store-based `get`-then-`put` claim would fail.
    """
    zoho = zoho_with(4)
    shared_threads: dict[str, dict] = {}
    client_a = FakeLangGraphClient(_shared=shared_threads)
    client_b = FakeLangGraphClient(_shared=shared_threads)
    ledger_a = TicketLedger(InMemoryStore())
    ledger_b = TicketLedger(InMemoryStore())

    report_a = run_poll(zoho, ledger_a, client_a, poll_settings)
    report_b = run_poll(zoho, ledger_b, client_b, poll_settings)

    assert len(report_a.started) == 4
    assert report_b.started == [], "the second poller duplicated work"
    assert len(report_b.skipped_conflict) == 4
    total_runs = len(client_a.runs) + len(client_b.runs)
    assert total_runs == 4


def test_a_conflict_does_not_stop_the_rest_of_the_batch(zoho_with, poll_settings):
    """One contended ticket must not block the other four."""
    zoho = zoho_with(5)
    shared: dict[str, dict] = {}
    other = FakeLangGraphClient(_shared=shared)
    other.create_thread(
        thread_id_for_ticket("ticket-2"), metadata={}, if_exists="raise"
    )

    client = FakeLangGraphClient(_shared=shared)
    report = run_poll(zoho, TicketLedger(InMemoryStore()), client, poll_settings)

    assert len(report.started) == 4
    assert report.skipped_conflict == ["ticket-2"]


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


def test_restart_with_a_fresh_ledger_does_not_restart_work(
    zoho_with, poll_settings
):
    """Process restarts, in-memory ledger is empty, platform state survives.

    The ledger is an optimisation; the platform is the truth. A restart must
    cost 409s, not duplicate runs.
    """
    zoho = zoho_with(3)
    client = FakeLangGraphClient()

    run_poll(zoho, TicketLedger(InMemoryStore()), client, poll_settings)
    assert len(client.runs) == 3

    # Fresh process: brand-new empty ledger, same platform.
    report = run_poll(zoho, TicketLedger(InMemoryStore()), client, poll_settings)

    assert report.started == []
    assert len(report.skipped_conflict) == 3
    assert len(client.runs) == 3


def test_already_replied_tickets_are_skipped_before_any_api_call(
    zoho_with, poll_settings
):
    """A ticket we already answered must never be re-opened by the poller."""
    zoho = zoho_with(2)
    ledger = TicketLedger(InMemoryStore())
    ledger.record_reply(
        "ticket-0", thread_id=thread_id_for_ticket("ticket-0"), content="done"
    )
    client = FakeLangGraphClient()

    report = run_poll(zoho, ledger, client, poll_settings)

    assert report.skipped_replied == ["ticket-0"]
    assert report.started == ["ticket-1"]
    assert thread_id_for_ticket("ticket-0") not in client.threads


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_zoho_failure_skips_the_tick_without_raising(poll_settings):
    """Zoho unreachable: skip, do not crash, do not advance anything."""
    zoho = FakeZohoDeskClient()
    zoho.fail_next = ConnectionError("zoho unreachable")
    client = FakeLangGraphClient()

    report = run_poll(zoho, TicketLedger(InMemoryStore()), client, poll_settings)

    assert report.started == []
    assert report.errors and "list_tickets failed" in report.errors[0]
    assert client.runs == []


def test_one_broken_ticket_does_not_stop_the_others(zoho_with, poll_settings):
    zoho = zoho_with(3)
    ledger = TicketLedger(InMemoryStore())

    class FlakyClient(FakeLangGraphClient):
        def create_run(self, thread_id, **kwargs):
            if thread_id == thread_id_for_ticket("ticket-1"):
                raise RuntimeError("run creation blew up")
            return super().create_run(thread_id, **kwargs)

    client = FlakyClient()
    report = run_poll(zoho, ledger, client, poll_settings)

    assert len(report.started) == 2
    assert len(report.errors) == 1
    assert "ticket-1" in report.errors[0]


def test_fan_out_is_capped(poll_settings):
    """A backlog must not open hundreds of concurrent runs in one tick."""
    zoho = FakeZohoDeskClient()
    for i in range(MAX_NEW_THREADS_PER_TICK + 10):
        zoho.add_ticket(make_ticket(i))

    report = run_poll(
        zoho, TicketLedger(InMemoryStore()), FakeLangGraphClient(), poll_settings
    )

    assert report.capped is True
    assert len(report.started) == MAX_NEW_THREADS_PER_TICK


# ---------------------------------------------------------------------------
# What the run is handed
# ---------------------------------------------------------------------------


def test_run_binds_ticket_identity_in_config(zoho_with, poll_settings):
    """Ticket identity must reach the tools via config, not via the model."""
    zoho = zoho_with(1)
    client = FakeLangGraphClient()
    run_poll(zoho, TicketLedger(InMemoryStore()), client, poll_settings)

    run = client.runs[0]
    ctx = run["config"]["configurable"][TICKET_CONTEXT_KEY]
    assert ctx["ticket_id"] == "ticket-0"
    assert ctx["email"] == "user0@example.com"
    assert run["thread_id"] == thread_id_for_ticket("ticket-0")


def test_run_rejects_rather_than_enqueues_a_concurrent_pass(
    zoho_with, poll_settings
):
    """`enqueue` is the platform default and is wrong for this workload.

    Enqueuing would run a duplicate triage pass after the current one, which
    means a second gate prompt and a second chance to reply.
    """
    zoho = zoho_with(1)
    client = FakeLangGraphClient()
    run_poll(zoho, TicketLedger(InMemoryStore()), client, poll_settings)

    assert client.runs[0]["multitask_strategy"] == "reject"


def test_only_configured_statuses_are_polled():
    """Closed tickets must not be re-triaged."""
    zoho = FakeZohoDeskClient()
    open_ticket = make_ticket(1)
    closed = Ticket(**{**make_ticket(2).__dict__, "status": "Closed"})
    zoho.add_ticket(open_ticket)
    zoho.add_ticket(closed)

    client = FakeLangGraphClient()
    report = run_poll(
        zoho,
        TicketLedger(InMemoryStore()),
        client,
        Settings(zoho_transport="fake", zoho_poll_statuses=("Open",)),
    )

    assert report.started == [open_ticket.id]


# ---------------------------------------------------------------------------
# The customer's own words are bound into the thread at claim time
#
# Gate 1 cannot fetch them later: anything above `interrupt()` runs before a
# human sees the request and again on every resume. So the poller reads them
# once, here, and they ride in `configurable`.
# ---------------------------------------------------------------------------


def _ticket(ticket_id: str) -> Ticket:
    """A ticket with a real description — the customer's original question."""
    return Ticket(
        id=ticket_id,
        ticket_number="4242",
        subject="Helm apps not listed for admin user",
        description="<p>Our admin cannot see the <b>cluster list</b>.</p>",
        status="Open",
        created_time=_recent(120),
        modified_time=_recent(60),
        web_url="https://desk.zoho.com/t/1",
        email="priya@bigco.example",
    )


def test_the_bound_context_carries_the_customers_question():
    from pagerduty_triage.zoho.client import Thread

    zoho = FakeZohoDeskClient()
    zoho.add_ticket(
        _ticket("t-1"),
        threads=[
            Thread(
                id="th-1",
                created_time="2026-09-16T09:00:00.000Z",
                direction="in",
                author="Priya",
                content="<p>Admin sees an <b>empty</b> cluster list.</p>",
            ),
            Thread(
                id="th-2",
                created_time="2026-09-16T09:10:00.000Z",
                direction="out",
                author="Support",
                content="<p>Looking into it.</p>",
            ),
            Thread(
                id="th-3",
                created_time="2026-09-16T09:20:00.000Z",
                direction="in",
                author="Priya",
                content="<p>Any update? We are blocked.</p>",
            ),
        ],
    )
    client = FakeLangGraphClient()
    report = poll_once(
        zoho=zoho,
        ledger=TicketLedger(InMemoryStore()),
        client=client,
        settings=Settings(),
    )

    assert report.started == ["t-1"]
    ctx = client.runs[0]["config"]["configurable"][TICKET_CONTEXT_KEY]
    assert "cluster list" in ctx["description"], (
        "the ticket description is the customer's original question and must "
        "be bound into the thread, HTML stripped"
    )
    assert ctx["customer_messages"] == [
        "Admin sees an empty cluster list.",
        "Any update? We are blocked.",
    ], "later customer messages ride along; the support agent's reply does not"


def test_the_description_is_not_shown_twice():
    """Zoho fills the description and the first inbound thread from the same
    mail. Showing a reviewer the same paragraph twice trains them to skim."""
    from pagerduty_triage.zoho.client import Thread

    zoho = FakeZohoDeskClient()
    zoho.add_ticket(
        _ticket("t-1"),
        threads=[
            Thread(
                id="th-1",
                created_time=_recent(120),
                direction="in",
                author="Priya",
                content="<p>Our admin cannot see the <b>cluster list</b>.</p>",
            )
        ],
    )
    client = FakeLangGraphClient()
    poll_once(
        zoho=zoho,
        ledger=TicketLedger(InMemoryStore()),
        client=client,
        settings=Settings(),
    )

    ctx = client.runs[0]["config"]["configurable"][TICKET_CONTEXT_KEY]
    assert ctx["description"] == "Our admin cannot see the cluster list."
    assert ctx["customer_messages"] == []


def test_a_conversation_read_failure_does_not_stop_triage():
    """A secondary read is best-effort. Refusing to triage a ticket because
    its thread list 404'd would be the wrong trade."""

    class ExplodingThreads(FakeZohoDeskClient):
        def list_threads(self, ticket_id, *, limit=50):
            raise RuntimeError("Zoho said no")

    zoho = ExplodingThreads()
    zoho.add_ticket(_ticket("t-1"))
    client = FakeLangGraphClient()
    report = poll_once(
        zoho=zoho,
        ledger=TicketLedger(InMemoryStore()),
        client=client,
        settings=Settings(),
    )

    assert report.started == ["t-1"]
    ctx = client.runs[0]["config"]["configurable"][TICKET_CONTEXT_KEY]
    assert ctx["customer_messages"] == []
    assert ctx["description"], "the description comes from the ticket, not the threads"


def test_a_very_long_conversation_is_capped_and_says_so():
    from pagerduty_triage.tools.deps import MAX_CARRIED_CUSTOMER_MESSAGES
    from pagerduty_triage.zoho.client import Thread

    zoho = FakeZohoDeskClient()
    zoho.add_ticket(
        _ticket("t-1"),
        threads=[
            Thread(
                id=f"th-{i}",
                created_time="2026-09-16T09:00:00.000Z",
                direction="in",
                author="Priya",
                content=f"message {i}",
            )
            for i in range(40)
        ],
    )
    client = FakeLangGraphClient()
    poll_once(
        zoho=zoho,
        ledger=TicketLedger(InMemoryStore()),
        client=client,
        settings=Settings(),
    )

    ctx = client.runs[0]["config"]["configurable"][TICKET_CONTEXT_KEY]
    assert len(ctx["customer_messages"]) == MAX_CARRIED_CUSTOMER_MESSAGES
    assert ctx["customer_messages_truncated"] is True, (
        "a capped thread must be flagged so the reviewer is told, not shown a "
        "silently partial conversation"
    )
    assert ctx["customer_messages"][-1] == "message 39", "keep the most recent"


# ---------------------------------------------------------------------------
# The one-ticket manual trigger takes exactly the same route
# ---------------------------------------------------------------------------


def test_start_ticket_opens_the_same_thread_the_poller_would():
    from pagerduty_triage.poller import start_ticket

    zoho = FakeZohoDeskClient()
    zoho.add_ticket(_ticket("t-1"))
    client = FakeLangGraphClient()
    ledger = TicketLedger(InMemoryStore())

    report = start_ticket(
        ticket_id="t-1", zoho=zoho, ledger=ledger, client=client
    )

    assert report.started == ["t-1"]
    assert list(client.threads) == [thread_id_for_ticket("t-1")]
    assert client.runs[0]["multitask_strategy"] == "reject"
    assert ledger.is_claimed("t-1"), "the manual path writes the same ledger"


def test_starting_the_same_ticket_twice_by_hand_starts_it_once():
    """The ledger is a skip, the thread insert is the mutex. Both hold here."""
    from pagerduty_triage.poller import start_ticket

    zoho = FakeZohoDeskClient()
    zoho.add_ticket(_ticket("t-1"))
    client = FakeLangGraphClient()
    ledger = TicketLedger(InMemoryStore())

    start_ticket(ticket_id="t-1", zoho=zoho, ledger=ledger, client=client)
    second = start_ticket(ticket_id="t-1", zoho=zoho, ledger=ledger, client=client)

    assert second.started == []
    assert len(client.runs) == 1


def test_starting_by_hand_with_a_cold_ledger_still_starts_once():
    """The realistic local case: a fresh `python -m` process, empty ledger.

    Layer 2 — `create_thread(if_exists="raise")` — is what holds, and it is
    the only layer that was ever load-bearing.
    """
    from pagerduty_triage.poller import start_ticket

    zoho = FakeZohoDeskClient()
    zoho.add_ticket(_ticket("t-1"))
    shared = FakeLangGraphClient()

    start_ticket(
        ticket_id="t-1",
        zoho=zoho,
        ledger=TicketLedger(InMemoryStore()),
        client=shared,
    )
    second = start_ticket(
        ticket_id="t-1",
        zoho=zoho,
        ledger=TicketLedger(InMemoryStore()),  # a brand-new process
        client=shared,
    )

    assert second.skipped_conflict == ["t-1"]
    assert len(shared.runs) == 1


def test_start_ticket_on_an_unknown_id_reports_and_does_nothing():
    from pagerduty_triage.poller import start_ticket

    client = FakeLangGraphClient()
    report = start_ticket(
        ticket_id="nope",
        zoho=FakeZohoDeskClient(),
        ledger=TicketLedger(InMemoryStore()),
        client=client,
    )

    assert report.started == []
    assert report.errors and "nope" in report.errors[0]
    assert client.threads == {}


def test_the_ticket_context_is_bound_to_the_thread_not_only_to_the_run():
    """Run-level `configurable` does not survive into later runs, and a gate
    resume is a later run. If the context lives only on the first run, every
    Slack approval re-enters the graph with no ticket bound and dies — after
    consuming the gate. Two live tickets were spent exactly this way."""
    zoho = FakeZohoDeskClient()
    zoho.add_ticket(make_ticket(0))
    client = FakeLangGraphClient()
    poll_once(
        zoho=zoho,
        ledger=TicketLedger(InMemoryStore()),
        client=client,
        settings=Settings(),
    )

    thread = next(iter(client.threads.values()))
    bound = thread["metadata"]["ticket_context"]

    run_config = client.runs[0]["config"]["configurable"]["ticket_context"]
    assert bound == run_config, (
        "the thread must carry the same context the run was given, or a "
        "resume cannot reconstruct it"
    )


def test_binding_the_context_does_not_erase_the_claim_metadata():
    """The claim metadata is how a ticket is traced back from a thread."""
    zoho = FakeZohoDeskClient()
    zoho.add_ticket(make_ticket(0))
    client = FakeLangGraphClient()
    poll_once(
        zoho=zoho,
        ledger=TicketLedger(InMemoryStore()),
        client=client,
        settings=Settings(),
    )

    metadata = next(iter(client.threads.values()))["metadata"]

    assert metadata["zoho_ticket_id"]
    assert metadata["ticket_context"]
