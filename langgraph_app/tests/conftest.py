"""Shared fixtures. Nothing here touches a network, ever.

If a test in this suite ever needs credentials or an internet connection,
that is a bug in the test, not a missing secret.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pagerduty_triage.ledger import InMemoryStore, TicketLedger  # noqa: E402
from pagerduty_triage.settings import Settings  # noqa: E402
from pagerduty_triage.sprint_tasks import FakeSprintTasksClient  # noqa: E402
from pagerduty_triage.tools.deps import (  # noqa: E402
    TICKET_CONTEXT_KEY,
    TicketContext,
    ToolDeps,
)
from pagerduty_triage.zoho.client import Thread, Ticket  # noqa: E402
from pagerduty_triage.zoho.fake import FakeZohoDeskClient  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Settings with both kill switches ON, so tests exercise the real paths.

    Production defaults them off; a test that left them off would assert the
    refusal message instead of the behaviour it means to check.
    """
    return Settings(
        zoho_transport="fake",
        replies_enabled=True,
        issue_creation_enabled=True,
        sprint_tasks_repo="devtron-labs/sprint-tasks",
    )


@pytest.fixture
def ticket() -> Ticket:
    return Ticket(
        id="ticket-1001",
        ticket_number="4242",
        subject="Helm apps not listed for admin user",
        description="<p>Our admin cannot see Helm apps.</p>",
        status="Open",
        created_time="2026-09-16T09:00:00.000Z",
        modified_time="2026-09-16T09:30:00.000Z",
        web_url="https://desk.zoho.com/agent/devtron/tickets/details/1001",
        contact_name="Priya",
        account_name="BigCo",
        email="priya@bigco.example",
    )


@pytest.fixture
def zoho(ticket: Ticket) -> FakeZohoDeskClient:
    client = FakeZohoDeskClient()
    client.add_ticket(
        ticket,
        threads=[
            Thread(
                id="th-1",
                created_time="2026-09-16T09:00:00.000Z",
                direction="in",
                author="Priya",
                content="<p>Admin user sees an empty cluster list.</p>",
            )
        ],
    )
    return client


@pytest.fixture
def ledger() -> TicketLedger:
    return TicketLedger(InMemoryStore())


@pytest.fixture
def sprint() -> FakeSprintTasksClient:
    return FakeSprintTasksClient()


@pytest.fixture
def deps(settings, zoho, sprint, ledger) -> ToolDeps:
    return ToolDeps(settings=settings, zoho=zoho, sprint_tasks=sprint, ledger=ledger)


@pytest.fixture
def config(ticket: Ticket) -> dict[str, Any]:
    """A RunnableConfig carrying the bound ticket, as the poller would send."""
    ctx = TicketContext(
        ticket_id=ticket.id,
        ticket_number=ticket.ticket_number,
        subject=ticket.subject,
        web_url=ticket.web_url,
        email=ticket.email,
    )
    return {"configurable": {TICKET_CONTEXT_KEY: ctx.to_configurable()}}


class GateReached(Exception):
    """Raised by the interrupt spy to stand in for a real graph interrupt.

    A real `interrupt()` suspends the graph by raising `GraphInterrupt`. The
    spy raises this instead, which lets a test assert both that the gate was
    reached AND — because execution stops right there — that nothing below it
    ran.
    """

    def __init__(self, payload: Any):
        super().__init__("gate reached")
        self.payload = payload


@pytest.fixture
def gate_spy(monkeypatch):
    """Control what `interrupt()` does inside the tool modules.

    Usage:
        gate_spy.park()                       # raise GateReached, like a real gate
        gate_spy.answer({"approved": True})   # resume with this value
        gate_spy.payloads                     # every payload passed to interrupt()
    """
    from pagerduty_triage.tools import github_tools, zoho_tools

    class Spy:
        def __init__(self) -> None:
            self.payloads: list[Any] = []
            self._response: Any = None
            self._park = True

        def park(self) -> None:
            self._park = True

        def answer(self, value: Any) -> None:
            self._park = False
            self._response = value

        def __call__(self, payload: Any) -> Any:
            self.payloads.append(payload)
            if self._park:
                raise GateReached(payload)
            return self._response

        @property
        def calls(self) -> int:
            return len(self.payloads)

    spy = Spy()
    monkeypatch.setattr(zoho_tools, "interrupt", spy)
    monkeypatch.setattr(github_tools, "interrupt", spy)
    return spy


@pytest.fixture
def send_reply_tool(deps):
    from pagerduty_triage.tools.zoho_tools import build_zoho_tools

    return {t.name: t for t in build_zoho_tools(deps)}["zoho_send_reply"]


@pytest.fixture
def create_issue_tool(deps):
    from pagerduty_triage.tools.github_tools import build_github_tools

    return {t.name: t for t in build_github_tools(deps)}["create_sprint_issue"]


def call_tool(tool, **kwargs):
    """Invoke a LangChain tool's underlying function directly.

    Bypasses schema coercion so tests exercise the function body, which is
    where the gate lives.
    """
    return tool.func(**kwargs)
