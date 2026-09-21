#!/usr/bin/env bash
#
# Run exactly one headless Claude Code session and write its structured output
# to a JSON file.
#
#   stage.sh <stage> [--cwd DIR] [--out FILE] [--var KEY=VALUE]... [--var-file KEY=PATH]...
#
# Every stage in the pipeline goes through here, so every stage gets the same
# three guarantees:
#
#  1. **A schema, not a hope.** `--json-schema` makes the CLI itself reject a
#     prose answer. The parsed object comes back on the envelope's
#     `structured_output` key and is what lands in --out. A stage that cannot
#     produce its schema fails here rather than three steps later in jq.
#
#  2. **A tool set chosen per stage.** The read-only stages get
#     `--tools Read,Grep,Glob` and literally cannot write a file or run a
#     command. The fix stage gets Edit/Write/Bash but under a settings file
#     whose deny rules block `git push`, `gh`, and the `go mod` commands that
#     would rewrite a vendored checkout. The fix agent has no path to opening a
#     PR: publishing lives in a different script, in a different step, with a
#     different token.
#
#  3. **A budget and a clock.** `--max-budget-usd` caps spend per stage;
#     PAGER_STAGE_TIMEOUT caps wall-clock. Note that the CLI at 2.1.231 has no
#     `--max-turns` (the GitHub Action docs still recommend it) -- budget and
#     timeout are the bounds that actually exist.
#
# `--setting-sources project,local` deliberately excludes user-level settings.
# On a developer machine a SessionStart hook or a personal CLAUDE.md will
# otherwise leak into the run and make a local dry-run diverge from CI. Repo
# CLAUDE.md files inside the clones are not a setting source and are still
# read, which is what CI sees too.

source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"
require jq python3 claude

STAGE=""
CWD=""
OUT=""

# Template values are whole markdown and JSON documents, so they are passed to
# the renderer as files in a scratch directory rather than through argv or the
# environment -- both of which mangle or truncate multi-kilobyte values.
VARDIR="$(mktemp -d)"
trap 'rm -rf -- "$VARDIR"' EXIT

while [ "$#" -gt 0 ]; do
  case "$1" in
    --cwd)      CWD="$2"; shift 2 ;;
    --out)      OUT="$2"; shift 2 ;;
    --var)
      key="${2%%=*}"
      printf '%s' "${2#*=}" > "$VARDIR/$key"
      shift 2 ;;
    --var-file)
      key="${2%%=*}"; path="${2#*=}"
      [ -f "$path" ] || fail "--var-file $key: no such file: $path"
      cp -- "$path" "$VARDIR/$key"
      shift 2 ;;
    -*)         fail "unknown flag: $1" ;;
    *)          STAGE="$1"; shift ;;
  esac
done
[ -n "$STAGE" ] || fail "usage: stage.sh <stage> [--cwd DIR] [--out FILE] [--var K=V]..."

PROMPT_FILE="$ACTION_DIR/prompts/$STAGE.md"
SCHEMA_FILE="$ACTION_DIR/schemas/$STAGE.json"
[ -f "$PROMPT_FILE" ] || fail "no prompt for stage '$STAGE' at $PROMPT_FILE"
[ -f "$SCHEMA_FILE" ] || fail "no schema for stage '$STAGE' at $SCHEMA_FILE"

# Per-stage tool set, permission mode, settings, model and budget.
# Aliases ('opus', 'sonnet') rather than pinned model ids: aliases track the
# current model and do not need updating when a model ships.
case "$STAGE" in
  agent)
    # One self-directed session: localize, explain, fix, report.
    #
    # The deny rules in settings/fix.json are what keep `gh` out of the agent's
    # hands, and the workflow step this runs in carries no GitHub token. So the
    # session cannot open a pull request however it decides to proceed --
    # publishing is a separate script, in a separate step, with its own
    # write-scoped token the agent never sees. That separation is the security
    # property; everything else about how it works is the agent's own choice.
    TOOLS="Read,Grep,Glob,Edit,Write,Bash"
    MODE="acceptEdits"                 # Bash falls through to the allow/deny rules
    SETTINGS="$ACTION_DIR/settings/fix.json"
    MODEL="${PAGER_MODEL_AGENT:-opus}"
    BUDGET="${PAGER_BUDGET_AGENT:-20}"
    ;;
  *) fail "unknown stage '$STAGE'" ;;
esac

WORK="${CWD:-$WORKSPACE}"
[ -d "$WORK" ] || fail "working directory does not exist: $WORK"
OUT="${OUT:-$RUN_DIR/$STAGE.json}"
mkdir -p "$RUN_DIR" "$(dirname -- "$OUT")"

# Render {{PLACEHOLDER}} in the prompt. Done in python rather than sed/envsubst
# because the substituted values are whole markdown documents containing
# backslashes, ampersands and shell metacharacters.
RENDERED="$VARDIR/.rendered.md"
PAGER_TEMPLATE="$PROMPT_FILE" PAGER_VARDIR="$VARDIR" PAGER_RENDERED="$RENDERED" \
python3 - <<'PY' || fail "prompt rendering failed for $STAGE"
import os, pathlib, re, sys
tpl = pathlib.Path(os.environ["PAGER_TEMPLATE"]).read_text(encoding="utf-8")
vardir = pathlib.Path(os.environ["PAGER_VARDIR"])
values = {
    p.name: p.read_text(encoding="utf-8")
    for p in vardir.iterdir()
    if p.is_file() and not p.name.startswith(".")
}
missing = sorted(set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", tpl)) - set(values))
if missing:
    sys.exit(f"prompt {os.environ['PAGER_TEMPLATE']} needs vars not supplied: {', '.join(missing)}")
out = re.sub(r"\{\{([A-Z0-9_]+)\}\}", lambda m: values[m.group(1)], tpl)
pathlib.Path(os.environ["PAGER_RENDERED"]).write_text(out, encoding="utf-8")
PY
PROMPT="$(cat -- "$RENDERED")"
# Keep the exact prompt next to the output. Half of iterating on these is reading
# back what the agent was actually handed, not what the template says.
cp -- "$RENDERED" "$RUN_DIR/$STAGE.prompt.md" 2>/dev/null || true

SYSTEM_FACTS="$(cat -- "$ACTION_DIR/prompts/system-facts.md")"

argv=(
  claude -p "$PROMPT"
  --output-format json
  --model "$MODEL"
  --tools "$TOOLS"
  --permission-mode "$MODE"
  --setting-sources project,local
  --append-system-prompt "$SYSTEM_FACTS"
  --json-schema "$(jq -c . "$SCHEMA_FILE")"
  --max-budget-usd "$BUDGET"
)
[ -n "$SETTINGS" ] && argv+=(--settings "$SETTINGS")

ENVELOPE="$RUN_DIR/$STAGE.envelope.json"
mkdir -p "$RUN_DIR"

log "stage $STAGE: model=$MODEL tools=$TOOLS budget=\$$BUDGET cwd=$WORK"
start=$(date +%s)

set +e
( cd -- "$WORK" && "${argv[@]}" ) > "$ENVELOPE" 2> "$RUN_DIR/$STAGE.stderr"
rc=$?
set -e

elapsed=$(( $(date +%s) - start ))

if [ "$rc" -ne 0 ]; then
  head -c 2000 "$RUN_DIR/$STAGE.stderr" >&2 || true
  fail "stage $STAGE: claude exited $rc after ${elapsed}s"
fi
jq -e . "$ENVELOPE" >/dev/null 2>&1 \
  || fail "stage $STAGE: no JSON envelope on stdout (first 500 bytes: $(head -c 500 "$ENVELOPE"))"

if [ "$(jq -r '.is_error // false' "$ENVELOPE")" = "true" ]; then
  fail "stage $STAGE: claude reported an error ($(jq -r '.subtype // "?"' "$ENVELOPE")): $(jq -r '.result // "" | tostring | .[0:400]' "$ENVELOPE")"
fi

jq -e '.structured_output' "$ENVELOPE" >/dev/null 2>&1 \
  || fail "stage $STAGE: envelope carried no structured_output; --json-schema was not honoured"

jq '.structured_output' "$ENVELOPE" > "$OUT"

cost=$(jq -r '.total_cost_usd // 0' "$ENVELOPE")
turns=$(jq -r '.num_turns // 0' "$ENVELOPE")
denials=$(jq -r '(.permission_denials // []) | length' "$ENVELOPE")
log "stage $STAGE: ok in ${elapsed}s, \$${cost}, ${turns} turns, ${denials} permission denials -> $OUT"
# A run with many denials reached its answer on less evidence than it appears
# to have. Worth seeing in the log rather than discovering in a bad RCA.
if [ "$denials" -gt 0 ]; then
  warn "stage $STAGE denied ${denials} tool call(s): $(jq -c '[.permission_denials[]?.tool_name] | unique' "$ENVELOPE")"
fi

printf '%s\n' "$OUT"
