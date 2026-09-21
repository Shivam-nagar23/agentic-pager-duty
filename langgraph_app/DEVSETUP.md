# Dev setup

Everything below is about `langgraph_app/`. Run the commands from that directory.

## Install

```sh
uv sync --extra dev
uv pip install -e .
```

`langgraph dev` — the local server — is a separate extra, because it is
neither a runtime dependency nor something the test suite needs:

```sh
uv sync --extra local
```

The editable install drops `__editable__.pagerduty_triage-0.1.0.pth` into
`.venv/lib/python3.14/site-packages/`, containing the absolute path to `src/`.
That one line is what makes `import pagerduty_triage` work — from pytest, from
a plain `python`, and from the IDE, as long as the IDE is using this
interpreter. If imports go red, the interpreter is the first thing to check.

## Tests

```sh
.venv/bin/python -m pytest
```

213 tests, no network, no credentials. A test that needs either is a bug in
the test.

## GoLand

**1. Point the project at `.venv/bin/python`.**

Settings → Languages & Frameworks → Python Interpreter → the gear icon → *Add
Local Interpreter* → **Select existing** → Type: *Python* → Interpreter:

```
<repo>/langgraph_app/.venv/bin/python
```

Do not create a new virtualenv from that dialog — it would be empty of the
deepagents/langchain stack and every third-party import would go red instead.
Confirm afterwards that the package list in that dialog shows `deepagents`,
`langchain`, and `pagerduty-triage` (the last one marked editable).

**2. Mark the source roots.**

In the Project tool window, right-click →  *Mark Directory as*:

| Directory              | Mark as         |
| ---------------------- | --------------- |
| `langgraph_app/src`    | **Sources Root** |
| `langgraph_app/tests`  | **Test Sources Root** |

`src` is redundant with the `.pth` file for resolution, but it is what makes
*Go to Declaration* land on the working copy rather than on a cached copy of
the install.

`tests` is **not** redundant. `tests/test_gates.py` does
`from conftest import GateReached, call_tool`, which pytest makes work through
`pythonpath = tests` in `pytest.ini`. The IDE does not read that setting for
its own resolution, so without the Test Sources Root mark that one import
stays red while the test itself passes.

**3. Set the test runner to pytest.**

Settings → Tools → Python Integrated Tools → Testing → Default test runner:
**pytest**. Working directory: `langgraph_app`.

Pytest's configuration lives in `pytest.ini` and nowhere else — `pyproject.toml`
deliberately carries no `[tool.pytest.ini_options]` block, because `pytest.ini`
would silently win over it.

## Running the graph locally

Needs no credentials in the default configuration: `ZOHO_TRANSPORT` defaults to
`fake`, and both `REPLIES_ENABLED` and `ISSUE_CREATION_ENABLED` default off.
Copy `.env.example` to `.env` before changing any of that.
