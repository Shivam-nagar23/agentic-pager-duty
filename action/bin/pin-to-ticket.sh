#!/usr/bin/env bash
#
# Rewind the workspace to the state a developer saw when they picked a closed
# ticket up.
#
#   PAGER_WORKSPACE=/tmp/pager-ws pin-to-ticket.sh 2960
#   PAGER_WORKSPACE=/tmp/pager-ws pin-to-ticket.sh --reset
#
# Why this exists
# ---------------
# Replaying a closed ticket against HEAD does not test the pipeline, it tests
# whether the pipeline notices the bug is already fixed. Observed on ticket 2960:
# localization found all eight ground-truth files, and then the RCA stage
# correctly returned `can_explain: false` with "the defect is not present in
# these checkouts; both repos at HEAD already contain the complete fix". That is
# the right answer and it is useless for exercising the fix, review and publish
# stages.
#
# So: for every fix PR the corpus records for a ticket, ask GitHub what commit
# that PR was opened against (`baseRefOid`) and check that repo out there. The
# agent then reads the code as it was before the fix.
#
# This is a testing tool. It is never used on a live pager ticket, where HEAD is
# exactly what you want.

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
require gh git jq python3

REPO_ROOT="$(cd -- "$ACTION_DIR/.." && pwd)"
CORPUS="${PAGER_CORPUS:-$REPO_ROOT/data/corpus.json}"

if [ "${1:-}" = "--reset" ]; then
  for r in "${REPOS[@]}"; do
    [ -d "$WORKSPACE/$r/.git" ] || continue
    log "reset $r to origin/$BASE_BRANCH"
    git -C "$WORKSPACE/$r" fetch --depth 1 --filter=blob:none origin "$BASE_BRANCH" >/dev/null
    git -C "$WORKSPACE/$r" reset --hard FETCH_HEAD >/dev/null
  done
  exit 0
fi

TICKET="${1:-}"
[ -n "$TICKET" ] || fail "usage: pin-to-ticket.sh <issue-number> | --reset"
[ -f "$CORPUS" ] || fail "no corpus at $CORPUS"

if [ "$WORKSPACE" = "$REPO_ROOT/workspace" ]; then
  fail "refusing to rewind $WORKSPACE — that is the shared read-only clone set.
Point PAGER_WORKSPACE at a copy you can throw away."
fi

# repo -> one fix PR number (the first; all PRs on one ticket in one repo share a
# base closely enough for this purpose).
mapfile -t PAIRS < <(
  PAGER_CORPUS="$CORPUS" PAGER_TICKET="$TICKET" python3 - <<'PY'
import json, os, re
corpus = json.load(open(os.environ["PAGER_CORPUS"], encoding="utf-8"))
want = str(os.environ["PAGER_TICKET"])
rec = next((r for r in corpus if str(r.get("number")) == want), None)
if rec is None:
    raise SystemExit(f"ticket {want} is not in the corpus")
seen = {}
for url, repo in (rec.get("pr_repos") or {}).items():
    m = re.search(r"/pull/(\d+)", url)
    if m and repo not in seen:
        seen[repo] = m.group(1)
for repo, num in seen.items():
    print(f"{repo} {num}")
PY
)
[ "${#PAIRS[@]}" -gt 0 ] || fail "ticket $TICKET has no fix PRs recorded in the corpus"

for pair in "${PAIRS[@]}"; do
  repo="${pair%% *}"; pr="${pair##* }"
  dir="$WORKSPACE/$repo"
  if [ ! -d "$dir/.git" ]; then
    warn "$repo is not checked out (fix PR #$pr) — skipping"
    continue
  fi
  base="$(gh pr view "$pr" --repo "$GH_OWNER/$repo" --json baseRefOid -q .baseRefOid)" \
    || { warn "$repo: could not read PR #$pr"; continue; }
  log "$repo -> $base (base of PR #$pr)"
  git -C "$dir" fetch --depth 1 --filter=blob:none origin "$base" >/dev/null \
    || { warn "$repo: could not fetch $base (too old for a shallow fetch?)"; continue; }
  git -C "$dir" checkout --detach FETCH_HEAD >/dev/null 2>&1 \
    || warn "$repo: checkout failed"
done

log "workspace pinned to the pre-fix state for ticket $TICKET; run 'pin-to-ticket.sh --reset' to undo"
