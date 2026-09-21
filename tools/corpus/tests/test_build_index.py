from build_index import PR_FILE_CAP, build_index, is_noise, render_markdown

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


# --- fork mirroring: (repo, path) keys, distinct-ticket counts -------------------

MIRRORED = [
    {
        "number": 10,
        "affected_areas": ["RBAC Issues"],
        "pr_repos": {
            "https://github.com/devtron-labs/devtron/pull/100": "devtron",
            "https://github.com/devtron-labs/devtron-enterprise/pull/200":
                "devtron-enterprise",
        },
        "fix_prs": {
            "https://github.com/devtron-labs/devtron/pull/100":
                ["util/rbac/EnforcerUtil.go"],
            "https://github.com/devtron-labs/devtron-enterprise/pull/200":
                ["util/rbac/EnforcerUtil.go"],
        },
    },
]


def test_fork_mirrored_fix_counts_once_per_repo_not_twice():
    """One incident fixed in both fork halves is one ticket of evidence per repo.

    The old index keyed on the bare path, so this rendered as `(2x)` and read as two
    independent incidents corroborating the file. It was one.
    """
    index = build_index(MIRRORED)
    entries = index["RBAC Issues"]["files"]
    assert {(e["repo"], e["path"], e["count"]) for e in entries} == {
        ("devtron", "util/rbac/EnforcerUtil.go", 1),
        ("devtron-enterprise", "util/rbac/EnforcerUtil.go", 1),
    }


def test_many_prs_on_one_ticket_cannot_outvote_separate_tickets():
    """Counts are distinct tickets, so a six-PR ticket is still worth one."""
    noisy_ticket = {
        "number": 20,
        "affected_areas": ["CD"],
        "pr_repos": {
            f"https://github.com/devtron-labs/devtron/pull/{n}": "devtron"
            for n in range(300, 306)
        },
        "fix_prs": {
            f"https://github.com/devtron-labs/devtron/pull/{n}": ["pkg/wide.go"]
            for n in range(300, 306)
        },
    }
    two_tickets = [
        {
            "number": n,
            "affected_areas": ["CD"],
            "pr_repos": {f"https://github.com/devtron-labs/devtron/pull/{n}": "devtron"},
            "fix_prs": {f"https://github.com/devtron-labs/devtron/pull/{n}":
                        ["pkg/narrow.go"]},
        }
        for n in (21, 22)
    ]
    index = build_index([noisy_ticket, *two_tickets])
    counts = {e["path"]: e["count"] for e in index["CD"]["files"]}
    assert counts["pkg/wide.go"] == 1
    assert counts["pkg/narrow.go"] == 2
    assert index["CD"]["files"][0]["path"] == "pkg/narrow.go"


def test_ticket_counted_once_even_if_two_prs_touch_same_file_in_same_repo():
    record = {
        "number": 30,
        "affected_areas": ["CD"],
        "pr_repos": {
            "https://github.com/devtron-labs/devtron/pull/1": "devtron",
            "https://github.com/devtron-labs/devtron/pull/2": "devtron",
        },
        "fix_prs": {
            "https://github.com/devtron-labs/devtron/pull/1": ["pkg/a.go"],
            "https://github.com/devtron-labs/devtron/pull/2": ["pkg/a.go"],
        },
    }
    index = build_index([record])
    assert index["CD"]["files"] == [
        {"repo": "devtron", "path": "pkg/a.go", "count": 1}
    ]


# --- noise filter ---------------------------------------------------------------

NOISE_PATHS = [
    "vendor/github.com/foo/bar.go",
    "kubewatch/vendor/modules.txt",
    "go.mod",
    "go.sum",
    "git-sensor/go.mod",
    "git-sensor/go.sum",
    "vendor/modules.txt",
    "env_gen.json",
    "env_gen.md",
    "git-sensor/env_gen.json",
    "CHANGELOG/release-notes-v2.0.0.md",
    ".github/workflows/pr-issue-validator.yaml",
    ".github/CODEOWNERS",
    "package.json",
    "yarn.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
]


def test_is_noise_matches_build_plumbing():
    for path in NOISE_PATHS:
        assert is_noise(path), path


def test_is_noise_leaves_real_source_alone():
    for path in [
        "util/rbac/EnforcerUtil.go",
        "license-manager/pkg/auth/user/UserService.go",
        "src/components/App.tsx",
        "pkg/vendoring/Service.go",  # 'vendor' as a substring, not a directory
        "internal/gomod/Parser.go",
        "scripts/sql/00104401_indexes.up.sql",
        "src/config/package.json.ts",
        "pkg/github/Client.go",
    ]:
        assert not is_noise(path), path


def test_noise_paths_are_excluded_from_counts():
    record = {
        "number": 40,
        "affected_areas": ["CD"],
        "pr_repos": {"https://github.com/devtron-labs/devtron/pull/1": "devtron"},
        "fix_prs": {
            "https://github.com/devtron-labs/devtron/pull/1":
                [*NOISE_PATHS, "pkg/real.go"]
        },
    }
    index = build_index([record])
    assert [e["path"] for e in index["CD"]["files"]] == ["pkg/real.go"]


# --- per-PR cap on bulk changes -------------------------------------------------

def _big_pr(n_files, noise_files=0, number=50):
    files = [f"pkg/f{i:03d}.go" for i in range(n_files)]
    files += [f"vendor/github.com/x/y{i}.go" for i in range(noise_files)]
    return {
        "number": number,
        "affected_areas": ["CD"],
        "pr_repos": {"https://github.com/devtron-labs/devtron/pull/9": "devtron"},
        "fix_prs": {"https://github.com/devtron-labs/devtron/pull/9": files},
    }


def test_outlier_pr_is_dropped_not_downweighted():
    index = build_index([_big_pr(PR_FILE_CAP + 1)])
    assert index["CD"]["files"] == []
    assert len(index["CD"]["dropped_prs"]) == 1
    assert index["CD"]["dropped_prs"][0]["files"] == PR_FILE_CAP + 1


def test_pr_at_the_cap_is_kept():
    index = build_index([_big_pr(PR_FILE_CAP)])
    assert len(index["CD"]["files"]) == PR_FILE_CAP
    assert index["CD"]["dropped_prs"] == []


def test_cap_is_measured_after_noise_filtering():
    """A small fix that vendors 50 dependencies is still a small fix."""
    index = build_index([_big_pr(2, noise_files=50)])
    assert len(index["CD"]["files"]) == 2
    assert index["CD"]["dropped_prs"] == []


def test_dropping_a_pr_leaves_the_tickets_other_prs_intact():
    record = {
        "number": 60,
        "affected_areas": ["CD"],
        "pr_repos": {
            "https://github.com/devtron-labs/devtron/pull/1": "devtron",
            "https://github.com/devtron-labs/devtron/pull/2": "devtron",
        },
        "fix_prs": {
            "https://github.com/devtron-labs/devtron/pull/1":
                [f"pkg/f{i}.go" for i in range(PR_FILE_CAP + 5)],
            "https://github.com/devtron-labs/devtron/pull/2": ["pkg/targeted.go"],
        },
    }
    index = build_index([record])
    assert [e["path"] for e in index["CD"]["files"]] == ["pkg/targeted.go"]
    assert len(index["CD"]["dropped_prs"]) == 1


# --- deterministic ordering -----------------------------------------------------

TIED = [
    {
        "number": n,
        "affected_areas": ["CD"],
        "pr_repos": {f"https://github.com/devtron-labs/devtron/pull/{n}": "devtron"},
        "fix_prs": {f"https://github.com/devtron-labs/devtron/pull/{n}": [path]},
    }
    for n, path in [(71, "pkg/zulu.go"), (72, "pkg/alpha.go"), (73, "pkg/mike.go")]
]


def test_ties_break_alphabetically_not_by_corpus_order():
    """At count 1 the old `Counter.most_common` handed the top-N slots to whichever
    ticket happened to parse first, silently and unreproducibly."""
    forward = [e["path"] for e in build_index(TIED)["CD"]["files"]]
    reverse = [e["path"] for e in build_index(list(reversed(TIED)))["CD"]["files"]]
    assert forward == reverse == ["pkg/alpha.go", "pkg/mike.go", "pkg/zulu.go"]


def test_ties_break_by_repo_before_path():
    records = [
        {
            "number": 80,
            "affected_areas": ["CD"],
            "pr_repos": {
                "https://github.com/devtron-labs/devtron-enterprise/pull/1":
                    "devtron-enterprise",
                "https://github.com/devtron-labs/devtron/pull/2": "devtron",
            },
            "fix_prs": {
                "https://github.com/devtron-labs/devtron-enterprise/pull/1": ["z.go"],
                "https://github.com/devtron-labs/devtron/pull/2": ["z.go"],
            },
        }
    ]
    entries = build_index(records)["CD"]["files"]
    assert [e["repo"] for e in entries] == ["devtron", "devtron-enterprise"]


def test_equal_counts_break_on_corpus_wide_evidence_before_alphabet():
    """Alphabetical order alone hands the top-N to `.github/` and `README.md`.

    Within an area everything below is tied at one ticket, so the area gives no
    ordering. The file that has been a fix site in other areas too is the better
    prior and must outrank the alphabetically-earlier one-off.
    """
    def rec(number, area, repo, files):
        url = f"https://github.com/devtron-labs/{repo}/pull/{number}"
        return {"number": number, "affected_areas": [area],
                "pr_repos": {url: repo}, "fix_prs": {url: files}}

    records = [
        rec(1, "CD", "devtron", ["aaa/one_off.go", "zzz/recurring.go"]),
        rec(2, "CI (Blocking)", "devtron", ["zzz/recurring.go"]),
        rec(3, "PANIC IN CODE", "devtron", ["zzz/recurring.go"]),
    ]
    entries = build_index(records)["CD"]["files"]
    assert [e["count"] for e in entries] == [1, 1]
    assert [e["path"] for e in entries] == ["zzz/recurring.go", "aaa/one_off.go"]


def test_count_still_wins_over_alphabetical_order():
    index = build_index(TIED + [TIED[0] | {"number": 74}])
    assert index["CD"]["files"][0]["path"] == "pkg/zulu.go"
    assert index["CD"]["files"][0]["count"] == 2


# --- off-target repos are noted, not deleted ------------------------------------

OFF_TARGET = [
    {
        "number": 90,
        "affected_areas": ["CD"],
        "pr_repos": {
            "https://github.com/devtron-labs/protos/pull/1": "protos",
            "https://github.com/devtron-labs/devtron/pull/2": "devtron",
        },
        "fix_prs": {
            "https://github.com/devtron-labs/protos/pull/1": ["gen/service.pb.go"],
            "https://github.com/devtron-labs/devtron/pull/2": ["pkg/real.go"],
        },
    }
]


def test_off_target_repo_files_are_not_ranked():
    index = build_index(OFF_TARGET)
    assert [e["path"] for e in index["CD"]["files"]] == ["pkg/real.go"]
    assert index["CD"]["repos"] == ["devtron"]


def test_off_target_repo_is_recorded_with_its_ticket():
    index = build_index(OFF_TARGET)
    assert index["CD"]["off_target"] == {"protos": [90]}


def test_off_target_repo_appears_as_a_note_in_the_markdown():
    markdown = render_markdown(build_index(OFF_TARGET))
    assert "protos" in markdown
    assert "#90" in markdown
    assert "gen/service.pb.go" not in markdown


# --- area normalisation and provenance ------------------------------------------

def test_case_variant_area_names_merge_into_the_commonest_spelling():
    records = [
        {
            "number": n,
            "affected_areas": [area],
            "pr_repos": {f"https://github.com/devtron-labs/devtron/pull/{n}": "devtron"},
            "fix_prs": {f"https://github.com/devtron-labs/devtron/pull/{n}":
                        ["pkg/ci.go"]},
        }
        for n, area in [
            (101, "CI (Non blocking)"),
            (102, "CI (Non blocking)"),
            (103, "CI (Non Blocking)"),
        ]
    ]
    index = build_index(records)
    assert list(index) == ["CI (Non blocking)"]
    assert index["CI (Non blocking)"]["ticket_count"] == 3
    assert index["CI (Non blocking)"]["files"][0]["count"] == 3


def test_unreadable_prs_are_carried_through_as_provenance():
    records = [
        {
            "number": 110,
            "affected_areas": ["CD"],
            "pr_repos": {},
            "fix_prs": {},
            "unreadable_prs": ["https://github.com/devtron-labs/gone/pull/1"],
        }
    ]
    index = build_index(records)
    assert index["CD"]["unreadable_prs"] == [
        "https://github.com/devtron-labs/gone/pull/1"
    ]
    assert "could not be read" in render_markdown(index)


def test_ticket_count_is_distinct_tickets_per_area():
    index = build_index(CORPUS)
    assert index["RBAC Issues"]["ticket_count"] == 2
    assert index["ci (blocking)"]["ticket_count"] == 1


# --- rendering ------------------------------------------------------------------

def test_markdown_groups_files_under_their_repo():
    markdown = render_markdown(build_index(MIRRORED))
    assert "### devtron\n- `util/rbac/EnforcerUtil.go` (1 ticket)" in markdown
    assert "### devtron-enterprise\n- `util/rbac/EnforcerUtil.go` (1 ticket)" in markdown


def test_markdown_top_n_applies_per_repo():
    markdown = render_markdown(build_index(MIRRORED), top_n=1)
    # one entry per repo survives; neither repo crowds the other out
    assert markdown.count("`util/rbac/EnforcerUtil.go`") == 2


def test_markdown_truncation_is_announced():
    markdown = render_markdown(build_index([_big_pr(PR_FILE_CAP)]), top_n=3)
    assert "more file(s) not shown" in markdown


def test_markdown_explains_that_counts_are_tickets_not_appearances():
    markdown = render_markdown(build_index(MIRRORED))
    assert "three separate pager tickets" in markdown
    assert "hard fork" in markdown
