#!/usr/bin/env python3
"""Render the human-facing output of a run from the agent's report.

    report.py pr <repo>              -> pull request body for that repo
    report.py comment <pr-urls...>   -> the "we opened these" issue comment
    report.py stop <stage> <reason>  -> the "we stopped, here is what we found" comment

Everything is read from `$PAGER_RUN_DIR/agent.json`, which is the only artifact
the agent produces. A run that stopped early has fewer fields populated; the
renderers degrade rather than fail.

The bias throughout is towards making a wrong fix look unfinished. What was
*not* verified is a top-level section rather than a footnote, and the PR body
says in its first line that nothing here has been established by a human.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RUN = Path(os.environ.get("PAGER_RUN_DIR") or os.environ.get("RUN_DIR") or "/tmp/pager-run")


def load(name: str = "agent.json"):
    path = RUN / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def issue_number() -> str:
    p = RUN / "issue-number.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else "?"


def bullets(items, empty="_(none reported)_"):
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    return "\n".join(f"- {i}" for i in items) if items else empty


def causal_chain(a) -> str:
    out = []
    for i, hop in enumerate(a.get("causal_chain") or [], 1):
        where = "/".join(x for x in (hop.get("repo"), hop.get("path")) if x)
        sym = hop.get("symbol") or ""
        loc = f"`{where}`" + (f" · `{sym}`" if sym else "") if where else "_(user-visible)_"
        out.append(f"{i}. **{hop.get('step', '').strip()}**  \n   {loc}  \n   {hop.get('why', '').strip()}")
    return "\n".join(out) if out else "_(no chain recorded)_"


def rejected(a) -> str:
    return bullets(
        f"**{r.get('hypothesis')}** — ruled out by {r.get('ruled_out_by')}"
        for r in a.get("rejected_explanations") or []
    )


def repo_entry(a, repo: str) -> dict:
    return next((r for r in a.get("repos") or [] if r.get("repo") == repo), {})


def security_warning(a) -> list[str]:
    if not a.get("security_sensitive"):
        return []
    return [
        "> ⚠️ **This change is marked security-sensitive** (auth, RBAC, casbin, secrets, "
        "or tenancy). A wrong fix here is a security hole that passes a green build. "
        "Do not merge on the strength of CI.",
        "",
    ]


def cmd_pr(repo: str) -> str:
    a = load() or {}
    r = repo_entry(a, repo)
    n = issue_number()

    parts = [
        f"## Agent-authored draft fix for pager issue #{n}",
        "",
        "> This pull request was written by an automated agent and reviewed by nobody. "
        "Devtron's test suites do not cover this code "
        "(`make test-unit` runs only `./pkg/pipeline`; `dashboard` CI never runs tests), "
        "so **you are the gate.** Read the causal chain below and decide whether it holds.",
        "",
        f"Tracking issue: {os.environ.get('PAGER_ISSUE_URL', f'#{n}')}",
        "",
        "### Root cause",
        "",
        a.get("root_cause", "_(not recorded)_"),
        "",
        "### From symptom to defect",
        "",
        causal_chain(a),
        "",
        "### Explanations considered and rejected",
        "",
        rejected(a),
        "",
        f"### What this PR changes in `{repo}`",
        "",
        r.get("change_summary", "_(not recorded)_"),
        "",
        f"_Risk:_ {r.get('risk') or 'not recorded'}",
        "",
        bullets(f"`{f}`" for f in r.get("changed_files") or []),
        "",
        "### Verified",
        "",
    ]

    build = r.get("build") or {}
    if build.get("command"):
        parts.append(f"- Build: `{build['command']}` — {'passed' if build.get('ok') else 'FAILED'}")
    else:
        parts.append(
            "- Build: **not run.** This change has not been compiled. "
            f"{build.get('output') or ''}".rstrip()
        )
    test = r.get("test") or {}
    if test.get("command"):
        parts.append(f"- Tests: `{test['command']}` — {'passed' if test.get('ok') else 'FAILED'}")
    else:
        parts.append("- Tests: none run.")
    parts += [bullets(r.get("verified"), empty="- _(nothing else claimed)_"), ""]

    parts += [
        "### NOT verified",
        "",
        "Read this section before the diff.",
        "",
        bullets(
            r.get("not_verified"),
            empty="- _Nothing was listed here, which on a non-trivial change usually "
                  "means the agent did not look._",
        ),
        "",
    ]

    if a.get("unknowns"):
        parts += ["Open assumptions:", "", bullets(a["unknowns"]), ""]

    parts += ["### Verification plan for QA", ""]
    steps = r.get("verification_plan") or []
    parts += (
        ["\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)), ""]
        if steps
        else ["_No verification plan was produced. Treat that as a reason for extra scrutiny._", ""]
    )

    parts += security_warning(a)
    parts += [
        "---",
        "",
        "🤖 Generated by the agentic pager-duty fix engine. Draft, `agent-authored`, "
        "do not merge without review.",
    ]
    return "\n".join(parts)


def cmd_comment(pr_urls: list[str]) -> str:
    a = load() or {}
    parts = [
        "## Agent fix engine: draft PR(s) opened",
        "",
        "### Root cause",
        "",
        a.get("root_cause", "_(not recorded)_"),
        "",
        "### From symptom to defect",
        "",
        causal_chain(a),
        "",
        "### Pull requests",
        "",
        bullets(pr_urls, empty="_(none)_"),
        "",
    ]
    if len(pr_urls) > 1:
        parts += [
            "These are cross-linked. `devtron` and `devtron-enterprise` are a hard fork at "
            "the same module path, so a shared-code fix needs a near-identical but "
            "separately written diff in each — **merging only one leaves the other half "
            "broken.**",
            "",
        ]
    parts += security_warning(a)
    parts += [
        "Drafts, labelled `agent-authored`. Nothing here has been verified by a human; "
        "the verification plan is in each PR body.",
    ]
    return "\n".join(parts)


def cmd_stop(stage: str, reason: str) -> str:
    a = load() or {}
    parts = [
        f"## Agent fix engine: stopped — no PR opened",
        "",
        reason.strip(),
        "",
        "The engine is built to stop rather than open a speculative pull request. "
        "Everything it established before stopping is below, so this can be picked up "
        "from here rather than from zero.",
        "",
    ]
    if a.get("root_cause"):
        parts += ["### What it did establish", "", a["root_cause"], "",
                  "### From symptom to defect", "", causal_chain(a), ""]
    if a.get("rejected_explanations"):
        parts += ["**Ruled out:**", rejected(a), ""]
    if a.get("unknowns"):
        parts += ["**Open questions:**", bullets(a["unknowns"]), ""]

    diffs = sorted((RUN / "diffs").glob("*.diff")) if (RUN / "diffs").exists() else []
    if diffs:
        parts += [
            "A partial patch exists in the workflow run artifacts (`pager-run/diffs/`). "
            "Read it as a starting point, not as a fix.",
            "",
        ]
    parts += security_warning(a)
    parts += ["---", "", "🤖 agentic pager-duty fix engine"]
    return "\n".join(parts)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd = argv[1]
    if cmd == "pr":
        print(cmd_pr(argv[2]))
    elif cmd == "comment":
        print(cmd_comment(argv[2:]))
    elif cmd == "stop":
        print(cmd_stop(argv[2], " ".join(argv[3:])))
    else:
        sys.exit(f"unknown command: {cmd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
