"""Reading pending interrupts out of the *real* platform payload shapes.

This file exists because ``platform.py`` had no tests at all, and that gap had
a cost. Every other suite exercises ``FakeThreadStateClient``, which models a
thread as ``{thread_id: [interrupt_id]}`` — a shape in which the bug below is
not expressible. The fake agreed with the code, the code was wrong, and the
first thing to notice was a production gate that could not be clicked.

So these tests deliberately feed in payloads copied from a live
``langgraph dev`` server (thread ``22406d5b…``, ticket #101, parked at gate 2)
rather than payloads invented to match the parser.
"""

from __future__ import annotations

import pytest

from pagerduty_triage.slack.platform import PlatformThreads, _interrupt_rows
from pagerduty_triage.slack.resume import ResumeFailed, ResumeRunFailed

# The real interrupt id, and the real *task* id that holds it. Keeping both
# literal is the point of the test: they are different values, and only one of
# them may ever be called an interrupt id.
INTERRUPT_ID = "ad48fdc772c48291f25b8f06f7ec2864"
TASK_ID = "c60fecff-9c2c-bc5b-85f9-c8aceec8ce08"

INTERRUPT_ROW = {"id": INTERRUPT_ID, "value": {"gate": "create_sprint_issue"}}

# Abridged from GET /threads/{id}/state. The keys that matter are all here:
# a flat `interrupts` list AND a `tasks` list whose entries carry their own
# `id` (a task id) alongside their `interrupts`.
GET_STATE_PAYLOAD = {
    "interrupts": [INTERRUPT_ROW],
    "tasks": [
        {
            "id": TASK_ID,
            "name": "tools",
            "path": ["__pregel_pull", "tools"],
            "error": None,
            "result": None,
            "state": None,
            "checkpoint": None,
            "interrupts": [INTERRUPT_ROW],
        }
    ],
}

# Abridged from POST /threads/search: a {task_id: [Interrupt]} mapping.
SEARCH_ROW = {
    "thread_id": "22406d5b-e589-5761-ab61-6fd73c4e2a60",
    "status": "interrupted",
    "interrupts": {TASK_ID: [INTERRUPT_ROW]},
}


TICKET_CONTEXT = {"ticket_id": "278544000000372001", "ticket_number": "101"}


class _StubSDK:
    """Stands in for the langgraph_sdk client, returning captured payloads."""

    def __init__(self, state=None, rows=None, metadata=None, run_status="success"):
        outer = self

        class _Threads:
            def get_state(self, thread_id):
                return outer._state

            def search(self, *, status=None, limit=None):
                return outer._rows

            def get(self, thread_id):
                return {"thread_id": thread_id, "metadata": outer._metadata}

        class _Runs:
            def create(self, thread_id, assistant_id, **kwargs):
                outer.created.append(
                    {"thread_id": thread_id, "assistant_id": assistant_id, **kwargs}
                )
                return {"run_id": "run-1"}

            def join(self, thread_id, run_id):
                outer.joined.append(run_id)
                return {}

            def get(self, thread_id, run_id):
                return {"run_id": run_id, "status": outer._run_status}

        self._state = state
        self._rows = rows
        self._metadata = {} if metadata is None else metadata
        self._run_status = run_status
        self.created: list = []
        self.joined: list = []
        self.threads = _Threads()
        self.runs = _Runs()


def _platform(**kwargs) -> PlatformThreads:
    """A PlatformThreads wired to a stub, bypassing __init__'s SDK import."""
    platform = PlatformThreads.__new__(PlatformThreads)
    platform._client = _StubSDK(**kwargs)
    return platform


# ---------------------------------------------------------------------------
# the regression
# ---------------------------------------------------------------------------


def test_a_task_id_is_never_reported_as_a_pending_interrupt():
    """The bug, stated directly.

    A task row also has an ``id``. Yielding it as an interrupt produced a
    two-element list for a thread parked on exactly one interrupt, which made
    ``resume_gate`` refuse to guess between them and report an open gate as
    already answered. Ticket #101 sat unanswerable because of this.
    """
    ids = _platform(state=GET_STATE_PAYLOAD).pending_interrupt_ids("t")

    assert ids == [INTERRUPT_ID]
    assert TASK_ID not in ids, (
        "a task id was returned as an interrupt id; resume_gate will see a "
        "phantom second pending interrupt and refuse a perfectly good click"
    )


def test_one_parked_gate_reads_as_exactly_one_pending_interrupt():
    """The count is what `resume_gate`'s ambiguity check keys off, so it is
    worth asserting on its own and not just as a side effect of the ids."""
    assert len(_platform(state=GET_STATE_PAYLOAD).pending_interrupt_ids("t")) == 1


def test_the_id_on_the_button_matches_the_id_read_back_from_the_thread():
    """The notifier renders the button from `search`; the handler checks it
    against `get_state`. Two code paths, two payload shapes, and the whole
    gate depends on them agreeing."""
    rendered = _platform(rows=[SEARCH_ROW]).parked_gates()
    checked = _platform(state=GET_STATE_PAYLOAD).pending_interrupt_ids("t")

    assert [g.interrupt_id for g in rendered] == checked


# ---------------------------------------------------------------------------
# the shapes, individually
# ---------------------------------------------------------------------------


def test_flat_interrupts_list_is_read():
    assert list(_interrupt_rows([INTERRUPT_ROW])) == [INTERRUPT_ROW]


def test_search_task_id_mapping_is_read():
    assert list(_interrupt_rows({TASK_ID: [INTERRUPT_ROW]})) == [INTERRUPT_ROW]


def test_task_row_is_descended_into_not_yielded():
    rows = list(_interrupt_rows(GET_STATE_PAYLOAD["tasks"]))

    assert rows == [INTERRUPT_ROW]


def test_a_task_parked_on_nothing_yields_nothing():
    """A running task has an id and an empty interrupts list. It must not
    contribute an id, or a thread that is merely busy would look parked."""
    running = [{"id": TASK_ID, "name": "tools", "interrupts": []}]

    assert list(_interrupt_rows(running)) == []


def test_duplicate_interrupt_across_both_sources_is_reported_once():
    """`get_state` reports the same interrupt flat *and* under its task. That
    is one gate, not two — and counting it twice reproduces the original bug
    by a different route."""
    ids = _platform(state=GET_STATE_PAYLOAD).pending_interrupt_ids("t")

    assert ids.count(INTERRUPT_ID) == 1


def test_parked_gates_carries_the_payload_the_card_is_rendered_from():
    gates = _platform(rows=[SEARCH_ROW]).parked_gates()

    assert len(gates) == 1
    assert gates[0].interrupt_id == INTERRUPT_ID
    assert gates[0].payload["gate"] == "create_sprint_issue"


# ---------------------------------------------------------------------------
# The ticket binding must survive into the resume run
#
# Run-level `configurable` is not inherited by later runs, and a resume IS a
# later run. Dropping it does not fail safely: the gate is consumed and the
# graph then dies inside the tool with MissingTicketContext. Two live tickets
# were spent this way.
# ---------------------------------------------------------------------------


def test_resume_re_supplies_the_ticket_context_the_tools_need():
    p = _platform(metadata={"ticket_context": TICKET_CONTEXT})

    p.resume("t", assistant_id="triage", value={"approved": True})

    config = p._client.created[0]["config"]
    assert config["configurable"]["ticket_context"] == TICKET_CONTEXT, (
        "the resume run must carry the ticket binding; without it the tools "
        "cannot tell which ticket they are acting on and refuse"
    )


def test_resume_refuses_a_thread_with_no_bound_ticket_rather_than_spending_it():
    """The failure has to happen BEFORE the run is created. Afterwards the
    interrupt is already consumed and the ticket is unrecoverable."""
    p = _platform(metadata={})

    with pytest.raises(ResumeFailed):
        p.resume("t", assistant_id="triage", value={"approved": True})

    assert p._client.created == [], (
        "a run was created for an unbound thread; that spends the gate on "
        "work that cannot succeed"
    )


def test_a_thread_missing_only_the_context_key_is_still_refused():
    """Claim metadata alone (ticket id, subject) is not the bound context."""
    p = _platform(metadata={"zoho_ticket_id": "278544000000372001"})

    with pytest.raises(ResumeFailed):
        p.resume("t", assistant_id="triage", value={"approved": True})


# ---------------------------------------------------------------------------
# "Created" is not "succeeded"
# ---------------------------------------------------------------------------


def test_a_run_that_dies_after_creation_is_reported_not_celebrated():
    p = _platform(metadata={"ticket_context": TICKET_CONTEXT}, run_status="error")

    with pytest.raises(ResumeRunFailed):
        p.resume("t", assistant_id="triage", value={"approved": True})


def test_a_run_that_succeeds_returns_normally():
    p = _platform(metadata={"ticket_context": TICKET_CONTEXT}, run_status="success")

    p.resume("t", assistant_id="triage", value={"approved": True})

    assert p._client.joined == ["run-1"], (
        "the run must actually be waited on, or a later failure goes unseen"
    )


def test_the_resume_waits_on_the_run_before_reporting_anything():
    """Ordering, stated as a test: join happens after create and before the
    caller is told the approval took effect."""
    p = _platform(metadata={"ticket_context": TICKET_CONTEXT})

    p.resume("t", assistant_id="triage", value={"approved": True})

    assert len(p._client.created) == 1 and p._client.joined == ["run-1"]
