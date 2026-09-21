"""Gate payload -> Slack Block Kit. Pure: no I/O, no clients, no clock.

``gates.py`` already emits a structured dict — that was a deliberate choice so
this module could exist without re-parsing prose. Nothing here inspects the
graph, calls Zoho, or knows what a thread is. Payload in, blocks out.

## Four rules this module enforces

1. **Never summarise what the reviewer is approving.** The customer reply and
   the issue body are rendered *verbatim*.

2. **Verbatim means a block that cannot be re-formatted.** The proposed text
   goes in a ``rich_text`` / ``rich_text_preformatted`` block, not a mrkdwn
   section with a ``` fence. A fence is not safe here: agent- and
   customer-authored text can itself contain a fence, which would break out
   and let Slack reinterpret the remainder as markdown. Slack's own docs call
   ``rich_text`` "strongly preferred" over mrkdwn formatting, and its
   ``text`` elements are not markdown-parsed at all. Every mrkdwn object that
   carries agent- or customer-derived prose also sets ``verbatim: true``, which
   stops Slack auto-linking a bare URL or turning ``#123`` into a channel
   reference.

3. **If it does not fit, do not offer Approve.** Slack caps a message at 50
   blocks. Long text is chunked; if it still will not fit, the Approve control
   is *removed* and the message says to answer from the thread state instead.
   An Approve button next to text the reviewer cannot fully see is worse than
   no button at all.

4. **Build from an allowlist, never from the whole payload.** Only the named
   fields below are rendered. A future field added to the payload — an
   internal id, a token, a debug blob — does not silently reach a channel.

## Redaction

Ticket text is attacker-influenced customer input, and customers do paste
credentials into support tickets. Anything matching a known credential shape
is masked before it is posted. This is a seatbelt, not a guarantee: the real
control is that the channel is private and scoped.

## Limits, from the Block Kit reference

blocks/message 50 · ``section.text`` 3000 · button ``text`` 75 ·
button ``value`` 2000 · ``action_id`` 255 · ``block_id`` 255. A select
option's ``value`` is capped at 75, which is why the gate context rides in
``block_id`` and the option value is a short code.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_SECTION_TEXT = 2900   # documented 3000, kept under
MAX_HEADER_TEXT = 150
MAX_BLOCKS = 45           # documented 50, kept under
MAX_BUTTON_TEXT = 75
MAX_OPTION_TEXT = 75
MAX_BLOCK_ID = 255
MAX_VERBATIM_CHUNK = 2900

#: Block budget for the customer's own words. The reviewer is approving the
#: *reply*, so the reply is never squeezed to make room for the question — the
#: question gets a fixed allowance and anything past it is dropped **with a
#: visible banner**, never silently. ~11 KB of question and ~8 KB of follow-ups
#: is far more than any reply-worthy support ticket.
MAX_QUESTION_BLOCKS = 4
MAX_FOLLOWUP_BLOCKS = 3

ACTION_APPROVE = "gate_approve"
ACTION_REJECT = "gate_reject"

#: Canned rejection reasons, keyed by a short code because a Slack select
#: option ``value`` is capped at 75 characters. Free text would need a modal,
#: and a modal needs a ``trigger_id`` that Slack documents as **single-use and
#: valid for three seconds** — so ``views.open`` has to happen synchronously
#: inside the button handler, and a second payload type (``view_submission``)
#: has to be parsed, verified and routed. A fixed menu returns a real,
#: actionable reason for a fraction of that machinery.
#:
#: There is no "rejected, no reason" option, on purpose: the agent is
#: instructed to revise and retry, and it cannot revise against silence.
REJECT_REASONS: dict[str, dict[str, str]] = {
    "zoho_send_reply": {
        "tone": "Wrong tone, or too technical for this customer. Rewrite it "
        "plainly, and do not include stack traces or internal terminology.",
        "wrong": "Factually wrong or misleading. Re-read the ticket and the "
        "conversation, and state only what the evidence supports.",
        "incomplete": "Incomplete — it does not answer what the customer "
        "actually asked. Address their question directly.",
        "is_bug": "This is a platform bug, not a question. Do not reply; "
        "classify it as a platform bug and file the pager issue instead.",
        "stop": "Stop working this ticket. A human is taking it over. Do not "
        "reply and do not file an issue: summarise what you found and stop.",
    },
    "create_sprint_issue": {
        "not_bug": "This is not a platform bug. Do not open an issue — answer "
        "the customer instead, or explain why neither applies.",
        "classification": "The classification or the affected area is wrong. "
        "Re-read the ticket and correct it; localization keys on that field.",
        "thin": "The filled template is too thin for a developer to pick up. "
        "Add concrete reproduction steps and the real observed behaviour.",
        "duplicate": "This duplicates an issue that already exists. Do not "
        "open a second one; say which issue it duplicates and stop.",
        "stop": "Stop working this ticket. A human is taking it over. Do not "
        "open an issue: summarise what you found and stop.",
    },
}

#: Menu labels. Short, because a select option's text is capped at 75 chars.
#: The full sentence above is what the agent actually receives.
_REASON_LABELS = {
    "tone": "Wrong tone / too technical",
    "wrong": "Factually wrong or misleading",
    "incomplete": "Does not answer the question",
    "is_bug": "This is a platform bug, not a question",
    "not_bug": "Not a platform bug",
    "classification": "Classification or affected area is wrong",
    "thin": "Template too thin to pick up",
    "duplicate": "Duplicate of an existing issue",
    "stop": "Stop — a human is taking this over",
}

#: Credential shapes worth masking if someone pasted one into a ticket.
_SECRET_PATTERNS = [
    re.compile(r"xox[baprse]-[A-Za-z0-9-]{10,}"),          # Slack tokens
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),             # GitHub PATs
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),             # Anthropic keys
    re.compile(r"\b1000\.[a-fA-F0-9]{32}\.[a-fA-F0-9]{32}\b"),  # Zoho OAuth
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{20,}={0,2}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)(AKIA|ASIA)[A-Z0-9]{16}"),            # AWS access key ids
]

REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Mask anything that looks like a credential. Never raises."""
    out = text or ""
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def _escape(text: str) -> str:
    """Slack mrkdwn escaping. Only three characters are special."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _section(text: str, *, verbatim: bool = True) -> dict[str, Any]:
    """An mrkdwn section. ``verbatim`` defaults to True — we are almost always
    rendering text we did not write."""
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": _clip(text, MAX_SECTION_TEXT),
            "verbatim": verbatim,
        },
    }


def _context(text: str) -> dict[str, Any]:
    return {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": _clip(text, MAX_SECTION_TEXT),
                "verbatim": True,
            }
        ],
    }


def _header(text: str) -> dict[str, Any]:
    # A header block takes plain_text only — no mrkdwn, no links.
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": _clip(text, MAX_HEADER_TEXT)},
    }


def verbatim_blocks(body: str) -> list[dict[str, Any]]:
    """Text exactly as written, in blocks Slack will not reformat.

    ``rich_text_preformatted`` holds plain ``text`` elements which are not
    markdown-parsed, so nothing inside the body — a fence, an asterisk, a
    ``<http://…>`` — can change how the rest is displayed.
    """
    chunks: list[str] = []
    current = ""
    for line in (body or "").splitlines(keepends=True):
        while len(line) > MAX_VERBATIM_CHUNK:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:MAX_VERBATIM_CHUNK])
            line = line[MAX_VERBATIM_CHUNK:]
        if len(current) + len(line) > MAX_VERBATIM_CHUNK:
            chunks.append(current)
            current = line
        else:
            current += line
    if current or not chunks:
        chunks.append(current)

    return [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_preformatted",
                    "elements": [{"type": "text", "text": chunk}],
                }
            ],
        }
        for chunk in chunks
    ]


def context_block_id(*, thread_id: str, interrupt_id: str, gate: str) -> str:
    """Where the button's context lives.

    A select option's ``value`` is capped at 75 characters, which a thread
    UUID plus an interrupt id does not fit inside. ``block_id`` allows 255 and
    Slack echoes it back on every action in the block, so the context rides
    there and the option ``value`` carries only a short reason code.
    """
    raw = json.dumps(
        {"t": thread_id, "i": interrupt_id, "g": gate}, separators=(",", ":")
    )
    if len(raw) > MAX_BLOCK_ID:
        raise ValueError(
            f"gate context is {len(raw)} chars, over Slack's {MAX_BLOCK_ID} "
            "block_id limit"
        )
    return raw


def parse_context_block_id(raw: Any) -> dict[str, str]:
    """Inverse of :func:`context_block_id`. Raises ``ValueError`` on junk.

    Untrusted input — it comes back through Slack — so every field is checked
    for presence and coerced rather than trusted.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unparseable gate context: {raw!r}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"gate context is not an object: {raw!r}")
    out = {
        "thread_id": str(data.get("t", "")),
        "interrupt_id": str(data.get("i", "")),
        "gate": str(data.get("g", "")),
    }
    if not out["thread_id"] or not out["interrupt_id"]:
        raise ValueError(f"gate context is missing thread or interrupt: {raw!r}")
    if out["gate"] not in REJECT_REASONS:
        raise ValueError(f"gate context names an unknown gate: {out['gate']!r}")
    return out


def _actions_block(
    *, block_id: str, gate: str, approve_label: str, confirm: dict[str, Any]
) -> dict[str, Any]:
    options = [
        {
            "text": {
                "type": "plain_text",
                "text": _clip(_REASON_LABELS.get(code, code), MAX_OPTION_TEXT),
            },
            "value": code,
        }
        for code in REJECT_REASONS[gate]
    ]
    return {
        "type": "actions",
        "block_id": block_id,
        "elements": [
            {
                "type": "button",
                "action_id": ACTION_APPROVE,
                "style": "primary",
                "text": {
                    "type": "plain_text",
                    "text": _clip(approve_label, MAX_BUTTON_TEXT),
                },
                "value": "approve",
                # A second, deliberate click. This is the only irreversible
                # control in the message, and Slack's confirm dialog is free.
                "confirm": confirm,
            },
            {
                "type": "static_select",
                "action_id": ACTION_REJECT,
                "placeholder": {
                    "type": "plain_text",
                    "text": "Reject with a reason…",
                },
                "options": options,
            },
        ],
    }


def _confirm(title: str, text: str, confirm_label: str) -> dict[str, Any]:
    return {
        "title": {"type": "plain_text", "text": _clip(title, MAX_HEADER_TEXT)},
        "text": {"type": "mrkdwn", "text": _clip(text, MAX_SECTION_TEXT)},
        "confirm": {"type": "plain_text", "text": _clip(confirm_label, 30)},
        "deny": {"type": "plain_text", "text": "Cancel"},
    }


def _ticket_line(payload: dict[str, Any]) -> str:
    ticket = payload.get("ticket") or {}
    subject = redact(_escape(str(ticket.get("subject", "")))) or "(no subject)"
    url = str(ticket.get("url", ""))
    action = payload.get("proposed_action") or {}
    number = str(action.get("ticket_number", ""))
    label = f"Zoho ticket #{number}" if number else "Zoho ticket"
    link = f"<{url}|{label}>" if url.startswith("http") else label
    return f"{link}  ·  *{subject}*"


def _zoho_link(payload: dict[str, Any], label: str) -> str:
    url = str((payload.get("ticket") or {}).get("url", ""))
    return f"<{url}|{label}>" if url.startswith("http") else "the Zoho ticket"


def _budgeted_verbatim(
    body: str, *, budget: int
) -> tuple[list[dict[str, Any]], int]:
    """Verbatim blocks, capped. Returns the blocks and how many were dropped.

    Dropping is only ever safe because the caller posts a banner saying so —
    see :func:`customer_words_blocks`. Same bargain ``_disarm`` makes: the
    reviewer may be shown less than everything, but never *without knowing*.
    """
    chunks = verbatim_blocks(body)
    if len(chunks) <= budget:
        return chunks, 0
    return chunks[:budget], len(chunks) - budget


def customer_words_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """What the customer actually wrote, verbatim.

    This is the half of gate 1 that is not the draft reply. Approving a reply
    without it means approving an answer to a question you have not read, and
    the subject line is not the question.

    Same treatment as the draft reply, for the same reasons: ``rich_text``
    rather than a mrkdwn fence, because customer text can contain a fence and
    break out of one; ``redact`` first, because customers paste credentials
    into support tickets.
    """
    ticket = payload.get("ticket") or {}
    question = redact(str(ticket.get("customer_question", "") or "")).strip()
    followups = [
        redact(str(m)).strip()
        for m in (ticket.get("customer_followups") or [])
        if str(m).strip()
    ]
    carried_truncated = bool(ticket.get("customer_followups_truncated"))

    if not question and not followups:
        # Fail visibly. A reviewer must know the difference between "the
        # customer wrote nothing" and "we failed to carry what they wrote".
        return [
            _section(
                ":warning: *The customer's own words are not in this request.* "
                "Nothing was carried through from the ticket, so there is "
                f"nothing here to check the reply against — read "
                f"{_zoho_link(payload, 'the Zoho ticket')} before approving."
            )
        ]

    blocks: list[dict[str, Any]] = []
    dropped = 0

    if question:
        blocks.append(_section("*What the customer asked, verbatim:*"))
        chunks, dropped = _budgeted_verbatim(question, budget=MAX_QUESTION_BLOCKS)
        blocks.extend(chunks)

    if followups:
        blocks.append(
            _section(
                f"*Then they wrote {len(followups)} more "
                f"message{'s' if len(followups) != 1 else ''}, oldest first, "
                "verbatim:*"
            )
        )
        joined = "\n\n———\n\n".join(followups)
        chunks, dropped_f = _budgeted_verbatim(joined, budget=MAX_FOLLOWUP_BLOCKS)
        blocks.extend(chunks)
        dropped += dropped_f

    if dropped or carried_truncated:
        blocks.append(
            _context(
                ":scissors: *This is not the whole conversation.* It was "
                "shortened to fit in Slack. Read the full thread in "
                f"{_zoho_link(payload, 'the Zoho ticket')} before approving — "
                "what is missing may be what the reply has to answer."
            )
        )
    return blocks


def _warning_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    warnings = [str(w) for w in (payload.get("warnings") or []) if str(w).strip()]
    if not warnings:
        return []
    body = "\n".join(f"• {redact(_escape(w))}" for w in warnings)
    return [_section(f":warning: *The agent flagged this itself:*\n{body}")]


def _disarm(blocks: list[dict[str, Any]], gate: str) -> list[dict[str, Any]]:
    """Too long to show in full: no Approve button, and say why — at the top.

    The banner goes at index 1, immediately under the header, because the
    message is about to be truncated to ``MAX_BLOCKS`` and a banner appended
    at the end would be the first thing cut. That is the exact failure this
    guard exists to prevent: a reviewer seeing a silently shortened reply with
    nothing telling them it was shortened.
    """
    kept = [b for b in blocks if b.get("type") != "actions"]
    kept.insert(1, _too_long_banner(gate))
    return kept[:MAX_BLOCKS]


def _too_long_banner(gate: str) -> dict[str, Any]:
    what = "reply" if gate == "zoho_send_reply" else "issue body"
    return _section(
        f":no_entry: *This {what} is too long to show in full here, so there "
        "is no Approve button.* Approving text you cannot see is the failure "
        "this gate exists to prevent. Read the full text from the thread "
        "state and answer it there — see the README."
    )


def render_gate_message(
    payload: dict[str, Any], *, thread_id: str, interrupt_id: str
) -> dict[str, Any]:
    """Render one parked gate.

    Returns ``{"text": <notification fallback>, "blocks": [...]}``. The
    ``text`` field is what appears in push notifications and what screen
    readers announce, so it is never omitted — but it is a *summary*, and the
    thing being approved is only ever in the blocks.
    """
    gate = str(payload.get("gate", ""))
    if gate not in REJECT_REASONS:
        raise ValueError(f"unknown gate {gate!r}; refusing to render")

    block_id = context_block_id(
        thread_id=thread_id, interrupt_id=interrupt_id, gate=gate
    )

    if gate == "zoho_send_reply":
        blocks, fallback = _render_reply_gate(payload, block_id)
    else:
        blocks, fallback = _render_issue_gate(payload, block_id)

    if payload.get("version") != 1:
        # A v2 payload rendered by a v1 renderer is a silent mis-render, which
        # is exactly what GATE_PAYLOAD_VERSION exists to prevent. Show the
        # banner and take the controls away.
        blocks = [b for b in blocks if b.get("type") != "actions"]
        blocks.insert(
            0,
            _section(
                f":rotating_light: *This gate payload is version "
                f"{payload.get('version')!r}, but this Slack renderer only "
                "understands version 1.* Fields may be missing or wrong, so "
                "there are no buttons. Answer it from the thread state."
            ),
        )

    return {"text": fallback, "blocks": blocks[:MAX_BLOCKS]}


def _render_reply_gate(
    payload: dict[str, Any], block_id: str
) -> tuple[list[dict[str, Any]], str]:
    action = payload.get("proposed_action") or {}
    reply_text = redact(str(action.get("reply_text", "")))
    to = redact(_escape(str(action.get("to", "")))) or "(unknown recipient)"
    ticket = payload.get("ticket") or {}
    url = str(ticket.get("url", ""))

    blocks: list[dict[str, Any]] = [
        _header("Send this reply to a customer?"),
        _section(_ticket_line(payload)),
    ]
    # The question first, then the answer — the order a reviewer reads in.
    blocks.extend(customer_words_blocks(payload))
    blocks.append(
        _section(
            f"*This exact text will be emailed to* `{to}` *if you approve.* "
            "It is shown below verbatim and unedited."
        )
    )
    blocks.extend(
        verbatim_blocks(reply_text or "(the agent proposed an empty reply)")
    )

    if url.startswith("http"):
        blocks.append(
            _context(f"Full ticket, with attachments and history: <{url}|Zoho Desk>.")
        )
    reasoning = str(payload.get("reasoning", "")).strip()
    if reasoning:
        blocks.append(
            _context(f"*Why the agent wants to send this:* {redact(_escape(reasoning))}")
        )
    blocks.extend(_warning_blocks(payload))

    if len(blocks) + 1 <= MAX_BLOCKS:
        blocks.append(
            _actions_block(
                block_id=block_id,
                gate="zoho_send_reply",
                approve_label="Approve and send",
                confirm=_confirm(
                    "Send this to the customer?",
                    "This emails the text above to the customer. There is no "
                    "undo.",
                    "Send it",
                ),
            )
        )
    else:
        blocks = _disarm(blocks, "zoho_send_reply")

    subject = str(ticket.get("subject", ""))
    return blocks, f"Approval needed — send a customer reply on: {subject}"


def _render_issue_gate(
    payload: dict[str, Any], block_id: str
) -> tuple[list[dict[str, Any]], str]:
    action = payload.get("proposed_action") or {}
    repo = redact(_escape(str(action.get("repo", "")))) or "(unknown repo)"
    title = redact(_escape(str(action.get("title", "")))) or "(no title)"
    labels = ", ".join(
        f"`{_escape(str(label))}`" for label in (action.get("labels") or [])
    )
    area = redact(_escape(str(action.get("affected_areas", "")))) or "(none)"
    body = redact(str(action.get("body", "")))
    classification = (
        redact(_escape(str(payload.get("classification", "")))) or "(none)"
    )

    blocks: list[dict[str, Any]] = [
        _header("File this as a platform bug?"),
        _section(_ticket_line(payload)),
        _section(
            ":rotating_light: *Approving files a real issue in sprint-tasks.* "
            "It does not start the fix agent on its own — that needs the "
            "`agent-fix` label, added to the issue afterwards. Once it is, an "
            "unattended chain begins: an agent clones the code repos, writes a "
            "fix, pushes branches and opens *draft* pull requests. Reject if "
            "you are not sure this is a platform bug."
        ),
        _section(
            f"*Repo:* `{repo}`\n"
            f"*Title:* {title}\n"
            f"*Labels:* {labels or '(none)'}\n"
            f"*Classification:* {classification}\n"
            f"*Affected area:* {area}"
        ),
        _section("*The issue body, verbatim:*"),
    ]
    blocks.extend(verbatim_blocks(body or "(empty body)"))

    reasoning = str(payload.get("reasoning", "")).strip()
    if reasoning:
        blocks.append(
            _context(
                f"*Why the agent calls this a platform bug:* "
                f"{redact(_escape(reasoning))}"
            )
        )
    blocks.extend(_warning_blocks(payload))

    if len(blocks) + 1 <= MAX_BLOCKS:
        blocks.append(
            _actions_block(
                block_id=block_id,
                gate="create_sprint_issue",
                approve_label="Approve and file",
                confirm=_confirm(
                    "File this issue in sprint-tasks?",
                    "This opens a real issue. Adding the `agent-fix` label to "
                    "it afterwards is what starts the fix agent, which pushes "
                    "branches and opens draft PRs.",
                    "File it",
                ),
            )
        )
    else:
        blocks = _disarm(blocks, "create_sprint_issue")

    subject = str((payload.get("ticket") or {}).get("subject", ""))
    return blocks, f"Approval needed — file a sprint-tasks issue for: {subject}"


def answered_message(
    *, gate: str, approved: bool, approver_user_id: str, reason: str
) -> dict[str, Any]:
    """What replaces the original message once the gate has been answered.

    Replacing it is what stops the same reviewer, on a second device or after
    a scroll-back, seeing live buttons for a gate that is already closed.
    ``replace_original`` is documented on ``response_url``, which Slack allows
    for 30 minutes and five uses after the interaction — ample for one edit.
    """
    who = f"<@{approver_user_id}>"
    what = (
        "sending the reply to the customer"
        if gate == "zoho_send_reply"
        else "filing the sprint-tasks issue"
    )
    if approved:
        line = f":white_check_mark: *Approved by {who}* — {what} now."
    else:
        line = (
            f":x: *Rejected by {who}.* Nothing was done. The agent was told:\n"
            f">{_escape(reason)}"
        )
    return {
        "replace_original": "true",
        "text": line,
        "blocks": [_section(line)],
    }


def refusal_text(user_id: str) -> str:
    """Shown, only to the clicker, when they are not an approver."""
    return (
        ":no_entry: You are not an approver for pager-duty gates, so nothing "
        f"happened. (Your Slack id is `{user_id}`; it is not in "
        "`SLACK_APPROVER_USER_IDS`.) The request is still waiting."
    )


def already_answered_text() -> str:
    return (
        ":information_source: This gate has already been answered, so nothing "
        "happened just now. A parked thread is resumed exactly once."
    )


def resume_failed_text(detail: str) -> str:
    """Shown when the resume did not go through and the gate is still open.

    Deliberately worded as the opposite of :func:`already_answered_text`. The
    reviewer must come away knowing the request is *still waiting on them*,
    because the failure mode this replaces told them the opposite and left a
    ticket stranded with nobody expecting to act on it.
    """
    return (
        f":warning: That click did not go through: {detail}. Nothing was "
        "approved, the request is *still waiting*, and the buttons still "
        "work — please click again."
    )


def run_failed_text(detail: str) -> str:
    """Shown when the approval took effect but the run then died.

    Deliberately *not* "click again": the interrupt is spent, so a second
    click cannot help and telling the reviewer to try would waste their time
    and hide the fact that a ticket now needs hands-on attention.
    """
    return (
        f":rotating_light: Your approval was recorded, but the run then "
        f"failed: {detail}. The requested action did *not* happen. Clicking "
        "again will not help — this ticket needs a look."
    )


def error_text(detail: str) -> str:
    return (
        f":warning: That click could not be processed: {detail}. Nothing was "
        "approved and the request is still waiting."
    )
