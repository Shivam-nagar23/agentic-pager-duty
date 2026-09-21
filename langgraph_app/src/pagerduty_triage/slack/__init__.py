"""Slack approval for the two human gates.

A **presentation layer over the existing ``interrupt()``**, not an
architecture change. The graph is untouched: ``gates.py`` already emits a
structured dict, and this package turns that dict into Block Kit on the way
out and into the same ``{"approved": ...}`` envelope on the way back.

    thread parks on interrupt()
            │
      notifier.py  ──── renderer.py ────▶ Slack message, Approve / Reject
            │
            ▼
      (reviewer clicks)
            │
      http_app.py ──▶ signing.py  (is this really Slack, and recent?)
            │     └─▶ config.py   (is this the approver?)
            │
      handler.py ──▶ resume.py ──▶ LangGraph Command(resume=…)

Why the package is shaped this way:

* ``signing.py`` has no other imports. It is the security boundary, and a
  boundary with dependencies is a boundary with excuses.
* ``renderer.py`` is pure — payload in, blocks out, no I/O — so what a
  reviewer sees is testable without a Slack.
* ``resume.py`` owns exactly-once. See its module docstring for which layer
  is actually load-bearing.
* ``http_app.py`` is a separate ASGI app, not part of the graph, because
  Slack needs an unauthenticated public route and the graph deployment must
  not have one.
"""

from __future__ import annotations

__all__ = [
    "config",
    "client",
    "handler",
    "notifier",
    "renderer",
    "resume",
    "signing",
]
