"""Tests for the pure logic in `replay.py`.

Nothing here starts a subprocess or touches the network. `run_claude` and
`replay` are deliberately untested: they are a thin shell around the `claude`
CLI, and a test that mocked the CLI would only assert that the mock matches the
argv we wrote, which is the part most likely to go stale. What *is* tested is
everything between the model's reply and `score.score_prediction`, because that
is where a silent mismatch corrupts the baseline.
"""
import json

import pytest

from replay import (
    PROMPT,
    REPO_NAMES,
    build_prompt,
    check_workspace,
    extract_json,
    redact_ground_truth,
    validate_prediction,
)
from score import KNOWN_REPOS

VALID = {
    "repos": ["devtron"],
    "files": ["devtron/util/rbac/EnforcerUtil.go"],
    "reasoning": "RBAC enforcement for helm apps lives here.",
}


# --- extract_json ------------------------------------------------------------


def test_bare_json_object():
    assert extract_json(json.dumps(VALID)) == VALID


def test_json_with_surrounding_prose():
    output = (
        "I looked at the RBAC layer (see util/rbac) and concluded the following.\n"
        + json.dumps(VALID)
        + "\nHappy to dig further if useful."
    )
    assert extract_json(output) == VALID


def test_json_in_a_fenced_code_block():
    output = "Here is the answer:\n```json\n" + json.dumps(VALID) + "\n```\n"
    assert extract_json(output) == VALID


def test_no_json_at_all_raises():
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json("The bug is somewhere in the RBAC enforcement layer.")


def test_malformed_json_raises():
    # Balanced braces, but not parseable — a trailing comma and an unquoted key.
    with pytest.raises(ValueError):
        extract_json('{"files": ["devtron/a.go",], repos: ["devtron"]}')


def test_empty_output_raises():
    with pytest.raises(ValueError, match="empty"):
        extract_json("   ")


def test_object_without_required_keys_is_rejected_not_returned():
    """A stray object in prose must not be mistaken for a prediction.

    The failure mode this guards against is a parser that returns *something*
    for every reply: it turns an instruction-following failure into a silent
    zero in the baseline, which reads as a model failure rather than a harness
    one.
    """
    with pytest.raises(ValueError, match="none contain"):
        extract_json('Config looked like {"enabled": true} so I stopped there.')


def test_prose_containing_braces_does_not_swallow_the_answer():
    """The regression that motivated replacing a greedy `\\{.*\\}` pattern.

    With two brace-bearing blocks in one reply, a greedy match spans from the
    first `{` to the last `}` and parses as nothing at all.
    """
    output = (
        'The casbin policy object looked like {"obj": "helm/*"} in the DB.\n'
        "My conclusion:\n" + json.dumps(VALID)
    )
    assert extract_json(output) == VALID


def test_braces_inside_string_values_do_not_break_balancing():
    payload = dict(VALID, reasoning='The policy template is "{team}/{env}/{app}".')
    assert extract_json("Answer: " + json.dumps(payload)) == payload


def test_escaped_quote_inside_string_does_not_break_balancing():
    payload = dict(VALID, reasoning='It builds a \\"helm\\" object path.')
    assert extract_json(json.dumps(payload)) == payload


def test_last_qualifying_object_wins_when_schema_is_restated():
    """Models often echo the requested shape, then answer. The answer is last."""
    echo = {"repos": ["<repo-name>"], "files": ["<repo>/path.go"], "reasoning": "..."}
    output = (
        "I will return:\n" + json.dumps(echo) + "\n\nActual answer:\n"
        + json.dumps(VALID)
    )
    assert extract_json(output) == VALID


def test_nested_object_is_not_mistaken_for_a_top_level_candidate():
    payload = dict(VALID, reasoning="ignored")
    payload["extra"] = {"files": ["decoy/decoy.go"]}
    assert extract_json(json.dumps(payload))["files"] == VALID["files"]


# --- validate_prediction -----------------------------------------------------


def test_valid_prediction_passes_through():
    out = validate_prediction(VALID)
    assert out["files"] == VALID["files"]
    assert out["repos"] == ["devtron"]
    assert out["reasoning"] == VALID["reasoning"]


def test_unprefixed_path_warns_because_score_cannot_attribute_a_repo():
    with pytest.warns(UserWarning, match="not prefixed"):
        out = validate_prediction(
            {"repos": ["devtron"], "files": ["util/rbac/EnforcerUtil.go"]}
        )
    # Still returned: score.py matches it on relative path alone, so dropping it
    # would understate recall. The warning is the signal, not a silent fix.
    assert out["files"] == ["util/rbac/EnforcerUtil.go"]


def test_repo_prefixed_paths_do_not_warn():
    import warnings as _warnings

    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        validate_prediction(VALID)


def test_files_must_be_a_list_of_strings():
    with pytest.raises(ValueError, match="list of strings"):
        validate_prediction({"files": "devtron/a.go"})
    with pytest.raises(ValueError, match="list of strings"):
        validate_prediction({"files": [{"path": "devtron/a.go"}]})


def test_missing_repos_is_derived_from_the_file_prefixes():
    out = validate_prediction(
        {"files": ["devtron/a.go", "dashboard/src/b.tsx", "devtron/c.go"]}
    )
    assert out["repos"] == ["dashboard", "devtron"]


def test_blank_and_whitespace_paths_are_dropped():
    out = validate_prediction({"files": ["  devtron/a.go  ", "", "   "]})
    assert out["files"] == ["devtron/a.go"]


# --- prompt ------------------------------------------------------------------


def test_score_known_repos_is_a_subset_of_the_repos_the_prompt_names():
    """The one place these two lists could drift apart and quietly halve recall.

    `REPO_NAMES` (8) is deliberately larger than `score.KNOWN_REPOS` (5): the
    three newer repos are split off a predicted path only when a truth PR URL
    names them. Superset is the safe direction. The reverse — a repo `score`
    would split that the prompt never mentions — means the agent can never name
    it, so recall on those tickets is structurally zero.
    """
    assert set(KNOWN_REPOS) <= set(REPO_NAMES)


def test_prompt_lists_every_repo_the_agent_may_predict():
    """An agent that is not told a repo exists will never name a file in it."""
    prompt = build_prompt({"title": "t", "body": "b"})
    for repo in REPO_NAMES:
        assert repo in prompt


def test_repo_names_has_no_duplicates():
    assert len(REPO_NAMES) == len(set(REPO_NAMES))


def test_paths_under_the_newer_repos_do_not_warn():
    """`notifier/...` obeys the contract even though score.KNOWN_REPOS omits it.

    Warning here would train the operator to ignore the warning that matters.
    """
    import warnings as _warnings

    for repo in set(REPO_NAMES) - set(KNOWN_REPOS):
        with _warnings.catch_warnings():
            _warnings.simplefilter("error")
            validate_prediction({"files": [f"{repo}/pkg/thing.go"]})


def test_check_workspace_flags_drift_in_both_directions(tmp_path):
    """Regression guard for the failure that actually happened mid-build.

    Three repos appeared in `workspace/` while the prompt still named five, so
    the agent could not have localized into them.
    """
    (tmp_path / "devtron").mkdir()
    (tmp_path / "some-new-repo").mkdir()
    with pytest.warns(UserWarning) as caught:
        drift = check_workspace(tmp_path)
    messages = " ".join(str(w.message) for w in caught)
    assert "some-new-repo" in messages  # on disk, not in the prompt
    assert "notifier" in messages  # in the prompt, not on disk
    assert "some-new-repo" in drift


def test_check_workspace_is_silent_when_disk_matches_the_prompt(tmp_path):
    import warnings as _warnings

    for repo in REPO_NAMES:
        (tmp_path / repo).mkdir()
    (tmp_path / ".git").mkdir()  # dotfile dirs are not repos
    with _warnings.catch_warnings():
        _warnings.simplefilter("error")
        assert check_workspace(tmp_path) == []


def test_prompt_worked_example_paths_are_repo_prefixed():
    """Every path shown as `Correct:` must survive score.py's repo split.

    If the example itself were unprefixed, the prompt would be teaching the
    shape that scores worst.
    """
    correct_block = PROMPT.split("Correct:")[1].split("Wrong:")[0]
    examples = [
        line.strip().strip('"')
        for line in correct_block.splitlines()
        if line.strip().startswith('"')
    ]
    assert examples
    for path in examples:
        assert path.partition("/")[0] in KNOWN_REPOS, path


def test_prompt_does_not_leak_the_smoke_test_answer():
    """2960's ground-truth files must not appear in the prompt template.

    This is the one ticket used to judge the harness. An example drawn from its
    fix PR would make the smoke test measure the prompt, not the agent.
    """
    for leaked in (
        "EnforcerUtil.go",
        "EnforcerUtilHelm.go",
        "EnforcerUtilHelmObject_test.go",
        "AppRepository.go",
    ):
        assert leaked not in PROMPT


# --- ground-truth redaction --------------------------------------------------
# Every body in the 41-record corpus contains its own fix-PR URLs, because the
# "## PR Links" section the agent would read is the same section
# tools/corpus/parse_issue.py scrapes to build the answer key.

REAL_TAIL = """### 👍 Expected behavior

The user should be able to list helm apps.

## PR Links
- https://github.com/devtron-labs/devtron-enterprise/pull/3402
- https://github.com/devtron-labs/devtron/pull/7034

## Microservices
- [x] Orchestrator
- [ ] Dashboard
"""
REPORT = "### 📜 Description\n\n" + "Helm apps are not listed for admins. " * 12


def test_pr_links_section_is_cut_from_the_body():
    redacted = redact_ground_truth(REPORT + REAL_TAIL)
    assert "PR Links" not in redacted
    assert "7034" not in redacted
    assert "devtron-enterprise" not in redacted
    # ...and the checklist below it, which names the service outright.
    assert "Orchestrator" not in redacted
    assert "Expected behavior" in redacted


def test_pr_url_outside_the_pr_links_section_is_redacted():
    body = REPORT + "\n\nA similar fix was https://github.com/devtron-labs/devtron/pull/1 here."
    redacted = redact_ground_truth(body)
    assert "pull/1" not in redacted
    assert "[redacted]" in redacted


def test_commit_and_issue_urls_are_redacted_too():
    body = REPORT + (
        "\nSee https://github.com/devtron-labs/dashboard/issues/42 and "
        "https://github.com/devtron-labs/devtron/commit/abc123def."
    )
    redacted = redact_ground_truth(body)
    assert "issues/42" not in redacted
    assert "abc123def" not in redacted


def test_a_body_that_is_only_a_template_is_not_reduced_to_nothing():
    """An empty prompt scores zero and reads as a model failure, not a harness one."""
    redacted = redact_ground_truth(
        "## PR Links\n- https://github.com/devtron-labs/devtron/pull/7034\n"
    )
    assert redacted.strip()
    assert "7034" not in redacted


def test_redaction_survives_heading_level_and_spacing_variants():
    for heading in ("## PR Links", "#### PR links", "  ### PR  Links", "# PR Links"):
        body = REPORT + f"\n{heading}\n- https://github.com/devtron-labs/devtron/pull/7034\n"
        assert "7034" not in redact_ground_truth(body), heading


def test_body_without_any_leak_is_left_alone():
    assert redact_ground_truth(REPORT) == REPORT.rstrip()


def test_empty_body_is_handled():
    assert redact_ground_truth("") == ""


def test_build_prompt_redacts_the_answer_key():
    """The guard that matters: no fix-PR number reaches the agent."""
    prompt = build_prompt({"title": "PagerBug: helm", "body": REPORT + REAL_TAIL})
    assert "7034" not in prompt
    assert "3402" not in prompt
    assert "Helm apps are not listed for admins" in prompt


def test_every_corpus_body_is_scrubbed_of_its_own_fix_pr_numbers():
    """Runs against the real corpus, so a template change is caught here.

    Skips rather than fails when the corpus is mid-rebuild or absent — this is
    a leak check, not a corpus test.
    """
    import json as _json
    import re as _re

    from replay import CORPUS

    if not CORPUS.exists():
        pytest.skip("corpus not built")
    try:
        records = _json.loads(CORPUS.read_text())
    except _json.JSONDecodeError:
        pytest.skip("corpus mid-rebuild")

    pull = _re.compile(r"github\.com/[\w.-]+/([\w.-]+)/pull/(\d+)", _re.I)
    for record in records:
        prompt = build_prompt(record)
        for url in record.get("fix_prs", {}):
            match = pull.search(url)
            if not match:
                continue
            repo, number = match.groups()
            assert f"pull/{number}" not in prompt, (record["number"], url)
            assert f"{repo}/pull" not in prompt, (record["number"], url)


def test_build_prompt_includes_title_and_body():
    prompt = build_prompt({"title": "PagerBug: helm broken", "body": "steps here"})
    assert "PagerBug: helm broken" in prompt
    assert "steps here" in prompt


def test_extra_context_is_injected_and_optional():
    record = {"title": "t", "body": "b"}
    assert "PAST FIXES" not in build_prompt(record)
    assert "PAST FIXES" in build_prompt(record, "PAST FIXES\nrbac -> util/rbac/")


def test_build_prompt_tolerates_a_record_missing_a_title():
    assert "b" in build_prompt({"body": "b"})
