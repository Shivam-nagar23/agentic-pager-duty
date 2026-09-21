#!/usr/bin/env bash
#
# The pipeline. One self-directed agent session, then publishing.
#
#   run.sh <issue-number>
#
# Env:
#   PAGER_DRY_RUN=1      run everything, publish nothing (no branch, no PR, no comment)
#   PAGER_SKIP_CLONE=1   reuse the source checkouts already on disk
#
# What the harness enforces, now that the agent plans its own work:
#
#   * The session has no GitHub token and `gh` is denied by settings/fix.json,
#     so it cannot open a pull request however it decides to proceed.
#   * Publishing is a separate script in a separate step with a separate
#     write-scoped token the agent never sees.
#   * Every PR is --draft and labelled agent-authored; branch protection on main
#     is the second lock.
#   * A spend cap per run, and a job timeout.
#
# What is NOT enforced here, stated plainly because the previous four-stage
# shape claimed otherwise: "stop rather than ship a fix you cannot defend" is an
# instruction in the prompt, not a jq test between processes. It always was --
# the old `jq -r .can_explain` read a field the agent wrote about itself. What
# stops a bad fix is that it arrives as a draft PR carrying its own causal chain
# and not_verified list, for a human who has to click merge.
#
# There is no build step and no vendor/ materialisation. The two-tier clone
# existed because localization ran first and named the repos worth vendoring;
# one session decides that mid-run, so there is nothing to target, and
# materialising all eight costs ~1.8 GB to gain a check that was never evidence
# of correctness anyway. `go build` proves the change compiles. The draft PR's
# own CI proves the same thing, later, for free, and in the place a reviewer is
# already looking. The agent is told not to compile and to say so in its report.

BIN="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$BIN/lib.sh"
require jq git gh python3 claude

ISSUE="${1:-}"
[ -n "$ISSUE" ] || fail "usage: run.sh <issue-number>"

# repo-map.md + pager-index.md. `resolve_context_dir` searches PAGER_CONTEXT_DIR,
# then $ACTION_DIR/context, then ../context, and fails loudly if neither file is
# there rather than letting the agent run without its context artifacts.
CONTEXT_DIR="$(resolve_context_dir)"

# Publishing is deferred to a later workflow step that holds the write token.
# Recording the intent rather than acting on it keeps this script free of any
# credential that can push.
publish_or_defer() {
  local mode="$1"; shift
  if [ "${PAGER_PUBLISH_DEFERRED:-0}" = "1" ]; then
    jq -n --arg m "$mode" '$ARGS.positional as $a | {mode:$m, args:$a}' --args "$@" \
      > "$RUN_DIR/publish-intent.json"
    log "deferred publish intent: $mode $*"
  else
    "$BIN/publish.sh" "--$mode" "$@"
  fi
}

stop() {
  local reason="$1"
  printf 'agent\n%s\n' "$reason" > "$RUN_DIR/stop.txt"
  log "HALT — $reason"
  publish_or_defer stop "agent" "$reason" || warn "stop reporting failed"
  exit 0
}

# ------------------------------------------------------------------- 0: prepare

"$BIN/prepare.sh" "$ISSUE"
AREA="$(cat "$RUN_DIR/area.txt")"

if [ "${PAGER_SKIP_CLONE:-0}" != "1" ]; then
  "$BIN/clone.sh" search
else
  log "PAGER_SKIP_CLONE=1 — reusing $WORKSPACE"
fi

# The index slice for this area, extracted deterministically rather than left to
# the agent to find. If the area is missing or does not match a section (20% of
# tickets carry no area at all), the slice is empty and the prompt says so.
index_slice "$AREA" "$CONTEXT_DIR/pager-index.md" > "$RUN_DIR/index-slice.md" || true
if [ ! -s "$RUN_DIR/index-slice.md" ]; then
  printf '(No section in the past-PR index matches "%s". The index gives you no prior for this ticket — rely on the ticket text and repo-map §3.)\n' \
    "${AREA:-no affected area reported}" > "$RUN_DIR/index-slice.md"
fi


# --------------------------------------------------------------------- 1: agent

"$BIN/stage.sh" agent \
  --cwd "$WORKSPACE" \
  --var-file "TICKET=$RUN_DIR/ticket.md" \
  --var-file "INDEX_SLICE=$RUN_DIR/index-slice.md" \
  --var "AREA=${AREA:-not reported}" \
  --var "CONTEXT_DIR=$CONTEXT_DIR" \
  >/dev/null

AGENT="$RUN_DIR/agent.json"
OUTCOME="$(jq -r '.outcome' "$AGENT")"
log "agent: outcome=$OUTCOME repos=$(jq -c '[.repos[].repo]' "$AGENT") security_sensitive=$(jq -r '.security_sensitive' "$AGENT")"

if [ "$OUTCOME" != "fixed" ]; then
  stop "$(jq -r '.stop_reason // "no reason recorded"' "$AGENT")"
fi

mapfile -t FIX_REPOS < <(jq -r '.repos[].repo' "$AGENT")
[ "${#FIX_REPOS[@]}" -gt 0 ] || stop "reported 'fixed' but named no repository, so there is nothing to publish"

# The agent claiming a change is not the same as a change existing. publish.sh
# refuses a repo with no diff anyway; catching it here produces a comment that
# explains the run instead of a bare harness failure.
for repo in "${FIX_REPOS[@]}"; do
  [ -d "$WORKSPACE/$repo/.git" ] || stop "named $repo, which is not a repository in this workspace"
  [ -n "$(git -C "$WORKSPACE/$repo" status --porcelain)" ] \
    || stop "reported a change in $repo but the working tree is clean — nothing was actually edited"
done

mkdir -p "$RUN_DIR/diffs"
for repo in "${FIX_REPOS[@]}"; do
  git -C "$WORKSPACE/$repo" diff > "$RUN_DIR/diffs/$repo.diff" 2>/dev/null || true
done

# ------------------------------------------------------------------- 2: publish

publish_or_defer ship "${FIX_REPOS[@]}"
log "done"
