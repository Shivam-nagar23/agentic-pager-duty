"""What the reviewer actually sees.

The gate payload was built as a structured dict precisely so this layer could
exist. These tests hold it to the two promises that make a Slack approval
trustworthy: the reviewer sees the *exact* text that will be sent, and the
message does not leak anything it should not.
"""

from __future__ import annotations

import json

import pytest

from pagerduty_triage.gates import build_gate_payload
from pagerduty_triage.slack.renderer import (
    MAX_BLOCKS,
    MAX_SECTION_TEXT,
    REDACTED,
    context_block_id,
    parse_context_block_id,
    render_gate_message,
)

THREAD = "11111111-1111-5111-8111-111111111111"
INTERRUPT = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"

REPLY = (
    "Hi Priya,\n\n"
    "We reproduced the empty Helm app list and it is a permissions bug on our "
    "side. A fix is on the way.\n\n"
    "— Devtron Support"
)

CUSTOMER_QUESTION = (
    "Our admin user opens the Helm apps tab and the list is completely "
    "empty, even though the same user can see the Devtron apps. This started "
    "after we upgraded to 1.2.0. Is this expected?"
)


def reply_payload(**over):
    payload = build_gate_payload(
        gate="zoho_send_reply",
        ticket_id="ticket-1001",
        ticket_subject="Helm apps not listed for admin user",
        zoho_url="https://desk.zoho.com/agent/devtron/tickets/details/1001",
        proposed_action={
            "type": "customer_reply",
            "to": "priya@bigco.example",
            "ticket_number": "4242",
            "reply_text": over.pop("reply_text", REPLY),
        },
        reasoning="Answerable without a code change.",
        warnings=over.pop("warnings", []),
        customer_question=over.pop("customer_question", CUSTOMER_QUESTION),
        customer_followups=over.pop("customer_followups", []),
        customer_followups_truncated=over.pop(
            "customer_followups_truncated", False
        ),
    )
    payload.update(over)
    return payload


def issue_payload(**over):
    payload = build_gate_payload(
        gate="create_sprint_issue",
        ticket_id="ticket-1001",
        ticket_subject="Helm apps not listed for admin user",
        zoho_url="https://desk.zoho.com/agent/devtron/tickets/details/1001",
        classification="platform_bug",
        proposed_action={
            "type": "sprint_tasks_issue",
            "repo": "devtron-labs/sprint-tasks",
            "title": "PagerBug: Helm apps inaccessible for admin users",
            "labels": ["pager-duty", "agent-triaged"],
            "body": over.pop("body", "## Description\n\nAdmins see nothing.\n"),
            "affected_areas": "rbac issues",
        },
        reasoning="Correct permissions, 403 anyway.",
        warnings=over.pop("warnings", []),
    )
    payload.update(over)
    return payload


def render(payload):
    return render_gate_message(
        payload, thread_id=THREAD, interrupt_id=INTERRUPT
    )


def all_text(message) -> str:
    """Every string anywhere in the rendered message."""
    return json.dumps(message)


def verbatim_text(message) -> str:
    """Only the text inside rich_text_preformatted blocks."""
    out = []
    for block in message["blocks"]:
        if block.get("type") != "rich_text":
            continue
        for element in block["elements"]:
            for leaf in element.get("elements", []):
                out.append(leaf.get("text", ""))
    return "".join(out)


def verbatim_chunks(message) -> list[str]:
    """Each rich_text_preformatted block's text, as its own string.

    Gate 1 now renders two verbatim regions — the customer's question and the
    draft reply — so "is the reply verbatim" is asked as "is there a block
    holding exactly the reply", which is stricter than a substring check.
    """
    out = []
    for block in message["blocks"]:
        if block.get("type") != "rich_text":
            continue
        for element in block["elements"]:
            out.append("".join(leaf.get("text", "") for leaf in element.get("elements", [])))
    return out


def actions_blocks(message):
    return [b for b in message["blocks"] if b.get("type") == "actions"]


# ---------------------------------------------------------------------------
# Gate 1: the exact text, unaltered
# ---------------------------------------------------------------------------


def test_reply_text_is_shown_verbatim_and_unsummarised():
    message = render(reply_payload())

    assert REPLY in verbatim_chunks(message), (
        "the reviewer must approve the exact bytes that will be emailed, not "
        "a reformatted or truncated version of them"
    )


def test_reply_text_containing_a_code_fence_cannot_escape_its_block():
    """A mrkdwn ``` fence would break out here and let Slack reformat the
    rest. rich_text_preformatted is not markdown-parsed, so it cannot."""
    hostile = "Here is my config:\n```\nsecret: no\n```\nand *bold* _stuff_"

    message = render(reply_payload(reply_text=hostile))

    assert hostile in verbatim_chunks(message)
    for block in message["blocks"]:
        if block.get("type") == "rich_text":
            assert block["elements"][0]["type"] == "rich_text_preformatted"


def test_recipient_and_ticket_link_are_both_shown():
    message = render(reply_payload())
    text = all_text(message)

    assert "priya@bigco.example" in text
    assert "desk.zoho.com/agent/devtron/tickets/details/1001" in text
    assert "4242" in text


def test_the_reviewer_is_pointed_at_the_customers_own_words():
    """The question is rendered here AND the ticket is still linked.

    The link is not redundant: it is where the attachments, the account and
    the rest of the history live, and it is the fallback whenever the thread
    was too long to show in full.
    """
    message = render(reply_payload())

    assert CUSTOMER_QUESTION in verbatim_text(message)
    assert "Zoho" in all_text(message)


def test_agent_warnings_reach_the_reviewer():
    message = render(
        reply_payload(warnings=["A reply was ALREADY SENT for this ticket."])
    )

    assert "ALREADY SENT" in all_text(message)


def test_reply_gate_offers_approve_and_reject():
    message = render(reply_payload())
    blocks = actions_blocks(message)

    assert len(blocks) == 1
    ids = [e["action_id"] for e in blocks[0]["elements"]]
    assert ids == ["gate_approve", "gate_reject"]
    approve = blocks[0]["elements"][0]
    assert approve["confirm"], "the irreversible control needs a confirm step"


# ---------------------------------------------------------------------------
# Gate 2: the consequence is visible
# ---------------------------------------------------------------------------


def test_issue_gate_states_what_approval_actually_starts():
    message = render(issue_payload())
    text = all_text(message)

    assert "draft" in text and "pull request" in text, (
        "approving gate 2 starts an unattended chain ending in open PRs; the "
        "reviewer must be told that, not just 'file an issue?'"
    )


def test_issue_body_is_shown_verbatim():
    body = "## Description\n\n*not* bold\n\n## Steps\n\n1. do a thing\n"
    message = render(issue_payload(body=body))

    assert verbatim_text(message) == body


def test_issue_gate_shows_classification_and_affected_area():
    text = all_text(render(issue_payload()))

    assert "platform_bug" in text
    assert "rbac issues" in text
    assert "devtron-labs/sprint-tasks" in text


# ---------------------------------------------------------------------------
# Leaking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "xoxb-1234567890-abcdefghijklmno",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123",
        "Bearer abcdefghijklmnopqrstuvwxyz012345",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_credentials_pasted_into_a_ticket_are_masked(secret):
    """Customers do paste tokens into support tickets. A seatbelt, not a
    guarantee — the real control is that the channel is private."""
    message = render(reply_payload(reply_text=f"my token is {secret} ok?"))

    text = all_text(message)
    assert secret not in text
    assert REDACTED in text


def test_the_renderer_never_dumps_the_whole_payload():
    """Built from an allowlist, so a future field cannot silently reach a
    channel."""
    payload = reply_payload()
    payload["internal_debug"] = "s3://bucket/checkpoint-blob"

    assert "internal_debug" not in all_text(render(payload))
    assert "s3://bucket" not in all_text(render(payload))


def test_the_resume_envelope_is_not_posted_to_the_channel():
    """`respond_with` is instructions for a curl user, not for a reviewer, and
    showing it invites someone to answer the gate by hand."""
    assert "respond_with" not in all_text(render(reply_payload()))


# ---------------------------------------------------------------------------
# Limits and disarming
# ---------------------------------------------------------------------------


def test_a_reply_too_long_to_display_removes_the_approve_button():
    """An Approve button next to text the reviewer cannot see is worse than no
    button at all."""
    huge = "\n".join(f"line {i} " + "x" * 200 for i in range(1000))

    message = render(reply_payload(reply_text=huge))

    assert actions_blocks(message) == []
    assert "no Approve button" in all_text(message)


def test_rendered_messages_stay_inside_slack_limits():
    for payload in (reply_payload(), issue_payload()):
        message = render(payload)
        assert len(message["blocks"]) <= MAX_BLOCKS
        assert message["text"], "the notification fallback is never empty"
        for block in message["blocks"]:
            if block.get("type") == "section":
                assert len(block["text"]["text"]) <= MAX_SECTION_TEXT


def test_a_future_payload_version_disarms_the_buttons():
    """A v2 payload rendered by a v1 renderer is a silent mis-render. Show the
    banner, take the controls away."""
    message = render(reply_payload(version=2))

    assert actions_blocks(message) == []
    assert "version 1" in all_text(message)


def test_an_unknown_gate_refuses_to_render():
    payload = reply_payload()
    payload["gate"] = "delete_production"

    with pytest.raises(ValueError):
        render(payload)


def test_mrkdwn_text_is_marked_verbatim():
    """Stops Slack auto-linking a bare URL or turning #123 into a channel ref
    inside text we did not write."""
    for block in render(reply_payload())["blocks"]:
        if block.get("type") == "section":
            assert block["text"].get("verbatim") is True


# ---------------------------------------------------------------------------
# The button context round-trip
# ---------------------------------------------------------------------------


def test_block_id_round_trips():
    raw = context_block_id(
        thread_id=THREAD, interrupt_id=INTERRUPT, gate="zoho_send_reply"
    )
    assert parse_context_block_id(raw) == {
        "thread_id": THREAD,
        "interrupt_id": INTERRUPT,
        "gate": "zoho_send_reply",
    }


def test_block_id_fits_slacks_limit():
    raw = context_block_id(
        thread_id=THREAD, interrupt_id=INTERRUPT, gate="create_sprint_issue"
    )
    assert len(raw) <= 255


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "[]", '{"t":"x"}', '{"t":"x","i":"y","g":"nope"}'],
)
def test_malformed_block_id_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_context_block_id(raw)


# ---------------------------------------------------------------------------
# Gate 1 shows the customer's question, not just the subject line
#
# A reviewer approving a reply is approving an *answer*. Approving an answer
# to a question you have not read is the failure this section exists to stop.
# ---------------------------------------------------------------------------


def test_the_customers_question_reaches_the_rendered_gate_message():
    message = render(reply_payload())
    assert CUSTOMER_QUESTION in verbatim_text(message), (
        "the customer's own words never reached the Slack message. The "
        "reviewer would be approving a reply without seeing what was asked."
    )


def test_the_customers_question_is_verbatim_not_summarised():
    """Same treatment as the draft reply: character for character."""
    odd = (
        "Why does *this* not work?\n"
        "```\nlevel=error msg=\"rbac: denied\"\n```\n"
        "See <https://example.test/x> and #4242."
    )
    message = render(reply_payload(customer_question=odd))
    assert odd in verbatim_text(message)


def test_the_customers_question_is_in_a_block_markdown_cannot_escape():
    """A fence inside customer text must not reformat the rest of the message.

    Customer text is attacker-influenced. If it landed in an mrkdwn section
    inside a ``` fence, a fence in the ticket would close ours and let Slack
    reinterpret everything after it — including the draft reply.
    """
    message = render(reply_payload(customer_question="```\nnot a fence\n```"))
    holders = [
        b for b in message["blocks"]
        if b.get("type") == "rich_text"
        and any(
            e.get("type") == "rich_text_preformatted" for e in b.get("elements", [])
        )
    ]
    assert holders, "customer text must ride in rich_text_preformatted"
    for block in message["blocks"]:
        if block.get("type") == "section":
            assert "not a fence" not in json.dumps(block)


def test_later_customer_messages_are_shown_too():
    message = render(
        reply_payload(
            customer_followups=[
                "Any update on this? It is blocking our release.",
                "We tried recreating the user; no change.",
            ]
        )
    )
    body = verbatim_text(message)
    assert "blocking our release" in body
    assert "recreating the user" in body


def test_credentials_in_the_customers_question_are_masked():
    leaked = "sk-ant-api03-" + "A" * 40
    message = render(reply_payload(customer_question=f"here is my key {leaked}"))
    assert leaked not in all_text(message)
    assert "REDACTED" in all_text(message)


def test_a_missing_customer_question_is_said_out_loud():
    """Absent words and no words are different, and must look different."""
    message = render(reply_payload(customer_question="", customer_followups=[]))
    text = all_text(message)
    assert "not in this request" in text
    assert "Zoho" in text


def test_a_huge_customer_thread_neither_blows_the_cap_nor_truncates_silently():
    message = render(
        reply_payload(
            customer_question="\n".join(f"line {i} of a pasted log" for i in range(4000))
        )
    )
    assert len(message["blocks"]) <= 50, "Slack rejects a message over 50 blocks"
    assert "not the whole conversation" in all_text(message), (
        "customer text was shortened with nothing telling the reviewer so"
    )


def test_a_capped_customer_thread_says_so_even_when_every_block_fits():
    """`customer_followups_truncated` comes from the poller, not the renderer.

    The context builder drops the oldest messages before they ever reach here,
    so the renderer cannot detect that loss by counting blocks — it has to be
    told, and it has to pass it on.
    """
    message = render(
        reply_payload(
            customer_followups=["short"],
            customer_followups_truncated=True,
        )
    )
    assert "not the whole conversation" in all_text(message)


def test_a_long_customer_thread_does_not_crowd_out_the_reply():
    """The reviewer is approving the reply. It is never the thing dropped."""
    message = render(
        reply_payload(
            customer_question="x" * 60000,
            reply_text=REPLY,
        )
    )
    assert REPLY in verbatim_text(message)


def test_gate_two_names_the_label_that_actually_starts_the_fix_engine():
    """The workflow in `action/` triggers on `agent-fix`, not `pager-duty`.

    Telling a reviewer that approving starts an unattended chain, when in fact
    a second human step does, is the kind of inaccuracy that gets a gate
    clicked through — in both directions.
    """
    text = all_text(render(issue_payload()))

    assert "agent-fix" in text
    assert "draft" in text and "pull request" in text
