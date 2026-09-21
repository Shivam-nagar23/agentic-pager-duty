"""Posting a parked gate to Slack, once, and never approving one.

The notifier is the only part of this layer with a timer attached to it (a
cron). That makes it the one place where "an unanswered gate parks forever"
could be broken by accident, so it is tested for the *absence* of behaviour as
much as for the presence of it.
"""

from __future__ import annotations

import pytest

from pagerduty_triage.gates import build_gate_payload
from pagerduty_triage.ledger import InMemoryStore
from pagerduty_triage.slack.client import FakeSlackClient
from pagerduty_triage.slack.config import SlackSettings
from pagerduty_triage.slack.notifier import (
    NS_SLACK_POSTS,
    notify_parked_gates,
)
from pagerduty_triage.slack.platform import ParkedGate

THREAD = "11111111-1111-5111-8111-111111111111"
INTERRUPT = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


@pytest.fixture
def settings() -> SlackSettings:
    return SlackSettings(
        bot_token="xoxb-not-real",
        signing_secret="s",
        channel_id="C_PAGER",
        approver_user_ids=frozenset({"U_OWNER"}),
        langgraph_api_url="http://localhost:2024",
    )


def gate_payload(gate="zoho_send_reply"):
    return build_gate_payload(
        gate=gate,
        ticket_id="ticket-1001",
        ticket_subject="Helm apps not listed for admin user",
        zoho_url="https://desk.zoho.com/agent/devtron/tickets/details/1001",
        proposed_action={
            "type": "customer_reply",
            "to": "priya@bigco.example",
            "ticket_number": "4242",
            "reply_text": "Hi Priya, we are on it.",
        },
        reasoning="Answerable without a code change.",
    )


class FakeSource:
    def __init__(self, gates):
        self.gates = gates
        self.calls = 0
        self.fail = None

    def parked_gates(self, *, limit=50):
        self.calls += 1
        if self.fail:
            raise self.fail
        return list(self.gates)


def run(source, slack, store, settings):
    return notify_parked_gates(
        source=source, slack=slack, store=store, settings=settings
    )


def test_a_parked_gate_reaches_the_channel(settings):
    source = FakeSource([ParkedGate(THREAD, INTERRUPT, gate_payload())])
    slack, store = FakeSlackClient(), InMemoryStore()

    report = run(source, slack, store, settings)

    assert report.posted == [INTERRUPT]
    assert len(slack.posted) == 1
    assert slack.posted[0]["channel"] == "C_PAGER"
    assert slack.posted[0]["blocks"], "blocks are where the decision lives"
    assert slack.posted[0]["text"], "the notification fallback is never empty"


def test_the_same_gate_is_not_reposted_every_tick(settings):
    """A two-minute cron re-sees every parked thread forever. Without this the
    reviewer gets the same approval request 720 times a day."""
    source = FakeSource([ParkedGate(THREAD, INTERRUPT, gate_payload())])
    slack, store = FakeSlackClient(), InMemoryStore()

    run(source, slack, store, settings)
    second = run(source, slack, store, settings)

    assert len(slack.posted) == 1
    assert second.skipped_already_posted == [INTERRUPT]


def test_a_second_gate_on_the_same_thread_is_posted(settings):
    """Gate 1 then gate 2 on one ticket are different interrupts, and the
    dedupe key is the interrupt id — not the thread id."""
    store, slack = InMemoryStore(), FakeSlackClient()
    run(
        FakeSource([ParkedGate(THREAD, INTERRUPT, gate_payload())]),
        slack,
        store,
        settings,
    )
    run(
        FakeSource(
            [ParkedGate(THREAD, "second-interrupt", gate_payload("create_sprint_issue"))]
        ),
        slack,
        store,
        settings,
    )

    assert len(slack.posted) == 2


def test_the_posted_record_holds_no_ticket_content(settings):
    """The store is durable and shared. It needs enough to dedupe and to trace
    a message, and nothing a customer wrote."""
    source = FakeSource([ParkedGate(THREAD, INTERRUPT, gate_payload())])
    slack, store = FakeSlackClient(), InMemoryStore()

    run(source, slack, store, settings)

    record = store.get(NS_SLACK_POSTS, INTERRUPT).value
    assert set(record) == {
        "interrupt_id",
        "thread_id",
        "gate",
        "channel",
        "message_ts",
        "posted_at",
    }
    assert "Priya" not in str(record)


def test_one_unrenderable_gate_does_not_block_the_others(settings):
    """One malformed payload must not stop every other reviewer request."""
    broken = ParkedGate(THREAD, "broken", {"version": 1, "gate": "nonsense"})
    good = ParkedGate(THREAD, INTERRUPT, gate_payload())
    slack, store = FakeSlackClient(), InMemoryStore()

    report = run(FakeSource([broken, good]), slack, store, settings)

    assert report.posted == [INTERRUPT]
    assert len(report.errors) == 1


def test_slack_being_down_is_reported_not_raised(settings):
    source = FakeSource([ParkedGate(THREAD, INTERRUPT, gate_payload())])
    slack, store = FakeSlackClient(), InMemoryStore()
    slack.fail_next = RuntimeError("slack is down")

    report = run(source, slack, store, settings)

    assert report.posted == []
    assert report.errors
    # Not recorded as posted, so the next tick tries again.
    assert store.get(NS_SLACK_POSTS, INTERRUPT) is None


def test_a_failed_search_does_not_kill_the_cron(settings):
    source = FakeSource([])
    source.fail = RuntimeError("platform unreachable")

    report = run(source, FakeSlackClient(), InMemoryStore(), settings)

    assert report.errors and report.posted == []


def test_the_notifier_never_resumes_anything():
    """It posts. It does not decide. A notifier that could resume a thread
    would be a timeout that approves."""
    import inspect

    from pagerduty_triage.slack import notifier

    src = inspect.getsource(notifier)
    # Checked structurally rather than by word, so prose about resuming in the
    # module docstring does not make this test lie in either direction.
    for banned in ("resume_gate", "from .resume", "ThreadStateClient", "approved"):
        assert banned not in src, (
            f"notifier.py references {banned!r}; it must be able to post a "
            "gate request and nothing else"
        )
