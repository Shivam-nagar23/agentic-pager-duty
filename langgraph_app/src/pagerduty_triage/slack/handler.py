"""What happens when a button is clicked: verify, authorize, resume.

Deliberately framework-free. :func:`handle_interaction` takes raw bytes and a
header mapping and returns a plain :class:`HandlerResult`; ``http_app.py`` is
the twenty lines that turn that into an ASGI response. The whole
authorization decision is therefore testable without an HTTP server, and
without a Slack.

## The order is the design

    verify signature  ->  parse  ->  authorize user  ->  resume
         |                              |
      401, nothing else happens      refused, visibly, thread untouched

Nothing before ``verify`` inspects the body, and nothing after a failed
authorization touches LangGraph. Both are load-bearing:

* **Verification first** because the body is attacker-controlled until the
  HMAC passes. Parsing it earlier would mean parsing hostile input on an
  unauthenticated public route.
* **Authorization before resume** because resuming *is* the privileged act.
  Checking afterwards would mean the mail is already sent.

## Acknowledging inside three seconds

Slack requires an HTTP 200 within three seconds or it shows the reviewer an
error. Resuming a thread and updating the message are two or three network
calls, so they are **not** done inline: :class:`HandlerResult` carries a
``followup`` callable that ``http_app.py`` runs as a background task after
the 200 has already gone out. Everything the reviewer sees afterwards arrives
through ``response_url``, which Slack keeps valid for 30 minutes.

## What this never does

It never approves. There is no code path in this file that sends
``{"approved": True}`` without an explicit ``gate_approve`` action from an
allowlisted user id on a currently-parked interrupt. Timeouts, retries,
parse failures, unknown actions and unknown users all resolve to "do
nothing, and say so".
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .client import SlackClient
from .config import SlackSettings
from .renderer import (
    ACTION_APPROVE,
    ACTION_REJECT,
    REJECT_REASONS,
    already_answered_text,
    answered_message,
    error_text,
    parse_context_block_id,
    refusal_text,
    resume_failed_text,
    run_failed_text,
)
from .resume import (
    AlreadyAnswered,
    ResumeFailed,
    ResumeRunFailed,
    ThreadStateClient,
    resume_gate,
)
from .signing import VerificationFailure, verify_request

logger = logging.getLogger(__name__)


@dataclass
class HandlerResult:
    """What the HTTP layer should do.

    ``status`` is what Slack sees immediately. ``followup`` is what runs
    after the response has been sent — always ``None`` for a rejected
    request, so an unverified caller can never cause outbound work.
    """

    status: int
    body: str = ""
    followup: Callable[[], None] | None = None
    #: For logs and tests. Never returned to the caller over HTTP.
    outcome: str = ""
    detail: str = ""


def _ephemeral(text: str) -> dict[str, Any]:
    """A response_url body that only the clicker sees.

    Slack's documented default for ``response_url`` is ephemeral, but it is
    stated explicitly here because the default is not obvious to a reader and
    getting it wrong would post a refusal into the channel.
    """
    return {"response_type": "ephemeral", "replace_original": "false", "text": text}


def handle_interaction(
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    settings: SlackSettings,
    slack: SlackClient,
    threads: ThreadStateClient,
    now: float | None = None,
) -> HandlerResult:
    """Handle one Slack interaction POST. Never raises."""

    # ---- 1. Is this really Slack, and is it recent? ------------------------
    # Fail-closed and detail-free: an unauthenticated caller learns only that
    # they were refused, never which check refused them.
    try:
        verify_request(
            signing_secret=settings.signing_secret,
            headers=headers,
            body=raw_body,
            max_skew_seconds=settings.max_skew_seconds,
            now=now,
        )
    except VerificationFailure as exc:
        return HandlerResult(
            status=401, body="", outcome="unverified", detail=str(exc)
        )

    # ---- 2. Parse. Only now, and defensively. ------------------------------
    try:
        payload = _parse_payload(raw_body)
    except ValueError as exc:
        return HandlerResult(status=400, outcome="unparseable", detail=str(exc))

    if payload.get("type") != "block_actions":
        # One Request URL receives every interaction type. Anything we did not
        # ask for is acknowledged and ignored rather than guessed at.
        return HandlerResult(
            status=200, outcome="ignored", detail=str(payload.get("type"))
        )

    response_url = str(payload.get("response_url", ""))
    user_id = str(((payload.get("user") or {}).get("id")) or "")

    # ---- 3. Is this person allowed to answer a gate? -----------------------
    if not settings.is_approver(user_id):
        # Visibly refused, and the gate stays parked. Note the ordering: we
        # have not looked at which thread or which action yet, so a
        # non-approver cannot even probe for valid thread ids.
        return HandlerResult(
            status=200,
            outcome="not_approver",
            detail=user_id,
            followup=_responder(slack, response_url, _ephemeral(refusal_text(user_id))),
        )

    # ---- 4. Which gate, and what did they choose? --------------------------
    try:
        action = _first_action(payload)
        context = parse_context_block_id(action.get("block_id"))
        approved, reason = _decision_from(action, context["gate"])
    except ValueError as exc:
        return HandlerResult(
            status=200,
            outcome="bad_action",
            detail=str(exc),
            followup=_responder(
                slack, response_url, _ephemeral(error_text(str(exc)))
            ),
        )

    # ---- 5. Resume, after the 200. -----------------------------------------
    def followup() -> None:
        try:
            resume_gate(
                client=threads,
                thread_id=context["thread_id"],
                interrupt_id=context["interrupt_id"],
                assistant_id=settings.triage_assistant_id,
                approved=approved,
                reason=reason,
                approver_user_id=user_id,
            )
        except AlreadyAnswered as exc:
            # The gate really is closed. Saying so is correct here, and it is
            # the only branch allowed to say so — see the ResumeFailed branch.
            logger.info(
                "slack gate click ignored, already answered: thread=%s "
                "interrupt=%s gate=%s: %s",
                context["thread_id"],
                context["interrupt_id"],
                context["gate"],
                exc,
            )
            _safe(slack.respond, response_url, _ephemeral(already_answered_text()))
            return
        except ResumeRunFailed as exc:
            # The gate IS spent, so this is not retryable and must not be
            # dressed up as one. The reviewer needs to know the action did not
            # happen even though their click was accepted.
            logger.error(
                "slack gate resumed but the run FAILED, gate is spent: "
                "thread=%s interrupt=%s gate=%s: %s",
                context["thread_id"],
                context["interrupt_id"],
                context["gate"],
                exc,
            )
            _safe(slack.respond, response_url, _ephemeral(run_failed_text(str(exc))))
            return
        except ResumeFailed as exc:
            # The gate is still open and this click did nothing. Never report
            # this as "already answered": that wording tells the reviewer to
            # stop clicking, which is how a recoverable failure turns into a
            # permanently stranded ticket.
            logger.warning(
                "slack gate resume FAILED, gate still answerable: thread=%s "
                "interrupt=%s gate=%s: %s",
                context["thread_id"],
                context["interrupt_id"],
                context["gate"],
                exc,
            )
            _safe(slack.respond, response_url, _ephemeral(resume_failed_text(str(exc))))
            return
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
            # Unknown failure. It is not known to have consumed the gate, so
            # it is treated as retryable, exactly like ResumeFailed.
            logger.exception(
                "slack gate resume errored, gate still answerable: thread=%s "
                "interrupt=%s gate=%s",
                context["thread_id"],
                context["interrupt_id"],
                context["gate"],
            )
            _safe(
                slack.respond,
                response_url,
                _ephemeral(resume_failed_text(f"the thread could not be resumed ({exc!r})")),
            )
            return

        logger.info(
            "slack gate resumed: thread=%s interrupt=%s gate=%s approved=%s "
            "approver=%s",
            context["thread_id"],
            context["interrupt_id"],
            context["gate"],
            approved,
            user_id,
        )
        # Replace the original message so its buttons are gone. Cosmetic: the
        # resume already happened, and a failure here cannot undo it.
        _safe(
            slack.respond,
            response_url,
            answered_message(
                gate=context["gate"],
                approved=approved,
                approver_user_id=user_id,
                reason=reason,
            ),
        )

    return HandlerResult(
        status=200,
        outcome="approved" if approved else "rejected",
        detail=context["thread_id"],
        followup=followup,
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _parse_payload(raw_body: bytes) -> dict[str, Any]:
    """``application/x-www-form-urlencoded`` with a ``payload=`` JSON field.

    Slack documents this encoding for every interaction type. The HMAC covers
    these percent-encoded bytes, which is why verification happens before this
    function is ever called.
    """
    try:
        form = urllib.parse.parse_qs(raw_body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("body is not valid UTF-8") from exc
    values = form.get("payload") or []
    if not values:
        raise ValueError("no payload field in the form body")
    try:
        payload = json.loads(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload is not a JSON object")
    return payload


def _first_action(payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("the interaction carried no actions")
    action = actions[0]
    if not isinstance(action, dict):
        raise ValueError("the first action is not an object")
    return action


def _decision_from(action: dict[str, Any], gate: str) -> tuple[bool, str]:
    """Map one Slack action onto ``(approved, reason)``.

    Only ``gate_approve`` yields ``True``, and only for the exact action id.
    Everything unrecognised raises, which the caller turns into "nothing
    happened" — an unknown control must never resolve to approval.
    """
    action_id = str(action.get("action_id", ""))

    if action_id == ACTION_APPROVE:
        return True, ""

    if action_id == ACTION_REJECT:
        selected = action.get("selected_option")
        code = ""
        if isinstance(selected, dict):
            code = str(selected.get("value", ""))
        if not code:
            # Not a documented shape for a select, but cheap to tolerate: a
            # plain ``value`` would appear here if the control were ever
            # rendered as a button instead.
            code = str(action.get("value", ""))
        reason = REJECT_REASONS.get(gate, {}).get(code, "")
        if not reason:
            raise ValueError(f"unknown rejection reason {code!r}")
        # A reject always carries a reason. `gates.py` turns a reasonless
        # rejection into "rejected without a reason", which the agent cannot
        # revise against — so there is no option here that produces one.
        return False, reason

    raise ValueError(f"unknown action {action_id!r}")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _responder(
    slack: SlackClient, response_url: str, body: dict[str, Any]
) -> Callable[[], None]:
    def run() -> None:
        _safe(slack.respond, response_url, body)

    return run


def _safe(fn: Callable[..., Any], *args: Any) -> None:
    """Talking back to Slack is best-effort. A failed cosmetic update must
    not become an exception in a background task nobody is watching.

    Best-effort is not the same as invisible: if we cannot even tell the
    reviewer what went wrong, that fact is logged rather than dropped."""
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        logger.exception("could not talk back to Slack; the reviewer was not told")
