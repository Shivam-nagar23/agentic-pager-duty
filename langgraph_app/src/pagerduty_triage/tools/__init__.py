"""Tool construction.

Two of these tools have real-world side effects, and both are gated. The
gates are `interrupt()` calls inside the tool bodies — see `gates.py` for why
they live there and not in `create_deep_agent(interrupt_on=...)`.
"""

from __future__ import annotations

from .deps import MissingTicketContext, TicketContext, ToolDeps, TICKET_CONTEXT_KEY
from .github_tools import build_github_tools
from .zoho_tools import build_zoho_tools

#: Tools whose execution changes something outside this process. Every one of
#: these must call `interrupt()` before it acts. `tests/test_gates.py` asserts
#: that this list and the set of interrupting tools are the same set, so
#: adding an ungated side-effecting tool breaks the build.
GATED_TOOLS = frozenset({"zoho_send_reply", "create_sprint_issue"})


def build_tools(deps: ToolDeps) -> list:
    """Every tool the main agent gets, beyond deepagents' built-ins."""
    return [*build_zoho_tools(deps), *build_github_tools(deps)]


__all__ = [
    "GATED_TOOLS",
    "MissingTicketContext",
    "TICKET_CONTEXT_KEY",
    "TicketContext",
    "ToolDeps",
    "build_github_tools",
    "build_tools",
    "build_zoho_tools",
]
