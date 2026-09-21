#!/bin/sh
# One Zoho poll tick against the deployed LangGraph app.
#
# Deliberately tiny and credential-light: this container reaches exactly one
# host and holds no Zoho, GitHub, Slack or Bedrock secret. Those live in the
# LangGraph deployment. The worst a compromise here achieves is triggering a
# poll that would have happened anyway within five minutes.
set -eu

: "${LANGGRAPH_API_URL:?LANGGRAPH_API_URL is not set}"
: "${LANGGRAPH_API_KEY:?LANGGRAPH_API_KEY is not set}"

# /runs/wait, not /runs: this pod's exit code should reflect the poll itself,
# not merely that the platform accepted the request.
code=$(curl -sS -o /tmp/out.json -w '%{http_code}' \
  --max-time "${POLL_TIMEOUT:-240}" --retry 2 --retry-connrefused \
  -X POST "${LANGGRAPH_API_URL%/}/runs/wait" \
  -H "x-api-key: ${LANGGRAPH_API_KEY}" \
  -H 'content-type: application/json' \
  -d '{"assistant_id":"poller","input":{}}')

echo "HTTP ${code}"
cat /tmp/out.json
echo

[ "${code}" = "200" ] || { echo "poll failed"; exit 1; }

# A run can return 200 having failed inside the graph. The report is the
# poller's own account of the tick; no report means it did not complete, and
# that must not read as a quiet success.
grep -q '"report"' /tmp/out.json || { echo "no report — the graph did not complete"; exit 1; }
