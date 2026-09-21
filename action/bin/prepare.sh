#!/usr/bin/env bash
#
# Fetch one sprint-tasks issue and turn it into the pipeline's ticket input.
#
#   prepare.sh <issue-number>
#
# Writes:
#   $RUN_DIR/ticket.md    the ticket as the agent sees it, redacted
#   $RUN_DIR/area.txt     the first "Affected areas" value, or empty
#   $RUN_DIR/issue.json   the raw gh payload, for debugging
#
# Redaction is not optional and it is not cosmetic. Devtron's pager issues carry
# a PR template whose "## PR Links" section is filled in *after* the fix ships:
# 119 of 135 corpus bodies contain their own fix-PR URLs, and the repo name is
# right there in the URL. Those links were not on the issue when a developer
# picked it up, so removing them restores the real point-in-time state rather
# than hiding information. Leave them in and the agent is reading the answer
# key, in production as well as in the eval.
#
# The same cut also removes the "## Microservices" checklist and the release
# notes tail that follow, which are themselves strong hints about which service
# is at fault.

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
require gh python3

ISSUE="${1:-}"
[ -n "$ISSUE" ] || fail "usage: prepare.sh <issue-number>"
ISSUE_REPO="${PAGER_ISSUE_REPO:-$GH_OWNER/sprint-tasks}"

mkdir -p "$RUN_DIR"

# publish.sh reads this to know which issue to comment on, and it runs in a
# separate workflow step that never sees run.sh's arguments. Nothing wrote it in
# the CI path -- only dry-run.sh did -- so publishing could never have worked in
# Actions, on any run that got far enough to try. Written before the fetch so a
# failed fetch still leaves publish.sh able to report the failure on the issue.
printf '%s\n' "$ISSUE" > "$RUN_DIR/issue-number.txt"

log "fetching $ISSUE_REPO#$ISSUE"
gh issue view "$ISSUE" --repo "$ISSUE_REPO" \
   --json number,title,body,labels,url,state \
   > "$RUN_DIR/issue.json" \
  || fail "could not read $ISSUE_REPO#$ISSUE (is GH_TOKEN scoped to it?)"

PAGER_ISSUE_JSON="$RUN_DIR/issue.json" \
PAGER_OUT_TICKET="$RUN_DIR/ticket.md" \
PAGER_OUT_AREA="$RUN_DIR/area.txt" \
python3 - <<'PY'
import json, os, re

issue = json.load(open(os.environ["PAGER_ISSUE_JSON"], encoding="utf-8"))
body = issue.get("body") or ""

HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
PR_LINKS_HEADING = re.compile(r"^[ \t]{0,3}#{1,4}[ \t]*PR[ \t]*Links\b", re.I | re.M)
GITHUB_REF_URL = re.compile(
    r"https?://(?:www\.)?github\.com/[\w.\-]+/[\w.\-]+/(?:pull|commit|issues)/\S+", re.I
)
MIN_BODY_CHARS = 200

# The template's own instructions live in HTML comments -- invisible on GitHub,
# very visible to a model, and they include an example PR link.
clean = HTML_COMMENT.sub("", body)


def section(text, heading):
    m = re.search(
        rf"^#{{2,3}}\s*{re.escape(heading)}\s*$(.*?)(?=^#{{1,3}}\s|\Z)",
        text, re.MULTILINE | re.DOTALL,
    )
    return m.group(1) if m else ""


areas = [
    ln.strip(" -*\t")
    for ln in section(clean, "Affected areas").splitlines()
]
areas = [a for a in areas if a and a.lower() not in {"none", "_no response_"}]

redacted = clean
m = PR_LINKS_HEADING.search(redacted)
# Guard: a malformed ticket whose template comes first would otherwise be cut to
# nothing, and an empty prompt is a worse failure than an unredacted one.
if m and m.start() >= MIN_BODY_CHARS:
    redacted = redacted[: m.start()]
redacted = GITHUB_REF_URL.sub("[redacted]", redacted).rstrip()

with open(os.environ["PAGER_OUT_TICKET"], "w", encoding="utf-8") as fh:
    fh.write(f"# {issue['title']}\n\n")
    fh.write(f"sprint-tasks issue #{issue['number']}\n")
    labels = ", ".join(l["name"] for l in issue.get("labels") or [])
    if labels:
        fh.write(f"Labels: {labels}\n")
    if areas:
        fh.write(f"Affected areas (as reported): {', '.join(areas)}\n")
    fh.write("\n---\n\n")
    fh.write(redacted + "\n")

with open(os.environ["PAGER_OUT_AREA"], "w", encoding="utf-8") as fh:
    fh.write(areas[0] if areas else "")

print(f"ticket: {len(redacted)} chars after redaction; area={areas[0] if areas else '(none)'}")
PY

[ -s "$RUN_DIR/ticket.md" ] || fail "redaction produced an empty ticket; inspect $RUN_DIR/issue.json"
log "wrote $RUN_DIR/ticket.md"
