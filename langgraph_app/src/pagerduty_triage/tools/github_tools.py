"""Gate 2: creating the sprint-tasks issue.

This is the seam. The issue is filed with `pager-duty` + `agent-triaged`; the
GitHub Action in `action/` triggers on **`agent-fix`**, which a human adds
afterwards — deliberately, because `pager-duty` is on every pager issue and
that label alone must not commission an agent. So the gate here is not really
"may I file a ticket" — it is **"is this actually a platform bug, and do you
accept a human being invited to set an agent on it"**.

Same ordering rules as gate 1: build payload, `interrupt()`, then act. See
`gates.py`.
"""

from __future__ import annotations

import json
import re

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import interrupt

from ..gates import GateDecision, build_gate_payload, rejection_message
from ..ledger import thread_id_for_ticket
from ..pager_template import (
    normalise_affected_area,
    render_issue_body,
    validate_fields,
)
from ..sprint_tasks import AGENT_LABEL, FIX_LABEL, PAGER_DUTY_LABEL, zoho_ticket_marker
from .deps import ToolDeps, ticket_context_from_config

PAGER_ISSUE_FILE = "/pager_issue.md"


def _extract_json_block(raw: str) -> dict:
    """Pull the ```json block out of what pager-scribe wrote."""
    fenced = re.search(r"```json\s*(.*?)```", raw, re.S)
    candidate = fenced.group(1) if fenced else raw
    return json.loads(candidate)


def build_github_tools(deps: ToolDeps) -> list:
    """Construct the sprint-tasks tools bound to one set of clients."""

    @tool
    def create_sprint_issue(
        pager_issue_json: str,
        classification_reasoning: str,
        config: RunnableConfig,
    ) -> str:
        """Create the sprint-tasks issue for a confirmed platform bug.

        PAUSES FOR HUMAN APPROVAL. The reviewer is confirming two things: that
        this really is a platform bug, and that the filled template is good
        enough for a developer to pick up.

        `pager_issue_json` is the exact JSON object pager-scribe wrote to
        /pager_issue.md (read it with read_file and pass it through — with or
        without its ```json fence). `classification_reasoning` is one or two
        sentences on why this is a platform bug rather than a k8s issue.

        Creating this issue triggers an automated chain that ends in a draft
        pull request. If the reviewer rejects, you get their reason: revise
        and call this again.
        """
        # ---- pure. runs twice. ---------------------------------------------
        ctx = ticket_context_from_config(config)
        expected_thread = thread_id_for_ticket(ctx.ticket_id)

        warnings: list[str] = []
        try:
            fields = _extract_json_block(pager_issue_json)
        except (json.JSONDecodeError, AttributeError) as exc:
            # Return rather than raise: this is the agent's mistake to fix,
            # and a human should not be woken for a malformed tool call.
            return (
                f"Could not parse pager_issue_json as JSON ({exc}). Read "
                f"{PAGER_ISSUE_FILE} and pass its ```json block through "
                "unchanged. Nothing was created and no human was asked."
            )
        if not isinstance(fields, dict):
            return (
                "pager_issue_json parsed to "
                f"{type(fields).__name__}, expected a JSON object. Nothing was "
                "created."
            )

        title = str(fields.pop("title", "")).strip()
        if not title:
            title = f"PagerBug: {ctx.subject}"[:100]
            warnings.append("pager-scribe supplied no title; derived one from the "
                            "Zoho subject.")

        report = validate_fields(fields)
        warnings.extend(report.warnings)
        for err in report.errors:
            warnings.append(f"REQUIRED FIELD MISSING: {err}")

        area_match = normalise_affected_area(fields.get("affected_areas"))
        if area_match.matched and not area_match.exact:
            warnings.append(
                f"affected_areas was rewritten to the controlled vocabulary: "
                f"{area_match.original!r} -> {area_match.canonical!r}. The "
                "localization step keys on this field, so check it."
            )
            fields["affected_areas"] = area_match.canonical
        elif not area_match.matched:
            warnings.append(
                f"affected_areas {area_match.original!r} is outside the "
                "controlled vocabulary and was left as written. Downstream "
                "localization will not match it to historical fixes."
            )

        existing = deps.ledger.issue_record(ctx.ticket_id)
        if existing:
            warnings.append(
                f"An issue for this ticket ALREADY EXISTS: {existing.get('issue_url')}. "
                "Approving will NOT create a second one — the tool will refuse."
            )

        body = render_issue_body(fields)
        # Provenance marker: lets `find_issue_by_zoho_ticket` locate this issue
        # later even if our store is restored from a backup.
        body = f"{body}\n<!-- {zoho_ticket_marker(ctx.ticket_id)} -->\n"
        labels = [PAGER_DUTY_LABEL, AGENT_LABEL, FIX_LABEL]

        payload = build_gate_payload(
            gate="create_sprint_issue",
            ticket_id=ctx.ticket_id,
            ticket_subject=ctx.subject,
            zoho_url=ctx.web_url,
            classification="platform_bug",
            proposed_action={
                "type": "sprint_tasks_issue",
                "repo": deps.settings.sprint_tasks_repo,
                "title": title,
                "labels": labels,
                # Verbatim. This is the text that will exist on GitHub.
                "body": body,
                "affected_areas": fields.get("affected_areas", ""),
            },
            reasoning=classification_reasoning,
            warnings=warnings,
        )

        # ---- THE GATE ------------------------------------------------------
        decision = GateDecision.parse(interrupt(payload))

        if not decision.approved:
            return rejection_message("create_sprint_issue", decision)

        # ---- approved ------------------------------------------------------
        replayed = deps.ledger.issue_record(ctx.ticket_id)
        if replayed:
            return (
                f"An issue for this ticket already exists: "
                f"{replayed.get('issue_url')}. Refusing to create a duplicate — "
                "a second issue would trigger the fix Action twice on the same "
                "bug. Nothing was created just now."
            )

        # Second line of defence: our store and GitHub can disagree after a
        # restore-from-backup. GitHub is the authority on what exists.
        remote = deps.sprint_tasks.find_issue_by_zoho_ticket(ctx.ticket_id)
        if remote:
            deps.ledger.record_issue(
                ctx.ticket_id,
                thread_id=expected_thread,
                issue_number=remote.number,
                issue_url=remote.url,
            )
            return (
                f"An issue for this ticket already exists on GitHub: {remote.url} "
                "(our records had lost it; they are now repaired). Nothing was "
                "created just now."
            )

        if not deps.settings.issue_creation_enabled:
            return (
                "APPROVED but NOT CREATED: ISSUE_CREATION_ENABLED is false, so "
                "this deployment cannot open issues. The approval is recorded in "
                "the thread. Set ISSUE_CREATION_ENABLED=true and re-run, or file "
                "the issue by hand from the approved body."
            )

        issue = deps.sprint_tasks.create_issue(title=title, body=body, labels=labels)
        deps.ledger.record_issue(
            ctx.ticket_id,
            thread_id=expected_thread,
            issue_number=issue.number,
            issue_url=issue.url,
        )
        return (
            f"Created {issue.url} with labels {labels}. The `agent-fix` label "
            "starts the fix engine, which will attempt a fix and open draft "
            "pull requests unattended — the human who approved this gate "
            "authorised that. Your work on this ticket is done — summarise "
            "and stop."
        )

    return [create_sprint_issue]
