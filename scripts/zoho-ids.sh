#!/usr/bin/env bash
#
# Print the Zoho Desk org id and department ids for a set of OAuth credentials.
#
#   scripts/zoho-ids.sh <client-id> <client-secret> <code-or-refresh-token> [dc]
#   scripts/zoho-ids.sh <client-id> <client-secret> - [dc]   # client credentials
#
# Pass "-" as the third argument to use the client-credentials grant, which
# needs no code at all. Export ZOHO_ORG_ID first if you already know it.
#
# `dc` is the data centre: in | com | eu | au | jp | ca | sa   (default: in)
#
# Accepts EITHER the 10-minute self-client code OR a refresh token, because the
# two look identical (`1000.xxx.yyy`) and telling them apart by eye has already
# cost this project an afternoon. It tries the refresh grant first; if that is
# rejected it treats the value as a code, exchanges it, and prints the refresh
# token it got — which is the value that belongs in ZOHO_REFRESH_TOKEN.
#
# Nothing is written to disk and no secret is echoed.
set -Eeuo pipefail

CID="${1:?client id}"; SEC="${2:?client secret}"; VAL="${3:?code or refresh token}"
DC="${4:-in}"
ACCOUNTS="https://accounts.zoho.${DC}"
DESK="https://desk.zoho.${DC}"

command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

say() { printf '\n== %s ==\n' "$1"; }

token_from() {  # grant_type, field, value
  curl -s -X POST "$ACCOUNTS/oauth/v2/token" \
    -d "grant_type=$1" -d "client_id=$CID" -d "client_secret=$SEC" -d "$2=$3"
}

say "authenticating"
if [ "$VAL" = "-" ]; then
  # No code, no refresh token: client credentials. Needs an org id to scope
  # `soid`, so this path can only list departments, not discover the org.
  echo "  client-credentials grant (no code needed)"
  resp="$(curl -s -X POST "$ACCOUNTS/oauth/v2/token" \
    -d grant_type=client_credentials -d "client_id=$CID" -d "client_secret=$SEC" \
    -d "scope=Desk.tickets.READ,Desk.search.READ,Desk.basic.READ,Desk.channels.email.READ" \
    ${ZOHO_ORG_ID:+-d "soid=ZohoDesk.$ZOHO_ORG_ID"})"
  if ! echo "$resp" | jq -e '.access_token' >/dev/null 2>&1; then
    echo "  FAILED: $(echo "$resp" | jq -c .)" >&2
    echo "  If this says invalid_client, export ZOHO_ORG_ID=<org> first —" >&2
    echo "  client credentials must be scoped to one Desk org via soid." >&2
    exit 1
  fi
  echo "  ok — no refresh token needed; leave ZOHO_REFRESH_TOKEN empty"
  AT="$(echo "$resp" | jq -r .access_token)"
else
resp="$(token_from refresh_token refresh_token "$VAL")"
if echo "$resp" | jq -e '.access_token' >/dev/null 2>&1; then
  echo "  the value you passed is a REFRESH TOKEN — use it as ZOHO_REFRESH_TOKEN"
else
  echo "  not a refresh token ($(echo "$resp" | jq -r '.error // "unknown"')); trying it as a 10-minute code"
  resp="$(token_from authorization_code code "$VAL")"
  if ! echo "$resp" | jq -e '.access_token' >/dev/null 2>&1; then
    first="$(echo "$resp" | jq -c .)"
    # A code is issued by one data centre's console and is meaningless at
    # another. Probing is free and rules out the mistake that is hardest to
    # see: a production org on a different DC from the sandbox you tested with.
    echo "  $DC rejected it ($first) — probing other data centres"
    for alt in com in eu au jp ca sa; do
      [ "$alt" = "$DC" ] && continue
      alt_resp="$(curl -s -X POST "https://accounts.zoho.$alt/oauth/v2/token" \
        -d grant_type=authorization_code -d "client_id=$CID" \
        -d "client_secret=$SEC" -d "code=$VAL" 2>/dev/null || true)"
      if echo "$alt_resp" | jq -e '.access_token' >/dev/null 2>&1; then
        echo
        echo "  FOUND IT: this org is on data centre '$alt', not '$DC'."
        echo "  Re-run with '$alt' as the 4th argument, and set ZOHO_DATA_CENTRE=$alt."
        echo "  (the code is now spent — generate a fresh one first)"
        exit 1
      fi
      printf '    %s: %s\n' "$alt" "$(echo "$alt_resp" | jq -r '.error // "no response"')"
    done
    echo >&2
    echo "  FAILED everywhere: $first" >&2
    echo "  Every DC says invalid_code, so the code is expired or already used." >&2
    echo "  Generate a fresh one and run this within 10 minutes." >&2
    exit 1
  fi
  rt="$(echo "$resp" | jq -r '.refresh_token // empty')"
  if [ -n "$rt" ]; then
    echo "  exchanged. THIS is your ZOHO_REFRESH_TOKEN:"
    echo
    echo "    $rt"
    echo
    echo "  (a code is single-use — store this now, you cannot exchange it again)"
  else
    echo "  exchanged, but no refresh_token came back: the code was already used once." >&2
    echo "  Generate a fresh code and re-run." >&2
  fi
fi

AT="$(echo "$resp" | jq -r .access_token)"
fi

say "organizations"
orgs="$(curl -s "$DESK/api/v1/organizations" -H "Authorization: Zoho-oauthtoken $AT")"
echo "$orgs" | jq -r '.data[]? | "  ZOHO_ORG_ID=\(.id)   \(.companyName // .portalName // "?")"'
n="$(echo "$orgs" | jq '[.data[]?] | length')"
[ "$n" -gt 1 ] && echo "  ^ more than one org — take the PRODUCTION one, the ids look alike"

say "departments"
for oid in $(echo "$orgs" | jq -r '.data[]?.id'); do
  echo "  org $oid:"
  curl -s "$DESK/api/v1/departments" \
    -H "Authorization: Zoho-oauthtoken $AT" -H "orgId: $oid" \
  | jq -r '.data[]? | "    ZOHO_DEPARTMENT_ID=\(.id)   \(.name)   enabled=\(.isEnabled)"'
done

say "next"
echo "  Pick the NARROWEST department that carries pager tickets: the poller"
echo "  triages every Open ticket in whichever one it is given."
