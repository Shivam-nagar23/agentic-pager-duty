"""Graph wiring checks.

These skip when `deepagents` is not installed. They have now run: installing
the dependencies resolved both `TODO(verify on first deploy)` notes in
`agent.py` (a passed-in `FilesystemMiddleware` replaces the default rather
than doubling it; a subagent's `tools` list does NOT accept tool-name
strings), and turned up a third thing neither note anticipated — the
`general-purpose` subagent deepagents adds on its own inherits the main
agent's tools, gated ones included. See `_general_purpose_override()`.

Do not delete a skip you have not seen pass.

## These assert against the COMPILED graph, not the spec

`_tool_names` reads `ToolNode._tools_by_name` off the built graph, which is
the only place that answers "what can the model actually call". An earlier
version walked the `PregelNode` wrappers, found no `tools_by_name` on any of
them, and returned an empty set — under which `test_no_shell_tool` passed
while proving nothing. Hence `_assert_is_a_real_tool_set`: every test that
asserts an absence first asserts the set is real.
"""

from __future__ import annotations

import pytest

deepagents = pytest.importorskip(
    "deepagents", reason="deepagents not installed; graph wiring unverified"
)

from pagerduty_triage.agent import (  # noqa: E402
    GENERAL_PURPOSE,
    SAFE_FILESYSTEM_TOOLS,
    build_agent,
    _subagents,
)


@pytest.fixture
def agent(deps):
    return build_agent(deps, checkpointer=None)


def _tool_names(agent) -> set[str]:
    """Every tool name the compiled graph exposes to the model.

    A compiled graph's `.nodes` maps to `PregelNode` wrappers; the `ToolNode`
    holding the tool registry is behind `.bound`, so both are checked.
    """
    names: set[str] = set()
    for node in getattr(agent, "nodes", {}).values():
        for candidate in (node, getattr(node, "bound", None)):
            for attr in ("tools_by_name", "tools"):
                found = getattr(candidate, attr, None)
                if isinstance(found, dict):
                    names.update(found)
                elif isinstance(found, (list, tuple)):
                    names.update(getattr(t, "name", "") for t in found)
    return {n for n in names if n}


def _assert_is_a_real_tool_set(names: set[str]) -> None:
    """Guard against an absence test passing because introspection broke.

    `read_file` is the one tool every agent here is guaranteed to hold, so an
    introspection walk that lost the registry fails here rather than silently
    reporting that nothing dangerous is present.
    """
    assert names, "tool introspection returned nothing; the walk is broken"
    assert "read_file" in names, (
        f"tool introspection found {sorted(names)} but not read_file; the walk "
        "is reading the wrong object"
    )


def _subagent_tool_names(agent) -> dict[str, set[str]]:
    """Tool names per compiled subagent, read off the `task` tool's closure.

    deepagents keeps the compiled subagent graphs in a closure cell named
    `subagent_graphs` on the `task` tool's function. That is private, and
    deliberately reached anyway: it is the only view of what a subagent can
    actually call, and the alternative — asserting against the specs we wrote
    — cannot see the subagent deepagents adds by itself, which is the one that
    was wrong. A rename upstream fails this loudly.
    """
    task = _tool_registry(agent)["task"]
    func = task.func
    cells = dict(zip(func.__code__.co_freevars, func.__closure__ or ()))
    assert "subagent_graphs" in cells, (
        "deepagents no longer keeps compiled subagents in a `subagent_graphs` "
        f"closure cell (found {sorted(cells)}); update this walk"
    )
    return {
        name: _tool_names(graph)
        for name, graph in cells["subagent_graphs"].cell_contents.items()
    }


def _tool_registry(agent) -> dict:
    for node in agent.nodes.values():
        registry = getattr(getattr(node, "bound", None), "tools_by_name", None)
        if isinstance(registry, dict):
            return registry
    raise AssertionError("no ToolNode registry found on the compiled graph")


def test_no_shell_tool(agent):
    """`execute` runs shell commands and must not reach this agent.

    deepagents' FilesystemMiddleware ships `execute` by default. This agent
    reads customer-written text — attacker-influenced input by definition —
    so a shell is an unacceptable capability regardless of prompt wording.

    If this fails, `_filesystem_middleware()` did not replace the default
    middleware but was appended alongside it. Fix the middleware wiring; do
    not weaken this test.

    Checks the subagents too. Each compiles its own filesystem middleware, so
    the main agent being clean says nothing about theirs.
    """
    names = _tool_names(agent)
    _assert_is_a_real_tool_set(names)
    assert "execute" not in names

    for sub_name, sub_tools in _subagent_tool_names(agent).items():
        _assert_is_a_real_tool_set(sub_tools)
        assert "execute" not in sub_tools, f"subagent {sub_name} holds a shell"


def test_planning_tool_is_present(agent):
    """`write_todos` is NOT a deepagents default in 0.7.14.

    The main-agent prompt instructs the model to call it. If TodoListMiddleware
    is missing, the model calls a tool that does not exist on every ticket.
    """
    assert "write_todos" in _tool_names(agent)


def test_both_gated_tools_are_reachable(agent):
    from pagerduty_triage.tools import GATED_TOOLS

    assert GATED_TOOLS <= _tool_names(agent)


def test_filesystem_tools_are_the_safe_subset(agent):
    names = _tool_names(agent)
    _assert_is_a_real_tool_set(names)
    assert set(SAFE_FILESYSTEM_TOOLS) <= names
    # `delete` is the other tool the safe subset drops. Not a shell, but the
    # agent has no reason to remove files it wrote.
    assert "delete" not in names


def test_three_subagents_with_the_expected_names():
    names = {s["name"] for s in _subagents()}
    assert names == {"triage-analyst", "responder", "pager-scribe"}


def test_subagents_use_system_prompt_key_not_prompt():
    """deepagents 0.7.14 expects `system_prompt`; `prompt` is silently ignored.

    A subagent declared with the wrong key runs with an empty system prompt —
    no error, just a subagent that has forgotten its job.
    """
    for sub in _subagents():
        assert "system_prompt" in sub
        assert sub["system_prompt"].strip()
        assert "prompt" not in sub or "system_prompt" in sub


def test_no_subagent_can_reach_a_gated_tool():
    """Least privilege: only the main agent holds the side-effecting tools.

    Not a gate repair — `interrupt()` fires from a subagent's tool too — but a
    subagent has no business holding a tool that emails a customer.
    """
    from pagerduty_triage.tools import GATED_TOOLS

    for sub in _subagents():
        # An ABSENT `tools` key is the failure mode, not just a populated one:
        # deepagents reads a missing `tools` as "inherit the main agent's",
        # which is how a subagent would silently acquire both gated tools.
        assert "tools" in sub, (
            f"subagent {sub['name']} omits `tools` and would inherit the main "
            "agent's, gated tools included"
        )
        assert not (set(sub["tools"]) & GATED_TOOLS), (
            f"subagent {sub['name']} was granted a gated tool"
        )


def test_no_compiled_subagent_holds_a_gated_tool(agent):
    """The same claim, checked on the built graph rather than on our specs.

    This is the test that catches what `_subagents()` cannot see: deepagents
    auto-adds a `general-purpose` subagent inheriting the main agent's tools
    unless a spec of that name is supplied, and that default held both gated
    tools. `_general_purpose_override()` supplies one.
    """
    from pagerduty_triage.tools import GATED_TOOLS

    by_subagent = _subagent_tool_names(agent)
    assert by_subagent, "no compiled subagents found"
    for sub_name, sub_tools in by_subagent.items():
        _assert_is_a_real_tool_set(sub_tools)
        assert not (GATED_TOOLS & sub_tools), (
            f"compiled subagent {sub_name} can call "
            f"{sorted(GATED_TOOLS & sub_tools)}"
        )


def test_every_compiled_subagent_is_one_we_declared(agent):
    """Nothing delegable exists that this repo did not put there on purpose."""
    expected = {s["name"] for s in _subagents()} | {GENERAL_PURPOSE}
    assert set(_subagent_tool_names(agent)) == expected
