"""The manual trigger is a thin wrapper, and that is its whole value.

If it ever grows its own thread-id derivation or its own claim, the
once-per-ticket guarantee stops being one mechanism and starts being two that
have to agree.
"""

from __future__ import annotations

import ast
import inspect
import os

from pagerduty_triage import start_ticket as cli


def _called_names(module) -> set[str]:
    """Every attribute/function name this module actually calls.

    Read from the AST, not from the source text, so the module is free to
    *document* `uuid5` and `threads.create` in its docstring — which it should
    — while still being held to not calling them.
    """
    tree = ast.parse(inspect.getsource(module))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def test_the_cli_does_not_reimplement_the_claim():
    called = _called_names(cli)

    assert "create_thread" not in called, (
        "the manual trigger must go through poller.start_ticket, not open a "
        "thread itself — the claim is the mutex and there must be one of it"
    )
    assert "create_run" not in called, "run creation belongs to _start_one"
    assert "uuid5" not in called, "thread ids come from thread_id_for_ticket only"
    assert "start_ticket" in called and "thread_id_for_ticket" in called


def test_the_cli_module_is_importable_without_any_credentials():
    """It is run from a terminal by a human who has not sourced anything yet;
    an import-time credential read would be a confusing crash."""
    assert cli.main is not None


def test_dotenv_reader_never_overrides_the_real_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ZOHO_TRANSPORT=rest\nNEW_ONE=hello\n")
    monkeypatch.setenv("ZOHO_TRANSPORT", "fake")
    monkeypatch.delenv("NEW_ONE", raising=False)

    names = cli.load_dotenv(env)

    assert os.environ["ZOHO_TRANSPORT"] == "fake"
    assert os.environ["NEW_ONE"] == "hello"
    assert names == ["NEW_ONE"], "it reports names only, never values"


def test_dotenv_reader_tolerates_a_missing_file(tmp_path):
    assert cli.load_dotenv(tmp_path / "nope.env") == []
