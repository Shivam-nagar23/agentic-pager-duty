"""Posting a parked gate into Slack, once.

## Why polling, and not a webhook

LangGraph Platform has no "a thread hit an interrupt" webhook. Its ``webhook=``
parameter fires on **run completion**, and a HITL ``interrupt()`` *is* a normal
run completion — the run's status is ``success`` and the payload carries no
interrupt. What changes is the *thread's* status, which becomes
``interrupted`` with its ``interrupts`` field populated, and the platform sets
that before it calls any webhook.

So the notifier asks: ``threads.search(status="interrupted")``. One call
returns the thread ids and the interrupt payloads together, which is exactly
what the renderer needs and nothing more.

## Once, not exactly-once

A ``posted`` record keyed on the interrupt id keeps the same gate from being
re-posted on every tick. It is a **record, not a lock** — the same distinction
``ledger.py`` draws, and for the same reason: ``BaseStore.put`` is
last-write-wins with no compare-and-set, so a get-then-put claim would look
right in tests and race in production.

That is acceptable here in a way it would not be at the send site, because the
worst case is cosmetic. Two notifier ticks racing post the gate to Slack
twice; both messages carry the same thread and interrupt id, and the *first*
click on either consumes the interrupt, so the second message's buttons fail
the precondition in ``resume.py`` and report "already answered". A duplicate
message, never a duplicate action.

## An unanswered gate parks forever

There is no reminder, no nag, no escalation and no timeout in this file. That
is not an omission: the invariant is that an unanswered gate never becomes an
implicit yes, and the cheapest way to guarantee it is to have no timer at all.
If a reminder is ever added it must post a *new message* and must never
resume a thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .client import SlackClient
from .config import SlackSettings
from .renderer import render_gate_message

#: Store namespace for "this gate has been posted to Slack". A tuple, per the
#: BaseStore convention, and alongside the namespaces in ``ledger.py``.
NS_SLACK_POSTS = ("pagerduty", "slack_posts")


class StoreLike(Protocol):
    def get(self, namespace: tuple[str, ...], key: str) -> Any: ...

    def put(
        self, namespace: tuple[str, ...], key: str, value: dict[str, Any]
    ) -> None: ...


class ParkedGateSource(Protocol):
    def parked_gates(self, *, limit: int = 50) -> list[Any]:
        """Threads currently parked, each with ``thread_id``, ``interrupt_id``
        and the structured interrupt ``payload``."""


@dataclass
class NotifyReport:
    """What one tick did. Returned so a cron run leaves a readable trace."""

    considered: int = 0
    posted: list[str] = field(default_factory=list)
    skipped_already_posted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"considered={self.considered} posted={len(self.posted)} "
            f"skipped={len(self.skipped_already_posted)} "
            f"errors={len(self.errors)}"
        )


def _unwrap(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    return getattr(item, "value", item)


def notify_parked_gates(
    *,
    source: ParkedGateSource,
    slack: SlackClient,
    store: StoreLike,
    settings: SlackSettings,
    limit: int = 50,
) -> NotifyReport:
    """One tick: post every parked gate that has not been posted yet.

    Never raises for a single bad gate — one unrenderable payload must not
    stop the other parked threads from reaching a reviewer.
    """
    report = NotifyReport()

    try:
        gates = source.parked_gates(limit=limit)
    except Exception as exc:  # noqa: BLE001 - a failed tick must not kill the cron
        report.errors.append(f"parked_gates failed: {exc!r}")
        return report

    report.considered = len(gates)

    for gate in gates:
        interrupt_id = str(getattr(gate, "interrupt_id", "") or "")
        thread_id = str(getattr(gate, "thread_id", "") or "")
        payload = getattr(gate, "payload", None) or {}

        if not interrupt_id or not thread_id:
            report.errors.append(f"gate with no ids: {gate!r}")
            continue

        try:
            if _unwrap(store.get(NS_SLACK_POSTS, interrupt_id)) is not None:
                report.skipped_already_posted.append(interrupt_id)
                continue

            message = render_gate_message(
                payload, thread_id=thread_id, interrupt_id=interrupt_id
            )
            posted = slack.post_message(
                channel=settings.channel_id,
                text=message["text"],
                blocks=message["blocks"],
            )
            # Recorded after the post, so the record can only ever describe a
            # message that really exists. A crash in between costs one
            # duplicate message on the next tick, which is the cheap failure.
            store.put(
                NS_SLACK_POSTS,
                interrupt_id,
                {
                    "interrupt_id": interrupt_id,
                    "thread_id": thread_id,
                    "gate": str(payload.get("gate", "")),
                    "channel": posted.channel,
                    "message_ts": posted.ts,
                    "posted_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            report.posted.append(interrupt_id)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"interrupt {interrupt_id}: {exc!r}")

    return report
