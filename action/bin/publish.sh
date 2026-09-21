#!/usr/bin/env bash
#
# The only script in this pipeline that writes to GitHub.
#
#   publish.sh --ship <repo> [<repo>...]     branch, push, draft PR per repo, comment
#   publish.sh --stop <stage> <reason>       comment the analysis, open nothing
#   publish.sh --from-intent                 replay whatever run.sh deferred
#
# It is a separate script, run in a separate workflow step, so that it is the
# only place the write-scoped token is in the environment. The agent stages
# cannot reach it: they run earlier, in a step whose env does not carry
# PAGER_GITHUB_TOKEN, with `gh` denied by their settings file. That is what
# makes "the agent cannot open its own PR" a property of the harness rather than
# an instruction in a prompt.
#
# PAGER_DRY_RUN=1 renders every body to $RUN_DIR and writes nothing to GitHub.

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
require gh git python3 jq

MODE="${1:-}"; shift || true
[ -n "$MODE" ] || fail "usage: publish.sh --ship <repo>... | --stop <stage> <reason> | --from-intent"

# Replay the intent run.sh recorded. Used by the workflow's publish step, which
# is the only step that holds the write-scoped token.
if [ "$MODE" = "--from-intent" ]; then
  intent="${PAGER_RUN_DIR:-$RUN_DIR}/publish-intent.json"
  if [ ! -f "$intent" ]; then
    # No intent means the pipeline died before deciding anything -- a harness
    # failure, not a halt. Say so on the issue rather than going silent.
    log "no publish intent at $intent; the pipeline failed before reaching a decision"
    exec "${BASH_SOURCE[0]}" --stop "harness" \
      "The fix engine failed before it reached a decision. Nothing was changed. See the workflow run log and the uploaded \`pager-run\` artifact."
  fi
  mapfile -t INTENT < <(jq -r '.mode, .args[]' "$intent")
  log "replaying deferred publish: ${INTENT[*]}"
  exec "${BASH_SOURCE[0]}" "--${INTENT[0]}" "${INTENT[@]:1}"
fi

ISSUE="$(cat "$RUN_DIR/issue-number.txt" 2>/dev/null || true)"
[ -n "$ISSUE" ] || fail "no issue number in $RUN_DIR/issue-number.txt"
ISSUE_REPO="${PAGER_ISSUE_REPO:-$GH_OWNER/sprint-tasks}"
DRY="${PAGER_DRY_RUN:-0}"

export PAGER_RUN_DIR="$RUN_DIR"
export PAGER_ISSUE_URL="https://github.com/$ISSUE_REPO/issues/$ISSUE"

if [ -n "${PAGER_GITHUB_TOKEN:-}" ]; then
  export GH_TOKEN="$PAGER_GITHUB_TOKEN"
  git config --global "url.https://x-access-token:${PAGER_GITHUB_TOKEN}@github.com/.insteadOf" \
    "https://github.com/"
fi

comment_issue() {
  local file="$1"
  if [ "$DRY" = "1" ]; then
    log "DRY RUN — would comment on $ISSUE_REPO#$ISSUE; body at $file"
    return
  fi
  gh issue comment "$ISSUE" --repo "$ISSUE_REPO" --body-file "$file" \
    || fail "failed to comment on $ISSUE_REPO#$ISSUE"
}

# --------------------------------------------------------------------- --stop

if [ "$MODE" = "--stop" ]; then
  stage="${1:-unknown}"; shift || true
  python3 "$ACTION_DIR/bin/report.py" stop "$stage" "$*" > "$RUN_DIR/stop-comment.md"
  comment_issue "$RUN_DIR/stop-comment.md"
  log "stop comment published (stage=$stage)"
  exit 0
fi

[ "$MODE" = "--ship" ] || fail "unknown mode: $MODE"
[ "$#" -gt 0 ] || fail "--ship needs at least one repo"

# --------------------------------------------------------------------- --ship

BRANCH="${PAGER_BRANCH_PREFIX:-agent/pager}-$ISSUE"
TITLE_BASE="$(jq -r '.title' "$RUN_DIR/issue.json")"
PR_URLS=()

for repo in "$@"; do
  dir="$WORKSPACE/$repo"
  [ -d "$dir/.git" ] || fail "$repo: no checkout at $dir"
  [ -n "$(git -C "$dir" status --porcelain)" ] || fail "$repo: nothing to commit"

  python3 "$ACTION_DIR/bin/report.py" pr "$repo" > "$RUN_DIR/pr-body-$repo.md"

  if [ "$DRY" = "1" ]; then
    log "DRY RUN — $repo: would push $BRANCH and open a draft PR against $BASE_BRANCH"
    log "DRY RUN — $repo: body at $RUN_DIR/pr-body-$repo.md, diff at $RUN_DIR/diffs/$repo.diff"
    PR_URLS+=("(dry run) $GH_OWNER/$repo — $BRANCH")
    continue
  fi

  git -C "$dir" config user.name  "${PAGER_GIT_NAME:-devtron-pager-agent}"
  git -C "$dir" config user.email "${PAGER_GIT_EMAIL:-pager-agent@devtron.ai}"
  git -C "$dir" checkout -b "$BRANCH" >/dev/null 2>&1 || git -C "$dir" checkout "$BRANCH"
  git -C "$dir" add -A
  git -C "$dir" commit -q -m "fix: $TITLE_BASE

Automated fix for pager issue $ISSUE_REPO#$ISSUE.
Root cause and verification plan are in the pull request body.

Refs: $PAGER_ISSUE_URL"

  # The checkout is shallow and blobless; --force-with-lease is meaningless on a
  # branch that does not exist yet, and a plain push is what we want.
  git -C "$dir" push -u origin "$BRANCH" \
    || fail "$repo: push failed — does the token have Contents:write on $GH_OWNER/$repo?"

  # `gh label create` is idempotent enough with `|| true`: the only expected
  # failure is "already exists", and a real permission failure surfaces on the
  # `gh pr create` below anyway.
  gh label create agent-authored --repo "$GH_OWNER/$repo" \
     --color B60205 --description "Opened by the pager-duty fix agent. Draft; never merge without review." \
     >/dev/null 2>&1 || true

  # --draft is the mechanical block on merging, and it is the one that actually
  # holds: a fine-grained PAT with pull_requests:write can merge, so the token
  # scope alone does not enforce the no-merge invariant. See the README.
  url="$(gh pr create \
      --repo "$GH_OWNER/$repo" \
      --base "$BASE_BRANCH" \
      --head "$BRANCH" \
      --draft \
      --label agent-authored \
      --title "fix: $TITLE_BASE (pager #$ISSUE)" \
      --body-file "$RUN_DIR/pr-body-$repo.md")" \
    || fail "$repo: gh pr create failed"
  log "$repo: $url"
  PR_URLS+=("$url")
done

# Cross-link the PRs to each other. A shared-code fix that lands in devtron but
# not devtron-enterprise leaves enterprise customers broken, so the pairing has
# to be visible from inside either PR.
if [ "${#PR_URLS[@]}" -gt 1 ] && [ "$DRY" != "1" ]; then
  for url in "${PR_URLS[@]}"; do
    others=()
    for other in "${PR_URLS[@]}"; do [ "$other" != "$url" ] && others+=("$other"); done
    gh pr comment "$url" --body "Paired with $(printf '%s ' "${others[@]}"). \
These repositories are a hard fork at the same module path, so this fix needs both diffs — \
**merging one without the other leaves the other half broken.**" >/dev/null || warn "cross-link failed for $url"
  done
fi

python3 "$ACTION_DIR/bin/report.py" comment "${PR_URLS[@]}" > "$RUN_DIR/issue-comment.md"
comment_issue "$RUN_DIR/issue-comment.md"
log "published ${#PR_URLS[@]} draft PR(s)"
