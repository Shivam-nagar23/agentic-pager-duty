"""Ask Claude Code to localize one pager bug; capture the files it names.

This is the *instrument*, not the subject. Everything here exists to make one
headless Claude Code session over `workspace/` produce a file list that
`score.py` can consume without reshaping, and to make every failure loud rather
than silently degrade into a bad measurement.

Output contract
---------------
`score.score_prediction` compares `(repo, relative_path)` pairs. Truth gets its
repo from the fix-PR *URL*; predictions have to carry theirs in the path itself.
So the prompt demands repo-prefixed paths — `devtron/util/rbac/Enforcer.go`,
never `util/rbac/Enforcer.go` — and `tests/test_replay.py` pins `REPO_NAMES`
against `score.KNOWN_REPOS` so the two sides cannot drift apart unnoticed.
`score._split` does tolerate an unprefixed path, but at a real cost
(`devtron-services` and `devtron-services-enterprise` share 27 relative paths in
the corpus and become indistinguishable), so `validate_prediction` warns loudly
when the agent returns one.

Isolation choices, and why
--------------------------
- ``--tools Read,Grep,Glob`` — read-only by construction. `workspace/` holds
  2.4 GB of shallow clones that must not be modified, and dropping Bash also
  removes `git log`, which matters: see "Known contamination" below.
- ``--permission-mode bypassPermissions`` — without it the session burns turns
  on denied Read calls (observed on 2026-09-16). Safe only because the tool set
  above cannot write.
- ``--setting-sources project,local`` — excludes the developer's *user-level*
  settings.json. The machine this was written on has a SessionStart hook that
  injects unrelated instructions into the session; the agent noticed it and
  commented on it in its answer. That is measurement noise and is not present in
  the GitHub Action this eval is meant to predict. Repo-level CLAUDE.md files
  (dashboard, devtron-enterprise, notifier and devtron-fe-common-lib each ship
  one) are *not* a setting source and are still discovered, which is what the
  Action would see too.
- ``--json-schema`` — a structural guarantee that the reply is one JSON object
  of the right shape. This is a *firmer* contract, not a laxer parser: a model
  that answers in prose still fails, it just fails in the CLI rather than in
  `extract_json`.

Known contamination (read before trusting any number this produces)
-------------------------------------------------------------------
**1. The ticket body contains its own answer key.** Devtron's pager issues have
a PR template pasted under the report whose "## PR Links" section lists the fix
PRs — the same section `tools/corpus/parse_issue.py` scrapes to build `fix_prs`.
41/41 bodies in the corpus leak at least one fix-PR URL, 37/41 leak every truth
PR number, and the repo name is right there in the URL. `redact_ground_truth`
strips it before prompting; without that, `repo_hit` is free and the baseline
means nothing. Any future change to how the body reaches the prompt must keep
that redaction.

**2. The clones are post-fix.** `workspace/` is at HEAD, which is *after* the
fix PRs merged.
For ticket 2960 the devtron clone is at a merge of #7035, one past the fix
#7034. The agent is therefore reading already-fixed code and cannot find the
defect by reasoning about it — it can only find the files that *implement the
area* the symptom points at. That is still the localization signal we want, but
it means this harness measures "which files own this behavior", not "which
files are wrong". Pinning each clone to the fix PR's merge-base would measure
the real task; it is not done here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path

#: Resolved from this file, not from cwd — `tools/eval/replay.py` -> repo root.
#: The same trick `tools/corpus/fetch_corpus.py` uses, for the same reason: the
#: runner has to work from `tools/eval/`, from the repo root, and from a
#: GitHub Action step, all of which have different working directories.
REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = REPO_ROOT / "workspace"
CORPUS = REPO_ROOT / "data" / "corpus.json"

#: The repositories the agent is told exist, and the prefixes a prediction may
#: use. These are the eight clones in `workspace/`, matching "Target
#: repositories" in CLAUDE.md.
#:
#: This is a *superset* of `score.KNOWN_REPOS`, which lists five. The extra
#: three (`athena-be`, `notifier`, `devtron-fe-common-lib`) are still scored
#: correctly, and the asymmetry is safe in both directions — worth spelling out,
#: because getting this wrong is exactly the class of bug that already cost this
#: project once:
#:
#: - When a ticket's truth PRs live in one of the three, `score._splittable_repos`
#:   adds that repo from the PR *URL*, so `notifier/pkg/x.go` splits to
#:   `("notifier", "pkg/x.go")` and matches, and `repo_hit` is True.
#: - When they do not, `score._split` leaves the path whole as
#:   `(None, "notifier/pkg/x.go")`. Truth paths are repo-*relative* and never
#:   begin with a repo name, so it matches nothing and contributes no named repo.
#:   A wrong-repo guess therefore cannot manufacture a hit.
#:
#: So no change to `score.py` is required. `tests/test_replay.py` pins the
#: superset relation so a future edit to either list is a deliberate one.
#:
#: Not covered: the 135-record corpus has truth PRs in two repos that are
#: neither listed here nor cloned in `workspace/` — `protos` (ticket 1956) and
#: `hotfix-migrations` (ticket 1818). Those two are structurally unlocalizable
#: and should be excluded from the baseline rather than averaged in as zeros.
REPO_NAMES = (
    "devtron",
    "devtron-enterprise",
    "dashboard",
    "devtron-services",
    "devtron-services-enterprise",
    "athena-be",
    "notifier",
    "devtron-fe-common-lib",
)

#: 900s was enough over five repos (2960 finished in 3m22s) but not over eight —
#: a re-run hit the wall at 15min without answering. Measured, not guessed.
DEFAULT_TIMEOUT = int(os.environ.get("REPLAY_TIMEOUT", "1800"))
DEFAULT_MODEL = os.environ.get("REPLAY_MODEL", "sonnet")

#: Read-only by construction. Bash is deliberately absent: it would let the
#: session write into `workspace/` and, worse, read git history for the very
#: commit we are asking it to predict.
DEFAULT_TOOLS = "Read,Grep,Glob"

#: Enforced by the CLI, so a prose answer is a hard failure instead of a parse
#: puzzle. Mirrors what `validate_prediction` checks.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "repos": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["repos", "files", "reasoning"],
    "additionalProperties": False,
}

# The worked example below is deliberately drawn from an unrelated area of the
# codebase. Using the RBAC/Helm paths here would hand the agent the answer to
# ticket 2960, the one ticket this harness is smoke-tested on.
PROMPT = """You are localizing a production bug in the Devtron platform.

Below is a pager-duty bug report filed by Devtron's support team. These
repositories are checked out as directories in your current working directory:

{repo_list}

Find the files most likely to contain the bug. Read code to confirm your
reasoning rather than guessing from filenames alone. Grep for the concepts in
the report, follow the call path, and open the files you name.

A fix often spans two repositories, so check both sides of these pairs whenever
the symptom could live in either:

- `devtron-enterprise` is a hard fork of `devtron`, not a dependency. Both
  declare the same Go module path and share large amounts of code, but files at
  identical paths can have divergent bodies.
- `devtron-services-enterprise` wraps `devtron-services` the same way.
- `dashboard` depends on `devtron-fe-common-lib`. Many UI bugs live in the
  shared component library rather than in `dashboard` itself.

OUTPUT CONTRACT — this is scored mechanically, so the exact path shape matters:

- Every entry in "files" MUST begin with one of the repository directory names
  listed above, followed by the file's path relative to that repository's root.
- Do NOT emit absolute paths, `./` prefixes, a `workspace/` prefix, line
  numbers, or ranges.
- Write the path exactly as it appears on disk, including case.

Correct:
  "devtron/pkg/pipeline/CiCdPipelineOrchestrator.go"
  "dashboard/src/components/ClusterNodes/ClusterList.tsx"
  "devtron-services/kubelink/pkg/service/HelmAppService.go"

Wrong:
  "pkg/pipeline/CiCdPipelineOrchestrator.go"      (no repository prefix)
  "workspace/devtron/pkg/pipeline/Orchestrator.go" (extra workspace/ prefix)
  "/Users/x/workspace/devtron/pkg/..."             (absolute path)

List between 3 and 12 files, most likely first. "repos" must list only the
repository names you are naming files in. "reasoning" is a few sentences on why
those files: name the call path or the specific logic you believe is at fault.
{extra_context}
--- BUG REPORT ---
{title}

{body}
"""


# --- Ground-truth redaction --------------------------------------------------
#
# Devtron's pager issues carry a PR template pasted below the bug report, and its
# "## PR Links" section lists the fix PRs. That is not incidental: it is the very
# section `tools/corpus/parse_issue.py` scrapes to build `fix_prs`, so the answer
# key is inside the question. Measured on the 41-record corpus, 41/41 bodies
# contain at least one fix-PR URL and 37/41 contain every truth PR number.
#
# Handing that to the agent makes `repo_hit` nearly free — the repo is in the URL
# — and the "## Microservices" checklist just below it narrows the service too.
# Redaction is not optional politeness; without it the baseline is meaningless.

#: Everything from this heading on is PR-template boilerplate, not bug report.
_PR_LINKS_HEADING = re.compile(r"^[ \t]{0,3}#{1,4}[ \t]*PR[ \t]*Links\b", re.I | re.M)

#: Belt and braces for PR/commit/issue URLs that appear outside that section.
_GITHUB_REF_URL = re.compile(
    r"https?://\S*github\.com/[\w.-]+/[\w.-]+/(?:pull|commit|issues)/\w+\S*", re.I
)

#: Keep enough of the report that a truncation cannot silently empty the prompt.
_MIN_BODY_CHARS = 200


def redact_ground_truth(body: str) -> str:
    """Strip the fix-PR answer key out of a ticket body before prompting.

    Two passes, because the leak has two shapes:

    1. Cut the body at the "## PR Links" heading. This also removes the
       "## Microservices" checklist and release-notes tail that follow it, which
       are themselves strong hints about which service is at fault.
    2. Redact any remaining GitHub pull/commit/issue URL, wherever it sits.

    The cut is skipped when the heading appears within the first
    `_MIN_BODY_CHARS`, so a malformed ticket whose template comes first is
    URL-redacted rather than reduced to nothing — an empty prompt would score
    zero and look like a model failure.
    """
    if not body:
        return ""
    match = _PR_LINKS_HEADING.search(body)
    if match and match.start() >= _MIN_BODY_CHARS:
        body = body[: match.start()]
    return _GITHUB_REF_URL.sub("[redacted]", body).rstrip()


def build_prompt(record: dict, extra_context: str = "") -> str:
    """Render the localization prompt for one corpus record.

    `extra_context` is the seam for Phase 2: the past-PR index is injected here
    so the cold and warm runs differ in exactly one input.
    """
    block = f"\n{extra_context.strip()}\n" if extra_context.strip() else ""
    return PROMPT.format(
        repo_list="\n".join(f"  {name}" for name in REPO_NAMES),
        extra_context=block,
        title=record.get("title", ""),
        body=redact_ground_truth(record.get("body", "")),
    )


def check_workspace(workspace: Path | None = None) -> list[str]:
    """Warn if the clones on disk disagree with the repos named in the prompt.

    `workspace/` gained three repos mid-build without the prompt knowing, which
    would have meant the agent could never name a file in them. A repo on disk
    that the prompt omits is invisible to the agent; a repo in the prompt that
    is missing from disk invites a hallucinated path. Both are warnings, not
    errors — the eval should still run — but neither should pass unnoticed.
    """
    root = Path(workspace) if workspace is not None else WORKSPACE
    on_disk = {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}
    missing = sorted(set(REPO_NAMES) - on_disk)
    unlisted = sorted(on_disk - set(REPO_NAMES))
    if missing:
        warnings.warn(
            f"prompt names repo(s) not checked out in {root}: {missing} — "
            f"the agent may invent paths under them",
            stacklevel=2,
        )
    if unlisted:
        warnings.warn(
            f"{root} contains repo(s) the prompt does not name: {unlisted} — "
            f"the agent cannot localize into them; add them to REPO_NAMES",
            stacklevel=2,
        )
    return missing + unlisted


def run_claude(
    prompt: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    model: str = DEFAULT_MODEL,
    tools: str = DEFAULT_TOOLS,
    cwd: Path | None = None,
    max_budget_usd: float | None = None,
) -> dict:
    """Run one headless Claude Code session and return the CLI's JSON envelope.

    The envelope (``--output-format json``) carries `result` (the final text),
    `structured_output` (the parsed object, because `--json-schema` is set),
    plus `total_cost_usd`, `duration_ms`, `num_turns` and `permission_denials`.
    The last one is worth keeping: a run full of denials produced its answer
    with less evidence than it appears to have.

    Raises rather than returning a sentinel. A silent failure here becomes a
    zero in the baseline that looks like a model failure but is a harness bug.
    """
    workdir = Path(cwd) if cwd is not None else WORKSPACE
    if not workdir.is_dir():
        raise FileNotFoundError(
            f"workspace not found at {workdir} — expected the repo clones there"
        )
    check_workspace(workdir)

    argv = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--permission-mode", "bypassPermissions",
        "--setting-sources", "project,local",
        "--json-schema", json.dumps(OUTPUT_SCHEMA),
    ]
    if tools:
        argv += ["--tools", tools]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", str(max_budget_usd)]

    try:
        result = subprocess.run(
            argv, cwd=workdir, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"claude exceeded {timeout}s in {workdir}; raise REPLAY_TIMEOUT or "
            f"narrow the prompt"
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"claude exited {result.returncode}: {result.stderr[:1000] or result.stdout[:1000]}"
        )

    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"claude did not emit a JSON envelope: {result.stdout[:500]!r}"
        ) from exc

    if envelope.get("is_error"):
        raise RuntimeError(
            f"claude reported an error ({envelope.get('subtype')}): "
            f"{str(envelope.get('result'))[:500]}"
        )
    return envelope


def _balanced_object_spans(text: str) -> list[tuple[int, int]]:
    """Byte spans of every balanced, top-level ``{...}`` in `text`.

    String-aware, so braces and escaped quotes inside JSON string values do not
    throw off the depth count. This is what replaces a greedy ``\\{.*\\}``:
    that pattern welds several JSON blocks into one unparseable blob, and one
    brace in surrounding prose swallows the real answer.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0:
                    spans.append((start, index + 1))
    return spans


def extract_json(output: str, required: tuple[str, ...] = ("files",)) -> dict:
    """Pull the prediction object out of a model reply.

    Strict on purpose. It accepts three shapes and nothing else:

    1. the whole reply is one JSON object (the contract);
    2. the reply is a ```json fenced block;
    3. the reply is an object embedded in prose.

    Among embedded candidates it keeps only objects carrying every key in
    `required` and returns the *last* such object, because a model that
    restates the schema before answering puts the answer second. Candidates
    lacking those keys are discarded rather than returned, so a stray `{}` in
    prose cannot become a prediction.

    Raises `ValueError` when nothing qualifies. That is the intended behavior:
    a reply with no parseable prediction is an instruction-following failure
    worth seeing, not something to paper over with a looser pattern.
    """
    if not isinstance(output, str) or not output.strip():
        raise ValueError("empty model output")

    text = output.strip()
    if text.startswith("```"):
        # ```json\n{...}\n``` — drop the fence markers and re-enter below.
        fenced = text.split("```")
        text = max((part for part in fenced), key=len)
        text = text.removeprefix("json").strip()

    candidates: list[dict] = []
    for start, end in _balanced_object_spans(text):
        try:
            parsed = json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            candidates.append(parsed)

    if not candidates:
        raise ValueError(f"no JSON object in output: {output[:300]!r}")

    qualified = [obj for obj in candidates if all(key in obj for key in required)]
    if not qualified:
        raise ValueError(
            f"JSON object(s) found but none contain {list(required)}: "
            f"{output[:300]!r}"
        )
    return qualified[-1]


def validate_prediction(prediction: dict) -> dict:
    """Coerce a raw prediction to `{repos, files, reasoning}` and flag drift.

    Warns — does not raise — on paths that are not repo-prefixed. `score.py`
    still scores them, by matching on the relative path alone, but it cannot
    attribute them to a repo. Silently accepting them is how a whole baseline
    ends up subtly wrong, so the warning names the offending paths.
    """
    files = prediction.get("files")
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ValueError(f"'files' must be a list of strings, got {files!r}")

    cleaned = [f.strip() for f in files if f.strip()]
    # Checked against REPO_NAMES (the eight the prompt names), not
    # score.KNOWN_REPOS (five). A path under `notifier/` obeys the contract even
    # though `score` only learns that prefix from a truth PR URL.
    unprefixed = [f for f in cleaned if f.partition("/")[0] not in REPO_NAMES]
    if unprefixed:
        warnings.warn(
            f"{len(unprefixed)} predicted path(s) are not prefixed with a known "
            f"repo and cannot be attributed to one when scored: {unprefixed} — "
            f"the prompt's output contract is not being followed",
            stacklevel=2,
        )

    repos = prediction.get("repos")
    if not isinstance(repos, list):
        repos = sorted({f.partition("/")[0] for f in cleaned} & set(REPO_NAMES))

    return {
        "repos": [r for r in repos if isinstance(r, str)],
        "files": cleaned,
        "reasoning": str(prediction.get("reasoning", "")),
    }


def replay(record: dict, extra_context: str = "", **kwargs) -> dict:
    """Localize one corpus record.

    Returns `{repos, files, reasoning, meta}`. `files` is what
    `score.score_prediction` consumes directly; `meta` carries cost, wall
    clock, turn count and the session id so a run can be priced and re-opened.
    """
    envelope = run_claude(build_prompt(record, extra_context), **kwargs)

    structured = envelope.get("structured_output")
    if isinstance(structured, dict) and "files" in structured:
        raw = structured
    else:
        # --json-schema did not produce a parsed object (older CLI, or the
        # model refused the schema). Fall back to parsing the text reply.
        raw = extract_json(envelope.get("result") or "")

    prediction = validate_prediction(raw)
    prediction["meta"] = {
        "number": record.get("number"),
        "cost_usd": envelope.get("total_cost_usd"),
        "duration_ms": envelope.get("duration_ms"),
        "num_turns": envelope.get("num_turns"),
        "session_id": envelope.get("session_id"),
        "permission_denials": len(envelope.get("permission_denials") or []),
        "used_structured_output": raw is structured,
    }
    return prediction


def load_record(number: int, corpus: Path = CORPUS) -> dict:
    records = json.loads(corpus.read_text())
    for record in records:
        if record.get("number") == number:
            return record
    raise KeyError(f"ticket {number} not in {corpus}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("number", type=int, help="corpus ticket number, e.g. 2960")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--tools", default=DEFAULT_TOOLS)
    parser.add_argument("--max-budget-usd", type=float, default=None)
    parser.add_argument(
        "--score", action="store_true", help="also score against the record's fix_prs"
    )
    args = parser.parse_args(argv)

    record = load_record(args.number)
    prediction = replay(
        record,
        timeout=args.timeout,
        model=args.model,
        tools=args.tools,
        max_budget_usd=args.max_budget_usd,
    )
    if args.score:
        from score import score_prediction

        prediction["score"] = score_prediction(prediction["files"], record["fix_prs"])
    json.dump(prediction, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
