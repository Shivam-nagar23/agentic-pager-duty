"""The two human gates.

This module holds the payload and decision shapes. The gates themselves —
the `interrupt()` calls — live in the tool bodies in `tools/`, because that
is the whole point: a gate that lives in configuration or in a prompt is a
gate that can be configured or argued away.

## Why raw `interrupt()` and not deepagents' `interrupt_on=`

deepagents 0.7.14 ships a built-in HITL route: pass
`interrupt_on={"zoho_send_reply": {...}}` to `create_deep_agent` and it
installs `HumanInTheLoopMiddleware`, which interrupts before the tool runs.
We do not use it, for two reasons:

1. **It is configuration, not code.** The gate exists because a keyword
   argument was passed at graph-construction time. Delete the argument — or
   mistype the tool name in the dict key, which fails silently — and the tool
   runs unattended. A gate whose absence is silent is not a gate. With
   `interrupt()` in the tool body there is no configuration to get wrong:
   the only code path to the side effect goes through the interrupt.

2. **Its payload is fixed and prose-shaped.** The middleware emits
   `{"action_requests": [{"name", "args", "description"}], "review_configs": [...]}`
   where `description` is a rendered string like
   `"Tool execution requires approval\\n\\nTool: ...\\nArgs: {...}"`. The
   design calls for a structured payload a Slack renderer can turn into
   blocks without re-parsing prose. A raw `interrupt()` passes our dict
   through **verbatim** (verified against deepagents 0.7.14 + langgraph
   1.2.11), which is exactly what post-v1 needs.

The cost of this choice is that we own the resume envelope. The middleware
expects `Command(resume={"decisions": [{"type": "approve"}]})`; our tools
receive whatever dict the resumer sends, raw. `GateDecision.parse` is
deliberately liberal about what it accepts and conservative about what it
treats as approval.

## The re-execution rule, which is the sharpest edge here

`interrupt()` raises. On resume, LangGraph **re-runs the tool function from
its first line** and `interrupt()` returns the resume value instead of
raising. Everything above the `interrupt()` call therefore executes twice.

That dictates the shape of every gated tool, without exception:

    def gated_tool(...):
        payload = build_payload(...)     # pure. runs twice. must be safe.
        decision = interrupt(payload)    # <- the gate
        if not decision.approved:
            return revise_instruction    # agent revises and may retry
        return do_the_side_effect(...)   # runs exactly once, after approval

Put the side effect above the `interrupt()` and you have not built a gate —
you have built a thing that does the action twice and then asks permission.
`tests/test_gates.py::test_no_side_effect_before_interrupt` exists to make
that mistake fail loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

GateType = Literal["zoho_send_reply", "create_sprint_issue"]

#: Bump when the payload shape changes, so a Slack renderer built against v1
#: can detect a v2 payload rather than silently mis-rendering it.
GATE_PAYLOAD_VERSION = 1


def build_gate_payload(
    *,
    gate: GateType,
    ticket_id: str,
    ticket_subject: str,
    zoho_url: str,
    proposed_action: dict[str, Any],
    reasoning: str,
    classification: str = "",
    warnings: list[str] | None = None,
    customer_question: str = "",
    customer_followups: list[str] | None = None,
    customer_followups_truncated: bool = False,
) -> dict[str, Any]:
    """The structured interrupt payload. A dict, never prose.

    Every field is something a reviewer needs in order to decide, or that a
    renderer needs in order to show them. In particular ``proposed_action``
    carries the action **verbatim** — the exact reply text, or the exact
    issue title and body — because the reviewer is approving that text, not
    a summary of it.

    ``warnings`` is how the agent tells on itself: a pager-scribe that had to
    guess at the affected area, or a responder working from a thin ticket,
    surfaces that here rather than burying it in the reasoning prose.

    ``customer_question`` and ``customer_followups`` are **the customer's own
    words**, verbatim, carried in from ``TicketContext``. A reviewer approving
    a reply has to see what was asked; a subject line and a deep link are not
    that. They are additive fields — a renderer built against version 1 that
    does not know about them still renders correctly — so
    ``GATE_PAYLOAD_VERSION`` is deliberately *not* bumped. Bump it only when an
    existing field changes shape or disappears.

    All of it is untrusted, attacker-influenced input. Redaction is the
    renderer's job, at the point of display.
    """
    return {
        "version": GATE_PAYLOAD_VERSION,
        "gate": gate,
        "ticket": {
            "id": ticket_id,
            "subject": ticket_subject,
            "url": zoho_url,
            # Verbatim, never summarised — same treatment as the draft reply.
            "customer_question": customer_question,
            "customer_followups": list(customer_followups or []),
            "customer_followups_truncated": bool(customer_followups_truncated),
        },
        "classification": classification,
        "proposed_action": proposed_action,
        "reasoning": reasoning,
        "warnings": warnings or [],
        # What the resumer must send back. Carried in the payload so a Slack
        # renderer (or a human with curl) never has to guess the envelope.
        "respond_with": {
            "approve": {"approved": True},
            "reject": {"approved": False, "reason": "<why, so the agent can revise>"},
        },
    }


@dataclass(frozen=True)
class GateDecision:
    """A human's answer to a gate.

    Rejection carries a reason. That is not politeness: the agent is
    instructed to revise and retry, and it cannot revise against silence.
    """

    approved: bool
    reason: str = ""
    #: Anything else the resumer sent. Reserved for the Slack layer, which
    #: will want to record approver identity here.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> "GateDecision":
        """Interpret a resume value.

        **Only an explicit, unambiguous yes counts as approval.** Everything
        else — a bare string, a malformed dict, ``None`` because someone
        resumed with no argument — is a rejection carrying an explanatory
        reason. This is the code-level expression of the invariant that an
        unanswered gate never becomes an implicit yes: even a *badly*
        answered gate does not become one.
        """
        if isinstance(raw, dict):
            value = raw.get("approved", raw.get("approve"))
            if value is True:
                return cls(
                    approved=True,
                    reason=str(raw.get("reason", "")),
                    extra={
                        k: v
                        for k, v in raw.items()
                        if k not in ("approved", "approve", "reason")
                    },
                )
            if value is False:
                return cls(
                    approved=False,
                    reason=str(raw.get("reason", "")) or "rejected without a reason",
                    extra={
                        k: v
                        for k, v in raw.items()
                        if k not in ("approved", "approve", "reason")
                    },
                )
            return cls(
                approved=False,
                reason=(
                    "the resume payload had no boolean 'approved' field, so it was "
                    f"treated as a rejection. Got: {raw!r}. Send "
                    "{'approved': true} to approve."
                ),
            )

        # Deliberately strict: "approve" as a bare string is NOT approval.
        # Someone typing a word into a debug console must not be able to send
        # mail to a paying customer.
        return cls(
            approved=False,
            reason=(
                f"expected a dict like {{'approved': true}}, got {type(raw).__name__}: "
                f"{raw!r}. Treated as a rejection."
            ),
        )


def rejection_message(gate: GateType, decision: GateDecision) -> str:
    """What the tool returns to the agent when a human rejects.

    Phrased as an instruction to revise rather than as an error, because the
    agent's correct next move is to fix the draft and try again — not to give
    up, and not to look for another route.
    """
    what = {
        "zoho_send_reply": "The reply was NOT sent",
        "create_sprint_issue": "The sprint-tasks issue was NOT created",
    }[gate]
    return (
        f"{what}. A human reviewer rejected it with this reason:\n\n"
        f"    {decision.reason}\n\n"
        "Revise your work to address that reason specifically, then call this "
        "tool again with the corrected version. Do not attempt to reach the "
        "same outcome by another route — there is no other route, and trying "
        "is a bug worth reporting in your final summary. If the reviewer's "
        "reason tells you to stop, stop and say so."
    )
