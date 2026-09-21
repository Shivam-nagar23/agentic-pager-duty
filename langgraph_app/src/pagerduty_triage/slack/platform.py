"""The real LangGraph Platform client. Imported only at runtime, never by tests.

Two jobs, both thin:

* find threads parked on an interrupt, for the notifier;
* read a thread's pending interrupts and resume it, for the handler.

Everything above this file talks to the ``ThreadStateClient`` protocol in
``resume.py`` or to ``ParkedGate`` below, so the whole Slack layer is
exercised against fakes with no platform and no network.

## Facts this file depends on, and where they came from

Checked against ``langgraph_sdk`` 0.4.x source rather than recalled:

* ``get_sync_client(url=…, api_key=…)`` returns a synchronous client with the
  same surface as the async one. The handler runs in Starlette's threadpool,
  so sync is the right flavour and avoids an event loop inside a background
  task.
* ``client.runs.create(thread_id, assistant_id, command={"resume": value},
  multitask_strategy="reject")`` POSTs
  ``{"assistant_id": …, "command": {"resume": …}}`` to
  ``/threads/{id}/runs``.
* **The SDK strips ``None``**, at both levels: ``command={"resume": None}``
  serialises to ``{"command": {}}`` and the server rejects it as an empty
  input. A rejection must therefore never resume with ``None`` — ours never
  does, it resumes with ``{"approved": False, "reason": …}``, and
  ``_reject_none`` makes that a loud failure rather than a silent no-op.
* ``client.threads.search(status="interrupted")`` returns thread rows that
  already carry ``interrupts``: a ``{task_id: [Interrupt]}`` mapping. One
  call, no second round trip.
* An ``Interrupt`` is ``{"value": …, "id": …}``. The field is ``id``, not
  ``interrupt_id``; some published examples still show an older shape without
  it.
* ``client.threads.get_state(thread_id)`` returns a flat top-level
  ``interrupts`` list as well as ``tasks[].interrupts``. Both are read here,
  because the flat field is the newer of the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..tools.deps import TICKET_CONTEXT_KEY
from .resume import ResumeConflict, ResumeFailed, ResumeRunFailed


@dataclass(frozen=True)
class ParkedGate:
    """One thread waiting on one interrupt."""

    thread_id: str
    interrupt_id: str
    payload: dict[str, Any]


def _is_conflict(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 409 or "409" in str(exc)


def _is_interrupt(row: Any) -> bool:
    """Is this dict an ``Interrupt``, as opposed to a task that holds some?

    ``"id" in row`` is *not* enough, and assuming it was is what produced the
    bug this function now guards. A task row from ``threads.get_state`` also
    carries an ``id`` -- its own task id -- so the loose test yielded tasks as
    though they were interrupts. The resulting id list held a real interrupt
    id *and* a task id, which made ``resume_gate`` see two "pending
    interrupts" on a thread parked on exactly one, refuse to guess between
    them, and report a perfectly answerable gate as already answered. No run
    was ever created and the gate was permanently unclickable.

    An ``Interrupt`` is ``{"id": ..., "value": ...}``; a task is
    ``{"id", "name", "interrupts", ...}`` and has no ``value``. Requiring
    ``value`` is what tells them apart.
    """
    return isinstance(row, dict) and "id" in row and "value" in row


def _interrupt_rows(container: Any) -> Iterable[dict[str, Any]]:
    """Yield ``Interrupt`` dicts out of whichever shape we were handed.

    ``threads.search`` gives ``{task_id: [Interrupt]}``; ``threads.get_state``
    gives both a flat ``[Interrupt]`` and a ``tasks`` list whose entries hold
    their own ``interrupts``. Tolerating all three keeps this the only place
    that needs to know.
    """
    if isinstance(container, dict):
        if _is_interrupt(container):
            yield container
            return
        if "interrupts" in container:
            # A task row. Descend into the interrupts it holds; never yield
            # the task itself, whose id is a task id and not an interrupt id.
            yield from _interrupt_rows(container["interrupts"])
            return
        # A ``{task_id: [Interrupt]}`` mapping from ``threads.search``.
        for value in container.values():
            yield from _interrupt_rows(value)
    elif isinstance(container, list):
        for item in container:
            yield from _interrupt_rows(item)


class PlatformThreads:
    """Sync LangGraph Platform access. Satisfies ``ThreadStateClient``."""

    def __init__(self, *, url: str, api_key: str = ""):
        from langgraph_sdk import get_sync_client

        self._client = get_sync_client(url=url, api_key=api_key or None)

    # -- notifier side -------------------------------------------------------

    def parked_gates(self, *, limit: int = 50) -> list[ParkedGate]:
        """Every thread currently parked on an interrupt, with its payload."""
        rows = self._client.threads.search(status="interrupted", limit=limit)
        out: list[ParkedGate] = []
        for row in rows or []:
            thread_id = str(row.get("thread_id", ""))
            if not thread_id:
                continue
            for interrupt in _interrupt_rows(row.get("interrupts")):
                value = interrupt.get("value")
                out.append(
                    ParkedGate(
                        thread_id=thread_id,
                        interrupt_id=str(interrupt.get("id", "")),
                        payload=value if isinstance(value, dict) else {},
                    )
                )
        return out

    # -- ThreadStateClient ---------------------------------------------------

    def pending_interrupt_ids(self, thread_id: str) -> list[str]:
        state = self._client.threads.get_state(thread_id) or {}
        ids: list[str] = []
        for source in (state.get("interrupts"), state.get("tasks")):
            for interrupt in _interrupt_rows(source):
                interrupt_id = str(interrupt.get("id", ""))
                if interrupt_id and interrupt_id not in ids:
                    ids.append(interrupt_id)
        return ids

    def ticket_context(self, thread_id: str) -> dict[str, Any]:
        """The ticket bound to this thread, read back from thread metadata.

        Run-level ``configurable`` does not survive into later runs, and a
        resume *is* a later run. The poller therefore writes the context onto
        the thread, and this reads it back so the resume can re-supply it.
        """
        thread = self._client.threads.get(thread_id) or {}
        metadata = thread.get("metadata") or {}
        bound = metadata.get(TICKET_CONTEXT_KEY)
        return bound if isinstance(bound, dict) and bound else {}

    def resume(self, thread_id: str, *, assistant_id: str, value: Any) -> Any:
        _reject_none(value)
        bound = self.ticket_context(thread_id)
        if not bound:
            # Refuse rather than resume unbound. A resume without the ticket
            # context does not fail harmlessly: it consumes the gate and then
            # dies inside the tool with MissingTicketContext, which is how
            # ticket #101 was spent without an issue ever being created.
            # ResumeFailed keeps the gate answerable once the thread is
            # backfilled.
            raise ResumeFailed(
                f"thread {thread_id} has no {TICKET_CONTEXT_KEY!r} in its "
                "metadata, so a resume would re-enter the graph with no "
                "ticket bound and die after consuming the gate. The gate is "
                "untouched. Threads created before the context was persisted "
                "need backfilling."
            )
        try:
            run = self._client.runs.create(
                thread_id,
                assistant_id,
                command={"resume": value},
                # Re-supply what the original run carried. Without this the
                # tools cannot tell which ticket they are acting on.
                config={"configurable": {TICKET_CONTEXT_KEY: bound}},
                # The platform default is `enqueue`, which would *queue* a
                # second resume rather than refuse it. For gate 1 that is a
                # second email to a paying customer.
                multitask_strategy="reject",
            )
        except Exception as exc:  # noqa: BLE001
            if _is_conflict(exc):
                raise ResumeConflict(str(exc)) from exc
            raise

        self._raise_if_the_run_died(thread_id, run)
        return run

    def _raise_if_the_run_died(self, thread_id: str, run: Any) -> None:
        """Wait for the resume run to finish, and fail if it failed.

        ``runs.create`` returns the moment the run is *queued*; the graph then
        succeeds or dies asynchronously. Returning straight after create is
        why two tickets were spent while Slack showed the reviewer a green
        tick -- the run died of ``MissingTicketContext`` a second later and
        nothing reported it back.

        The wait is ``runs.join``, which blocks **server-side**. That matters
        beyond convenience: nothing in this package may run a client-side
        timer, because a timer that can fire is a timer that could one day
        approve something. ``join`` adds no clock here -- it returns when the
        run reaches a terminal state, and this code has no opinion about when
        that is. The caller is already a background task, so blocking costs
        the reviewer nothing but a few seconds of latency on the
        confirmation.
        """
        run_id = str((run or {}).get("run_id", "")) if isinstance(run, dict) else ""
        if not run_id:
            return
        try:
            self._client.runs.join(thread_id, run_id)
            status = str(
                (self._client.runs.get(thread_id, run_id) or {}).get("status", "")
            )
        except Exception:  # noqa: BLE001
            # Losing sight of the run is not evidence that it failed, and
            # claiming failure here would send the reviewer chasing a ghost.
            return
        if status == "error":
            raise ResumeRunFailed(
                f"run {run_id} on thread {thread_id} was created and then "
                "failed, so the gate is spent but the work did not happen. "
                "Clicking again will not help; this needs a look."
            )


def _reject_none(value: Any) -> None:
    """Guard the ``None``-stripping trap described in the module docstring."""
    if value is None or value == {}:
        raise ValueError(
            "refusing to resume with an empty value: the SDK strips None, so "
            "this would serialise to {'command': {}} and either error or "
            "resume as something other than the decision that was made"
        )
