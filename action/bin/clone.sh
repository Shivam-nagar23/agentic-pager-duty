#!/usr/bin/env bash
#
# Check the eight target repos out in two tiers.
#
#   clone.sh search                 # tier 1: all eight, source only, no vendor
#   clone.sh build <repo> [<repo>]  # tier 2: add vendor/ back, for the named repos
#
# Why two tiers
# -------------
# A naive "clone only the repo we need" is circular: localization is the thing
# that tells you which repo you need. But the circle only bites if a searchable
# checkout is expensive, and here it is not. Measured against the clones in
# workspace/ (2,511 MB total):
#
#   vendor/           1,785 MB   71%   needed only to BUILD, never to search
#   docs/ + assets/     296 MB   12%   screenshots and gifs, no code
#   .git                277 MB   11%   history; --depth 1 drops nearly all of it
#   -------------------------------------------------------------------------
#   everything else    ~150 MB    6%   <- this is what localization reads
#
# So tier 1 clones all eight at `--depth 1 --filter=blob:none --sparse` with
# vendor/, docs/, assets/ and node_modules/ excluded from the sparse set. A
# blobless partial clone never downloads a blob outside the sparse set, so the
# 1.8 GB of vendored dependencies is not fetched at all. ~150 MB for all eight.
#
# Tier 2 runs after localization has named 1-2 repos and re-adds vendor/ for
# just those, which git fetches on demand from the promisor remote. Go modules
# vendor their deps and build offline, so `go build` needs the full vendor tree
# of the repo being changed -- but only of that repo. devtron +
# devtron-enterprise (the most common pair, 12 of 13 devtron tickets) costs
# 332 MB at that point; devtron-services costs 637 MB unless you name a single
# module, which `build` does when given `repo:module`.
#
# Net: ~150 MB always, plus ~175-340 MB in the common case, against 2.5 GB for
# eight naive clones. Combine with actions/cache on $WORKSPACE (see the
# workflow) and the steady-state cost is a `git fetch --depth 1` per repo.
#
# Credentials are never written into any repo's config: the token lives in a
# global insteadOf rule for the duration of the script, so $WORKSPACE stays
# safe to cache.

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
require git

# Paths excluded from tier 1. `--no-cone` patterns, gitignore syntax.
SPARSE_EXCLUDE=(
  '!/vendor/'  '!/**/vendor/'
  '!/docs/'    '!/**/node_modules/'
  '!/assets/'
)

setup_credentials() {
  local token="${PAGER_GITHUB_TOKEN:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}"
  [ -n "$token" ] || fail "PAGER_GITHUB_TOKEN is not set; the enterprise repos are private"

  # Not per-repo, so the token stays out of every .git/config and the workspace
  # can be cached and uploaded as an artifact without leaking it.
  #
  # But NOT the caller's ~/.gitconfig either. Writing there persists a live
  # credential in plaintext after the run ends and applies it to every git
  # operation the user performs afterwards -- and it silently collides with an
  # existing `insteadOf` for the same prefix, which is common on developer
  # machines configured for private Go modules. Both were observed: a token left
  # behind in a developer's global config, and an SSH rewrite winning the
  # collision so fetches went to a key that could not authenticate.
  #
  # GIT_CONFIG_GLOBAL redirects "global" to a run-scoped file instead. Same
  # effect for this process tree, nothing left behind. It is exported, so the
  # agent stages inherit it too.
  # NOT inside RUN_DIR: that directory is uploaded as a workflow artifact, so a
  # credential written there is downloadable by anyone with read access to the
  # repo, for the artifact's whole retention period. Observed exactly that -- a
  # live installation token sitting in `pager-run/pager-gitconfig` in an
  # uploaded artifact. Keep it beside the run dir, never in it.
  local cfg="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/pager-gitconfig"
  mkdir -p "$(dirname "$cfg")"
  : > "$cfg"
  chmod 600 "$cfg"
  export GIT_CONFIG_GLOBAL="$cfg"

  git config --global "url.https://x-access-token:${token}@github.com/.insteadOf" \
    "https://github.com/"
  git config --global advice.detachedHead false
}

clone_search() {
  local repo="$1" dest="$WORKSPACE/$1"

  if [ -d "$dest/.git" ]; then
    log "refresh $repo"
    git -C "$dest" fetch --depth 1 --filter=blob:none origin "$BASE_BRANCH" \
      || fail "$repo: fetch failed"
    git -C "$dest" reset --hard FETCH_HEAD >/dev/null
    return
  fi

  log "clone $repo (source only)"
  git clone --depth 1 --filter=blob:none --sparse \
      --branch "$BASE_BRANCH" \
      "https://github.com/$GH_OWNER/$repo.git" "$dest" \
    || fail "$repo: clone failed (does branch '$BASE_BRANCH' exist? is the token scoped to it?)"

  git -C "$dest" sparse-checkout set --no-cone '/*' "${SPARSE_EXCLUDE[@]}"
}

# Re-materialise vendor/ for one repo, or for one module of a multi-module repo.
# `devtron-services` and `devtron-services-enterprise` have no top-level go.mod;
# each subdirectory is its own module with its own vendor/, so passing
# `devtron-services:git-sensor` fetches 1 module's vendor instead of all nine.
clone_build() {
  local spec="$1"
  local repo="${spec%%:*}" module="${spec#*:}"
  local dest="$WORKSPACE/$repo"
  [ -d "$dest/.git" ] || fail "$repo was never checked out; run 'clone.sh search' first"

  if [ "$module" = "$repo" ]; then
    log "materialise $repo (full tree incl. vendor)"
    # `sparse-checkout disable` triggers a lazy fetch of every blob outside the
    # sparse set -- for devtron-enterprise that is ~1.8 GB in a single pack, and
    # a dropped connection there is common enough to be the expected case, not
    # the exceptional one. It MUST be checked: without this, a failed fetch left
    # the repo with no vendor/, the function returned 0, and the fix stage then
    # halted on a build that could never have worked. One retry, then fail loudly.
    git -C "$dest" sparse-checkout disable \
      || { warn "$repo: vendor fetch failed, retrying once"
           git -C "$dest" sparse-checkout disable \
             || fail "$repo: could not materialise vendor/; the build gate cannot pass"; }
  else
    log "materialise $repo/$module (module vendor only)"
    git -C "$dest" sparse-checkout set --no-cone \
      '/*' '!/vendor/' '!/**/vendor/' '!/docs/' '!/assets/' '!/**/node_modules/' \
      "/$module/"
  fi
  git -C "$dest" checkout -- . 2>/dev/null || true

  # `yarn lint` is dashboard's real CI gate and it needs node_modules. Install it
  # here rather than letting the fix agent do it: it is deterministic, it is slow
  # enough to eat a meaningful slice of the stage budget, and `yarn install` is on
  # the fix stage's deny list precisely so an agent cannot decide to re-run it.
  if [ -f "$dest/package.json" ] && [ ! -d "$dest/node_modules" ]; then
    if command -v yarn >/dev/null 2>&1; then
      log "yarn install --immutable in $repo (this is slow; it is dashboard's CI gate)"
      ( cd "$dest" && yarn install --immutable ) \
        || warn "$repo: yarn install failed; 'yarn lint' will not run in the fix stage"
    else
      warn "$repo: yarn not on PATH; 'yarn lint' will not run in the fix stage"
    fi
  fi
}

main() {
  local mode="${1:-search}"; shift || true
  mkdir -p "$WORKSPACE"
  setup_credentials

  case "$mode" in
    search)
      local pids=() r
      for r in "${REPOS[@]}"; do
        clone_search "$r" &
        pids+=("$!")
      done
      local rc=0 p
      for p in "${pids[@]}"; do wait "$p" || rc=1; done
      [ "$rc" -eq 0 ] || fail "one or more checkouts failed; see the log above"
      du -sh "$WORKSPACE" 2>/dev/null | sed 's/^/[pager] workspace size: /' >&2
      ;;
    build)
      [ "$#" -gt 0 ] || fail "usage: clone.sh build <repo[:module]> ..."
      local spec
      for spec in "$@"; do clone_build "$spec"; done
      ;;
    *)
      fail "usage: clone.sh {search|build <repo[:module]> ...}"
      ;;
  esac
}

main "$@"
