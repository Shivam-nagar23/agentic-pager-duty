import warnings

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


# --- Repo-prefix normalization and independent repo_hit -----------------------
# The replay runner's prompt asks the agent for repo-prefixed paths
# ("devtron/pkg/auth/rbac.go") while truth from `gh pr view --json files` is
# repo-relative ("pkg/auth/rbac.go"). A literal set intersection scores a perfect
# prediction as 0.0. These tests pin the normalization and make repo_hit carry
# information independent of file_recall.


def test_repo_prefixed_prediction_matches_relative_truth():
    """A perfect prediction in the shape replay.py actually asks for scores 1.0."""
    s = score_prediction(
        ["devtron/pkg/auth/rbac.go", "devtron/pkg/auth/enforcer.go"], TRUTH
    )
    assert s["file_recall"] == 1.0
    assert s["file_precision"] == 1.0
    assert s["repo_hit"] is True


def test_near_miss_in_right_repo_is_repo_hit():
    """Right repo, wrong file: the forgiving signal repo_hit is supposed to be."""
    s = score_prediction(["devtron/pkg/auth/policy.go"], TRUTH)
    assert s["repo_hit"] is True
    assert s["file_recall"] == 0.0
    assert s["file_precision"] == 0.0


def test_right_path_wrong_repo_is_not_a_file_match():
    """Guards against a blind strip-the-first-segment fix inventing a hit."""
    s = score_prediction(["dashboard/pkg/auth/rbac.go"], TRUTH)
    assert s["file_recall"] == 0.0
    assert s["file_precision"] == 0.0
    assert s["repo_hit"] is False


def test_multi_repo_truth_scores_both_repos():
    """Ticket 2960's shape: one fix spanning devtron and devtron-enterprise."""
    truth = {
        "https://github.com/devtron-labs/devtron/pull/7034": ["pkg/auth/rbac.go"],
        "https://github.com/devtron-labs/devtron-enterprise/pull/3402": [
            "pkg/auth/rbac.go"
        ],
    }
    both = score_prediction(
        ["devtron/pkg/auth/rbac.go", "devtron-enterprise/pkg/auth/rbac.go"], truth
    )
    assert both["file_recall"] == 1.0
    assert both["n_true"] == 2

    one = score_prediction(["devtron/pkg/auth/rbac.go"], truth)
    assert one["file_recall"] == 0.5
    assert one["file_precision"] == 1.0
    assert one["repo_hit"] is True


def test_unknown_prefix_is_not_stripped():
    """Only known repo names split off; real paths start with pkg/, util/, internal/."""
    s = score_prediction(["notarepo/pkg/auth/rbac.go"], TRUTH)
    assert s["file_recall"] == 0.0
    assert s["repo_hit"] is False


# --- Shapes taken from the real corpus ---------------------------------------


def test_services_repo_service_subdir_is_preserved():
    """devtron-services truth paths carry a service subdir (kubewatch/, git-sensor/).

    Stripping only the repo segment must leave that subdir intact, and it is what
    distinguishes the two services repos, which share 27 identical relative paths.
    """
    truth = {
        "https://github.com/devtron-labs/devtron-services/pull/1": [
            "kubewatch/pkg/informer/informer.go",
            "git-sensor/go.mod",
        ],
    }
    s = score_prediction(["devtron-services/kubewatch/pkg/informer/informer.go"], truth)
    assert s["file_recall"] == 0.5
    assert s["file_precision"] == 1.0
    assert s["repo_hit"] is True

    wrong_sibling = score_prediction(
        ["devtron-services-enterprise/git-sensor/go.mod"], truth
    )
    assert wrong_sibling["file_recall"] == 0.0
    assert wrong_sibling["repo_hit"] is False


def test_truth_repo_outside_the_five_warns_and_still_splits():
    """athena-be and notifier appear in the real corpus but are not target repos.

    A repo we do not know about must not silently fall back to the prefix bug, and
    must announce itself so the constant can be updated deliberately.
    """
    truth = {"https://github.com/devtron-labs/athena-be/pull/9": ["components/x.tsx"]}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s = score_prediction(["athena-be/components/x.tsx"], truth)
    assert s["file_recall"] == 1.0
    assert s["repo_hit"] is True
    assert any("athena-be" in str(w.message) for w in caught)


def test_known_repos_do_not_warn():
    """The warning must be silent on the normal five-repo case."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        score_prediction(["devtron/pkg/auth/rbac.go"], TRUTH)
    assert caught == []
