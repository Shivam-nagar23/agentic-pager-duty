"""The return path: turning an answered Slack message back into a running graph.

This module is where "a double-click must not double-resume" is won or lost,
so the mechanism is spelled out rather than assumed.

## What a resume actually is

A parked thread is a thread whose latest checkpoint has a task holding a
pending ``interrupt``. Answering it means creating a new run on that thread
with a ``Command(resume=<value>)``; LangGraph re-executes the interrupted task
from its first line, and ``interrupt()`` returns ``<value>`` instead of
raising. The value we send is exactly the envelope ``GateDecision.parse``
already accepts — ``{"approved": true}`` or
``{"approved": false, "reason": ...}`` — so the Slack layer introduces no new
contract.

## Exactly-once, honestly labelled

Three layers, and only the first two are enforcement:

1. **The pending-interrupt precondition.** Before resuming, read the thread
   state and require that it is still parked on the *same* interrupt id the
   button was rendered for. A first click consumes that interrupt; by the time
   a second click arrives the thread is either running or finished, and in
   both cases the precondition fails and we refuse. This is what makes a
   late second click (the reviewer coming back and clicking again) safe.

2. **``multitask_strategy="reject"`` on the resume run.** Two clicks landing
   inside the same second can both pass the precondition — the read is not
   atomic with the write. This is the backstop for that race: the platform
   refuses to create a second run while one is in flight on the thread, and
   that refusal is a database-level decision, not ours. It is the same
   mechanism ``poller.py`` relies on, for the same reason.

3. **The ledger guards inside the gated tools.** Not part of this module, but
   worth stating: even if both layers above failed, ``zoho_send_reply``
   re-checks ``ledger.reply_record`` after the gate and refuses to send a
   second mail. A double resume costs a wasted model turn, not a second email
   to a paying customer.

**What is deliberately NOT the lock:** a record in the store. ``BaseStore.put``
is last-write-wins with no compare-and-set, so a get-then-put claim would look
correct in tests and race in production — the project already learned this in
``ledger.py`` and the lesson applies unchanged here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class AlreadyAnswered(Exception):
    """The thread is not parked on the interrupt this click was rendered for.

    Raised for a double-click, a Slack redelivery, and a click on a message
    that someone already answered. All three are the same condition and all
    three get the same answer: do nothing, and say so.
    """


class ResumeFailed(Exception):
    """The resume could not be attempted or did not go through.

    Distinct from :class:`AlreadyAnswered` on purpose, and the distinction is
    the whole point: ``AlreadyAnswered`` means *the gate is closed, correctly*
    and a second click must do nothing. ``ResumeFailed`` means *the gate is
    still open* and this click achieved nothing -- so the reviewer must be
    told to click again, not told it was already handled.

    Collapsing the two is what stranded ticket #101: a state-reading bug
    raised ``AlreadyAnswered`` for a thread that was still parked, so the
    reviewer was told the gate was closed while it was in fact untouched, and
    there was no path back to it. Anything that is not positively known to
    have consumed the gate belongs here.
    """


class ResumeRunFailed(Exception):
    """The run was created, consumed the gate, and then died.

    The opposite of :class:`ResumeFailed` in the one way that matters to the
    reviewer: clicking again will *not* help, because the interrupt is
    already spent. This needs a human to look, not a retry.

    It exists because "the run was created" is not the same as "the approval
    took effect", and reporting the first as the second is how two tickets
    were spent while Slack showed a green tick.
    """


class ResumeConflict(Exception):
    """The platform refused the run because one is already in flight (409)."""


class ThreadStateClient(Protocol):
    """The slice of ``langgraph_sdk`` this module uses.

    Narrow on purpose: it keeps the handler testable against a fake and makes
    the exact platform semantics we depend on explicit rather than implied.
    """

    def pending_interrupt_ids(self, thread_id: str) -> list[str]:
        """Ids of interrupts the thread is currently parked on.

        Empty when the thread is running or finished.
        """

    def resume(self, thread_id: str, *, assistant_id: str, value: Any) -> Any:
        """Create a run carrying ``Command(resume=value)``.

        MUST pass ``multitask_strategy="reject"`` and MUST raise
        ``ResumeConflict`` on the platform's 409. That exception is layer 2.
        """


@dataclass(frozen=True)
class ResumeOutcome:
    thread_id: str
    interrupt_id: str
    approved: bool
    reason: str
    approver: str


def resume_gate(
    *,
    client: ThreadStateClient,
    thread_id: str,
    interrupt_id: str,
    assistant_id: str,
    approved: bool,
    reason: str,
    approver_user_id: str,
) -> ResumeOutcome:
    """Answer one parked gate, exactly once.

    Raises ``AlreadyAnswered`` if the thread has moved on. Never retries: a
    retry here is indistinguishable from a second click.
    """
    pending = client.pending_interrupt_ids(thread_id)
    if interrupt_id not in pending:
        raise AlreadyAnswered(
            f"thread {thread_id} is not parked on interrupt {interrupt_id}"
        )
    if len(pending) > 1:
        # A plain `Command(resume=value)` is ambiguous when several tasks are
        # parked — LangGraph raises rather than guessing, and so do we. The
        # design has one gate at a time (the two gates are sequential in one
        # deep agent), so this is a "something changed" signal, not a case to
        # handle. The map form, `resume={interrupt_id: value}`, is the fix if
        # parallel gates ever become real.
        #
        # ResumeFailed, not AlreadyAnswered: refusing to guess leaves the gate
        # exactly as it was, so the reviewer must be able to click again once
        # whatever produced the ambiguity is resolved.
        raise ResumeFailed(
            f"thread {thread_id} has {len(pending)} pending interrupts "
            f"({', '.join(pending)}); refusing to guess which one this click "
            "answers. The gate is untouched and can still be answered."
        )

    value: dict[str, Any] = {"approved": bool(approved)}
    if reason:
        value["reason"] = reason
    # Recorded in the thread for free: GateDecision.extra keeps unknown keys,
    # so the approver's identity ends up in the graph's own history without
    # the gate code needing to know Slack exists.
    value["approver"] = approver_user_id
    value["approved_via"] = "slack"

    try:
        client.resume(thread_id, assistant_id=assistant_id, value=value)
    except ResumeConflict as exc:
        raise AlreadyAnswered(
            f"a run is already in flight on thread {thread_id}"
        ) from exc

    return ResumeOutcome(
        thread_id=thread_id,
        interrupt_id=interrupt_id,
        approved=bool(approved),
        reason=reason,
        approver=approver_user_id,
    )


@dataclass
class FakeThreadStateClient:
    """In-memory stand-in for LangGraph Platform. Never networked.

    ``parked`` maps thread id -> list of pending interrupt ids. ``resume``
    pops the answered interrupt, which is exactly what the real platform does
    to the checkpoint, and is what makes the double-click test meaningful.
    """

    parked: dict[str, list[str]] = field(default_factory=dict)
    resumes: list[dict[str, Any]] = field(default_factory=list)
    #: Set to simulate a run already in flight: the next resume raises 409.
    conflict_on_next: bool = False
    #: Set to simulate a run that is created, consumes the gate, and then
    #: dies. The gate is spent, so the fake pops the interrupt as the real
    #: platform would before raising.
    run_dies: bool = False
    #: Set to simulate a transient platform failure — a dropped connection, a
    #: 5xx. The next resume raises this and, crucially, leaves ``parked``
    #: alone, which is what makes "a failed resume stays answerable" testable.
    fail_next_resume_with: Exception | None = None

    def park(self, thread_id: str, interrupt_id: str) -> None:
        self.parked.setdefault(thread_id, []).append(interrupt_id)

    def pending_interrupt_ids(self, thread_id: str) -> list[str]:
        return list(self.parked.get(thread_id, []))

    def resume(self, thread_id: str, *, assistant_id: str, value: Any) -> Any:
        if self.conflict_on_next:
            self.conflict_on_next = False
            raise ResumeConflict("409")
        if self.fail_next_resume_with is not None:
            exc, self.fail_next_resume_with = self.fail_next_resume_with, None
            raise exc
        ids = self.parked.get(thread_id, [])
        if ids:
            ids.pop(0)
        if self.run_dies:
            self.run_dies = False
            raise ResumeRunFailed("run died after the gate was consumed")
        self.resumes.append(
            {"thread_id": thread_id, "assistant_id": assistant_id, "value": value}
        )
        return {"run_id": f"run-{len(self.resumes)}"}
