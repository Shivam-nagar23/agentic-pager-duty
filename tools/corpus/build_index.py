"""Turn the corpus into `affected_area -> historically touched files`.

Regenerate the context file with:

    cd tools/corpus && python build_index.py


The index is a *prior* for localization: given a new ticket's affected area, which
files have fixes historically landed in. Three things make the raw corpus unusable
as that prior, and this module exists to correct all three.

1. **Fork mirroring.** `devtron` and `devtron-enterprise` are a hard fork of each
   other, so most fixes land as two near-identical PRs on one ticket. Counting bare
   paths made a single incident look like two, and the count column — which a reader
   reads as evidence strength — was measuring fork topology instead. Counts are
   therefore keyed on `(repo, path)` and count **distinct tickets**, not PR-file
   appearances. A mirrored fix counts once per repo; a ticket that needed six PRs
   cannot outvote six separate tickets.

2. **Build plumbing.** Vendored dependencies, lockfiles, generated env docs and
   changelogs were 56% of all file hits in the corpus. They are never where a bug
   lives, and they crowded genuine single-hit signal out of the rendered top-N.
   `is_noise()` removes them before anything is counted.

3. **Release-merge PRs.** A handful of "fix PRs" are release merges or whole-subsystem
   rewrites touching dozens of files. They are dropped whole rather than
   down-weighted: a down-weighted release merge still sits in the ranking crowding
   out real single-hit signal. See `PR_FILE_CAP` for the threshold and its evidence.

Ties are broken deterministically, not by corpus order, so output is stable across runs.

**Measuring with this index:** the corpus and the replay set are the same closed pager
tickets. Indexing ticket N and then scoring localization on ticket N measures
memorization, not localization, and will report a lift that does not exist in
production. `build_index` is pure and holds no state between calls, so the eval
harness must build leave-one-out:

    build_index([r for r in corpus if r["number"] != ticket_under_test])
"""
import re
from collections import Counter, defaultdict

# Fallback only. Records produced by the current fetcher carry `pr_repos`.
REPO_FROM_URL = re.compile(r"github\.com/devtron-labs/([\w.-]+)/pull/")

#: The eight repos a pager fix can land in. Anything else is noted, not ranked.
TARGET_REPOS = frozenset(
    {
        "devtron",
        "devtron-enterprise",
        "dashboard",
        "devtron-services",
        "devtron-services-enterprise",
        "athena-be",
        "notifier",
        "devtron-fe-common-lib",
    }
)

#: Build plumbing: vendored deps, lockfiles, generated env docs, changelogs, CI config.
#: A bug is never fixed here; these paths ride along on real fixes and, unfiltered,
#: are 56% of all file hits in the 135-record corpus.
#:
#: The JS lockfiles and `.github/` are extensions of the reviewed list, both by the
#: same argument that put `go.mod`/`go.sum`/`CHANGELOG/` on it: two of the eight target
#: repos are JavaScript, so a Go-only lockfile rule leaves half the fleet unfiltered,
#: and `.github/` is repo CI/meta config in exactly the sense `CHANGELOG/` is repo
#: release metadata. Nothing here has ever been the site of a pager fix in this corpus.
NOISE = re.compile(
    r"""
      (?:^|/) vendor/                 # vendored dependency trees
    | (?:^|/) go\.mod$
    | (?:^|/) go\.sum$
    | (?:^|/) modules\.txt$           # vendor/modules.txt
    | (?:^|/) env_gen\.[^/]*$         # generated env documentation
    | ^ CHANGELOG/                    # release notes
    | ^ \.github/                     # workflows, CODEOWNERS, issue templates
    | (?:^|/) package\.json$          # JS dependency manifests and lockfiles,
    | (?:^|/) package-lock\.json$     #   the analogue of go.mod / go.sum for
    | (?:^|/) yarn\.lock$             #   `dashboard` and `devtron-fe-common-lib`
    | (?:^|/) pnpm-lock\.yaml$
    """,
    re.VERBOSE,
)

#: Max distinct non-noise files a PR may touch and still count as a targeted fix.
#:
#: Derived from the 135-record / 288-PR corpus, measured *after* noise filtering:
#: median 2 files, p90 = 6, p95 = 10, p97 = 12. 98.3% of PRs sit at or below 12.
#: The 8 PRs above it were inspected by hand and every one is a release merge
#: (touching `manifests/version.txt` + `releasenotes.md` + `charts/devtron/Chart.yaml`)
#: or a whole-subsystem rewrite — no targeted fix is lost. They carry 30% of the
#: post-noise file hits, which is the crowding-out this cap removes.
PR_FILE_CAP = 12


def is_noise(path: str) -> bool:
    """True for build plumbing that is never the site of a bug fix."""
    return bool(NOISE.search(path))


def _repo_for(record: dict, url: str) -> str:
    repo = (record.get("pr_repos") or {}).get(url)
    if repo:
        return repo
    match = REPO_FROM_URL.search(url)
    return match.group(1) if match else "unknown"


def _canonical_areas(corpus: list[dict]) -> dict[str, str]:
    """Map each area spelling to one canonical form, case-insensitively.

    The issue template has been retyped over the years: "CI (Non blocking)" and
    "CI (Non Blocking)" are the same area and must not be two buckets. The most
    frequently used spelling wins.
    """
    spellings: dict[str, Counter] = defaultdict(Counter)
    for record in corpus:
        for area in record.get("affected_areas", []):
            spellings[area.casefold()][area] += 1
    return {
        area: counter.most_common(1)[0][0]
        for key, counter in spellings.items()
        for area in counter
    }


def build_index(corpus: list[dict], cap: int = PR_FILE_CAP) -> dict:
    """Aggregate the corpus into `area -> {repos, files, ...}`.

    `files` entries carry `repo`, `path` and `count`, where `count` is the number of
    *distinct tickets* in that area whose fix touched that path in that repo. Entries
    are ordered by count descending, then repo, then path — deterministic regardless
    of corpus order.
    """
    canonical = _canonical_areas(corpus)
    areas: dict[str, dict] = {}

    for record in corpus:
        ticket = record.get("number")
        record_areas = [canonical[a] for a in record.get("affected_areas", [])]
        if not record_areas:
            continue

        for area in record_areas:
            bucket = areas.setdefault(
                area,
                {
                    "tickets": set(),
                    "repos": set(),
                    "hits": defaultdict(set),  # (repo, path) -> {ticket, ...}
                    "dropped_prs": [],
                    "off_target": defaultdict(set),  # repo -> {ticket, ...}
                    "unreadable_prs": [],
                },
            )
            bucket["tickets"].add(ticket)
            for entry in record.get("unreadable_prs") or []:
                # Fetcher writes {url, reason}; tolerate a bare URL string too.
                bucket["unreadable_prs"].append(
                    entry["url"] if isinstance(entry, dict) else entry
                )

            for url, files in (record.get("fix_prs") or {}).items():
                repo = _repo_for(record, url)
                signal = sorted({path for path in files if not is_noise(path)})

                # Cap before repo routing: a release merge is a release merge in any
                # repo. Measured after noise filtering, as the threshold was derived.
                if len(signal) > cap:
                    bucket["dropped_prs"].append(
                        {"url": url, "repo": repo, "files": len(signal), "ticket": ticket}
                    )
                    continue

                if repo not in TARGET_REPOS:
                    if signal:
                        bucket["off_target"][repo].add(ticket)
                    continue

                bucket["repos"].add(repo)
                for path in signal:
                    bucket["hits"][(repo, path)].add(ticket)

    # Secondary ranking key. Most (repo, path) pairs sit at count 1 inside their area,
    # where the area itself supplies no ordering at all. Plain alphabetical order is
    # deterministic but not neutral — it hands the top-N slots to `.github/...`,
    # `README.md` and other uppercase or dotted paths, which is defect 2 coming back
    # through the tie-break. Break those ties by how many pager tickets touched the
    # file *anywhere* in the corpus: among files with equal evidence here, a file
    # that has been a fix site repeatedly is a better prior than one touched once.
    overall: dict[tuple[str, str], set] = defaultdict(set)
    for bucket in areas.values():
        for key, tickets in bucket["hits"].items():
            overall[key] |= tickets

    return {
        area: {
            "repos": sorted(bucket["repos"]),
            "ticket_count": len(bucket["tickets"]),
            "files": [
                {"repo": repo, "path": path, "count": len(tickets)}
                for (repo, path), tickets in sorted(
                    bucket["hits"].items(),
                    key=lambda item: (
                        -len(item[1]),                  # tickets in this area
                        -len(overall[item[0]]),         # tickets corpus-wide
                        item[0][0],                     # repo
                        item[0][1],                     # path
                    ),
                )
            ],
            "dropped_prs": sorted(
                bucket["dropped_prs"], key=lambda d: (-d["files"], d["url"])
            ),
            "off_target": {
                repo: sorted(tickets)
                for repo, tickets in sorted(bucket["off_target"].items())
            },
            "unreadable_prs": sorted(set(bucket["unreadable_prs"])),
        }
        for area, bucket in areas.items()
    }


def _files_by_repo(entries: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group ranked file entries by repo, strongest-evidence repo first."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        grouped[entry["repo"]].append(entry)
    return sorted(
        grouped.items(),
        key=lambda item: (-sum(e["count"] for e in item[1]), item[0]),
    )


def render_markdown(index: dict, top_n: int = 10) -> str:
    """Render the index as the context file a localization agent reads.

    `top_n` is applied per repo, not per area, so a fix that mirrors across the fork
    shows up under both repos instead of one crowding out the other.
    """
    total_tickets = sum(data["ticket_count"] for data in index.values())
    lines = [
        "# Pager bug history: where fixes have landed",
        "",
        "Generated from closed `pager-duty` issues and the PRs that fixed them.",
        "Use as a prior for where to look first, not as an answer.",
        "",
        "## How to read the counts",
        "",
        "`(3 tickets)` means **three separate pager tickets** in this area had a fix",
        "touching that file *in that repo*. It is not a count of PRs or of file",
        "appearances. This matters because `devtron-enterprise` is a hard fork of",
        "`devtron`: most fixes land as two near-identical PRs on one ticket, so a bare",
        "path count would double every mirrored fix and read as twice the evidence.",
        "Files are therefore grouped by repo, and a mirrored fix counts once on each",
        "side. Expect the same relative path under both repos — that is one incident,",
        "and a fix will usually need a PR in each.",
        "",
        "Excluded before counting: vendored dependencies, `go.mod`/`go.sum`,",
        "`vendor/modules.txt`, generated `env_gen.*`, `CHANGELOG/`, `.github/`, and JS",
        "lockfiles — build plumbing that was 56% of raw file hits. Also dropped: fix PRs",
        f"touching more than {PR_FILE_CAP} non-plumbing files, which in this corpus are release",
        "merges and whole-subsystem rewrites rather than targeted fixes. Both exclusions",
        "are noted per area below.",
        "",
        "Where several files in an area are tied at the same ticket count — most of",
        "them are — they are ordered by how many pager tickets touched them anywhere in",
        "the corpus, then by path. Ordering never depends on corpus order.",
        "",
        f"Areas: {len(index)}. Tickets attributed to an area: {total_tickets}.",
        "",
    ]

    for area, data in sorted(index.items()):
        lines.append(f"## {area}")
        lines.append("")
        lines.append(
            f"_{data['ticket_count']} ticket(s)_ · repos: "
            f"{', '.join(data['repos']) if data['repos'] else 'none'}"
        )
        lines.append("")

        if not data["files"]:
            lines.append("_No file-level signal survived filtering._")
            lines.append("")

        for repo, entries in _files_by_repo(data["files"]):
            lines.append(f"### {repo}")
            for entry in entries[:top_n]:
                plural = "ticket" if entry["count"] == 1 else "tickets"
                lines.append(f"- `{entry['path']}` ({entry['count']} {plural})")
            if len(entries) > top_n:
                lines.append(f"- _… {len(entries) - top_n} more file(s) not shown_")
            lines.append("")

        for note in _provenance_notes(data):
            lines.append(note)
        if _provenance_notes(data):
            lines.append("")

    return "\n".join(lines)


def _provenance_notes(data: dict) -> list[str]:
    """Kept, not deleted: what was excluded from this area and why."""
    notes = []
    for repo, tickets in data["off_target"].items():
        refs = ", ".join(f"#{t}" for t in tickets)
        notes.append(
            f"> Not ranked: fixes in `{repo}` ({refs}), which is outside the eight "
            f"target repos. Noted rather than deleted — if pager fixes keep landing "
            f"there, the target list is wrong."
        )
    for dropped in data["dropped_prs"]:
        notes.append(
            f"> Dropped as a bulk change, not a targeted fix: {dropped['url']} "
            f"(#{dropped['ticket']}, {dropped['files']} non-plumbing files)."
        )
    if data["unreadable_prs"]:
        refs = ", ".join(data["unreadable_prs"])
        notes.append(f"> Fix PRs whose file list could not be read: {refs}.")
    return notes


def main(corpus_path, out_path, top_n: int = 10) -> dict:
    import json

    corpus = json.loads(corpus_path.read_text())
    index = build_index(corpus)
    out_path.write_text(render_markdown(index, top_n=top_n) + "\n")
    return index


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Regenerate context/pager-index.md")
    parser.add_argument("--corpus", type=Path, default=here / "../../data/corpus.json")
    parser.add_argument("--out", type=Path,
                        default=here / "../../context/pager-index.md")
    parser.add_argument("--top-n", type=int, default=10,
                        help="files shown per repo per area")
    options = parser.parse_args()

    built = main(options.corpus.resolve(), options.out.resolve(), options.top_n)
    ranked = sum(len(d["files"]) for d in built.values())
    dropped = sum(len(d["dropped_prs"]) for d in built.values())
    print(
        f"wrote {options.out.resolve()}: {len(built)} areas, {ranked} ranked "
        f"(repo, path) entries, {dropped} bulk PRs dropped",
        file=sys.stderr,
    )
