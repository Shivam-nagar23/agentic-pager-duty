# Cutting over to the production Zoho desk

Read-only, new tickets only, nothing sent to customers. Everything here is
config; no code changes are required.

The end state: the agent watches the production queue, classifies tickets, and
posts a Slack card for anything it wants to act on. It cannot email a customer,
because the credential has no scope to.

---

## 1. Zoho — a read-only self-client

### 1.1 Create the client

`https://api-console.zoho.in` (`.in` because `ZOHO_DATA_CENTRE=in`; use the
console for whichever DC the production org lives in — a wrong DC returns
`invalid_client`, which reads like bad credentials and is not).

**Self Client** → Create. Note the **Client ID** and **Client Secret**.

### 1.2 Generate a code, with read-only scopes

Self Client → **Generate Code**:

| Field | Value |
|---|---|
| Scope | `Desk.tickets.READ,Desk.search.READ,Desk.basic.READ,Desk.channels.email.READ` |
| Time duration | 10 minutes |
| Scope description | anything |

**`Desk.tickets.UPDATE` is deliberately absent.** That is the scope `sendReply`
needs. Without it, no code path can email a customer even if `REPLIES_ENABLED`
were flipped by accident — a flag is a decision, a missing scope is a guarantee.

Copy the code. It expires in ten minutes and is **not** a refresh token; pasting
it into `ZOHO_REFRESH_TOKEN` is a mistake that has already cost this project an
afternoon.

### 1.3 Exchange the code for a refresh token

```bash
curl -s -X POST 'https://accounts.zoho.in/oauth/v2/token' \
  -d 'grant_type=authorization_code' \
  -d 'client_id=<CLIENT_ID>' \
  -d 'client_secret=<CLIENT_SECRET>' \
  -d 'code=<THE_10_MINUTE_CODE>' | jq
```

Keep `refresh_token` from the response. `access_token` is short-lived and the
app mints its own.

If this returns `invalid_code`, the code expired or was already used — generate
a new one. If it returns `invalid_client`, the data centre is wrong.

### 1.4 Find the org and department ids

```bash
TOKEN=<access_token from the step above>

curl -s https://desk.zoho.in/api/v1/organizations \
  -H "Authorization: Zoho-oauthtoken $TOKEN" | jq '.data[] | {id, companyName}'

curl -s https://desk.zoho.in/api/v1/departments \
  -H "Authorization: Zoho-oauthtoken $TOKEN" \
  -H "orgId: <ORG_ID>" | jq '.data[] | {id, name, isEnabled}'
```

Pick the **narrowest department that contains pager tickets**. The poller
triages every Open ticket in whatever department it is given — on a catch-all
queue that includes billing questions and feature requests.

---

## 2. LangSmith — deployment environment

Deployments → the deployment → **Environment Variables**. Saving triggers a new
revision; changes do not apply until it redeploys.

### Replace

```
ZOHO_CLIENT_ID=<prod>
ZOHO_CLIENT_SECRET=<prod>
ZOHO_REFRESH_TOKEN=<prod, from 1.3>
ZOHO_ORG_ID=<prod>
ZOHO_DEPARTMENT_ID=<prod>
ZOHO_FROM_EMAIL=            # leave empty; nothing is sent
```

### Add

```
ZOHO_MIN_CREATED_AT=<the instant you cut over, e.g. 2026-09-25T06:18:21Z>
```

Tickets created before this are skipped whatever their modified time. Without
it the existing queue leaks in: a customer replying to an old ticket bumps its
modified time and it arrives looking like new work.

### Confirm, do not assume

```
ZOHO_TRANSPORT=rest          # `fake` polls nothing and looks healthy doing it
REPLIES_ENABLED=false
ISSUE_CREATION_ENABLED=      # see below
```

`ISSUE_CREATION_ENABLED=true` means an approved gate 2 creates a `sprint-tasks`
issue **and**, because the scribe applies `agent-fix` at creation, triggers the
fix engine into opening draft PRs. For a first day of watching, `false` gives a
genuinely observe-only mode: real tickets, real classifications, real drafts to
judge in Slack, nothing created anywhere.

### Optional, for the first hours

```
ZOHO_POLL_LIMIT=10           # down from 50, so a burst cannot run away
```

---

## 3. Cron

**No change needed.** The existing cron targets the `poller` assistant every 5
minutes and is unaffected by which desk it points at.

List what exists:

```python
from langgraph_sdk import get_sync_client
c = get_sync_client(url=DEPLOYMENT_URL, api_key=LANGSMITH_KEY)
print(c.crons.search())
```

To pause during cutover, delete it and recreate after:

```python
c.crons.delete(cron_id)
c.crons.create(assistant_id="poller", schedule="*/5 * * * *", input={})
```

The GitHub Actions poller has been removed; `deploy/zoho-poll-cronjob.yaml`
remains as a fallback if the platform cron is ever unavailable. Do not run two —
they would double-poll. The duplicate is *safe* (`threads.create(if_exists=
"raise")` is a primary-key insert) but doubles the noise.

---

## 4. Verify, in this order

**1. Config landed.**

```bash
curl -s https://<deployment>/slack/health
```

Expect `ok:true`, `missing_env:[]`, `gate_notification:"automatic"`.
`gate_notification:"manual"` means `PAGER_PUBLIC_URL` or `PAGER_WEBHOOK_SECRET`
is unset and gates will park silently.

**2. One poll by hand, before the cron fires.**

```bash
curl -s -X POST https://<deployment>/runs/wait \
  -H "x-api-key: $KEY" -H 'content-type: application/json' \
  -d '{"assistant_id":"poller","input":{}}'
```

Read the report:

- `considered=0` — nothing modified in the last 10 minutes. Expected on a quiet
  queue, and also what a `fake` transport looks like. Not proof of anything.
- `considered=N skipped_old=N started=0` — **the cutover is working.** The queue
  is visible and correctly ignored.
- `considered=N started=N` — tickets are being triaged. If N is large and
  `skipped_old=0`, `ZOHO_MIN_CREATED_AT` did not take effect.
- `errors=[...]` — read them. Auth failures name themselves.

**3. A real ticket.** Post one to the production desk and leave it alone. Within
five minutes a Slack card should appear without anybody running a command.

---

## Rollback

Set `ZOHO_TRANSPORT=fake` and redeploy. The graph runs, finds nothing, touches
nothing. Faster than deleting the cron and leaves the deployment intact for
inspection.
