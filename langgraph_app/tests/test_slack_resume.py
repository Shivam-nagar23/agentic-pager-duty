"""The return path: exactly-once resume.

The failure this suite is aimed at is not an attacker. It is the owner
clicking Approve twice because Slack felt slow, or Slack redelivering its own
interaction. The cost is a customer receiving two emails or a duplicate sprint
issue — the happy path going wrong.
"""

from __future__ import annotations

import pytest

from pagerduty_triage.slack.resume import (
    AlreadyAnswered,
    FakeThreadStateClient,
    ResumeConflict,
    ResumeFailed,
    resume_gate,
)

THREAD = "11111111-1111-5111-8111-111111111111"
INTERRUPT = "int-abc"


def _client() -> FakeThreadStateClient:
    client = FakeThreadStateClient()
    client.park(THREAD, INTERRUPT)
    return client


def _resume(client, **overrides):
    kwargs = dict(
        client=client,
        thread_id=THREAD,
        interrupt_id=INTERRUPT,
        assistant_id="triage",
        approved=True,
        reason="",
        approver_user_id="U_OWNER",
    )
    kwargs.update(overrides)
    return resume_gate(**kwargs)


def test_approve_resumes_with_the_envelope_the_gate_accepts():
    client = _client()

    _resume(client)

    assert len(client.resumes) == 1
    value = client.resumes[0]["value"]
    # This must be the exact shape GateDecision.parse treats as approval.
    assert value["approved"] is True
    from pagerduty_triage.gates import GateDecision

    assert GateDecision.parse(value).approved is True


def test_approver_identity_travels_into_the_thread():
    client = _client()

    _resume(client, approver_user_id="U_OWNER")

    from pagerduty_triage.gates import GateDecision

    decision = GateDecision.parse(client.resumes[0]["value"])
    assert decision.extra["approver"] == "U_OWNER"
    assert decision.extra["approved_via"] == "slack"


def test_double_click_resumes_exactly_once():
    """The load-bearing test. Second click must not reach the platform."""
    client = _client()

    _resume(client)
    with pytest.raises(AlreadyAnswered):
        _resume(client)

    assert len(client.resumes) == 1, (
        "a second click created a second resume run. On gate 1 that is a "
        "second email to a paying customer."
    )


def test_simultaneous_clicks_resume_once():
    """Both clicks pass the precondition; the platform's 409 is the backstop."""
    client = _client()
    client.conflict_on_next = True

    with pytest.raises(AlreadyAnswered):
        _resume(client)

    assert client.resumes == []
    assert client.pending_interrupt_ids(THREAD) == [INTERRUPT], (
        "a rejected resume must leave the thread parked, not consume it"
    )


def test_stale_message_for_a_finished_thread_is_refused():
    client = FakeThreadStateClient()  # nothing parked at all

    with pytest.raises(AlreadyAnswered):
        _resume(client)

    assert client.resumes == []


def test_click_on_a_different_interrupt_is_refused():
    """A second gate parked on the same thread must not be answered by the
    first gate's message."""
    client = FakeThreadStateClient()
    client.park(THREAD, "int-second-gate")

    with pytest.raises(AlreadyAnswered):
        _resume(client, interrupt_id="int-first-gate")

    assert client.resumes == []


def test_reject_carries_its_reason():
    client = _client()

    _resume(client, approved=False, reason="Too technical. Drop the stack trace.")

    from pagerduty_triage.gates import GateDecision

    decision = GateDecision.parse(client.resumes[0]["value"])
    assert decision.approved is False
    assert decision.reason == "Too technical. Drop the stack trace."


def test_resume_conflict_is_not_swallowed_into_an_approval():
    """A platform error must never be reported as a successful approval."""
    client = _client()
    client.conflict_on_next = True

    with pytest.raises(AlreadyAnswered):
        _resume(client, approved=True)

    assert client.resumes == []


# ---------------------------------------------------------------------------
# A failed resume must not consume the gate
#
# The exactly-once guarantee has a mirror image that is just as load-bearing
# and was missing: *at-least-once-until-it-works*. A click that achieves
# nothing must leave the gate exactly as it found it, and must be
# distinguishable from a click that was correctly ignored — otherwise the only
# honest report ("already answered") is a lie that strands the ticket.
# ---------------------------------------------------------------------------


def test_ambiguous_pending_interrupts_is_a_failure_not_an_answer():
    """Refusing to guess is the right call; calling it "already answered" is
    not. This is the exact shape that stranded ticket #101 — a real interrupt
    id alongside a task id misread as a second one."""
    client = _client()
    client.park(THREAD, "int-phantom")

    with pytest.raises(ResumeFailed) as caught:
        _resume(client)

    assert not isinstance(caught.value, AlreadyAnswered), (
        "an ambiguity the reviewer can recover from must never be reported "
        "as a gate that is already closed"
    )
    assert client.resumes == []
    assert INTERRUPT in client.pending_interrupt_ids(THREAD), (
        "the gate must still be parked, and therefore still answerable"
    )


def test_a_transient_platform_failure_leaves_the_gate_answerable():
    client = _client()
    client.fail_next_resume_with = RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        _resume(client)

    assert client.resumes == []
    assert client.pending_interrupt_ids(THREAD) == [INTERRUPT]


def test_clicking_again_after_a_failure_works():
    """The whole point of the fix: one bad click must not cost the ticket."""
    client = _client()
    client.fail_next_resume_with = RuntimeError("connection reset")
    with pytest.raises(RuntimeError):
        _resume(client)

    outcome = _resume(client)

    assert outcome.approved is True
    assert len(client.resumes) == 1
    assert client.pending_interrupt_ids(THREAD) == [], (
        "the retry must actually consume the gate, not just avoid erroring"
    )


def test_a_successful_resume_is_still_unrepeatable():
    """Retryability must not have been bought by weakening idempotency."""
    client = _client()
    _resume(client)

    with pytest.raises(AlreadyAnswered):
        _resume(client)

    assert len(client.resumes) == 1, "the gate was answered twice"


def test_failure_then_success_then_a_third_click_is_still_refused():
    """The full sequence, in one test: fail, retry, and the double-click
    guard still holds afterwards."""
    client = _client()
    client.fail_next_resume_with = RuntimeError("connection reset")
    with pytest.raises(RuntimeError):
        _resume(client)
    _resume(client)

    with pytest.raises(AlreadyAnswered):
        _resume(client)

    assert len(client.resumes) == 1
