# shellcheck shell=bash
# Shared helpers for the pager-duty fix engine.
#
# Sourced by every script in this directory. Defines the layout, the log
# helpers, and the two things that are easy to get wrong in a pipeline that is
# allowed to stop: `fail` (a harness bug — loud, non-zero) and `halt` (the
# pipeline deciding not to ship — expected, writes a stop reason and exits 0 so
# the workflow can still comment on the issue).

set -Eeuo pipefail

# `mapfile` is used throughout and is bash 4+. GitHub runners have bash 5;
# macOS still ships 3.2 at /bin/bash, so fail with a fixable message rather than
# "mapfile: command not found" halfway through a paid run.
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
  printf '[pager][fail] needs bash 4+ (found %s at %s). On macOS: brew install bash\n' \
    "${BASH_VERSION:-?}" "${BASH:-?}" >&2
  exit 1
fi

ACTION_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export ACTION_DIR

# Where stage inputs and outputs live. One directory per run; everything the
# owner needs to debug a run is in here, and the workflow uploads it as an
# artifact whether the run shipped a PR or not.
RUN_DIR="${PAGER_RUN_DIR:-${RUNNER_TEMP:-/tmp}/pager-run}"
export RUN_DIR

# Where the eight repos get checked out.
WORKSPACE="${PAGER_WORKSPACE:-$RUN_DIR/workspace}"
export WORKSPACE

# repo-map.md and pager-index.md. Two locations are searched so the same script
# works in this repo (where they live at ../context) and in sprint-tasks (where
# the installer copies them to action/context).
resolve_context_dir() {
  local candidates=(
    "${PAGER_CONTEXT_DIR:-}"
    "$ACTION_DIR/context"
    "$ACTION_DIR/../context"
  )
  local d
  for d in "${candidates[@]}"; do
    [ -n "$d" ] || continue
    if [ -f "$d/repo-map.md" ] && [ -f "$d/pager-index.md" ]; then
      (cd -- "$d" && pwd)
      return 0
    fi
  done
  fail "could not find repo-map.md + pager-index.md. Set PAGER_CONTEXT_DIR, or copy them to $ACTION_DIR/context/"
}

# The eight target repos. Order matters only for readable logs.
REPOS=(
  devtron
  devtron-enterprise
  dashboard
  devtron-services
  devtron-services-enterprise
  athena-be
  notifier
  devtron-fe-common-lib
)
export REPOS

GH_OWNER="${PAGER_GH_OWNER:-devtron-labs}"
export GH_OWNER

# CLAUDE.md: "Pager fixes target `main` in every repo, including `dashboard`
# (whose default branch is `develop`)."
BASE_BRANCH="${PAGER_BASE_BRANCH:-main}"
export BASE_BRANCH

log()  { printf '[pager] %s\n' "$*" >&2; }
warn() { printf '[pager][warn] %s\n' "$*" >&2; }

# A harness bug. The run should go red so it gets fixed, not degrade quietly
# into a bad analysis.
fail() { printf '[pager][fail] %s\n' "$*" >&2; exit 1; }

# The pipeline deciding not to ship. Expected, and the whole point of "no
# speculative PRs": write why, exit 0, let the caller comment it on the issue.
halt() {
  local stage="$1"; shift
  mkdir -p "$RUN_DIR"
  {
    printf '%s\n' "$stage"
    printf '%s\n' "$*"
  } > "$RUN_DIR/stop.txt"
  log "HALT at $stage: $*"
  exit 0
}

halted() { [ -f "$RUN_DIR/stop.txt" ]; }

require() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || fail "required command not found: $c"
  done
}

jqr() { jq -r "$@"; }

# Extract one pager-index.md section for an affected area, case-insensitively.
#
# The index is organised as `## <Area>` sections. Ticket areas do not match the
# template vocabulary exactly (repo-map §3.1: casing differs, `PANIC IN CODE`
# is not in the template at all, 20% of tickets carry no area), so match loosely
# and return empty rather than erroring when there is no section.
index_slice() {
  local area="$1" index="$2"
  [ -n "$area" ] || return 0
  awk -v want="$(printf '%s' "$area" | tr '[:upper:]' '[:lower:]')" '
    /^## / {
      hdr = tolower(substr($0, 4))
      gsub(/^[ \t]+|[ \t]+$/, "", hdr)
      inside = (hdr == want)
    }
    inside { print }
  ' "$index"
}

# The build/test paragraph injected into the fix prompt, straight from
# repo-map §4. Lives here rather than in run.sh so a local dry run feeds the
# agent exactly the text CI feeds it.
