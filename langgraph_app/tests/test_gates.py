"""Proof that the two gates cannot be bypassed.

The suite attacks the gates three ways:

1. **Behaviourally** — run the tool with the gate parked and assert the fake
   Zoho / GitHub client recorded nothing. This is the strong form: it checks
   that the *side effect did not happen*, not merely that an interrupt was
   raised. A tool that sent the mail and then interrupted would pass a
   "did it interrupt?" test and fail these.

2. **Structurally** — parse each gated tool's AST and assert the
   `interrupt()` call appears before any client call in execution order. This
   catches the bug a behavioural test cannot: someone adding a *new* side
   effect above the gate later.

3. **Adversarially** — feed the resume path every shape of garbage and assert
   that only an explicit `{"approved": True}` sends anything.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap

import pytest
from conftest import GateReached, call_tool

from pagerduty_triage.gates import GATE_PAYLOAD_VERSION, GateDecision
from pagerduty_triage.tools import GATED_TOOLS

REPLY = "Hello Priya, we have reproduced this and are investigating."

PAGER_FIELDS = {
    "title": "PagerBug: Helm apps inaccessible for admin users",
    "description": "Admin users cannot list Helm applications.",
    "affected_areas": "rbac issues",
    "additional_affected_areas": "None",
    "prod_environment": "prod",
    "impact_percentage": "more than 20%",
    "user_still_blocked": "yes",
    "user_unblocked_reason": "",
    "can_impact_other_clients": "yes",
    "impacted_client_count": "1-2",
    "reported_by": "client",
    "why_this_is_pager": "Admins are fully blocked from Helm apps in prod.",
    "impact_on_enterprise": "Enterprise RBAC users affected.",
    "steps_to_replicate": "1. Grant admin. 2. Open Helm Apps. 3. Empty list.",
    "expected_behavior": "The permitted cluster is listed.",
    "actual_behavior": "The cluster selector is empty and API returns 403.",
    "kubernetes_version": "",
    "cloud_provider": "",
    "browser": "",
    "proposed_solution": "",
}


def _pager_json() -> str:
    return "```json\n" + json.dumps(PAGER_FIELDS) + "\n```"


# ---------------------------------------------------------------------------
# 1. Behavioural: the side effect does not happen before the gate
# ---------------------------------------------------------------------------


def test_send_reply_interrupts_before_sending(
    send_reply_tool, zoho, gate_spy, config
):
    gate_spy.park()

    with pytest.raises(GateReached):
        call_tool(send_reply_tool, reply_text=REPLY, config=config)

    assert gate_spy.calls == 1, "the gate must be reached exactly once"
    # THE assertion. Not "did it interrupt" but "did it stay silent".
    assert zoho.sent_replies == [], (
        "zoho_send_reply contacted Zoho before the human gate. This is the "
        "failure the gate exists to prevent."
    )


def test_create_issue_interrupts_before_creating(
    create_issue_tool, sprint, gate_spy, config
):
    gate_spy.park()

    with pytest.raises(GateReached):
        call_tool(
            create_issue_tool,
            pager_issue_json=_pager_json(),
            classification_reasoning="Correct permissions, 403 anyway.",
            config=config,
        )

    assert gate_spy.calls == 1
    assert sprint.created == [], (
        "create_sprint_issue opened a GitHub issue before the human gate. That "
        "issue would have triggered the unattended fix workflow."
    )


# ---------------------------------------------------------------------------
# 2. Structural: interrupt() precedes every side effect, in source order
# ---------------------------------------------------------------------------


def _gated_functions():
    from pagerduty_triage.tools.github_tools import build_github_tools
    from pagerduty_triage.tools.zoho_tools import build_zoho_tools

    out = {}
    for factory in (build_zoho_tools, build_github_tools):
        src = textwrap.dedent(inspect.getsource(factory))
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in GATED_TOOLS:
                out[node.name] = node
    return out


def _first_line_matching(func: ast.FunctionDef, predicate) -> int | None:
    hits = [n.lineno for n in ast.walk(func) if predicate(n)]
    return min(hits) if hits else None


def _is_interrupt_call(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "interrupt"
    )


def _is_side_effect_call(node) -> bool:
    """A call that reaches the outside world: deps.zoho.X / deps.sprint_tasks.X.

    Ledger writes are excluded — they are local bookkeeping, and recording a
    send is supposed to happen after the send.
    """
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if not isinstance(f, ast.Attribute):
        return False
    owner = f.value
    return (
        isinstance(owner, ast.Attribute)
        and owner.attr in ("zoho", "sprint_tasks")
        and isinstance(owner.value, ast.Name)
        and owner.value.id == "deps"
    )


@pytest.mark.parametrize("name", sorted(GATED_TOOLS))
def test_interrupt_precedes_every_side_effect(name):
    """No outbound call may appear above the gate, in any gated tool.

    This is the test that survives refactoring. Someone adding a "just log it
    to Zoho first" line above the interrupt breaks this immediately, with a
    message explaining why they must not.
    """
    func = _gated_functions()[name]

    interrupt_line = _first_line_matching(func, _is_interrupt_call)
    assert interrupt_line is not None, (
        f"{name} is listed in GATED_TOOLS but never calls interrupt(). Either "
        "it is not actually gated, or it should not be in that set."
    )

    first_effect = _first_line_matching(func, _is_side_effect_call)
    if first_effect is None:
        return  # e.g. a tool whose only effect is via the ledger

    assert interrupt_line < first_effect, (
        f"{name} calls out to the world at line {first_effect}, which is ABOVE "
        f"its interrupt() at line {interrupt_line}. Everything above the "
        "interrupt runs before any human sees the request — and runs a second "
        "time on resume. Move the side effect below the gate."
    )


def test_every_side_effecting_tool_is_declared_gated(deps):
    """The GATED_TOOLS set must cover every tool that touches the world.

    Guards against the quiet failure mode: a new tool that emails someone but
    was never added to the set, so nothing above ever checks it.
    """
    from pagerduty_triage.tools import build_tools

    for tool in build_tools(deps):
        src = inspect.getsource(tool.func)
        touches_world = "deps.zoho." in src or "deps.sprint_tasks." in src
        # zoho_fetch_ticket reads; reads need no gate.
        is_read_only = "send_reply" not in src and "create_issue" not in src
        if touches_world and not is_read_only:
            assert tool.name in GATED_TOOLS, (
                f"{tool.name} performs a write against an external system but is "
                "not in GATED_TOOLS, so no gate test covers it."
            )


# ---------------------------------------------------------------------------
# 3. Adversarial: only an explicit yes is a yes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resume_value",
    [
        None,
        "approved",
        "yes",
        "true",
        True,
        {},
        {"approved": "yes"},
        {"approve": "true"},
        {"reason": "looks fine"},
        {"approved": None},
        [{"type": "approve"}],
        {"decisions": [{"type": "approve"}]},  # the deepagents envelope
    ],
)
def test_ambiguous_resume_never_sends(
    send_reply_tool, zoho, gate_spy, config, resume_value
):
    """Anything short of `{"approved": true}` is a rejection.

    Note `{"decisions": [{"type": "approve"}]}` in the list: that is the
    envelope deepagents' own HumanInTheLoopMiddleware expects. If someone
    wires a Slack button against the wrong contract, the failure is a
    non-send, not a send.
    """
    gate_spy.answer(resume_value)

    result = call_tool(send_reply_tool, reply_text=REPLY, config=config)

    assert zoho.sent_replies == [], f"{resume_value!r} was treated as approval"
    assert "NOT sent" in result


def test_explicit_approval_sends_exactly_once(
    send_reply_tool, zoho, gate_spy, config, ticket
):
    gate_spy.answer({"approved": True})

    result = call_tool(send_reply_tool, reply_text=REPLY, config=config)

    assert len(zoho.sent_replies) == 1
    sent = zoho.sent_replies[0]
    assert sent["ticket_id"] == ticket.id
    assert sent["content"] == REPLY
    assert sent["to_address"] == ticket.email
    assert "Reply sent" in result


def test_rejection_returns_the_reason_for_revision(
    send_reply_tool, zoho, gate_spy, config
):
    gate_spy.answer({"approved": False, "reason": "Too technical; drop the stack trace."})

    result = call_tool(send_reply_tool, reply_text=REPLY, config=config)

    assert zoho.sent_replies == []
    # The agent must receive the reason, or it cannot revise.
    assert "Too technical" in result
    assert "Revise" in result


def test_rejection_without_a_reason_still_says_something_useful(
    send_reply_tool, gate_spy, config
):
    gate_spy.answer({"approved": False})

    result = call_tool(send_reply_tool, reply_text=REPLY, config=config)

    assert "rejected without a reason" in result


# ---------------------------------------------------------------------------
# Replay: the re-execution path must not double-send
# ---------------------------------------------------------------------------


def test_resume_replay_does_not_double_send(
    send_reply_tool, zoho, gate_spy, config
):
    """Simulates: tool sends, process dies before checkpoint, tool re-runs.

    On resume LangGraph re-executes the tool from its first line with the
    approval already in hand. Without the ledger guard the customer gets the
    mail twice.
    """
    gate_spy.answer({"approved": True})

    first = call_tool(send_reply_tool, reply_text=REPLY, config=config)
    second = call_tool(send_reply_tool, reply_text=REPLY, config=config)

    assert len(zoho.sent_replies) == 1, (
        "the replayed execution sent a second copy of the reply to the customer"
    )
    assert "Reply sent" in first
    assert "already sent" in second


def test_replay_warning_reaches_the_reviewer(
    send_reply_tool, zoho, gate_spy, config
):
    """A reviewer asked to approve an already-sent reply must be told."""
    gate_spy.answer({"approved": True})
    call_tool(send_reply_tool, reply_text=REPLY, config=config)

    gate_spy.park()
    with pytest.raises(GateReached) as exc:
        call_tool(send_reply_tool, reply_text="A different reply", config=config)

    warnings = " ".join(exc.value.payload["warnings"])
    assert "ALREADY SENT" in warnings


def test_issue_creation_replay_does_not_duplicate(
    create_issue_tool, sprint, gate_spy, config
):
    gate_spy.answer({"approved": True})

    kwargs = dict(
        pager_issue_json=_pager_json(),
        classification_reasoning="RBAC bug reproduced on a second cluster.",
        config=config,
    )
    first = call_tool(create_issue_tool, **kwargs)
    second = call_tool(create_issue_tool, **kwargs)

    assert len(sprint.created) == 1, (
        "a duplicate sprint-tasks issue would trigger the fix workflow twice "
        "on the same bug, producing two competing draft PRs"
    )
    assert "Created" in first
    assert "already exists" in second


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------


def test_kill_switch_blocks_send_even_after_approval(
    deps, zoho, gate_spy, config
):
    """REPLIES_ENABLED=false must stop the send after a human said yes.

    This is what lets the whole pipeline run against production Zoho without
    any possibility of a customer receiving agent-written text.
    """
    from dataclasses import replace

    from pagerduty_triage.tools.zoho_tools import build_zoho_tools

    deps.settings = replace(deps.settings, replies_enabled=False)
    tool = {t.name: t for t in build_zoho_tools(deps)}["zoho_send_reply"]

    gate_spy.answer({"approved": True})
    result = call_tool(tool, reply_text=REPLY, config=config)

    assert zoho.sent_replies == []
    assert "NOT SENT" in result


# ---------------------------------------------------------------------------
# Payload shape, which the Slack layer will depend on
# ---------------------------------------------------------------------------


def test_gate_payload_is_structured_not_prose(
    send_reply_tool, gate_spy, config, ticket
):
    gate_spy.park()
    with pytest.raises(GateReached) as exc:
        call_tool(send_reply_tool, reply_text=REPLY, config=config)

    payload = exc.value.payload
    assert isinstance(payload, dict)
    assert payload["version"] == GATE_PAYLOAD_VERSION
    assert payload["gate"] == "zoho_send_reply"
    assert payload["ticket"]["id"] == ticket.id
    assert payload["ticket"]["url"] == ticket.web_url
    # Verbatim, so a Slack renderer shows exactly what will be sent.
    assert payload["proposed_action"]["reply_text"] == REPLY
    assert payload["reasoning"]
    assert payload["respond_with"]["approve"] == {"approved": True}


def test_issue_gate_payload_carries_the_real_body(
    create_issue_tool, gate_spy, config
):
    gate_spy.park()
    with pytest.raises(GateReached) as exc:
        call_tool(
            create_issue_tool,
            pager_issue_json=_pager_json(),
            classification_reasoning="because",
            config=config,
        )

    action = exc.value.payload["proposed_action"]
    # `agent-fix` is applied at creation, not by a human afterwards. Gate 2 is
    # already the authorisation: a reviewer who approves has seen the
    # classification and the filled template and said yes to the whole chain.
    # Asking them to then click a label is the same decision twice.
    #
    # It is in the *gate payload*, so it renders in the Slack card before the
    # click -- that is what makes the approval informed rather than a surprise.
    assert action["labels"] == ["pager-duty", "agent-triaged", "agent-fix"]
    assert "### Affected areas" in action["body"]
    assert "rbac issues" in action["body"]
    assert exc.value.payload["classification"] == "platform_bug"


# ---------------------------------------------------------------------------
# Ticket identity cannot come from the model
# ---------------------------------------------------------------------------


def test_tool_refuses_to_act_without_a_bound_ticket(send_reply_tool, gate_spy):
    """No ticket bound => hard failure, never a guess.

    The model has no `ticket_id` argument to supply, so a thread with no bound
    context has no recipient at all. Refusing loudly is the only safe move.
    """
    from pagerduty_triage.tools.deps import MissingTicketContext

    gate_spy.answer({"approved": True})
    with pytest.raises(MissingTicketContext):
        call_tool(send_reply_tool, reply_text=REPLY, config={"configurable": {}})


def test_model_cannot_choose_the_recipient(send_reply_tool):
    """`zoho_send_reply` must not expose a recipient argument to the model."""
    schema = send_reply_tool.args_schema.model_json_schema()
    exposed = set(schema.get("properties", {}))
    for forbidden in ("ticket_id", "to", "to_address", "email", "recipient"):
        assert forbidden not in exposed, (
            f"{forbidden!r} is model-settable, so a confused or prompt-injected "
            "agent could address this reply to a different customer."
        )
    assert exposed == {"reply_text"}


# ---------------------------------------------------------------------------
# GateDecision unit coverage
# ---------------------------------------------------------------------------


def test_gate_decision_parse_rejects_non_dict():
    assert GateDecision.parse("approved").approved is False
    assert GateDecision.parse(None).approved is False
    assert GateDecision.parse(True).approved is False


def test_gate_decision_parse_accepts_only_true():
    assert GateDecision.parse({"approved": True}).approved is True
    assert GateDecision.parse({"approved": False}).approved is False
    assert GateDecision.parse({"approved": 1}).approved is False


def test_gate_decision_keeps_extra_fields_for_slack():
    decision = GateDecision.parse(
        {"approved": True, "reason": "ok", "approver": "U123"}
    )
    assert decision.extra == {"approver": "U123"}


# ---------------------------------------------------------------------------
# 5. The reviewer sees what the customer asked
#
# The payload used to carry only the subject and a Zoho link, so gate 1 asked
# a human to approve an answer to a question they had not read. The words are
# carried in from the poller through `TicketContext`, NOT fetched here — a
# `deps.zoho.*` call above `interrupt()` runs before anyone sees the request
# and again on every resume, which is what
# `test_interrupt_precedes_every_side_effect` above exists to forbid.
# ---------------------------------------------------------------------------


def _config_with_words(ticket, **over):
    from pagerduty_triage.tools.deps import TICKET_CONTEXT_KEY, TicketContext

    ctx = TicketContext(
        ticket_id=ticket.id,
        ticket_number=ticket.ticket_number,
        subject=ticket.subject,
        web_url=ticket.web_url,
        email=ticket.email,
        description=over.pop("description", "Our admin cannot see Helm apps."),
        customer_messages=over.pop("customer_messages", ()),
        customer_messages_truncated=over.pop("truncated", False),
    )
    return {"configurable": {TICKET_CONTEXT_KEY: ctx.to_configurable()}}


def test_gate_one_payload_carries_the_customers_question(
    send_reply_tool, gate_spy, ticket, zoho
):
    gate_spy.park()
    with pytest.raises(GateReached):
        call_tool(
            send_reply_tool,
            reply_text=REPLY,
            config=_config_with_words(
                ticket, customer_messages=("Any update? We are blocked.",)
            ),
        )

    payload = gate_spy.payloads[0]["ticket"]
    assert payload["customer_question"] == "Our admin cannot see Helm apps."
    assert payload["customer_followups"] == ["Any update? We are blocked."]
    # And it got there without asking Zoho anything.
    assert zoho.sent_replies == []


def test_gate_one_says_so_when_the_customers_words_are_missing(
    send_reply_tool, gate_spy, ticket
):
    """A context with no words is a wiring failure, not a quiet ticket."""
    gate_spy.park()
    with pytest.raises(GateReached):
        call_tool(
            send_reply_tool,
            reply_text=REPLY,
            config=_config_with_words(ticket, description="", customer_messages=()),
        )

    warnings = " ".join(gate_spy.payloads[0]["warnings"])
    assert "customer's own words are NOT" in warnings


def test_the_customers_question_survives_the_round_trip_into_slack_blocks():
    """End to end, payload builder → renderer, with no Slack and no network."""
    from pagerduty_triage.slack.renderer import render_gate_message
    from pagerduty_triage.tools.deps import TicketContext
    from pagerduty_triage.gates import build_gate_payload

    ctx = TicketContext.from_configurable(
        TicketContext(
            ticket_id="t1",
            ticket_number="42",
            subject="s",
            web_url="https://desk.zoho.com/x",
            email="a@b.example",
            description="The cluster list is empty for our admin.",
        ).to_configurable()
    )
    payload = build_gate_payload(
        gate="zoho_send_reply",
        ticket_id=ctx.ticket_id,
        ticket_subject=ctx.subject,
        zoho_url=ctx.web_url,
        proposed_action={"type": "customer_reply", "to": ctx.email,
                         "ticket_number": ctx.ticket_number, "reply_text": REPLY},
        reasoning="r",
        customer_question=ctx.description,
        customer_followups=list(ctx.customer_messages),
    )
    message = render_gate_message(
        payload, thread_id="11111111-1111-5111-8111-111111111111", interrupt_id="i1"
    )
    assert "The cluster list is empty for our admin." in json.dumps(message)
