"""Fetch closed pager-duty issues and the files their fix PRs changed.

This module produces `data/corpus.json`, the ground truth for the localization
eval. Everything downstream is scored against it, so the guiding rule here is:
**a missing record is recoverable, a wrong record is not.** Truncated, empty, or
partially-fetched ground truth scores a correct prediction as wrong and leaves no
trace of why, so every path below either produces a complete record or refuses to
produce one at all.

Three hazards drive the shape of this file:

1. `gh pr view --json files` caps at 100 entries with no error and exit code 0
   (`athena-be#442` reports `changedFiles: 128` and hands back 100). Every fetch
   therefore asks for `changedFiles` alongside `files` and reconciles the two,
   falling back to `gh api --paginate` and finally to "unreadable" rather than
   storing a short list.
2. A degraded run — expired token, tripped rate limit — used to fail every PR
   lookup, write `[]`, and exit 0, destroying the committed fixture while
   reporting success. Failures are now classified; anything that indicts the run
   as a whole aborts it, and the output is written through a temp file that is
   only swapped into place after sanity and regression checks pass.
3. An issue that links three PRs and can only read two used to be stored as if it
   were complete. Per-PR losses are now recorded on the record itself in
   `unreadable_prs`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

REPO_OWNER = "devtron-labs"
REPO_NAME = "sprint-tasks"
REPO = f"{REPO_OWNER}/{REPO_NAME}"

# Repo-relative, resolved from this file so the script works from any cwd.
REPO_ROOT = Path(__file__).resolve().parents[2]
OUT = REPO_ROOT / "data" / "corpus.json"
SKIPPED_OUT = OUT.parent / "corpus_skipped.json"

#: `closedByPullRequestsReferences` needs a page size, and a page size is a cap.
#: Nothing in the API warns when the cap binds, so the returned `totalCount` is
#: checked against it and a run that would silently drop links aborts instead.
LINKED_PR_LIMIT = 20

#: Refuse to overwrite an existing corpus whose record count would drop below
#: this fraction of the committed file. A widening re-fetch goes up, not down; a
#: large drop means the run degraded, not that history changed.
REGRESSION_TOLERANCE = 0.9

#: Transient failures get a bounded retry before they are called fatal — a
#: secondary rate limit on a 222-issue run is likely and usually clears.
MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 15

#: Signature of the one seam the tests replace: argv in, stdout out.
Gh = Callable[..., str]

PR_URL_PARTS = re.compile(
    r"github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)"
)


class GhError(RuntimeError):
    """A `gh` invocation failed.

    Carries stderr. The original code discarded it, which threw away the only
    thing that distinguishes "rate limited" (abort, the run is worthless) from
    "PR was deleted" (skip this one PR, the run is fine).
    """

    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str):
        self.args_used = tuple(args)
        self.returncode = returncode
        self.stderr = (stderr or "").strip()
        super().__init__(
            f"gh {' '.join(args)} exited {returncode}: {self.stderr or '<no stderr>'}"
        )


class FatalGhError(GhError):
    """A failure that makes the whole run untrustworthy.

    Auth loss, rate limiting, and network failure do not affect one PR — they
    affect every subsequent lookup. Continuing past one produces a corpus that
    looks like "these issues had no fix PRs" when the truth is "we were locked
    out halfway through".
    """


class UnreadablePR(Exception):
    """One PR cannot yield trustworthy ground truth. The run is still valid."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


#: Failures that genuinely concern a single PR: it was deleted, it lives in a
#: repo this token cannot see, the URL is wrong. Matched case-insensitively
#: against stderr.
BENIGN_STDERR = (
    "could not resolve to a pullrequest",
    "could not resolve to a repository",
    "could not resolve to an issue",
    "no pull requests found",
    "not found (http 404)",
    "http 404",
)

#: Failures that indict the run. Checked only after BENIGN_STDERR, because a 403
#: from a rate limiter and a 404 from a deleted PR must not be conflated.
FATAL_STDERR = (
    "rate limit",
    "secondary rate limit",
    "abuse detection",
    "bad credentials",
    "requires authentication",
    "authentication failed",
    "gh auth login",
    "http 401",
    "http 403",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "connection refused",
    "connection reset",
    "broken pipe",
    "eof",
    "could not connect",
    "error connecting to",
    "server error",
    "no such host",
    "i/o timeout",
    "context deadline exceeded",
    "tls handshake",
)

#: Fatal failures worth retrying before giving up. Auth failures are not here:
#: a revoked token does not un-revoke itself.
RETRYABLE_STDERR = (
    "rate limit",
    "abuse detection",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "connection refused",
    "connection reset",
    "broken pipe",
    "eof",
    "could not connect",
    "error connecting to",
    "server error",
    "i/o timeout",
    "context deadline exceeded",
    "tls handshake",
)


def _matches(stderr: str, patterns: tuple[str, ...]) -> bool:
    lowered = stderr.lower()
    return any(pattern in lowered for pattern in patterns)


def is_benign(stderr: str) -> bool:
    """True when the failure concerns one PR rather than the whole run."""
    return _matches(stderr, BENIGN_STDERR)


def is_retryable(stderr: str) -> bool:
    return not is_benign(stderr) and _matches(stderr, RETRYABLE_STDERR)


def classify(args: tuple[str, ...], returncode: int, stderr: str) -> GhError:
    """Turn a failed `gh` run into the right exception type.

    Unrecognized failures are treated as fatal on purpose. An unknown error that
    is actually benign costs a re-run; an unknown error that is actually a rate
    limit, treated as benign, costs the corpus.
    """
    if is_benign(stderr):
        return GhError(args, returncode, stderr)
    if _matches(stderr, FATAL_STDERR):
        return FatalGhError(args, returncode, stderr)
    return FatalGhError(args, returncode, stderr)


def gh_text(*args: str) -> str:
    """Run `gh` and return stdout, retrying transient failures.

    This is the single seam the tests replace; everything else in this module
    goes through it, so no test needs the network.
    """
    last: GhError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        error = classify(args, result.returncode, result.stderr)
        if not is_retryable(result.stderr) or attempt == MAX_ATTEMPTS:
            raise error
        last = error
        wait = RETRY_BACKOFF_SECONDS * attempt
        print(
            f"transient gh failure (attempt {attempt}/{MAX_ATTEMPTS}), "
            f"retrying in {wait}s: {error.stderr[:200]}",
            file=sys.stderr,
        )
        time.sleep(wait)
    raise last  # pragma: no cover - loop above always returns or raises


def gh_json(*args: str, gh: Gh = gh_text) -> object:
    stdout = gh(*args)
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        # Exit code 0 with unparseable stdout is not a per-PR problem; it means
        # `gh` is not behaving the way this module assumes it does.
        raise FatalGhError(args, 0, f"unparseable gh output: {exc}") from exc


def list_closed_pager_issues(limit: int, gh: Gh = gh_text) -> list[dict]:
    return gh_json(
        "issue", "list",
        "--repo", REPO,
        "--label", "pager-duty",
        "--state", "closed",
        "--limit", str(limit),
        "--json", "number,title,body,labels",
        gh=gh,
    )


# `REPO_OWNER`/`REPO_NAME` are interpolated rather than hardcoded so the query and
# the `REPO` constant cannot drift apart.
LINKED_PRS_QUERY = """
query($number: Int!) {
  repository(owner: "%(owner)s", name: "%(name)s") {
    issue(number: $number) {
      closedByPullRequestsReferences(first: %(limit)d, includeClosedPrs: true) {
        totalCount
        nodes { url merged }
      }
    }
  }
}
""" % {"owner": REPO_OWNER, "name": REPO_NAME, "limit": LINKED_PR_LIMIT}


def linked_pr_urls(number: int, gh: Gh = gh_text) -> list[str]:
    """Merged PRs GitHub itself links to the issue.

    Fallback for issues that left the template's "PR Links" section blank — the
    fix PR is still recoverable from the closing/cross-reference link.

    `includeClosedPrs: true` is required (without it only *open* PRs come back),
    but it also admits closed-but-unmerged PRs, so an abandoned attempt whose
    description said "fixes #N" would otherwise become ground truth. Only merged
    PRs survive the filter.
    """
    try:
        data = gh_json(
            "api", "graphql",
            "-f", f"query={LINKED_PRS_QUERY}",
            "-F", f"number={number}",
            gh=gh,
        )
    except FatalGhError:
        raise
    except GhError:
        return []
    issue = (data.get("data", {}).get("repository", {}) or {}).get("issue") or {}
    refs = issue.get("closedByPullRequestsReferences") or {}
    nodes = refs.get("nodes") or []
    total = refs.get("totalCount")
    if isinstance(total, int) and total > LINKED_PR_LIMIT:
        raise FatalGhError(
            ("api", "graphql"),
            0,
            f"issue #{number} links {total} PRs but the query caps at "
            f"{LINKED_PR_LIMIT}; raise LINKED_PR_LIMIT rather than storing a "
            f"truncated link set",
        )
    return [node["url"] for node in nodes if node.get("merged")]


def _pr_url_parts(pr_url: str) -> tuple[str, str, str]:
    match = PR_URL_PARTS.search(pr_url)
    if not match:
        raise UnreadablePR("unparseable_pr_url")
    return match["owner"], match["repo"], match["number"]


def _paginated_files(owner: str, repo: str, number: str, gh: Gh) -> list[str]:
    """The complete file list, via the REST endpoint that actually paginates.

    Two traps here, both silent:

    - REST names the field `filename`, not `path` (that is the GraphQL/`gh pr
      view` spelling). Asking jq for `.path` yields one empty line per file — the
      right *count* with no content — which is why the caller reconciles against
      `changedFiles` and why empty lines are dropped rather than trusted.
    - `--paginate` defaults to 30 per page. `per_page=100` cuts a 128-file PR from
      five requests to two, which matters across a 222-issue run.
    """
    stdout = gh(
        "api", "--paginate",
        f"repos/{owner}/{repo}/pulls/{number}/files?per_page=100",
        "--jq", ".[].filename",
    )
    return [line for line in stdout.splitlines() if line.strip()]


def pr_changed_files(pr_url: str, gh: Gh = gh_text) -> dict:
    """Return `{"files": [...], "repo": ..., "paginated": bool}` for one PR.

    Raises `UnreadablePR` when no trustworthy file list can be produced, and
    `FatalGhError` when the failure means the run itself is compromised.

    The count reconciliation is the point of this function. `gh pr view` returns
    at most 100 files and says nothing about it, so `changedFiles` is fetched in
    the same call and compared; a mismatch triggers the paginated REST fetch, and
    a mismatch that survives that makes the PR unreadable. Storing the short list
    would score a correct prediction as wrong with nothing to show why.
    """
    owner, repo, number = _pr_url_parts(pr_url)
    try:
        data = gh_json(
            "pr", "view", pr_url,
            "--json", "files,changedFiles,headRepository,isCrossRepository,state",
            gh=gh,
        )
    except FatalGhError:
        raise
    except GhError as exc:
        raise UnreadablePR(f"pr_unreadable: {exc.stderr[:200]}") from exc

    if not isinstance(data, dict):
        raise UnreadablePR("pr_response_not_an_object")

    # `.get(key, default)` does not defend against an explicit null: a response
    # of `{"files": null}` returns None from `data.get("files", [])` and the old
    # list comprehension raised TypeError. Coerce, then validate.
    raw_files = data.get("files")
    if raw_files is None:
        raise UnreadablePR("missing_files_field")
    if not isinstance(raw_files, list):
        raise UnreadablePR("files_field_not_a_list")

    files = [
        entry["path"]
        for entry in raw_files
        if isinstance(entry, dict) and entry.get("path")
    ]

    expected = data.get("changedFiles")
    if not isinstance(expected, int):
        raise UnreadablePR("missing_changed_files_count")

    paginated = False
    if len(files) != expected:
        # The 100-entry cap, or something stranger. Either way, re-fetch.
        try:
            recovered = _paginated_files(owner, repo, number, gh)
        except FatalGhError:
            raise
        except GhError as exc:
            raise UnreadablePR(
                f"file_count_mismatch_{len(files)}_of_{expected}: {exc.stderr[:120]}"
            ) from exc
        if len(recovered) != expected:
            raise UnreadablePR(
                f"file_count_unreconciled_{len(files)}/"
                f"{len(recovered)}_of_{expected}"
            )
        files = recovered
        paginated = True

    # An empty list is not ground truth. The old code accepted it: `files is not
    # None` passed, `truth[url] = []` was stored, and `if not truth` was False
    # because the dict itself was non-empty, so a content-free record sailed
    # through as if it were a real answer.
    if not files:
        raise UnreadablePR("empty_file_list")

    return {
        "files": files,
        "repo": _base_repo(data, owner, repo),
        "paginated": paginated,
        "state": data.get("state"),
    }


def _base_repo(data: dict, url_owner: str, url_repo: str) -> str:
    """The repo the changed paths are relative to.

    `headRepository` was previously fetched and never used. It is the right
    source *except* for fork PRs, where the head repo is the contributor's fork
    and the files belong to the base repo — which is the one named in the PR URL.
    """
    head = data.get("headRepository") or {}
    head_name = head.get("name")
    if data.get("isCrossRepository") or not head_name:
        return url_repo
    return head_name


def build_record(issue: dict, gh: Gh = gh_text,
                 cache: "PrCache | None" = None) -> tuple[dict | None, dict | None]:
    """Return `(record, skip)`; exactly one is non-None."""
    from parse_issue import parse_affected_areas, parse_pr_links

    body = issue.get("body") or ""
    pr_urls = parse_pr_links(body)
    source = "body"
    if not pr_urls:
        # Template section left blank — fall back to GitHub's own issue↔PR link.
        pr_urls = cached_linked_pr_urls(issue["number"], gh=gh, cache=cache)
        source = "linked" if pr_urls else "body"

    truth: dict[str, list[str]] = {}
    pr_repos: dict[str, str] = {}
    unreadable: list[dict] = []
    paginated: list[str] = []
    for url in pr_urls:
        try:
            details = cached_pr_changed_files(url, gh=gh, cache=cache)
        except UnreadablePR as exc:
            unreadable.append({"url": url, "reason": exc.reason})
            continue
        truth[url] = details["files"]
        pr_repos[url] = details["repo"]
        if details["paginated"]:
            paginated.append(url)

    if not truth:
        return None, {
            "number": issue["number"],
            "title": issue["title"],
            "reason": "no_pr_links" if not pr_urls else "prs_unreadable",
            "pr_urls": pr_urls,
            "unreadable_prs": unreadable,
        }

    record = {
        "number": issue["number"],
        "title": issue["title"],
        "body": body,
        "labels": [label["name"] for label in issue.get("labels") or []],
        "affected_areas": parse_affected_areas(body),
        "fix_prs": truth,
        # Per-PR repo, so downstream stops re-deriving it by regexing the URL.
        "pr_repos": pr_repos,
        "pr_source": source,
        # Partial ground truth is still partial. Recording the loss here lets
        # downstream decide whether to use, weight, or drop the record instead of
        # being handed 2-of-3 PRs presented as the whole truth.
        "unreadable_prs": unreadable,
    }
    if paginated:
        record["paginated_prs"] = paginated
    return record, None


class PrCache:
    """Optional on-disk memo of per-PR lookups, so an interrupted run can resume.

    The corpus is written exactly once, at the very end, after the sanity and
    regression checks pass — that is what keeps a degraded or killed run from
    replacing a good fixture with a half-finished one. The cost of that choice is
    that an interruption at issue 200 of 222 throws away 200 issues' worth of API
    calls, which on a rate-limited run is the difference between retrying and
    giving up.

    This cache pays that cost back without weakening the guarantee: it stores
    *inputs* (what each PR's file list was), never the corpus itself, and it is
    written to scratch rather than to `data/`, so it can never be mistaken for
    ground truth. Fatal errors are deliberately not cached — a rate limit is a
    fact about the run, not about the PR, and caching it would bake a transient
    outage into every later run.
    """

    def __init__(self, path: Path | None = None):
        self.path = path
        self.entries: dict[str, object] = {}
        self._dirty = False
        if path and path.exists():
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict):
                    self.entries = loaded
            except (OSError, json.JSONDecodeError):
                # A corrupt cache is a cache miss, never a failure: the worst
                # case is that the run is as slow as it would have been anyway.
                self.entries = {}

    def get(self, key: str):
        return self.entries.get(key) if self.path else None

    def put(self, key: str, value) -> None:
        if not self.path:
            return
        self.entries[key] = value
        self._dirty = True
        self.flush()

    def flush(self) -> None:
        if self.path and self._dirty:
            _atomic_write_json(self.path, self.entries)
            self._dirty = False

    def __len__(self) -> int:
        return len(self.entries)


def cached_pr_changed_files(pr_url: str, gh: Gh = gh_text, cache: PrCache | None = None) -> dict:
    """`pr_changed_files`, memoized through `cache` when one is supplied."""
    if cache is None:
        return pr_changed_files(pr_url, gh=gh)
    entry = cache.get(f"pr:{pr_url}")
    if isinstance(entry, dict):
        if "unreadable" in entry:
            raise UnreadablePR(entry["unreadable"])
        if "ok" in entry:
            return entry["ok"]
    try:
        details = pr_changed_files(pr_url, gh=gh)
    except UnreadablePR as exc:
        cache.put(f"pr:{pr_url}", {"unreadable": exc.reason})
        raise
    cache.put(f"pr:{pr_url}", {"ok": details})
    return details


def cached_linked_pr_urls(number: int, gh: Gh = gh_text, cache: PrCache | None = None) -> list[str]:
    if cache is None:
        return linked_pr_urls(number, gh=gh)
    entry = cache.get(f"issue:{number}")
    if isinstance(entry, list):
        return entry
    urls = linked_pr_urls(number, gh=gh)
    cache.put(f"issue:{number}", urls)
    return urls


def _existing_record_count(path: Path) -> int:
    try:
        existing = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    return len(existing) if isinstance(existing, list) else 0


def sanity_check(records: list[dict], out: Path, force: bool = False) -> None:
    """Refuse to publish a corpus that is empty, blank, or a large regression."""
    if not records:
        raise SystemExit(
            "refusing to write an empty corpus: every issue failed, which means "
            "the run degraded rather than history being empty"
        )
    for record in records:
        if not record.get("fix_prs"):
            raise SystemExit(f"record #{record.get('number')} has no fix_prs")
        for url, files in record["fix_prs"].items():
            if not files:
                raise SystemExit(f"record #{record.get('number')} stores no files for {url}")

    previous = _existing_record_count(out)
    if previous and len(records) < previous * REGRESSION_TOLERANCE and not force:
        raise SystemExit(
            f"refusing to overwrite {out}: {len(records)} records would replace "
            f"{previous} (below the {REGRESSION_TOLERANCE:.0%} floor). Re-run, or "
            f"pass --force if the drop is intended."
        )


def _write_both(out: Path, records: list, skipped_out: Path, skipped: list) -> None:
    """Publish the corpus and its skip log together, or publish neither.

    Both payloads are encoded before either file is touched, so an encoding
    failure on the skip log cannot leave a swapped-in corpus paired with a stale
    log describing a different run.
    """
    corpus_text = json.dumps(records, indent=2) + "\n"
    skipped_text = json.dumps(skipped, indent=2) + "\n"
    _atomic_write_text(out, corpus_text)
    _atomic_write_text(skipped_out, skipped_text)


def _atomic_write_json(path: Path, payload: object) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename into place.

    `os.replace` is atomic within a filesystem, so a reader either sees the old
    file or the new one and never a partial write. A crash, a failed check, or a
    killed process therefore leaves the committed fixture exactly as it was —
    which is what happened when this run was interrupted the first time, and is
    the whole reason the corpus is written once at the end rather than
    incrementally.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def main(
    limit: int = 222,
    gh: Gh = gh_text,
    out: Path = OUT,
    skipped_out: Path = SKIPPED_OUT,
    force: bool = False,
    cache_path: Path | None = None,
    progress_every: int = 25,
) -> dict:
    records: list[dict] = []
    skipped: list[dict] = []
    cache = PrCache(cache_path) if cache_path else None
    issues = list_closed_pager_issues(limit, gh=gh)
    for index, issue in enumerate(issues, start=1):
        # A FatalGhError raised anywhere below propagates and aborts the run
        # before anything is written. That is the point: a degraded run must not
        # be able to replace a good corpus with a report of success.
        record, skip = build_record(issue, gh=gh, cache=cache)
        if record is not None:
            records.append(record)
        else:
            skipped.append(skip)
        if progress_every and index % progress_every == 0:
            print(f"  ...{index}/{len(issues)} issues, {len(records)} records",
                  file=sys.stderr)

    sanity_check(records, out, force=force)
    # Serialize both payloads before touching either file, so a failure to
    # encode the second cannot leave the first already swapped in.
    _write_both(out, records, skipped_out, skipped)

    stats = {
        "issues_seen": len(issues),
        "records": len(records),
        "skipped": len(skipped),
        "prs": sum(len(r["fix_prs"]) for r in records),
        "paginated_prs": sum(len(r.get("paginated_prs", [])) for r in records),
        "partial_records": sum(1 for r in records if r["unreadable_prs"]),
    }
    print(f"wrote {stats['records']} records to {out}", file=sys.stderr)
    print(f"skipped {stats['skipped']} issues (see {skipped_out})", file=sys.stderr)
    print(
        f"{stats['prs']} PRs, {stats['paginated_prs']} needed the pagination "
        f"fallback, {stats['partial_records']} records lost at least one PR",
        file=sys.stderr,
    )
    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("limit", nargs="?", type=int, default=222,
                        help="how many closed pager-duty issues to read")
    parser.add_argument("--force", action="store_true",
                        help="overwrite even if the record count regresses")
    parser.add_argument("--cache", type=Path, default=None,
                        help="resumable scratch cache of per-PR lookups; an "
                             "interrupted run re-run with the same path picks up "
                             "where it stopped. Never point this at data/.")
    options = parser.parse_args()
    main(options.limit, force=options.force, cache_path=options.cache)
