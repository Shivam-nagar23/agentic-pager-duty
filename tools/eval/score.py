"""Score a predicted file list against the files the real fix PR changed.

Two path shapes meet here and they are not the same shape:

- **Predictions** come from `replay.py`, whose prompt asks the agent for
  repo-prefixed paths: `devtron/pkg/auth/rbac.go`.
- **Truth** comes from `gh pr view --json files`, which is repo-relative:
  `pkg/auth/rbac.go`. The repo lives in the PR *URL*, not in the path.

So both sides are normalized to `(repo, relative_path)` pairs before comparison.
A literal set intersection of the raw strings scores a perfect prediction as 0.0.

Two metrics come out, and they are deliberately independent:

- `file_recall` / `file_precision` — did it name the exact files?
- `repo_hit` — did it name the right *repository*, even with the wrong file inside
  it? This is the weaker, more forgiving signal, and it is the one expected to move
  first when the past-PR index is added. Predicting `devtron/pkg/auth/policy.go`
  when the truth is `devtron/pkg/auth/rbac.go` is a near-miss worth distinguishing
  from naming the dashboard repo entirely.
"""
import re
import warnings

REPO_FROM_URL = re.compile(r"github\.com/devtron-labs/([\w.-]+)/pull/")

#: The five repositories a Devtron pager bug can live in — see "Target repositories"
#: in CLAUDE.md. These names are load-bearing: a predicted path is split into
#: (repo, relative_path) only when its leading segment is a repo we recognize, so a
#: typo here silently degrades scoring back toward comparing mismatched path shapes.
#:
#: This set is not the last word. Any repo named by the truth PR URLs is also
#: treated as a splittable prefix for that record (see `_splittable_repos`), which
#: keeps scoring correct for repos outside this list, and one outside this list
#: raises a warning so the omission is noticed rather than absorbed. The real corpus
#: already contains two: `athena-be` and `notifier`.
KNOWN_REPOS = frozenset(
    {
        "devtron",
        "devtron-enterprise",
        "dashboard",
        "devtron-services",
        "devtron-services-enterprise",
    }
)


def true_repos(truth: dict[str, list[str]]) -> set[str]:
    """The repositories the real fix PRs touched, read off the PR URLs."""
    return {
        match.group(1)
        for url in truth
        if (match := REPO_FROM_URL.search(url))
    }


def _splittable_repos(repos_in_truth: set[str]) -> set[str]:
    """Repo names that may be split off a predicted path, for one scoring call.

    The truth URLs are authoritative about which repos are in play, so a repo that
    appears there is splittable even if it is missing from `KNOWN_REPOS`. Warn in
    that case: it means either a new target repo or a typo in the constant, and both
    should be a deliberate edit rather than a quiet loss of scoring accuracy.
    """
    unknown = repos_in_truth - KNOWN_REPOS
    if unknown:
        warnings.warn(
            f"truth names repo(s) outside KNOWN_REPOS: {sorted(unknown)} — "
            "add them to score.KNOWN_REPOS if they are target repos",
            stacklevel=3,
        )
    return KNOWN_REPOS | repos_in_truth


def _split(path: str, repos: set[str]) -> tuple[str | None, str]:
    """Split `repo/relative/path` into (repo, relative_path).

    Only a leading segment that names a repo is split off. Everything else is left
    whole and reported as repo-less: real paths legitimately begin with `pkg/`,
    `util/`, `internal/`, `src/`, so blindly dropping the first segment would make
    `dashboard/pkg/auth/rbac.go` match a devtron truth file and manufacture hits.

    A repo-less path still matches on its relative path alone, which is what lets an
    unprefixed prediction score at all. That is a deliberate leniency with a known
    cost: `devtron-services` and `devtron-services-enterprise` share 27 identical
    relative paths in the current corpus, so an unprefixed prediction of one of those
    cannot be attributed to a repo and will match either. Prefixed predictions — the
    shape replay.py asks for — do not have this ambiguity.
    """
    head, sep, rest = path.partition("/")
    if sep and head in repos:
        return head, rest
    return None, path


def _pairs_match(
    predicted: tuple[str | None, str], true: tuple[str | None, str]
) -> bool:
    """Same relative path, and no *contradiction* between the two repos."""
    (pred_repo, pred_path), (true_repo, true_path) = predicted, true
    if pred_path != true_path:
        return False
    return pred_repo is None or true_repo is None or pred_repo == true_repo


def score_prediction(predicted: list[str], truth: dict[str, list[str]]) -> dict:
    """Score predicted file paths against a `fix_prs` mapping of PR URL -> files.

    Returns `file_recall`, `file_precision`, `repo_hit`, `n_true` and `n_predicted`.
    Both counts are of *unique* paths: a prediction that lists the same file twice is
    neither rewarded nor penalized for the repetition.

    Note for `run_eval.py`: a record whose truth is empty scores 0.0 rather than
    raising. Skip or flag `n_true == 0` there instead of averaging a structural zero
    into the baseline.
    """
    repos_in_truth = true_repos(truth)
    splittable = _splittable_repos(repos_in_truth)

    true_pairs = {
        (match.group(1) if (match := REPO_FROM_URL.search(url)) else None, path)
        for url, files in truth.items()
        for path in files
    }
    predicted_pairs = {_split(path, splittable) for path in predicted}

    matched_true = {
        true for true in true_pairs
        if any(_pairs_match(pred, true) for pred in predicted_pairs)
    }
    matched_predicted = {
        pred for pred in predicted_pairs
        if any(_pairs_match(pred, true) for true in true_pairs)
    }

    recall = len(matched_true) / len(true_pairs) if true_pairs else 0.0
    precision = (
        len(matched_predicted) / len(predicted_pairs) if predicted_pairs else 0.0
    )

    # Repos the prediction names: explicitly by prefix, or implicitly because a file
    # it named is a file that repo's fix PR changed.
    named_repos = {repo for repo, _ in predicted_pairs if repo is not None}
    named_repos |= {repo for repo, _ in matched_true if repo is not None}

    return {
        "file_recall": recall,
        "file_precision": precision,
        "repo_hit": bool(named_repos & repos_in_truth),
        "n_true": len(true_pairs),
        "n_predicted": len(predicted_pairs),
    }
