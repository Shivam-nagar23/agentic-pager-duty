#!/usr/bin/env bash
#
# Run the fix engine locally against a ticket. No GitHub writes.
#
#   dry-run.sh --ticket 2960                  # ticket text from data/corpus.json
#   dry-run.sh --ticket 2966 --live           # ticket text fetched with gh
#   dry-run.sh --ticket 2960 --score          # print predicted paths for tools/eval
#
# PAGER_DRY_RUN=1 is forced, so the stop and ship paths render their markdown to
# $PAGER_RUN_DIR instead of sending it. Artifacts land in action/.runs/<ticket>/:
# `agent.prompt.md` (the exact text the agent received -- read this first when a
# prompt edit misbehaves), `agent.json` (its report), and
# `agent.envelope.json` (cost, turns, duration, permission denials).
#
# The agent writes to the checkout, so this refuses to run against the repo's
# shared workspace/. Point PAGER_WORKSPACE at an expendable copy:
#
#   export PAGER_WORKSPACE=/tmp/pager-ws
#   action/bin/clone.sh search
#   action/bin/dry-run.sh --ticket 2966 --live
#
# Replaying a *closed* ticket needs a rewind -- the clones are at HEAD, which is
# after the fix merged, so the agent correctly reports the defect is not present:
#
#   PAGER_WORKSPACE=/tmp/pager-ws action/bin/pin-to-ticket.sh 2960
#   PAGER_WORKSPACE=/tmp/pager-ws action/bin/dry-run.sh --ticket 2960
#   PAGER_WORKSPACE=/tmp/pager-ws action/bin/pin-to-ticket.sh --reset
#
set -Eeuo pipefail
BIN="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$BIN/../.." && pwd)"

TICKET=""
SOURCE="corpus"
SCORE=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ticket) TICKET="$2"; shift 2 ;;
    --corpus) SOURCE="corpus"; shift ;;
    --live)   SOURCE="live";   shift ;;
    --score)  SCORE=1; shift ;;
    -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
[ -n "$TICKET" ] || { echo "usage: dry-run.sh --ticket <n> [--live] [--score]" >&2; exit 1; }

export PAGER_DRY_RUN=1
export PAGER_RUN_DIR="${PAGER_RUN_DIR:-$(cd -- "$BIN/.." && pwd)/.runs/$TICKET}"
export PAGER_WORKSPACE="${PAGER_WORKSPACE:-$REPO_ROOT/workspace}"
export PAGER_CONTEXT_DIR="${PAGER_CONTEXT_DIR:-$REPO_ROOT/context}"
export PAGER_SKIP_CLONE=1

source "$BIN/lib.sh"
mkdir -p "$RUN_DIR"
echo "$TICKET" > "$RUN_DIR/issue-number.txt"
CONTEXT_DIR="$(resolve_context_dir)"

log "run dir:   $RUN_DIR"
log "workspace: $WORKSPACE"

# ------------------------------------------------------------------ the ticket

if [ ! -s "$RUN_DIR/ticket.md" ]; then
  if [ "$SOURCE" = "corpus" ]; then
    [ -f "$REPO_ROOT/data/corpus.json" ] || fail "no data/corpus.json; use --live"
    PAGER_TICKET="$TICKET" PAGER_CORPUS="$REPO_ROOT/data/corpus.json" \
    PAGER_OUT_TICKET="$RUN_DIR/ticket.md" PAGER_OUT_AREA="$RUN_DIR/area.txt" \
    PAGER_OUT_ISSUE="$RUN_DIR/issue.json" \
    python3 - <<'PY'
import json, os, re
corpus = json.load(open(os.environ["PAGER_CORPUS"], encoding="utf-8"))
want = str(os.environ["PAGER_TICKET"])
rec = next((r for r in corpus if str(r.get("number")) == want), None)
if rec is None:
    raise SystemExit(f"ticket {want} is not in the corpus")

HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
PR_LINKS_HEADING = re.compile(r"^[ \t]{0,3}#{1,4}[ \t]*PR[ \t]*Links\b", re.I | re.M)
GITHUB_REF_URL = re.compile(
    r"https?://(?:www\.)?github\.com/[\w.\-]+/[\w.\-]+/(?:pull|commit|issues)/\S+", re.I)

body = HTML_COMMENT.sub("", rec.get("body") or "")
m = PR_LINKS_HEADING.search(body)
if m and m.start() >= 200:
    body = body[: m.start()]
body = GITHUB_REF_URL.sub("[redacted]", body).rstrip()

areas = rec.get("affected_areas") or []
with open(os.environ["PAGER_OUT_TICKET"], "w", encoding="utf-8") as fh:
    fh.write(f"# {rec['title']}\n\nsprint-tasks issue #{rec['number']}\n")
    if rec.get("labels"):
        fh.write("Labels: " + ", ".join(rec["labels"]) + "\n")
    if areas:
        fh.write("Affected areas (as reported): " + ", ".join(areas) + "\n")
    fh.write("\n---\n\n" + body + "\n")
open(os.environ["PAGER_OUT_AREA"], "w", encoding="utf-8").write(areas[0] if areas else "")
json.dump({"number": rec["number"], "title": rec["title"], "labels": [],
           "url": f"https://github.com/devtron-labs/sprint-tasks/issues/{rec['number']}"},
          open(os.environ["PAGER_OUT_ISSUE"], "w"))
print(f"corpus ticket {want}: area={areas[0] if areas else '(none)'}, "
      f"{len(body)} chars after redaction")
PY
  else
    "$BIN/prepare.sh" "$TICKET"
  fi
else
  log "reusing $RUN_DIR/ticket.md (delete it to re-fetch)"
fi

AREA="$(cat "$RUN_DIR/area.txt" 2>/dev/null || true)"
index_slice "$AREA" "$CONTEXT_DIR/pager-index.md" > "$RUN_DIR/index-slice.md" || true
[ -s "$RUN_DIR/index-slice.md" ] || printf '(No section in the past-PR index matches "%s".)\n' \
  "${AREA:-no affected area reported}" > "$RUN_DIR/index-slice.md"

# ----------------------------------------------------------------- guardrails

guard_writable_workspace() {
  if [ "$WORKSPACE" = "$REPO_ROOT/workspace" ]; then
    fail "the fix stage writes to the checkout, and $WORKSPACE is the repo's read-only clone set.
Point PAGER_WORKSPACE at an expendable copy first:
  PAGER_WORKSPACE=/tmp/pager-ws $BIN/clone.sh search
  PAGER_WORKSPACE=/tmp/pager-ws $0 --ticket $TICKET --stage $STAGE"
  fi
}


# --------------------------------------------------------------------- the run

guard_writable_workspace

"$BIN/run.sh" "$TICKET"

if [ "$SCORE" = "1" ] && [ -f "$RUN_DIR/agent.json" ]; then
  # `repo/path` lines, the form tools/eval/score.py consumes, so a prompt edit
  # can be scored against the corpus without leaving the harness.
  echo
  jq -r '.repos[] | .repo as $r | .changed_files[] | "\($r)/\(.)"' "$RUN_DIR/agent.json"
fi
