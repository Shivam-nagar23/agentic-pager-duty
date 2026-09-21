# Phases 1–2: Localization Harness Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prove Claude Code can find *where* a Devtron pager bug lives across five repos,
and measure how much a past-PR index improves it — before any Zoho or LangGraph work.

**Architecture:** Mine closed `pager-duty` issues in `devtron-labs/sprint-tasks` into a
ground-truth corpus (symptom → files the real fix PR changed). Build a replay harness that
runs Claude Code headless against a ticket body and scores its predicted files against that
truth. Measure cold, then build the `affected_area → hot files` index and measure again.
No Zoho, no LangGraph, no GitHub Actions in these two phases.

**Tech Stack:** Python 3.11+, `gh` CLI, pytest, Claude Code CLI (headless).

**Design:** [`2026-09-16-agentic-pager-duty-design.md`](2026-09-16-agentic-pager-duty-design.md)

---

## Conventions for this plan

- **No commits.** Per `CLAUDE.md`, Shivam reviews and commits everything himself. Where a
  normal plan would say "commit", this one says **"hand over for review"** — finish the
  task, stop, and summarize what changed. Do not run `git commit` or `git push`.
- **Don't write API shapes from memory.** Tasks that touch the Claude Code CLI or the
  GitHub Action begin with a step that reads current docs or `--help` output. If observed
  behavior contradicts this plan, trust the observation and say so.
- **Corpus is fixture data, not a snapshot to regenerate per test.** Fetch once, commit the
  JSON, test against it offline. Tests must never hit the network.

---

## Task 1: Fetch the ground-truth corpus

Closed `pager-duty` issues carry their fix PRs in the body under `## PR Links`, and the
affected area under `### Affected areas`. That is a labelled symptom → files-changed
dataset. Pull it into a local JSON file once.

**Files:**
- Create: `tools/corpus/fetch_corpus.py`
- Create: `data/corpus.json` (generated, committed as a fixture)

**Step 1: Confirm the shape on one known ticket**

Run:
```bash
gh issue view 2960 --repo devtron-labs/sprint-tasks --json title,body,labels,state | head -50
```
Expected: JSON with a `body` containing both `### Affected areas` and `## PR Links`
sections. Ticket 2960's affected area is `RBAC Issues` and it links PRs into
`devtron-enterprise` and `devtron`.

**Step 2: Write the fetch script**

```python
# tools/corpus/fetch_corpus.py
"""Fetch closed pager-duty issues and the files their fix PRs changed."""
import json
import subprocess
import sys
from pathlib import Path

REPO = "devtron-labs/sprint-tasks"
OUT = Path("data/corpus.json")


def gh_json(*args: str) -> object:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def list_closed_pager_issues(limit: int) -> list[dict]:
    return gh_json(
        "issue", "list",
        "--repo", REPO,
        "--label", "pager-duty",
        "--state", "closed",
        "--limit", str(limit),
        "--json", "number,title,body,labels",
    )


def pr_changed_files(pr_url: str) -> list[str] | None:
    """Return changed file paths, or None if the PR is unreadable (private/deleted)."""
    try:
        data = gh_json("pr", "view", pr_url, "--json", "files,headRepository")
    except subprocess.CalledProcessError:
        return None
    return [f["path"] for f in data.get("files", [])]


def main(limit: int = 60) -> None:
    from parse_issue import parse_affected_areas, parse_pr_links  # Task 2

    records = []
    for issue in list_closed_pager_issues(limit):
        body = issue.get("body") or ""
        pr_urls = parse_pr_links(body)
        truth: dict[str, list[str]] = {}
        for url in pr_urls:
            files = pr_changed_files(url)
            if files is not None:
                truth[url] = files
        if not truth:
            continue  # no readable ground truth — skip rather than record a blank
        records.append({
            "number": issue["number"],
            "title": issue["title"],
            "body": body,
            "labels": [label["name"] for label in issue["labels"]],
            "affected_areas": parse_affected_areas(body),
            "fix_prs": truth,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(records, indent=2))
    print(f"wrote {len(records)} records to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
```

**Step 3: Hand over for review** — do not run it yet; it depends on Task 2's parser.

---

## Task 2: Parse issue bodies (test-first)

**Files:**
- Create: `tools/corpus/parse_issue.py`
- Test: `tools/corpus/tests/test_parse_issue.py`

**Step 1: Write the failing tests**

```python
# tools/corpus/tests/test_parse_issue.py
from parse_issue import parse_affected_areas, parse_pr_links

BODY = """### Affected areas

RBAC Issues

### Additional affected areas

None

## PR Links
- https://github.com/devtron-labs/devtron-enterprise/pull/3402
- https://github.com/devtron-labs/devtron/pull/7034
"""


def test_parses_single_affected_area():
    assert parse_affected_areas(BODY) == ["RBAC Issues"]


def test_ignores_none_in_additional_areas():
    assert "None" not in parse_affected_areas(BODY)


def test_parses_pr_links():
    assert parse_pr_links(BODY) == [
        "https://github.com/devtron-labs/devtron-enterprise/pull/3402",
        "https://github.com/devtron-labs/devtron/pull/7034",
    ]


def test_missing_sections_return_empty():
    assert parse_affected_areas("no headings here") == []
    assert parse_pr_links("no headings here") == []


def test_pr_links_deduplicated():
    body = BODY + "\n- https://github.com/devtron-labs/devtron/pull/7034\n"
    assert len(parse_pr_links(body)) == 2
```

**Step 2: Run to verify they fail**

Run: `cd tools/corpus && python -m pytest tests/test_parse_issue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'parse_issue'`

**Step 3: Write the minimal implementation**

```python
# tools/corpus/parse_issue.py
"""Parse the Devtron pager-bug issue template."""
import re

PR_URL = re.compile(r"https://github\.com/devtron-labs/[\w.-]+/pull/\d+")


def _section(body: str, heading: str) -> str:
    """Text between `heading` and the next markdown heading."""
    pattern = re.compile(
        rf"^#{{2,3}}\s*{re.escape(heading)}\s*$(.*?)(?=^#{{1,3}}\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body)
    return match.group(1) if match else ""


def parse_affected_areas(body: str) -> list[str]:
    text = _section(body, "Affected areas")
    lines = [line.strip(" -*") for line in text.splitlines()]
    return [
        line for line in lines
        if line and line.lower() not in {"none", "_no response_"}
    ]


def parse_pr_links(body: str) -> list[str]:
    seen: dict[str, None] = {}
    for url in PR_URL.findall(body):
        seen.setdefault(url, None)
    return list(seen)
```

**Step 4: Run to verify they pass**

Run: `cd tools/corpus && python -m pytest tests/test_parse_issue.py -v`
Expected: PASS, 5 tests.

**Step 5: Fetch the real corpus**

Run: `cd tools/corpus && python fetch_corpus.py`
Expected: `wrote N records to data/corpus.json`, with N ≥ 30. If N is much lower,
inspect a few skipped issues by hand — the template may have changed over time, and the
parser may need a second heading variant.

**Step 6: Hand over for review.**

---

## Task 3: Scoring (test-first)

Given predicted file paths and the ground truth, produce numbers that make "did the index
help?" answerable.

**Files:**
- Create: `tools/eval/score.py`
- Test: `tools/eval/tests/test_score.py`

**Step 1: Write the failing tests**

```python
# tools/eval/tests/test_score.py
from score import score_prediction

TRUTH = {
    "https://github.com/devtron-labs/devtron/pull/7034": [
        "pkg/auth/rbac.go",
        "pkg/auth/enforcer.go",
    ],
}


def test_perfect_prediction():
    s = score_prediction(["pkg/auth/rbac.go", "pkg/auth/enforcer.go"], TRUTH)
    assert s["file_recall"] == 1.0
    assert s["file_precision"] == 1.0
    assert s["repo_hit"] is True


def test_partial_recall():
    s = score_prediction(["pkg/auth/rbac.go"], TRUTH)
    assert s["file_recall"] == 0.5
    assert s["file_precision"] == 1.0


def test_wrong_repo_is_not_a_hit():
    s = score_prediction(["dashboard/src/App.tsx"], TRUTH)
    assert s["repo_hit"] is False
    assert s["file_recall"] == 0.0


def test_empty_prediction_scores_zero_not_crash():
    s = score_prediction([], TRUTH)
    assert s["file_recall"] == 0.0
    assert s["file_precision"] == 0.0


def test_repo_hit_from_any_true_repo():
    truth = {
        "https://github.com/devtron-labs/devtron/pull/1": ["a.go"],
        "https://github.com/devtron-labs/dashboard/pull/2": ["b.tsx"],
    }
    s = score_prediction(["b.tsx"], truth)
    assert s["repo_hit"] is True
```

**Step 2: Run to verify they fail**

Run: `cd tools/eval && python -m pytest tests/test_score.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'score'`

**Step 3: Write the minimal implementation**

```python
# tools/eval/score.py
"""Score a predicted file list against the files the real fix PR changed."""
import re

REPO_FROM_URL = re.compile(r"github\.com/devtron-labs/([\w.-]+)/pull/")


def score_prediction(predicted: list[str], truth: dict[str, list[str]]) -> dict:
    true_files = {path for files in truth.values() for path in files}
    predicted_set = set(predicted)
    overlap = predicted_set & true_files

    recall = len(overlap) / len(true_files) if true_files else 0.0
    precision = len(overlap) / len(predicted_set) if predicted_set else 0.0

    return {
        "file_recall": recall,
        "file_precision": precision,
        "repo_hit": bool(overlap),
        "n_true": len(true_files),
        "n_predicted": len(predicted_set),
    }


def true_repos(truth: dict[str, list[str]]) -> set[str]:
    return {
        match.group(1)
        for url in truth
        if (match := REPO_FROM_URL.search(url))
    }
```

**Step 4: Run to verify they pass**

Run: `cd tools/eval && python -m pytest tests/test_score.py -v`
Expected: PASS, 5 tests.

**Step 5: Hand over for review.**

---

## Task 4: The replay runner

Run Claude Code headless against one ticket body with the five repos checked out, and
capture the files it predicts.

**Files:**
- Create: `tools/eval/replay.py`
- Create: `workspace/` (gitignored — the five clones live here)

**Step 1: Read the CLI surface before writing anything**

Run: `claude --help`
Look specifically for: the headless/print flag, how to pass a system prompt or extra
context, how to set the working directory, and whether output can be forced to JSON.
**Write down what you actually see** — do not assume this plan's flags are current.

**Step 2: Clone the five repos**

```bash
mkdir -p workspace && cd workspace
for r in devtron devtron-enterprise dashboard devtron-services devtron-services-enterprise; do
  gh repo clone "devtron-labs/$r" -- --depth 1 || echo "FAILED: $r"
done
```
Expected: five directories. Any failure here is a credentials problem — the enterprise
repos are private and need a PAT with access. Resolve before continuing.

**Step 3: Write the runner**

```python
# tools/eval/replay.py
"""Ask Claude Code to localize one pager bug; capture the files it names."""
import json
import re
import subprocess
from pathlib import Path

WORKSPACE = Path("workspace")

PROMPT = """You are localizing a production bug in the Devtron platform.

Below is a pager-duty bug report. The repositories are checked out in the current
directory: devtron, devtron-enterprise, dashboard, devtron-services, and
devtron-services-enterprise.

Find the files most likely to contain the bug. Read code to confirm your reasoning
rather than guessing from filenames alone.

{extra_context}

Return ONLY a JSON object, no prose around it:
{{"repos": ["repo-name"], "files": ["repo-name/path/to/file.go"], "reasoning": "..."}}

--- BUG REPORT ---
{body}
"""


def run_claude(prompt: str, timeout: int = 900) -> str:
    """Invoke Claude Code headless in the workspace. Flags verified in Step 1."""
    result = subprocess.run(
        ["claude", "-p", prompt],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr[:500]}")
    return result.stdout


def extract_json(output: str) -> dict:
    """Pull the JSON object out of the response; tolerate surrounding prose."""
    match = re.search(r"\{.*\}", output, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON in output: {output[:300]}")
    return json.loads(match.group(0))


def replay(record: dict, extra_context: str = "") -> dict:
    prompt = PROMPT.format(body=record["body"], extra_context=extra_context)
    return extract_json(run_claude(prompt))
```

**Step 4: Smoke-test on ticket 2960**

```bash
cd tools/eval && python -c "
import json, replay
corpus = json.load(open('../../data/corpus.json'))
record = next(r for r in corpus if r['number'] == 2960)
print(json.dumps(replay.replay(record), indent=2))
"
```
Expected: JSON naming `devtron` and/or `devtron-enterprise` and some auth/RBAC paths.
The real fix touched both repos, so naming only one is a partial hit, not a failure.

If the model returns prose instead of JSON, tighten the prompt rather than loosening
`extract_json` — a parser that accepts anything hides a real instruction-following problem.

**Step 5: Hand over for review.**

---

## Task 5: Baseline measurement

**Files:**
- Create: `tools/eval/run_eval.py`
- Create: `data/results-baseline.json` (generated)

**Step 1: Write the harness**

```python
# tools/eval/run_eval.py
"""Replay every corpus ticket and aggregate the scores."""
import argparse
import json
from pathlib import Path

import replay
from score import score_prediction


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-file", type=Path, default=None,
                        help="extra context injected into the prompt (the index)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    extra = args.context_file.read_text() if args.context_file else ""
    corpus = json.loads(Path("../../data/corpus.json").read_text())
    if args.limit:
        corpus = corpus[: args.limit]

    results = []
    for record in corpus:
        try:
            prediction = replay.replay(record, extra_context=extra)
            files = prediction.get("files", [])
            scored = score_prediction(files, record["fix_prs"])
            scored["error"] = None
        except Exception as exc:  # a crashed replay is a zero, not a gap
            files, scored = [], {
                "file_recall": 0.0, "file_precision": 0.0, "repo_hit": False,
                "n_true": 0, "n_predicted": 0, "error": str(exc)[:200],
            }
        scored["number"] = record["number"]
        scored["predicted"] = files
        results.append(scored)
        print(f"#{record['number']}: recall={scored['file_recall']:.2f} "
              f"repo_hit={scored['repo_hit']}")

    n = len(results)
    summary = {
        "n": n,
        "mean_file_recall": sum(r["file_recall"] for r in results) / n,
        "mean_file_precision": sum(r["file_precision"] for r in results) / n,
        "repo_hit_rate": sum(r["repo_hit"] for r in results) / n,
        "errors": sum(1 for r in results if r["error"]),
    }
    args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
```

**Step 2: Run the baseline**

Run: `cd tools/eval && python run_eval.py --out ../../data/results-baseline.json --limit 10`

Start at `--limit 10` to check cost and wall-clock before committing to the full corpus.
Each ticket is a full Claude Code session over five large repos — budget accordingly.

**Step 3: Record the numbers**

Write `summary` into `CLAUDE.md` under a new "Measurements" section, dated. This is the
number every later change is judged against.

**Step 4: Hand over for review.**

---

## Task 6: Build the past-PR index (test-first)

**Files:**
- Create: `tools/corpus/build_index.py`
- Test: `tools/corpus/tests/test_build_index.py`
- Create: `context/pager-index.md` (generated)

**Step 1: Write the failing tests**

```python
# tools/corpus/tests/test_build_index.py
from build_index import build_index

CORPUS = [
    {
        "number": 1, "affected_areas": ["RBAC Issues"],
        "fix_prs": {"https://github.com/devtron-labs/devtron/pull/1":
                    ["pkg/auth/rbac.go", "pkg/auth/enforcer.go"]},
    },
    {
        "number": 2, "affected_areas": ["RBAC Issues"],
        "fix_prs": {"https://github.com/devtron-labs/devtron/pull/2":
                    ["pkg/auth/rbac.go"]},
    },
    {
        "number": 3, "affected_areas": ["ci (blocking)"],
        "fix_prs": {"https://github.com/devtron-labs/devtron/pull/3":
                    ["pkg/pipeline/ci.go"]},
    },
]


def test_groups_files_by_affected_area():
    index = build_index(CORPUS)
    assert "RBAC Issues" in index
    assert "ci (blocking)" in index


def test_orders_files_by_frequency():
    index = build_index(CORPUS)
    files = [entry["path"] for entry in index["RBAC Issues"]["files"]]
    assert files[0] == "pkg/auth/rbac.go"  # appears twice


def test_records_hit_counts():
    index = build_index(CORPUS)
    counts = {e["path"]: e["count"] for e in index["RBAC Issues"]["files"]}
    assert counts["pkg/auth/rbac.go"] == 2
    assert counts["pkg/auth/enforcer.go"] == 1


def test_records_repos_per_area():
    index = build_index(CORPUS)
    assert index["RBAC Issues"]["repos"] == ["devtron"]


def test_ticket_with_no_areas_is_skipped():
    index = build_index(CORPUS + [{"number": 4, "affected_areas": [], "fix_prs": {}}])
    assert len(index) == 2
```

**Step 2: Run to verify they fail**

Run: `cd tools/corpus && python -m pytest tests/test_build_index.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'build_index'`

**Step 3: Write the minimal implementation**

```python
# tools/corpus/build_index.py
"""Turn the corpus into affected_area -> historically touched files."""
import re
from collections import Counter

REPO_FROM_URL = re.compile(r"github\.com/devtron-labs/([\w.-]+)/pull/")


def build_index(corpus: list[dict]) -> dict:
    areas: dict[str, dict] = {}
    for record in corpus:
        for area in record.get("affected_areas", []):
            bucket = areas.setdefault(area, {"_files": Counter(), "_repos": set()})
            for url, files in record.get("fix_prs", {}).items():
                if match := REPO_FROM_URL.search(url):
                    bucket["_repos"].add(match.group(1))
                bucket["_files"].update(files)

    return {
        area: {
            "repos": sorted(bucket["_repos"]),
            "files": [
                {"path": path, "count": count}
                for path, count in bucket["_files"].most_common()
            ],
        }
        for area, bucket in areas.items()
    }


def render_markdown(index: dict, top_n: int = 15) -> str:
    lines = [
        "# Pager bug history: where fixes have landed",
        "",
        "Generated from closed `pager-duty` issues and the PRs that fixed them.",
        "Use as a prior for where to look first, not as an answer.",
        "",
    ]
    for area, data in sorted(index.items()):
        lines.append(f"## {area}")
        lines.append(f"Repos: {', '.join(data['repos'])}")
        lines.append("")
        for entry in data["files"][:top_n]:
            lines.append(f"- `{entry['path']}` ({entry['count']}x)")
        lines.append("")
    return "\n".join(lines)
```

**Step 4: Run to verify they pass**

Run: `cd tools/corpus && python -m pytest tests/test_build_index.py -v`
Expected: PASS, 5 tests.

**Step 5: Generate the real index**

```bash
cd tools/corpus && python -c "
import json, pathlib, build_index
corpus = json.loads(pathlib.Path('../../data/corpus.json').read_text())
index = build_index.build_index(corpus)
out = pathlib.Path('../../context/pager-index.md')
out.parent.mkdir(exist_ok=True)
out.write_text(build_index.render_markdown(index))
print(f'{len(index)} areas')
"
```
Expected: a `context/pager-index.md` you can read and sanity-check. If "RBAC Issues"
doesn't point at auth/casbin paths, the corpus or the parser is wrong — investigate before
trusting any measurement built on it.

**Step 6: Hand over for review.**

---

## Task 7: Repo map

The index says *which files*; the repo map says *what each repo is and how to build it*.
Both are context the Action will carry.

**Files:**
- Create: `context/repo-map.md`

**Step 1: Gather the facts**

For each of the five repos, find and record: what it does, its language, its build command,
its test command, and the top-level directories that matter. Read each repo's own README,
`Makefile`, and CI workflows — do not guess.

**Step 2: Write it**

One section per repo, plus a short "which repo owns what" table mapping the pager
template's affected-area vocabulary (RBAC issues, ci blocking, cd blocking, login issues,
deployment from chart store, …) to the repos and subsystems that own them.

**Step 3: Hand over for review** — Shivam can correct the ownership map from experience
faster than any amount of reading will.

---

## Task 8: Measure with context

**Files:**
- Create: `data/results-with-context.json` (generated)
- Create: `context/combined.md` (index + repo map concatenated)

**Step 1: Combine the context**

```bash
cat context/repo-map.md context/pager-index.md > context/combined.md
```

**Step 2: Re-run on the same tickets**

Run:
```bash
cd tools/eval && python run_eval.py \
  --context-file ../../context/combined.md \
  --out ../../data/results-with-context.json --limit 10
```
Use **the same `--limit`** as the baseline — a different slice makes the comparison
meaningless.

**Step 3: Compare**

```bash
cd tools/eval && python -c "
import json
for name in ('baseline', 'with-context'):
    d = json.load(open(f'../../data/results-{name}.json'))
    print(name, d['summary'])
"
```

**Step 4: Write the verdict into `CLAUDE.md`**

Record both summaries and a one-line conclusion. Three honest outcomes, all worth writing
down: the index helps materially (proceed to Phase 3), it helps marginally (consider
whether the maintenance is worth it), or it doesn't help (say so — the localization problem
needs a different attack, and the design's central bet was wrong).

**Step 5: Hand over for review.**

---

## Phases 3–4 (not yet planned)

Deliberately unplanned until Task 8 produces numbers.

- **Phase 3:** LangGraph Zoho triage through issue creation, both gates live.
- **Phase 4:** The seam — `pager-duty` label triggers the GitHub Action; the PR link
  flows back to the Zoho ticket.

Writing detailed steps for these now would mean specifying a Zoho integration whose value
depends on a localization capability we haven't measured. If Task 8 says the approach
works, Phase 3 gets its own plan; if it doesn't, that plan would have been wasted.

**Prerequisite to unblock before Phase 3 starts:** determine whether a usable Zoho Desk MCP
server exists. If not, a thin MCP wrapper over their REST API is the first task of that
plan.
