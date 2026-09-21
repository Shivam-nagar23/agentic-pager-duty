"""The authorization boundary, end to end through the handler.

This endpoint is the thing standing between an agent's draft and a paying
customer's inbox. Every test in this file is a way past it that must stay
shut, exercised the way Slack would actually exercise it: a real signed
``application/x-www-form-urlencoded`` body with a ``payload=`` field.

Nothing here touches a network. Slack and LangGraph are both fakes.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from pagerduty_triage.slack.client import FakeSlackClient
from pagerduty_triage.slack.config import SlackSettings
from pagerduty_triage.slack.handler import handle_interaction
from pagerduty_triage.slack.renderer import (
    ACTION_APPROVE,
    ACTION_REJECT,
    REJECT_REASONS,
    context_block_id,
)
from pagerduty_triage.slack.resume import FakeThreadStateClient
from pagerduty_triage.slack.signing import signature_for

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
NOW = 1_700_000_000.0
THREAD = "11111111-1111-5111-8111-111111111111"
INTERRUPT = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
OWNER = "U_OWNER"
STRANGER = "U_STRANGER"
RESPONSE_URL = "https://hooks.slack.com/actions/T0/1/abc"


@pytest.fixture
def settings() -> SlackSettings:
    return SlackSettings(
        bot_token="xoxb-not-a-real-token",
        signing_secret=SECRET,
        channel_id="C_PAGER",
        approver_user_ids=frozenset({OWNER}),
        langgraph_api_url="http://localhost:2024",
        triage_assistant_id="triage",
    )


@pytest.fixture
def slack() -> FakeSlackClient:
    return FakeSlackClient()


@pytest.fixture
def threads() -> FakeThreadStateClient:
    client = FakeThreadStateClient()
    client.park(THREAD, INTERRUPT)
    return client


# ---------------------------------------------------------------------------
# building a request exactly as Slack builds one
# ---------------------------------------------------------------------------


def block_actions_body(
    *,
    user_id: str = OWNER,
    action_id: str = ACTION_APPROVE,
    gate: str = "zoho_send_reply",
    reason_code: str | None = None,
    thread_id: str = THREAD,
    interrupt_id: str = INTERRUPT,
) -> bytes:
    """A ``block_actions`` payload, form-encoded the way Slack POSTs it."""
    action: dict = {
        "type": "button" if action_id == ACTION_APPROVE else "static_select",
        "action_id": action_id,
        "block_id": context_block_id(
            thread_id=thread_id, interrupt_id=interrupt_id, gate=gate
        ),
        "action_ts": "1700000000.000100",
    }
    if action_id == ACTION_APPROVE:
        action["value"] = "approve"
    else:
        action["selected_option"] = {
            "text": {"type": "plain_text", "text": "…"},
            "value": reason_code,
        }

    payload = {
        "type": "block_actions",
        "user": {"id": user_id, "username": "someone", "team_id": "T0"},
        "api_app_id": "A0",
        "team": {"id": "T0", "domain": "devtron"},
        "channel": {"id": "C_PAGER", "name": "pager-approvals"},
        "container": {"type": "message", "message_ts": "1700000000.000001"},
        "trigger_id": "13345224609.738474920.8088930838d88f008e0",
        "response_url": RESPONSE_URL,
        "actions": [action],
    }
    return urllib.parse.urlencode({"payload": json.dumps(payload)}).encode("utf-8")


def sign(body: bytes, *, at: float = NOW, secret: str = SECRET) -> dict[str, str]:
    stamp = str(int(at))
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": signature_for(
            signing_secret=secret, timestamp=stamp, body=body
        ),
    }


def post(settings, slack, threads, body, headers=None, now=NOW, run_followup=True):
    """Deliver one request and, by default, run the background task.

    The handler returns its 200 before resuming — see the three-second rule —
    so a test that never ran the followup would be asserting on a request that
    had not finished.
    """
    result = handle_interaction(
        raw_body=body,
        headers=sign(body) if headers is None else headers,
        settings=settings,
        slack=slack,
        threads=threads,
        now=now,
    )
    if run_followup and result.followup:
        result.followup()
    return result


# ---------------------------------------------------------------------------
# 1. Signature verification — fail closed
# ---------------------------------------------------------------------------


def test_unsigned_request_is_rejected_and_resumes_nothing(settings, slack, threads):
    body = block_actions_body()

    result = post(settings, slack, threads, body, headers={})

    assert result.status == 401
    assert threads.resumes == [], (
        "an unsigned request resumed a thread. This endpoint is public; that "
        "is an internet stranger emailing a paying customer."
    )
    assert slack.responses == []
    assert result.followup is None, (
        "an unverified request must not schedule outbound work either"
    )


def test_stale_request_is_rejected(settings, slack, threads):
    body = block_actions_body()

    result = post(settings, slack, threads, body, now=NOW + 60 * 6)

    assert result.status == 401
    assert threads.resumes == []


def test_tampered_body_is_rejected(settings, slack, threads):
    """The classic attack: capture a genuine signed approval, swap the user id
    for your own — or the thread id for a different customer's — and replay."""
    genuine = block_actions_body(user_id=OWNER)
    headers = sign(genuine)
    forged = block_actions_body(user_id=STRANGER)

    result = post(settings, slack, threads, forged, headers=headers)

    assert result.status == 401
    assert threads.resumes == []


def test_wrong_signing_secret_is_rejected(settings, slack, threads):
    body = block_actions_body()

    result = post(
        settings, slack, threads, body, headers=sign(body, secret="wrong-secret")
    )

    assert result.status == 401
    assert threads.resumes == []


def test_deployment_with_no_signing_secret_accepts_nothing(slack, threads):
    """A missing env var must close the endpoint, not open it."""
    unconfigured = SlackSettings(
        signing_secret="", approver_user_ids=frozenset({OWNER})
    )
    body = block_actions_body()

    result = post(unconfigured, slack, threads, body, headers=sign(body, secret=""))

    assert result.status == 401
    assert threads.resumes == []


# ---------------------------------------------------------------------------
# 2. The approver allowlist
# ---------------------------------------------------------------------------


def test_non_approver_is_refused_visibly_and_resumes_nothing(
    settings, slack, threads
):
    body = block_actions_body(user_id=STRANGER)

    result = post(settings, slack, threads, body)

    assert result.outcome == "not_approver"
    assert threads.resumes == [], (
        "someone who is not an approver resumed a thread from inside Slack"
    )
    assert threads.pending_interrupt_ids(THREAD) == [INTERRUPT], (
        "the gate must still be parked after a refused click"
    )
    # Visibly refused, not silently dropped — otherwise the clicker assumes it
    # worked and nobody ever approves it.
    assert len(slack.responses) == 1
    sent = slack.responses[0]["body"]
    assert sent["response_type"] == "ephemeral", (
        "a refusal must go only to the clicker, not into the channel"
    )
    assert STRANGER in sent["text"]


def test_empty_allowlist_approves_nothing(slack, threads):
    """An unset SLACK_APPROVER_USER_ID means nobody, never everybody."""
    no_approvers = SlackSettings(
        signing_secret=SECRET, approver_user_ids=frozenset()
    )
    body = block_actions_body(user_id=OWNER)

    result = post(no_approvers, slack, threads, body)

    assert result.outcome == "not_approver"
    assert threads.resumes == []


def test_a_non_approver_cannot_probe_for_thread_ids(settings, slack, threads):
    """Authorization happens before the thread is looked at, so a refused
    click cannot be used to learn whether a thread id is real."""
    body = block_actions_body(user_id=STRANGER, thread_id="does-not-exist")

    result = post(settings, slack, threads, body)

    assert result.outcome == "not_approver"
    assert result.detail == STRANGER  # the thread id is never echoed back
    assert threads.resumes == []


# ---------------------------------------------------------------------------
# 3. The happy path — and exactly once
# ---------------------------------------------------------------------------


def test_approver_click_resumes_once_with_the_right_envelope(
    settings, slack, threads
):
    body = block_actions_body(user_id=OWNER, action_id=ACTION_APPROVE)

    result = post(settings, slack, threads, body)

    assert result.status == 200
    assert result.outcome == "approved"
    assert len(threads.resumes) == 1

    from pagerduty_triage.gates import GateDecision

    decision = GateDecision.parse(threads.resumes[0]["value"])
    assert decision.approved is True
    assert decision.extra["approver"] == OWNER
    assert threads.resumes[0]["assistant_id"] == "triage"


def test_double_click_resumes_exactly_once(settings, slack, threads):
    """The reviewer clicks twice because Slack felt slow. On gate 1 a second
    resume is a second email to a paying customer."""
    body = block_actions_body()

    first = post(settings, slack, threads, body)
    second = post(settings, slack, threads, body)

    assert first.outcome == "approved"
    assert len(threads.resumes) == 1, "the second click resumed the thread again"
    # The second click is answered, not ignored in silence.
    assert any(
        "already been answered" in r["body"].get("text", "")
        for r in slack.responses
    )
    assert second.status == 200


def test_slack_redelivering_the_same_request_resumes_once(
    settings, slack, threads
):
    """Byte-identical redelivery — same signature, same timestamp — is the
    same condition as a double-click and gets the same answer."""
    body = block_actions_body()
    headers = sign(body)

    post(settings, slack, threads, body, headers=headers)
    post(settings, slack, threads, body, headers=headers)

    assert len(threads.resumes) == 1


def test_two_reviewers_clicking_at_once_resume_once(settings, slack, threads):
    """Both pass the pending-interrupt precondition; the platform's 409 on
    `multitask_strategy="reject"` is the backstop, and it must not be mistaken
    for success."""
    threads.conflict_on_next = True
    body = block_actions_body()

    result = post(settings, slack, threads, body)

    assert result.outcome == "approved"  # the click was authorized...
    assert threads.resumes == []          # ...but the platform refused it
    assert threads.pending_interrupt_ids(THREAD) == [INTERRUPT]
    assert any(
        "already been answered" in r["body"].get("text", "")
        for r in slack.responses
    )


def test_answered_message_replaces_the_original(settings, slack, threads):
    """Leaving live buttons on an answered gate invites the second click this
    whole file exists to prevent."""
    post(settings, slack, threads, block_actions_body())

    replacement = slack.responses[-1]["body"]
    assert replacement["replace_original"] == "true"
    assert OWNER in replacement["text"]


# ---------------------------------------------------------------------------
# 4. Rejection carries a reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(REJECT_REASONS["zoho_send_reply"]))
def test_every_rejection_carries_an_actionable_reason(
    settings, slack, threads, code
):
    body = block_actions_body(action_id=ACTION_REJECT, reason_code=code)

    result = post(settings, slack, threads, body)

    assert result.outcome == "rejected"
    from pagerduty_triage.gates import GateDecision

    value = threads.resumes[0]["value"]
    decision = GateDecision.parse(value)
    assert decision.approved is False
    assert decision.reason == REJECT_REASONS["zoho_send_reply"][code]
    assert len(decision.reason) > 30, (
        "the agent is told to revise against this string; a terse one gives it "
        "nothing to act on"
    )


def test_a_rejection_never_resumes_with_none(settings, slack, threads):
    """The langgraph_sdk strips None: `command={"resume": None}` serialises to
    `{"command": {}}`, which is not a decision at all. A rejection that
    silently became a no-op resume would leave the thread parked and the
    reviewer believing they had answered it."""
    body = block_actions_body(action_id=ACTION_REJECT, reason_code="tone")

    post(settings, slack, threads, body)

    value = threads.resumes[0]["value"]
    assert value is not None
    assert value != {}
    assert value["approved"] is False
    assert value["reason"]

    # And the real platform client refuses to send such a value at all.
    from pagerduty_triage.slack.platform import _reject_none

    with pytest.raises(ValueError):
        _reject_none(None)
    with pytest.raises(ValueError):
        _reject_none({})


def test_rejection_is_reported_back_with_its_reason(settings, slack, threads):
    body = block_actions_body(action_id=ACTION_REJECT, reason_code="is_bug")

    post(settings, slack, threads, body)

    text = slack.responses[-1]["body"]["text"]
    assert "Rejected" in text
    assert "platform bug" in text


def test_reject_menu_has_no_reasonless_option():
    """`gates.py` turns a reasonless rejection into 'rejected without a
    reason', which the agent cannot revise against. There must be no way to
    produce one from Slack."""
    for gate, reasons in REJECT_REASONS.items():
        assert reasons, f"{gate} has no rejection reasons at all"
        for code, text in reasons.items():
            assert text.strip(), f"{gate}/{code} is an empty reason"


# ---------------------------------------------------------------------------
# 5. Nothing else approves
# ---------------------------------------------------------------------------


def test_unknown_action_id_does_not_approve(settings, slack, threads):
    body = block_actions_body(action_id="gate_approve_v2")

    result = post(settings, slack, threads, body)

    assert result.outcome == "bad_action"
    assert threads.resumes == []


def test_unknown_reason_code_does_not_resume(settings, slack, threads):
    body = block_actions_body(action_id=ACTION_REJECT, reason_code="lgtm")

    result = post(settings, slack, threads, body)

    assert result.outcome == "bad_action"
    assert threads.resumes == []


def test_tampered_block_id_is_refused(settings, slack, threads):
    """block_id is signed as part of the body, so this can only happen by our
    own bug — but it still must not resolve to an approval."""
    body = urllib.parse.urlencode(
        {
            "payload": json.dumps(
                {
                    "type": "block_actions",
                    "user": {"id": OWNER},
                    "response_url": RESPONSE_URL,
                    "actions": [
                        {"action_id": ACTION_APPROVE, "block_id": "not json"}
                    ],
                }
            )
        }
    ).encode()

    result = post(settings, slack, threads, body)

    assert result.outcome == "bad_action"
    assert threads.resumes == []


def test_other_interaction_types_are_acknowledged_and_ignored(
    settings, slack, threads
):
    """One Request URL receives every interaction type. Anything we did not
    ask for must be a no-op, not a guess."""
    body = urllib.parse.urlencode(
        {"payload": json.dumps({"type": "view_submission", "user": {"id": OWNER}})}
    ).encode()

    result = post(settings, slack, threads, body)

    assert result.status == 200
    assert result.outcome == "ignored"
    assert threads.resumes == []


def test_garbage_body_does_not_approve(settings, slack, threads):
    body = b"not-a-form-at-all"

    result = post(settings, slack, threads, body)

    assert result.status == 400
    assert threads.resumes == []


def test_no_handler_path_approves_without_an_explicit_approve_action():
    """Read the source: `True` is returned from exactly one branch, guarded by
    an equality check on the approve action id."""
    import inspect

    from pagerduty_triage.slack.handler import _decision_from

    src = inspect.getsource(_decision_from)
    approving_lines = [
        line for line in src.splitlines() if "return True" in line
    ]
    assert len(approving_lines) == 1, (
        f"expected exactly one approving branch, found {approving_lines}"
    )
    assert "ACTION_APPROVE" in src


def test_nothing_in_the_slack_package_can_auto_approve():
    """No timer, no retry, no default may ever produce an approval. The gate
    invariant is that an unanswered gate parks forever."""
    import pathlib

    import pagerduty_triage.slack as pkg

    root = pathlib.Path(pkg.__file__).parent
    for path in sorted(root.glob("*.py")):
        src = path.read_text()
        for banned in ("sleep(", "threading.Timer", "schedule(", "auto_approve"):
            assert banned not in src, (
                f"{path.name} contains {banned!r}. Nothing in this package may "
                "act on a timer: a timeout that approves is the one failure "
                "the gates exist to prevent."
            )


# ---------------------------------------------------------------------------
# 5. A failed click must be recoverable, and must say so
#
# Everything above this point defends against a click doing too much. These
# defend against the opposite failure, which is the one that actually bit:
# a click that did nothing while telling the reviewer it was already handled.
# ---------------------------------------------------------------------------


def _texts(slack) -> str:
    return " ".join(r["body"].get("text", "") for r in slack.responses)


def test_a_failed_resume_is_reported_as_still_waiting_not_already_answered(
    settings, slack, threads
):
    """The wording is the fix. "Already answered" tells the reviewer to stop
    clicking; for a gate that is still open, that is how a recoverable
    failure becomes a permanently stranded ticket."""
    threads.fail_next_resume_with = RuntimeError("connection reset")

    post(settings, slack, threads, block_actions_body())

    said = _texts(slack)
    assert "already been answered" not in said, (
        "a resume that never happened was reported as a gate already closed"
    )
    assert "still waiting" in said and "click again" in said
    assert threads.resumes == []
    assert threads.pending_interrupt_ids(THREAD) == [INTERRUPT], (
        "a failed click must leave the gate exactly as it found it"
    )


def test_the_reviewer_can_click_again_after_a_failure_and_it_works(
    settings, slack, threads
):
    """The acceptance condition, at the handler boundary."""
    threads.fail_next_resume_with = RuntimeError("connection reset")
    post(settings, slack, threads, block_actions_body())

    post(settings, slack, threads, block_actions_body())

    assert len(threads.resumes) == 1
    assert threads.resumes[0]["value"]["approved"] is True
    assert threads.pending_interrupt_ids(THREAD) == []


def test_a_second_click_after_a_successful_resume_is_refused(
    settings, slack, threads
):
    """Retryability must not have cost the exactly-once guarantee."""
    post(settings, slack, threads, block_actions_body())

    post(settings, slack, threads, block_actions_body())

    assert len(threads.resumes) == 1, "the gate was answered twice"
    assert "already been answered" in _texts(slack)


def test_an_ambiguous_thread_state_does_not_close_the_gate(
    settings, slack, threads
):
    """The live failure, reproduced through the handler: a phantom second
    pending interrupt must not make the gate unclickable forever."""
    threads.park(THREAD, "int-phantom")

    post(settings, slack, threads, block_actions_body())

    assert threads.resumes == []
    assert INTERRUPT in threads.pending_interrupt_ids(THREAD)
    said = _texts(slack)
    assert "already been answered" not in said
    assert "still waiting" in said


def test_a_failure_is_written_to_the_log(settings, slack, threads, caplog):
    """Slack messages are ephemeral and best-effort. If the reviewer is never
    told, the log is the only remaining trace that a gate needs attention."""
    import logging

    threads.fail_next_resume_with = RuntimeError("connection reset")

    with caplog.at_level(logging.WARNING, logger="pagerduty_triage.slack.handler"):
        post(settings, slack, threads, block_actions_body())

    assert any(THREAD in r.getMessage() for r in caplog.records), (
        "a failed resume left no trace in the log"
    )


def test_a_run_that_dies_after_approval_is_not_reported_as_success(
    settings, slack, threads
):
    """`runs.create` returning is not the work happening. The graph can die a
    second later, and it did — twice, on live tickets, while Slack showed the
    reviewer nothing but a green tick."""
    threads.run_dies = True

    post(settings, slack, threads, block_actions_body())

    said = _texts(slack)
    assert "failed" in said.lower()
    assert "click again" not in said, (
        "the gate is spent, so telling the reviewer to retry sends them in "
        "circles instead of flagging a ticket that needs hands on it"
    )
    assert "already been answered" not in said


def test_a_dead_run_does_not_leave_the_gate_looking_answerable(
    settings, slack, threads
):
    """It is spent. Saying otherwise would invite a click that cannot work."""
    threads.run_dies = True

    post(settings, slack, threads, block_actions_body())

    assert threads.pending_interrupt_ids(THREAD) == []
