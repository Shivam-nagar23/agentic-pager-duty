"""Zoho tools: three reads, and one gated write.

`zoho_send_reply` is **gate 1**. Read the ordering rules in `gates.py` before
touching it — the `interrupt()` call must stay above the send, and nothing
with a side effect may move above the `interrupt()`.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command, interrupt

from ..gates import GateDecision, build_gate_payload, rejection_message
from ..ledger import thread_id_for_ticket
from ..zoho.client import strip_html as _strip_html
from .deps import ToolDeps, ticket_context_from_config

TICKET_FILE = "/ticket.md"


def build_zoho_tools(deps: ToolDeps) -> list:
    """Construct the Zoho tools bound to one set of clients."""

    @tool
    def zoho_fetch_ticket(
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command:
        """Fetch this thread's Zoho ticket, its full conversation and its
        attachment list, and write it all to /ticket.md.

        Takes no arguments — the ticket is fixed by the thread. Call this
        once at the start; every subagent reads /ticket.md rather than
        calling Zoho again.
        """
        ctx = ticket_context_from_config(config)
        ticket = deps.zoho.get_ticket(ctx.ticket_id)
        threads = deps.zoho.list_threads(ctx.ticket_id)
        attachments = deps.zoho.list_attachments(ctx.ticket_id)

        lines = [
            f"# Zoho ticket #{ticket.ticket_number} — {ticket.subject}",
            "",
            f"- ticket id: {ticket.id}",
            f"- status: {ticket.status}",
            f"- priority: {ticket.priority or 'unset'}",
            f"- channel: {ticket.channel or 'unknown'}",
            f"- account: {ticket.account_name or 'unknown'}",
            f"- contact: {ticket.contact_name or 'unknown'}",
            f"- created: {ticket.created_time}",
            f"- last modified: {ticket.modified_time}",
            f"- link: {ticket.web_url}",
            "",
            "## Description",
            "",
            _strip_html(ticket.description) or "_(empty)_",
            "",
            "## Conversation (oldest first)",
            "",
        ]
        if not threads:
            lines.append("_(no conversation threads)_")
        for i, th in enumerate(threads, start=1):
            who = "customer" if th.direction == "in" else "support agent"
            lines += [
                f"### {i}. {who} — {th.author or 'unknown'} — {th.created_time}",
                "",
                _strip_html(th.content) or "_(empty)_",
                "",
            ]

        lines += ["## Attachments", ""]
        if not attachments:
            lines.append("_(none)_")
        else:
            lines += [
                f"- `{a.name}` ({a.size} bytes) — id {a.id}" for a in attachments
            ]
            lines += [
                "",
                "> You cannot open these. If one looks load-bearing (a log file, a "
                "screenshot of the error), say so in your triage rather than "
                "pretending to have read it.",
            ]

        content = "\n".join(lines) + "\n"
        summary = (
            f"Wrote {TICKET_FILE} ({len(content)} chars): ticket "
            f"#{ticket.ticket_number}, {len(threads)} conversation message(s), "
            f"{len(attachments)} attachment(s). Delegate to triage-analyst; do "
            "not read the file yourself."
        )
        # Return a state update rather than a string, so the ticket body lands
        # in the virtual filesystem WITHOUT passing through the main agent's
        # context. Only `summary` becomes a message the model sees. This is the
        # whole point of the filesystem hand-off: a 40KB ticket costs the main
        # agent one sentence.
        #
        # `files` uses deepagents' FileData shape — {"content", "encoding"} —
        # and a delta reducer that merges per-path, so this adds /ticket.md
        # without clobbering files subagents wrote.
        return Command(
            update={
                "files": {TICKET_FILE: {"content": content, "encoding": "utf-8"}},
                "messages": [ToolMessage(summary, tool_call_id=tool_call_id)],
            }
        )

    @tool
    def zoho_send_reply(reply_text: str, config: RunnableConfig) -> str:
        """Send a reply to the customer on this Zoho ticket.

        PAUSES FOR HUMAN APPROVAL before anything is sent. Pass the final
        reply body as `reply_text` — exactly what the customer should read.
        The recipient and ticket are fixed by the thread; you cannot choose
        them.

        If the reviewer rejects, you get their reason back: revise and call
        this again.
        """
        # ---- everything above interrupt() runs TWICE. keep it pure. --------
        ctx = ticket_context_from_config(config)
        expected_thread = thread_id_for_ticket(ctx.ticket_id)

        already = deps.ledger.reply_record(ctx.ticket_id)
        warnings: list[str] = []
        if already:
            warnings.append(
                f"A reply was ALREADY SENT for this ticket at {already.get('sent_at')} "
                f"(fingerprint {already.get('fingerprint')}). Approving this gate "
                "will NOT send a second one — the tool will refuse."
            )
        if not reply_text.strip():
            warnings.append("The proposed reply is empty.")
        if not ctx.description.strip() and not ctx.customer_messages:
            warnings.append(
                "The customer's own words are NOT in this request — the bound "
                "ticket context carries no description and no conversation. "
                "Read the ticket in Zoho before approving."
            )

        payload = build_gate_payload(
            gate="zoho_send_reply",
            ticket_id=ctx.ticket_id,
            ticket_subject=ctx.subject,
            zoho_url=ctx.web_url,
            proposed_action={
                "type": "customer_reply",
                "to": ctx.email,
                "ticket_number": ctx.ticket_number,
                # Verbatim. The reviewer approves this exact text.
                "reply_text": reply_text,
            },
            reasoning=(
                "The agent classified this ticket as answerable without a code "
                "change and drafted the reply above. Approving sends it to the "
                "customer as written."
            ),
            warnings=warnings,
            # Already in hand — bound by the poller. No Zoho call here, and
            # there must never be one: this code runs before the reviewer sees
            # anything, and again on every resume.
            customer_question=ctx.description,
            customer_followups=list(ctx.customer_messages),
            customer_followups_truncated=ctx.customer_messages_truncated,
        )

        # ---- THE GATE. nothing below here runs until a human answers. ------
        decision = GateDecision.parse(interrupt(payload))

        if not decision.approved:
            return rejection_message("zoho_send_reply", decision)

        # ---- approved. from here on, exactly-once matters. -----------------
        # Authoritative replay guard: if the send already happened and we
        # crashed before the checkpoint committed, this is the re-execution.
        replayed = deps.ledger.reply_record(ctx.ticket_id)
        if replayed:
            return (
                "A reply for this ticket was already sent at "
                f"{replayed.get('sent_at')} (fingerprint "
                f"{replayed.get('fingerprint')}). Refusing to send a second one. "
                "Nothing was sent just now. Treat this ticket as answered."
            )

        if not deps.settings.replies_enabled:
            return (
                "APPROVED but NOT SENT: REPLIES_ENABLED is false, so this "
                "deployment cannot send customer mail. The approval was recorded "
                "in the thread. A human must send this reply manually, or set "
                "REPLIES_ENABLED=true and re-run."
            )

        result = deps.zoho.send_reply(
            ctx.ticket_id,
            content=reply_text,
            to_address=ctx.email,
            content_type="html",
        )
        # Written immediately. The gap between the line above and this one is
        # the residual double-reply window; see ledger.py.
        deps.ledger.record_reply(
            ctx.ticket_id,
            thread_id=expected_thread,
            content=reply_text,
            zoho_thread_id=result.thread_id,
        )
        return (
            f"Reply sent to {ctx.email} on ticket #{ctx.ticket_number} "
            f"(Zoho thread {result.thread_id})."
        )

    return [zoho_fetch_ticket, zoho_send_reply]
