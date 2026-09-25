"""Only triage tickets created after a cutover instant.

Pointing the poller at a production desk for the first time, the backlog is the
problem. The 10-minute `modifiedTimeRange` window does not solve it: a customer
replying to a three-day-old ticket bumps its `modifiedTime`, so it arrives
looking exactly like new work. On a live queue that is a steady trickle of old
tickets being triaged, each costing a model run and a Slack card.

`ZOHO_MIN_CREATED_AT` draws a line. Tickets created before it are skipped
whatever their modified time. Unset, nothing changes.
"""

from __future__ import annotations

from pagerduty_triage.poller import poll_once
from pagerduty_triage.settings import load_settings
from langgraph.store.memory import InMemoryStore

from pagerduty_triage.ledger import TicketLedger
from pagerduty_triage.zoho.client import Ticket
from pagerduty_triage.zoho.fake import FakeZohoDeskClient

from tests.test_poller import FakeLangGraphClient


def _ticket(fake, *, tid: str, created: str) -> None:
    fake.add_ticket(
        Ticket(
            id=tid,
            ticket_number=tid,
            subject=f"ticket {tid}",
            description="body",
            status="Open",
            created_time=created,
            modified_time="2026-09-25T12:00:00.000Z",
            web_url=f"https://desk.example/{tid}",
        )
    )


def _run(monkeypatch, *, cutover: str | None):
    if cutover is None:
        monkeypatch.delenv("ZOHO_MIN_CREATED_AT", raising=False)
    else:
        monkeypatch.setenv("ZOHO_MIN_CREATED_AT", cutover)

    fake = FakeZohoDeskClient()
    _ticket(fake, tid="old", created="2026-09-01T10:00:00.000Z")
    _ticket(fake, tid="new", created="2026-09-25T10:00:00.000Z")

    return poll_once(
        zoho=fake,
        ledger=TicketLedger(InMemoryStore()),
        client=FakeLangGraphClient(),
        settings=load_settings(),
    )


def test_without_a_cutover_everything_is_considered(monkeypatch):
    report = _run(monkeypatch, cutover=None)
    assert set(report.started) == {"old", "new"}
    assert report.skipped_old == []


def test_tickets_created_before_the_cutover_are_skipped(monkeypatch):
    report = _run(monkeypatch, cutover="2026-09-20T00:00:00Z")
    assert report.started == ["new"]
    assert report.skipped_old == ["old"]


def test_skipped_old_is_reported_not_silent(monkeypatch):
    """The count belongs in the summary. A poller that silently drops most of
    what it sees is indistinguishable from a broken query."""
    report = _run(monkeypatch, cutover="2026-09-20T00:00:00Z")
    assert "skipped_old=1" in report.summary()


def test_a_ticket_with_no_created_time_is_not_dropped(monkeypatch):
    """Missing data must not silently exclude a ticket: dropping real work is
    worse than triaging one stale ticket."""
    fake = FakeZohoDeskClient()
    _ticket(fake, tid="blank", created="")
    monkeypatch.setenv("ZOHO_MIN_CREATED_AT", "2026-09-20T00:00:00Z")

    report = poll_once(
        zoho=fake,
        ledger=TicketLedger(InMemoryStore()),
        client=FakeLangGraphClient(),
        settings=load_settings(),
    )
    assert report.started == ["blank"]
