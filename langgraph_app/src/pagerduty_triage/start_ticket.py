"""Start triage for one named Zoho ticket, by hand.

    python -m pagerduty_triage.start_ticket <zoho-ticket-id>

## Why this exists

A hosted cron on LangGraph Platform needs Plus or Enterprise. A local run
(`langgraph dev`) has neither, so there is nothing to tick the `poller`
assistant. This is the poll loop with the *query* removed and nothing else
changed.

## What it deliberately does NOT do

It does not build its own thread id, its own run config or its own claim. It
calls :func:`pagerduty_triage.poller.start_ticket`, which calls the same
``_start_one`` the cron path calls, so:

* the thread id is still ``uuid5(NS, "zoho-ticket:<id>")``;
* the claim is still ``threads.create(if_exists="raise")`` — running this
  twice for one ticket loses the race the second time and reports a conflict;
* the ledger still records the sighting;
* ``multitask_strategy="reject"`` is still set on the run;
* the customer's own words are still read once and bound into the thread, so
  gate 1 can show them without fetching anything above its ``interrupt()``.

Bypassing any of that to "just start a run" would quietly remove the
once-per-ticket guarantee for exactly the run where it matters most — the
first one against a real customer's ticket.

## The one caveat, stated plainly

This is a **separate process** from the graph server, and
``agent.build_deps()`` gives each process its own in-memory ledger. So the
ledger's cheap skips (``has_replied`` / ``is_claimed``) start empty here and
are effectively useless across invocations. That is survivable because the
ledger was never the mutex — layer 2, the thread insert, is, and it lives in
the server's database. Run this twice and the second call reports
``skipped_conflict``, which is the correct outcome by the correct mechanism.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def load_dotenv(path: Path) -> list[str]:
    """Minimal ``.env`` reader. Returns the NAMES it set, never the values.

    `langgraph dev` reads `.env` for the server; a plain `python -m` does not,
    and forgetting that looks exactly like "my Zoho credentials are wrong".
    Existing environment variables always win, so an explicit
    ``ZOHO_TRANSPORT=fake`` on the command line is not silently overridden.
    """
    set_names: list[str] = []
    if not path.is_file():
        return set_names
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            set_names.append(key)
    return set_names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pagerduty_triage.start_ticket",
        description=(
            "Open exactly one triage thread for one Zoho Desk ticket, using "
            "the same claim, thread id and ledger the poller uses."
        ),
    )
    parser.add_argument(
        "ticket_id",
        help="The Zoho Desk ticket id (the long numeric id, not the #number).",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="LangGraph server base URL. Defaults to $LANGGRAPH_API_URL, or "
        "http://127.0.0.1:2024 for `langgraph dev`.",
    )
    parser.add_argument(
        "--assistant-id",
        default=None,
        help="Assistant to run. Defaults to $TRIAGE_ASSISTANT_ID or 'triage'.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Env file to read before starting. Default: ./.env",
    )
    args = parser.parse_args(argv)

    loaded = load_dotenv(Path(args.env_file))
    if loaded:
        print(f"read {len(loaded)} variable(s) from {args.env_file}", file=sys.stderr)

    url = args.url or os.environ.get("LANGGRAPH_API_URL") or "http://127.0.0.1:2024"
    os.environ["LANGGRAPH_API_URL"] = url
    assistant_id = (
        args.assistant_id or os.environ.get("TRIAGE_ASSISTANT_ID") or "triage"
    )

    # Imported here, after the env file is read, because `build_deps` reads
    # settings at call time and `_platform_client` reads LANGGRAPH_API_URL.
    from .agent import build_deps
    from .ledger import thread_id_for_ticket
    from .poller import start_ticket
    from .poller_graph import _platform_client

    deps = build_deps()
    if deps.settings.zoho_transport == "fake":
        print(
            "ZOHO_TRANSPORT is 'fake' — this will triage an in-memory stub "
            "ticket, not a real one. Set ZOHO_TRANSPORT=rest for a live run.",
            file=sys.stderr,
        )

    thread_id = thread_id_for_ticket(args.ticket_id)
    print(f"ticket {args.ticket_id} -> thread {thread_id}")
    print(f"server {url}, assistant {assistant_id!r}")

    report = start_ticket(
        ticket_id=args.ticket_id,
        zoho=deps.zoho,
        ledger=deps.ledger,
        client=_platform_client(),
        assistant_id=assistant_id,
    )
    print(report.summary())

    if report.started:
        print(
            "Started. Gate 1 or gate 2 will park this thread; run the Slack "
            "notifier to post it, or read it with:\n"
            f"  curl -s {url}/threads/{thread_id}/state | jq '.tasks[].interrupts[].value'"
        )
        return 0
    if report.skipped_conflict:
        print(
            "Already claimed: a thread for this ticket exists, so nothing was "
            "started. That is the once-per-ticket guarantee working. Delete "
            f"the thread to redo it:\n  curl -XDELETE {url}/threads/{thread_id}"
        )
        return 0
    if report.skipped_replied or report.skipped_seen:
        print("Skipped by the ledger — this process has already seen the ticket.")
        return 0
    for err in report.errors:
        print(f"error: {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
