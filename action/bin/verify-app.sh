#!/usr/bin/env bash
#
# Verify the pager GitHub App can reach every repo the engine needs.
#
#   action/bin/verify-app.sh <app-id> <path-to-private-key.pem>
#
# Mints a token the same way the workflow does, then lists what it can reach.
# This is the real check: a missing installation or permission is invisible
# until a run fails, and the failure lands 20 minutes into a job.
#
# The key is read, never printed, and no token is written to disk.

set -Eeuo pipefail

APP_ID="${1:-}"; KEY="${2:-}"
[ -n "$APP_ID" ] && [ -n "$KEY" ] || { echo "usage: verify-app.sh <app-id> <key.pem>" >&2; exit 1; }
[ -f "$KEY" ] || { echo "no such key: $KEY" >&2; exit 1; }

NEEDED=(sprint-tasks devtron devtron-enterprise dashboard devtron-services
        devtron-services-enterprise athena-be notifier devtron-fe-common-lib)

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

now=$(date +%s)
header='{"alg":"RS256","typ":"JWT"}'
# iat backdated 60s: GitHub rejects a JWT whose iat is in the future, and a
# second of clock skew is enough to do it.
payload=$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$APP_ID")
unsigned="$(printf '%s' "$header" | b64url).$(printf '%s' "$payload" | b64url)"
sig=$(printf '%s' "$unsigned" | openssl dgst -sha256 -sign "$KEY" -binary | b64url)
JWT="$unsigned.$sig"

echo "== App identity =="
app=$(curl -sS -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" \
        https://api.github.com/app)
if ! echo "$app" | jq -e '.slug' >/dev/null 2>&1; then
  echo "FAILED to authenticate as the App:"; echo "$app" | jq -r '.message // .' ; exit 1
fi
echo "$app" | jq -r '"  name: \(.name)\n  slug: \(.slug)\n  owner: \(.owner.login)"'
echo "  permissions: $(echo "$app" | jq -c '.permissions')"

echo
echo "== Installations =="
insts=$(curl -sS -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" \
          https://api.github.com/app/installations)
echo "$insts" | jq -r '.[] | "  account=\(.account.login) id=\(.id) selection=\(.repository_selection)"'

inst_id=$(echo "$insts" | jq -r '.[] | select(.account.login=="devtron-labs") | .id' | head -1)
[ -n "$inst_id" ] || { echo "NOT INSTALLED on devtron-labs"; exit 1; }

tok=$(curl -sS -X POST -H "Authorization: Bearer $JWT" -H "Accept: application/vnd.github+json" \
        "https://api.github.com/app/installations/$inst_id/access_tokens" | jq -r '.token')
[ -n "$tok" ] && [ "$tok" != "null" ] || { echo "could not mint an installation token"; exit 1; }

echo
echo "== Repositories the App can reach =="
repos=$(curl -sS -H "Authorization: Bearer $tok" -H "Accept: application/vnd.github+json" \
          "https://api.github.com/installation/repositories?per_page=100" | jq -r '.repositories[].name')

rc=0
for r in "${NEEDED[@]}"; do
  if grep -qx "$r" <<<"$repos"; then printf '  %-32s OK\n' "$r"
  else printf '  %-32s MISSING — install the App on it\n' "$r"; rc=1; fi
done

echo
echo "== Permissions on the minted token =="
echo "$app" | jq -r '.permissions | to_entries[] | "  \(.key): \(.value)"'
for need in "contents:write" "pull_requests:write" "issues:write"; do
  k="${need%%:*}"; v="${need##*:}"
  got=$(echo "$app" | jq -r --arg k "$k" '.permissions[$k] // "none"')
  [ "$got" = "$v" ] || { echo "  !! $k is '$got', needs '$v'"; rc=1; }
done

echo
[ "$rc" -eq 0 ] && echo "ALL GOOD — the workflow will be able to clone, push and open PRs." \
               || echo "NOT READY — fix the items marked above."
exit "$rc"
