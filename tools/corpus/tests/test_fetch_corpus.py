"""Tests for the corpus fetcher.

Every test drives the module through its single `gh` seam — a callable taking
argv and returning stdout — so nothing here touches the network. `FakeGh` below
plays the part of the CLI, including its failure modes, which is the only way to
exercise the behaviour that matters: `gh pr view --json files` truncating at 100
entries while reporting exit code 0.
"""
import json
import subprocess

import pytest

import fetch_corpus as fc


# --------------------------------------------------------------------------- #
# fake gh
# --------------------------------------------------------------------------- #

def gh_error(stderr, returncode=1):
    return fc.classify(("pr", "view"), returncode, stderr)


RATE_LIMITED = "API rate limit exceeded for user ID 1234. (HTTP 403)"
BAD_CREDENTIALS = "gh: Bad credentials (HTTP 401)"
NOT_FOUND = "GraphQL: Could not resolve to a PullRequest with the number of 99. (repository.pullRequest)"


def pr_response(paths, changed=None, repo="devtron", cross=False, state="MERGED"):
    """A `gh pr view --json files,changedFiles,...` payload."""
    return {
        "files": [{"path": p} for p in paths],
        "changedFiles": len(paths) if changed is None else changed,
        "headRepository": {"name": repo, "nameWithOwner": f"devtron-labs/{repo}"},
        "isCrossRepository": cross,
        "state": state,
    }


class FakeGh:
    """Stand-in for the `gh` CLI. Records calls; raises what it is told to."""

    def __init__(self, issues=(), prs=None, paginated=None, linked=None):
        self.issues = list(issues)
        self.prs = prs or {}
        self.paginated = paginated or {}
        self.linked = linked or {}
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "issue":
            return json.dumps(self.issues)
        if args[0] == "pr":
            return self._emit(self.prs[args[2]], json_encode=True)
        if args[:2] == ("api", "--paginate"):
            return self._emit(self.paginated[args[2]], lines=True)
        if args[:2] == ("api", "graphql"):
            number = int(args[5].split("=", 1)[1])
            return self._emit(self._graphql(number), json_encode=True)
        raise AssertionError(f"unexpected gh call: {args}")

    def _graphql(self, number):
        nodes, total = self.linked.get(number, ([], None))
        if isinstance(nodes, Exception):
            return nodes
        return {
            "data": {"repository": {"issue": {"closedByPullRequestsReferences": {
                "totalCount": len(nodes) if total is None else total,
                "nodes": nodes,
            }}}}
        }

    @staticmethod
    def _emit(value, json_encode=False, lines=False):
        if isinstance(value, Exception):
            raise value
        if lines:
            return "".join(f"{line}\n" for line in value)
        return json.dumps(value) if json_encode else value

    def paginate_calls(self):
        return [c for c in self.calls if c[:2] == ("api", "--paginate")]


URL = "https://github.com/devtron-labs/devtron/pull/7034"
BIG_URL = "https://github.com/devtron-labs/athena-be/pull/442"
BIG_PATH = "repos/devtron-labs/athena-be/pulls/442/files?per_page=100"


# --------------------------------------------------------------------------- #
# C1 — the 100-file truncation
# --------------------------------------------------------------------------- #

def test_truncated_pr_is_refetched_through_pagination():
    """The live bug: changedFiles says 128, --json files hands back 100."""
    truncated = [f"f{i}.go" for i in range(100)]
    complete = [f"f{i}.go" for i in range(128)]
    gh = FakeGh(
        prs={BIG_URL: pr_response(truncated, changed=128, repo="athena-be")},
        paginated={BIG_PATH: complete},
    )

    result = fc.pr_changed_files(BIG_URL, gh=gh)

    assert result["files"] == complete
    assert len(result["files"]) == 128
    assert result["paginated"] is True
    assert len(gh.paginate_calls()) == 1


def test_matching_counts_skip_the_pagination_fallback():
    gh = FakeGh(prs={URL: pr_response(["a.go", "b.go"])})

    result = fc.pr_changed_files(URL, gh=gh)

    assert result["files"] == ["a.go", "b.go"]
    assert result["paginated"] is False
    assert gh.paginate_calls() == []


def test_unreconcilable_count_is_unreadable_not_a_short_list():
    """A short list that cannot be reconciled must never be stored as truth."""
    gh = FakeGh(
        prs={BIG_URL: pr_response([f"f{i}" for i in range(100)], changed=128,
                                  repo="athena-be")},
        paginated={BIG_PATH: [f"f{i}" for i in range(110)]},
    )

    with pytest.raises(fc.UnreadablePR) as exc:
        fc.pr_changed_files(BIG_URL, gh=gh)
    assert "unreconciled" in exc.value.reason


def test_pagination_failure_is_unreadable_not_partial():
    gh = FakeGh(
        prs={BIG_URL: pr_response([f"f{i}" for i in range(100)], changed=128,
                                  repo="athena-be")},
        paginated={BIG_PATH: gh_error(NOT_FOUND)},
    )

    with pytest.raises(fc.UnreadablePR) as exc:
        fc.pr_changed_files(BIG_URL, gh=gh)
    assert "mismatch" in exc.value.reason


def test_pagination_rate_limit_aborts_rather_than_degrading():
    gh = FakeGh(
        prs={BIG_URL: pr_response([f"f{i}" for i in range(100)], changed=128,
                                  repo="athena-be")},
        paginated={BIG_PATH: gh_error(RATE_LIMITED)},
    )

    with pytest.raises(fc.FatalGhError):
        fc.pr_changed_files(BIG_URL, gh=gh)


# --------------------------------------------------------------------------- #
# C2 — empty and null file lists
# --------------------------------------------------------------------------- #

def test_empty_file_list_is_rejected():
    gh = FakeGh(prs={URL: pr_response([])})

    with pytest.raises(fc.UnreadablePR) as exc:
        fc.pr_changed_files(URL, gh=gh)
    assert exc.value.reason == "empty_file_list"


def test_null_file_list_does_not_raise_typeerror():
    """`.get("files", [])` returns None for an explicit null, not the default."""
    payload = pr_response([])
    payload["files"] = None
    gh = FakeGh(prs={URL: payload})

    with pytest.raises(fc.UnreadablePR) as exc:
        fc.pr_changed_files(URL, gh=gh)
    assert exc.value.reason == "missing_files_field"


def test_missing_files_key_is_rejected():
    gh = FakeGh(prs={URL: {}})

    with pytest.raises(fc.UnreadablePR):
        fc.pr_changed_files(URL, gh=gh)


def test_missing_changed_files_count_is_rejected():
    payload = pr_response(["a.go"])
    del payload["changedFiles"]
    gh = FakeGh(prs={URL: payload})

    with pytest.raises(fc.UnreadablePR) as exc:
        fc.pr_changed_files(URL, gh=gh)
    assert exc.value.reason == "missing_changed_files_count"


def test_empty_record_never_reaches_the_corpus(tmp_path):
    """The old `if not truth` was False for `{url: []}` — a blank record passed."""
    issue = {"number": 1, "title": "t", "body": f"- {URL}", "labels": []}
    gh = FakeGh(issues=[issue], prs={URL: pr_response([])})

    record, skip = fc.build_record(issue, gh=gh)

    assert record is None
    assert skip["reason"] == "prs_unreadable"


# --------------------------------------------------------------------------- #
# C3 — failure classification and the degraded run
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stderr", [
    RATE_LIMITED,
    "You have exceeded a secondary rate limit",
    BAD_CREDENTIALS,
    "gh: Resource protected by organization SAML enforcement (HTTP 403)",
    "error connecting to api.github.com: dial tcp: i/o timeout",
    "something nobody has seen before",
])
def test_run_compromising_failures_are_fatal(stderr):
    assert isinstance(fc.classify(("pr",), 1, stderr), fc.FatalGhError)


@pytest.mark.parametrize("stderr", [
    NOT_FOUND,
    "GraphQL: Could not resolve to a Repository with the name 'devtron-labs/gone'.",
    "gh: Not Found (HTTP 404)",
])
def test_per_pr_failures_are_not_fatal(stderr):
    error = fc.classify(("pr",), 1, stderr)
    assert isinstance(error, fc.GhError)
    assert not isinstance(error, fc.FatalGhError)


def test_error_carries_stderr():
    """Discarding stderr is what made "rate limited" and "PR deleted" the same."""
    error = fc.classify(("pr", "view"), 1, RATE_LIMITED)
    assert "rate limit" in error.stderr.lower()
    assert "rate limit" in str(error).lower()


def test_gh_text_captures_stderr_from_subprocess(monkeypatch):
    monkeypatch.setattr(fc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout="", stderr=BAD_CREDENTIALS))

    with pytest.raises(fc.FatalGhError) as exc:
        fc.gh_text("pr", "view", URL)
    assert exc.value.stderr == BAD_CREDENTIALS


def test_gh_text_does_not_retry_auth_failures(monkeypatch):
    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        return subprocess.CompletedProcess(a[0], 1, stdout="", stderr=BAD_CREDENTIALS)

    monkeypatch.setattr(fc.subprocess, "run", fake_run)
    monkeypatch.setattr(fc.time, "sleep", lambda _: None)

    with pytest.raises(fc.FatalGhError):
        fc.gh_text("pr", "view", URL)
    assert len(calls) == 1


def test_gh_text_retries_rate_limits_then_gives_up(monkeypatch):
    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        return subprocess.CompletedProcess(a[0], 1, stdout="", stderr=RATE_LIMITED)

    monkeypatch.setattr(fc.subprocess, "run", fake_run)
    monkeypatch.setattr(fc.time, "sleep", lambda _: None)

    with pytest.raises(fc.FatalGhError):
        fc.gh_text("pr", "view", URL)
    assert len(calls) == fc.MAX_ATTEMPTS


#: Killed a real 155-issue run. Transient, and must be retried rather than
#: aborting the fetch — but still fatal if it survives every retry, because a
#: network that is down does not produce trustworthy "this PR has no files".
CONNECTION_RESET = (
    'Post "https://api.github.com/graphql": read tcp 192.168.1.14:62991'
    "->20.205.243.168:443: read: connection reset by peer"
)


def test_connection_reset_is_retried_not_instantly_fatal(monkeypatch):
    attempts = []

    def fake_run(*a, **k):
        attempts.append(a)
        if len(attempts) < 3:
            return subprocess.CompletedProcess(a[0], 1, stdout="",
                                               stderr=CONNECTION_RESET)
        return subprocess.CompletedProcess(a[0], 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(fc.subprocess, "run", fake_run)
    monkeypatch.setattr(fc.time, "sleep", lambda _: None)

    assert fc.gh_text("pr", "view", URL) == '{"ok": true}'
    assert len(attempts) == 3


def test_connection_reset_that_never_clears_is_still_fatal(monkeypatch):
    monkeypatch.setattr(fc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout="", stderr=CONNECTION_RESET))
    monkeypatch.setattr(fc.time, "sleep", lambda _: None)

    with pytest.raises(fc.FatalGhError):
        fc.gh_text("pr", "view", URL)


def test_unparseable_output_is_fatal():
    with pytest.raises(fc.FatalGhError):
        fc.gh_json("pr", "view", gh=lambda *a: "not json")


def test_degraded_run_aborts_instead_of_writing_an_empty_corpus(tmp_path):
    """Token dies after `issue list` succeeds: abort, do not publish `[]`."""
    out = tmp_path / "corpus.json"
    out.write_text(json.dumps([{"number": 1, "fix_prs": {URL: ["a.go"]}}]))
    before = out.read_text()

    issue = {"number": 1, "title": "t", "body": f"- {URL}", "labels": []}
    gh = FakeGh(issues=[issue], prs={URL: gh_error(RATE_LIMITED)})

    with pytest.raises(fc.FatalGhError):
        fc.main(limit=1, gh=gh, out=out, skipped_out=tmp_path / "skipped.json")

    assert out.read_text() == before


def test_all_issues_unreadable_aborts_rather_than_emptying_the_file(tmp_path):
    out = tmp_path / "corpus.json"
    out.write_text(json.dumps([{"number": 1, "fix_prs": {URL: ["a.go"]}}]))
    before = out.read_text()

    issue = {"number": 1, "title": "t", "body": f"- {URL}", "labels": []}
    gh = FakeGh(issues=[issue], prs={URL: gh_error(NOT_FOUND)})

    with pytest.raises(SystemExit):
        fc.main(limit=1, gh=gh, out=out, skipped_out=tmp_path / "skipped.json")

    assert out.read_text() == before


def _issue(number, url):
    return {"number": number, "title": f"t{number}", "body": f"- {url}", "labels": []}


def _many(count, files=("a.go",)):
    urls = [f"https://github.com/devtron-labs/devtron/pull/{i}" for i in range(count)]
    issues = [_issue(i, url) for i, url in enumerate(urls)]
    prs = {url: pr_response(list(files)) for url in urls}
    return issues, prs


def test_refuses_to_overwrite_on_a_large_record_count_regression(tmp_path):
    out = tmp_path / "corpus.json"
    out.write_text(json.dumps([{"number": i, "fix_prs": {URL: ["a.go"]}}
                               for i in range(41)]))
    before = out.read_text()

    issues, prs = _many(5)
    gh = FakeGh(issues=issues, prs=prs)

    with pytest.raises(SystemExit) as exc:
        fc.main(limit=5, gh=gh, out=out, skipped_out=tmp_path / "skipped.json")
    assert "refusing to overwrite" in str(exc.value)
    assert out.read_text() == before


def test_force_overrides_the_regression_guard(tmp_path):
    out = tmp_path / "corpus.json"
    out.write_text(json.dumps([{"number": i, "fix_prs": {URL: ["a.go"]}}
                               for i in range(41)]))

    issues, prs = _many(5)
    gh = FakeGh(issues=issues, prs=prs)
    fc.main(limit=5, gh=gh, out=out, skipped_out=tmp_path / "skipped.json", force=True)

    assert len(json.loads(out.read_text())) == 5


def test_small_regression_within_tolerance_is_allowed(tmp_path):
    out = tmp_path / "corpus.json"
    out.write_text(json.dumps([{"number": i, "fix_prs": {URL: ["a.go"]}}
                               for i in range(10)]))

    issues, prs = _many(10)
    gh = FakeGh(issues=issues, prs=prs)
    stats = fc.main(limit=10, gh=gh, out=out, skipped_out=tmp_path / "skipped.json")

    assert stats["records"] == 10


def test_growth_is_never_blocked(tmp_path):
    out = tmp_path / "corpus.json"
    out.write_text(json.dumps([{"number": 1, "fix_prs": {URL: ["a.go"]}}]))

    issues, prs = _many(12)
    gh = FakeGh(issues=issues, prs=prs)
    stats = fc.main(limit=12, gh=gh, out=out, skipped_out=tmp_path / "skipped.json")

    assert stats["records"] == 12
    assert len(json.loads(out.read_text())) == 12


def test_no_temp_files_are_left_behind(tmp_path):
    out = tmp_path / "corpus.json"
    issues, prs = _many(3)
    fc.main(limit=3, gh=FakeGh(issues=issues, prs=prs), out=out,
            skipped_out=tmp_path / "skipped.json")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["corpus.json", "skipped.json"]


def test_sanity_check_rejects_a_record_with_an_empty_file_list(tmp_path):
    with pytest.raises(SystemExit):
        fc.sanity_check([{"number": 1, "fix_prs": {URL: []}}], tmp_path / "corpus.json")


# --------------------------------------------------------------------------- #
# C4 — partial ground truth
# --------------------------------------------------------------------------- #

def test_partial_ground_truth_is_recorded_not_hidden():
    good = "https://github.com/devtron-labs/devtron/pull/1"
    also = "https://github.com/devtron-labs/dashboard/pull/2"
    lost = "https://github.com/devtron-labs/devtron-enterprise/pull/3"
    issue = {"number": 7, "title": "t", "body": f"{good}\n{also}\n{lost}",
             "labels": [{"name": "pager-duty"}]}
    gh = FakeGh(prs={
        good: pr_response(["a.go"]),
        also: pr_response(["b.tsx"], repo="dashboard"),
        lost: gh_error(NOT_FOUND),
    })

    record, skip = fc.build_record(issue, gh=gh)

    assert skip is None
    assert len(record["fix_prs"]) == 2
    assert [u["url"] for u in record["unreadable_prs"]] == [lost]


def test_complete_record_reports_no_losses():
    url = "https://github.com/devtron-labs/devtron/pull/1"
    issue = {"number": 7, "title": "t", "body": url, "labels": []}

    record, _ = fc.build_record(issue, gh=FakeGh(prs={url: pr_response(["a.go"])}))

    assert record["unreadable_prs"] == []


# --------------------------------------------------------------------------- #
# linked-PR query: the second cap, unmerged PRs, and the hardcoded repo
# --------------------------------------------------------------------------- #

def test_only_merged_linked_prs_become_ground_truth():
    """An abandoned attempt that said "fixes #N" is not a fix."""
    gh = FakeGh(linked={9: ([
        {"url": "https://github.com/devtron-labs/devtron/pull/1", "merged": True},
        {"url": "https://github.com/devtron-labs/devtron/pull/2", "merged": False},
    ], None)})

    assert fc.linked_pr_urls(9, gh=gh) == ["https://github.com/devtron-labs/devtron/pull/1"]


def test_linked_pr_cap_being_hit_aborts_the_run():
    nodes = [{"url": f"https://github.com/devtron-labs/devtron/pull/{i}", "merged": True}
             for i in range(fc.LINKED_PR_LIMIT)]
    gh = FakeGh(linked={9: (nodes, fc.LINKED_PR_LIMIT + 5)})

    with pytest.raises(fc.FatalGhError) as exc:
        fc.linked_pr_urls(9, gh=gh)
    assert "caps at" in exc.value.stderr


def test_linked_pr_query_is_built_from_the_repo_constant():
    assert fc.REPO == f"{fc.REPO_OWNER}/{fc.REPO_NAME}"
    assert f'owner: "{fc.REPO_OWNER}"' in fc.LINKED_PRS_QUERY
    assert f'name: "{fc.REPO_NAME}"' in fc.LINKED_PRS_QUERY
    assert "sprint-tasks" not in fc.LINKED_PRS_QUERY.replace(fc.REPO_NAME, "", 1)
    assert f"first: {fc.LINKED_PR_LIMIT}" in fc.LINKED_PRS_QUERY


def test_linked_lookup_survives_a_benign_graphql_failure():
    gh = FakeGh(linked={9: (gh_error(NOT_FOUND), None)})
    assert fc.linked_pr_urls(9, gh=gh) == []


def test_linked_lookup_propagates_a_fatal_graphql_failure():
    gh = FakeGh(linked={9: (gh_error(RATE_LIMITED), None)})
    with pytest.raises(fc.FatalGhError):
        fc.linked_pr_urls(9, gh=gh)


def test_body_links_take_precedence_over_the_linked_query():
    url = "https://github.com/devtron-labs/devtron/pull/1"
    issue = {"number": 9, "title": "t", "body": url, "labels": []}
    gh = FakeGh(prs={url: pr_response(["a.go"])})

    record, _ = fc.build_record(issue, gh=gh)

    assert record["pr_source"] == "body"
    assert not [c for c in gh.calls if c[:2] == ("api", "graphql")]


def test_blank_pr_section_falls_back_to_the_linked_query():
    url = "https://github.com/devtron-labs/devtron/pull/1"
    issue = {"number": 9, "title": "t", "body": "no links here", "labels": []}
    gh = FakeGh(prs={url: pr_response(["a.go"])},
                linked={9: ([{"url": url, "merged": True}], None)})

    record, _ = fc.build_record(issue, gh=gh)

    assert record["pr_source"] == "linked"
    assert record["fix_prs"] == {url: ["a.go"]}


# --------------------------------------------------------------------------- #
# per-PR repo field
# --------------------------------------------------------------------------- #

def test_per_pr_repo_comes_from_head_repository():
    url = "https://github.com/devtron-labs/dashboard/pull/5"
    gh = FakeGh(prs={url: pr_response(["src/a.tsx"], repo="dashboard")})

    assert fc.pr_changed_files(url, gh=gh)["repo"] == "dashboard"


def test_fork_pr_repo_falls_back_to_the_base_repo_in_the_url():
    """For a fork PR the head repo is the contributor's copy; files are base-relative."""
    url = "https://github.com/devtron-labs/devtron/pull/5"
    gh = FakeGh(prs={url: pr_response(["a.go"], repo="someone-fork", cross=True)})

    assert fc.pr_changed_files(url, gh=gh)["repo"] == "devtron"


def test_record_carries_a_repo_for_every_fix_pr():
    a = "https://github.com/devtron-labs/devtron/pull/1"
    b = "https://github.com/devtron-labs/notifier/pull/2"
    issue = {"number": 9, "title": "t", "body": f"{a}\n{b}", "labels": []}
    gh = FakeGh(prs={a: pr_response(["a.go"]),
                     b: pr_response(["b.go"], repo="notifier")})

    record, _ = fc.build_record(issue, gh=gh)

    assert record["pr_repos"] == {a: "devtron", b: "notifier"}
    assert set(record["pr_repos"]) == set(record["fix_prs"])


def test_pagination_asks_rest_for_filename_not_path():
    """REST spells it `filename`; `.path` returns one empty line per file — the
    right count with no content, which would reconcile and store blanks."""
    truncated = [f"f{i}.go" for i in range(100)]
    gh = FakeGh(
        prs={BIG_URL: pr_response(truncated, changed=128, repo="athena-be")},
        paginated={BIG_PATH: [f"f{i}.go" for i in range(128)]},
    )
    fc.pr_changed_files(BIG_URL, gh=gh)

    call = gh.paginate_calls()[0]
    assert ".[].filename" in call
    assert "per_page=100" in call[2]


def test_blank_paginated_lines_are_not_counted_as_files():
    """The `.path` failure mode: 128 empty lines must not reconcile to 128 files."""
    gh = FakeGh(
        prs={BIG_URL: pr_response([f"f{i}" for i in range(100)], changed=128,
                                  repo="athena-be")},
        paginated={BIG_PATH: ["" for _ in range(128)]},
    )

    with pytest.raises(fc.UnreadablePR) as exc:
        fc.pr_changed_files(BIG_URL, gh=gh)
    assert "unreconciled" in exc.value.reason


# --------------------------------------------------------------------------- #
# resumability: an interrupted run must cost nothing but time
# --------------------------------------------------------------------------- #

def test_interrupted_run_leaves_the_existing_corpus_intact(tmp_path):
    """A killed process must not produce a half-written corpus."""
    out = tmp_path / "corpus.json"
    out.write_text(json.dumps([{"number": 1, "fix_prs": {URL: ["a.go"]}}]))
    before = out.read_text()

    issues, prs = _many(4)
    boom = KeyboardInterrupt()

    class Interrupting(FakeGh):
        def __call__(self, *args):
            if args[0] == "pr" and len(self.calls) > 2:
                raise boom
            return super().__call__(*args)

    with pytest.raises(KeyboardInterrupt):
        fc.main(limit=4, gh=Interrupting(issues=issues, prs=prs), out=out,
                skipped_out=tmp_path / "skipped.json")

    assert out.read_text() == before
    assert not list(tmp_path.glob(".corpus.json.*"))


def test_cache_lets_a_second_run_skip_work_already_done(tmp_path):
    cache = tmp_path / "cache.json"
    issues, prs = _many(3)

    first = FakeGh(issues=issues, prs=prs)
    fc.main(limit=3, gh=first, out=tmp_path / "corpus.json",
            skipped_out=tmp_path / "skipped.json", cache_path=cache)
    pr_calls_first = len([c for c in first.calls if c[0] == "pr"])

    second = FakeGh(issues=issues, prs={})  # any live PR lookup would KeyError
    stats = fc.main(limit=3, gh=second, out=tmp_path / "corpus.json",
                    skipped_out=tmp_path / "skipped.json", cache_path=cache)

    assert pr_calls_first == 3
    assert [c for c in second.calls if c[0] == "pr"] == []
    assert stats["records"] == 3


def test_cache_remembers_unreadable_prs_too():
    cache = fc.PrCache(None)  # no path: a no-op cache
    assert cache.get("pr:x") is None


def test_cache_records_per_pr_failures(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache = fc.PrCache(cache_path)
    gh = FakeGh(prs={URL: gh_error(NOT_FOUND)})

    with pytest.raises(fc.UnreadablePR):
        fc.cached_pr_changed_files(URL, gh=gh, cache=cache)
    with pytest.raises(fc.UnreadablePR):
        fc.cached_pr_changed_files(URL, gh=FakeGh(prs={}), cache=fc.PrCache(cache_path))

    assert len([c for c in gh.calls if c[0] == "pr"]) == 1


def test_fatal_failures_are_never_cached(tmp_path):
    """A rate limit is a fact about the run, not about the PR."""
    cache_path = tmp_path / "cache.json"
    gh = FakeGh(prs={URL: gh_error(RATE_LIMITED)})

    with pytest.raises(fc.FatalGhError):
        fc.cached_pr_changed_files(URL, gh=gh, cache=fc.PrCache(cache_path))

    reloaded = fc.PrCache(cache_path)
    assert reloaded.get(f"pr:{URL}") is None


def test_corrupt_cache_is_a_miss_not_a_crash(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{not json")

    cache = fc.PrCache(cache_path)
    gh = FakeGh(prs={URL: pr_response(["a.go"])})

    assert fc.cached_pr_changed_files(URL, gh=gh, cache=cache)["files"] == ["a.go"]


def test_cache_is_never_mistaken_for_the_corpus(tmp_path):
    """The cache stores lookups keyed by url, not a list of records."""
    cache_path = tmp_path / "cache.json"
    issues, prs = _many(2)
    fc.main(limit=2, gh=FakeGh(issues=issues, prs=prs), out=tmp_path / "corpus.json",
            skipped_out=tmp_path / "skipped.json", cache_path=cache_path)

    payload = json.loads(cache_path.read_text())
    assert isinstance(payload, dict)
    assert all(k.startswith(("pr:", "issue:")) for k in payload)


def test_unparseable_pr_url_is_unreadable():
    with pytest.raises(fc.UnreadablePR):
        fc.pr_changed_files("https://example.com/not-a-pr", gh=FakeGh())
