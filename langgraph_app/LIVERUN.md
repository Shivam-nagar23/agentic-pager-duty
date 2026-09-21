# First live run

One real Zoho ticket, real triage, real Slack approvals, a real `sprint-tasks`
issue — and **no mail to the customer**. Gate 1 renders and can be approved;
the kill switch stops the send. That is the whole point of this run.

Everything is local: `langgraph dev` on port 2024 plus a tunnel so Slack can
reach the interaction endpoint. No hosted cron (that needs Plus/Enterprise),
so the ticket is started by hand.

Work top to bottom. Every step is checkable before the next one.

---

## 0. Before you start — the kill switch

```sh
grep -E '^(REPLIES_ENABLED|ISSUE_CREATION_ENABLED)=' langgraph_app/.env
```

Must print:

```
REPLIES_ENABLED=false
ISSUE_CREATION_ENABLED=true
```

`REPLIES_ENABLED=false` is the customer-reply kill switch. With it false,
`zoho_send_reply` fires the gate, waits for you, accepts your approval, records
it in the thread — **and then returns "APPROVED but NOT SENT" instead of
calling Zoho.** No mail leaves. That is the behaviour this run is built around.

**If it is `true`, approving gate 1 emails a paying customer, for real, with no
undo.** Unset counts as false (the default), but set it explicitly anyway so a
stray shell export cannot flip it.

`ISSUE_CREATION_ENABLED=true` is the other switch, and this run wants it on —
a real issue in `sprint-tasks` is the end of the run. Leave it `false` if you
want a dry pass first; gate 2 will then approve-and-refuse the same way gate 1
does.

Re-check after `langgraph dev` is up:

```sh
curl -s localhost:2024/slack/health | jq
```

`missing_env` must be `[]` and `approvers_configured` must be `1`.

---

## 1. Environment

All of it goes in `langgraph_app/.env` (never committed — `.env.example` is
the list of names). Start from `cp .env.example .env`.

### Anthropic

| Variable | What | Where |
|---|---|---|
| `ANTHROPIC_API_KEY` | Model access | <https://console.anthropic.com> → Settings → API keys |
| `TRIAGE_MODEL` | Leave at `anthropic:claude-opus-5` | — |

### Zoho Desk

Set `ZOHO_TRANSPORT=rest`. **`fake` is the default** and will happily run the
whole pipeline against an in-memory stub ticket — the manual trigger prints a
warning when it sees `fake`, so read its first line.

| Variable | What | Where |
|---|---|---|
| `ZOHO_DATA_CENTRE` | `com` / `eu` / `in` / … | Whatever domain your Desk URL is on. Wrong value = auth against the wrong accounts host |
| `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET` | Self-client OAuth app | <https://api-console.zoho.com> → *Self Client* → Create → Client Secret tab |
| `ZOHO_REFRESH_TOKEN` | Long-lived token | Same console: *Generate Code* with the scopes below, then exchange the code. Mint it with `access_type=offline` **and** `prompt=consent` — without `prompt=consent` a re-auth returns an access token and **no refresh token**, silently |
| `ZOHO_ORG_ID` | `orgId` header | Desk → Setup (gear) → Developer Space → API → the org id shown there. A mismatch is **403 `OAUTH_ORG_MISMATCH`**, not 401 |
| `ZOHO_DEPARTMENT_ID` | Department the ticket lives in | Desk → Setup → Departments → open it, the id is in the URL |
| `ZOHO_FROM_EMAIL` | Verified From address | Desk → Setup → Email → Email Configuration. Unverified = 422 at send time |

Scopes, minimum set — paste exactly:

```
Desk.tickets.READ,Desk.tickets.UPDATE,Desk.search.READ,Desk.basic.READ,Desk.channels.email.READ
```

`sendReply` needs `Desk.tickets.**UPDATE**`, not `WRITE` and not `CREATE`. The
wrong one is a 403 `SCOPE_MISMATCH` that only shows up at the send.

### GitHub (gate 2)

| Variable | What | Where |
|---|---|---|
| `GITHUB_TOKEN` | `issues:write` on `devtron-labs/sprint-tasks` **only** | <https://github.com/settings/personal-access-tokens> → fine-grained → repository access: just `sprint-tasks` → Issues: Read and write |
| `SPRINT_TASKS_REPO` | `devtron-labs/sprint-tasks` | — |

**If `GITHUB_TOKEN` is unset the app falls back to a fake issue client** and
gate 2 will report a plausible issue number that does not exist. Set it.

### Slack

| Variable | What | Where |
|---|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-…` | api.slack.com/apps → your app → OAuth & Permissions → Bot User OAuth Token |
| `SLACK_SIGNING_SECRET` | The signing secret | Basic Information → App Credentials → Signing Secret. **Not** the bot token, **not** the verification token |
| `SLACK_CHANNEL_ID` | `C…`, not `#name` | Open the channel → click its name → *About* → the id at the bottom |
| `SLACK_APPROVER_USER_ID` | **Your own Slack member id**, `U…` | Your profile → ⋮ → *Copy member ID* |

`SLACK_APPROVER_USER_ID` is not a display name and not an email. It is the
whole authorization model: a click from any other user id is refused, visibly,
and resumes nothing.

**An empty allowlist approves nobody.** `is_approver` returns `False` on an
empty set — it never means "everyone". If you leave this unset, every Approve
click you make will be refused and the gate will stay parked. That is the
intended failure direction, but it will look like a bug at 2am, so set it now
and confirm `approvers_configured: 1` on `/slack/health`.

### Platform wiring

| Variable | Value for a local run |
|---|---|
| `LANGGRAPH_API_URL` | `http://127.0.0.1:2024` |
| `LANGGRAPH_API_KEY` | leave empty — `langgraph dev` has no auth |
| `TRIAGE_ASSISTANT_ID` | `triage` |

---

## 2. Start the server

`langgraph dev` is not installed by default — it is not a runtime dependency
and the tests do not need a server. Install it once:

```sh
cd langgraph_app
.venv/bin/python -m pip install -e ".[local]"    # or: uv sync --extra local
```

Then:

```sh
.venv/bin/python -m pytest -q          # 213 pass, offline. If not, stop here.
.venv/bin/langgraph dev                # http://127.0.0.1:2024
```

If the install or the server refuses on this Python (the venv is 3.14, and
`langgraph-cli` has lagged new releases before), make a 3.12 venv for the
server — the app code itself is 3.11+ and `langgraph.json` already declares
`python_version: "3.12"`.

Confirm the custom Slack routes came up with it:

```sh
curl -s localhost:2024/slack/health | jq
```

If that 404s, the `http.app` entry in `langgraph.json` did not load — nothing
downstream will work, and no Slack click will ever arrive.

Leave it running. Everything below is in a second terminal.

---

## 3. The tunnel

Slack must POST to your laptop. Use **cloudflared** — no account, no signup:

```sh
brew install cloudflared
cloudflared tunnel --url http://localhost:2024
```

It prints a URL like `https://random-words-1234.trycloudflare.com`. Check it:

```sh
curl -s https://<tunnel-host>/slack/health | jq
```

Two things that will cost you an hour if you skip them:

* **Slack's Request URL is the tunnel URL plus the real path** —
  `https://<tunnel-host>/slack/interactions`. Not the bare host, not
  `/slack/`, not `/interactions`.
* **Restarting cloudflared gives you a new hostname.** Every restart means
  going back into the Slack app config and updating the Request URL. A stale
  URL does not error loudly — clicks simply do nothing, or Slack shows a
  timeout in the message.

---

## 4. The Slack app

At <https://api.slack.com/apps>, *From scratch*, in the Devtron workspace.

1. **OAuth & Permissions → Bot Token Scopes:** add **`chat:write`**. That is
   the only scope. Do *not* add `chat:write.public` — the bot should be able to
   post in one channel it was invited to, and nowhere else.
2. **Install to Workspace.** Copy the `xoxb-…` token into `SLACK_BOT_TOKEN`.
3. **Basic Information → Signing Secret** into `SLACK_SIGNING_SECRET`.
4. **Create a private channel** — `#pager-approvals` — and **invite the bot**:
   `/invite @your-app-name` in the channel. Without the invite, every post
   fails with `not_in_channel` and you will see nothing at all in Slack.
   Private matters: these messages carry verbatim customer ticket text.
5. **Interactivity & Shortcuts → toggle on**, Request URL
   `https://<tunnel-host>/slack/interactions`. Slack sends a verification POST
   when you save; the route answers it. An unsigned probe gets a 401, which is
   correct.
6. **Your member id** → `SLACK_APPROVER_USER_ID`.

Restart `langgraph dev` after editing `.env` — it reads the file at startup.

---

## 5. Trigger one ticket

Get the ticket id from the Desk URL. Opening the ticket gives you something
like `…/tickets/details/**1234567000001234567**` — that long number is the id.
The `#4242` you see in the UI is the *ticket number*, which is not the same
thing and will not work.

```sh
cd langgraph_app
.venv/bin/python -m pagerduty_triage.start_ticket 1234567000001234567
```

It prints the derived thread id, then a one-line report. Expect
`started=1`.

This is the poll loop with the query removed — same `uuid5` thread id, same
`threads.create(if_exists="raise")` claim, same ledger write, same
`multitask_strategy="reject"`. Running it twice for one ticket prints
`skipped_conflict=1` and starts nothing, which is the once-per-ticket
guarantee doing its job, not a failure.

Watch the run in LangGraph Studio (the URL `langgraph dev` printed) or:

```sh
curl -s localhost:2024/threads/<thread-id>/state | jq '.tasks[].interrupts[].value'
```

---

## 6. Gate 1 in Slack

The notifier is a cron assistant, and a local run has no cron. Tick it by
hand — once, after the thread has parked:

```sh
curl -s -XPOST localhost:2024/runs/wait \
  -H 'content-type: application/json' \
  -d '{"assistant_id": "slack_notifier", "input": {}}' | jq
```

Run it again whenever a new gate parks. It will not double-post a gate it has
already posted within the same server process.

**What you should see in `#pager-approvals`:**

* a header, *Send this reply to a customer?*
* the Zoho ticket link and subject
* **what the customer actually asked, verbatim**, in a preformatted block —
  plus any later customer messages
* the exact reply text that would be emailed, verbatim
* the agent's reasoning, and any warnings it raised about itself
* **Approve and send** (with a confirm dialog) and a **Reject with a reason…**
  menu

If the conversation was too long it is shortened with a visible
`:scissors:` banner pointing at the Zoho ticket. If the message is longer than
Slack can render, the Approve button is *removed* on purpose and the message
tells you to answer from the thread state. Do not work around that.

**What approving does, with `REPLIES_ENABLED=false`:** the message is replaced
with ":white_check_mark: Approved by @you", the thread resumes, the tool
records your approval — and returns "APPROVED but NOT SENT". **Nothing reaches
the customer.** Check Zoho afterwards to confirm the ticket has no new
outbound thread; that check is the point of this run.

Rejecting picks a canned reason (there is no reasonless reject), the agent
gets that sentence back and revises. A revised draft parks a *new* interrupt —
tick the notifier again to see it.

---

## 7. Gate 2 and the real issue

If triage classified the ticket as a platform bug, the second gate parks the
same way. Tick the notifier, and you get *File this as a platform bug?* with
the repo, title, labels, classification, affected area and the filled pager
template body — verbatim.

Approving with `ISSUE_CREATION_ENABLED=true` and `GITHUB_TOKEN` set **creates a
real issue** in `devtron-labs/sprint-tasks`, labelled `pager-duty` and
`agent-triaged`. Check the URL the tool returns.

---

## 8. Handoff to the fix engine

The issue existing does **not** start anything. The fix engine triggers on the
**`agent-fix`** label, which you add by hand:

```sh
gh issue edit <number> --repo devtron-labs/sprint-tasks --add-label agent-fix
```

`pager-duty` is on every pager issue in the normal human process, so it cannot
be the trigger — labelling with it would commission an agent on everything.
`agent-fix` is explicit opt-in, and it is the one you add when you want the
agent to attempt a fix.

The fix engine is a separate system that lives in `action/` and is installed
into `sprint-tasks` separately. **It is not running just because this app is.**
See [`action/README.md`](../action/README.md) for installation and for what it
does once woken; none of it is re-documented here.

---

## 9. Things that will probably go wrong first

**`GET /slack/health` shows `missing_env`.** Exactly what it says. The most
common are `SLACK_APPROVER_USER_ID` (you copied a display name, not a `U…` id)
and `LANGGRAPH_API_URL`. Fix `.env`, restart `langgraph dev` — the health
endpoint reads names only, never values, so it is safe to paste the output.

**`/slack/health` 404s.** The custom HTTP app did not load. Check the `http`
block in `langgraph.json` and the `langgraph dev` startup log.

**Nothing appears in Slack.** In order: did you tick the notifier? Is the
thread actually parked (`/threads/<id>/state` shows a non-empty `interrupts`)?
Is the bot *in* the channel — `not_in_channel` is the usual answer. Is
`SLACK_CHANNEL_ID` a `C…` id rather than `#pager-approvals`?

**The Slack button does nothing, or Slack shows a dispatch failure.** Almost
always a stale tunnel URL: cloudflared was restarted and the Request URL in
the app config still points at the old hostname. Update it and click again —
the gate is still parked, nothing was lost.

**Slack signature mismatch (401 from `/slack/interactions`).** Three causes,
in order of likelihood: `SLACK_SIGNING_SECRET` is from a different app or is
actually the bot token; the server did not pick up an edited `.env`; your
laptop's clock is more than five minutes off, which makes every request look
like a replay. `date -u` against a known-good clock settles the third.

**"You are not an approver".** Your `U…` id is not in
`SLACK_APPROVER_USER_ID`. The message tells you the id it saw — paste that
one in. The gate is still waiting.

**Zoho 401 on the first fetch.** The refresh token is invalid, or it was
minted without `prompt=consent` and is not actually a refresh token. Mint a
fresh one. Note Zoho evicts refresh tokens at 20 active per client per user,
so an old one can stop working without anything being changed.

**Zoho 403 `OAUTH_ORG_MISMATCH`.** `ZOHO_ORG_ID` belongs to a different org
than the token. Not a 401, and not fixable by refreshing.

**Zoho 403 `SCOPE_MISMATCH` at send time only.** You have `Desk.tickets.WRITE`
instead of `UPDATE`. Re-mint with the scope list above.

**The manual trigger says `considered=0` / `errors=1 get_ticket failed`.** You
used the ticket *number* (`4242`) instead of the ticket *id*. Take the long
number from the ticket's URL.

**The manual trigger warns `ZOHO_TRANSPORT is 'fake'`.** You are triaging an
in-memory stub, not a real ticket. Set `ZOHO_TRANSPORT=rest` and restart.

**Gate 2 reported an issue that does not exist on GitHub.** `GITHUB_TOKEN` was
unset, so the fake issue client answered. Set it and re-run the ticket on a
fresh thread.

**The fix engine never wakes.** You added `pager-duty`, not `agent-fix`. Or the
workflow is not installed in `sprint-tasks` yet — see `action/README.md`.

---

## 10. Aborting, and what is left behind

**To stop everything:** Ctrl-C `langgraph dev`, Ctrl-C cloudflared. A parked
gate is not a running process — there is no timer anywhere in this codebase,
nothing auto-approves, and an unanswered gate simply stays unanswered.

What survives, and what does not:

| | Survives a `langgraph dev` restart? |
|---|---|
| The thread and its parked interrupt | Yes — `langgraph dev` persists locally |
| The reply / issue ledger | **No** — it is in-memory per process |
| The notifier's "already posted" record | Assume not — it lives in whatever store the dev server injects, and falls back to a per-process dict. A duplicate gate message is cosmetic: the first click on either consumes the interrupt and the other reports "already answered" |
| Anything already sent to Zoho or GitHub | Yes, obviously. Nothing undoes those |

The ledger being in-memory matters in one place: it is the *replay* guard on
`zoho_send_reply`. It is not the once-per-ticket mutex — that is the thread
insert, which does survive — but after a restart the "already replied" check
starts blank. With `REPLIES_ENABLED=false` this cannot cost you anything on
this run. Before the switch is ever turned on, the ledger needs a real store.

**To abandon one ticket** and be able to start it again cleanly:

```sh
curl -XDELETE localhost:2024/threads/<thread-id>
```

That drops the thread and its parked gate. The next `start_ticket` for the
same ticket id wins the claim again and triages from scratch. Any Slack
message already posted for the deleted thread is now dead — its buttons will
report "already answered" or fail the precondition. Delete or ignore it.

**To answer a gate without Slack** (Slack down, tunnel dead, message too long
to render):

```sh
# read what is being asked
curl -s localhost:2024/threads/<thread-id>/state | jq '.tasks[].interrupts[].value'

# approve
curl -s -XPOST localhost:2024/threads/<thread-id>/runs \
  -H 'content-type: application/json' \
  -d '{"assistant_id": "triage", "command": {"resume": {"approved": true}}}'

# reject with a reason the agent can act on
curl -s -XPOST localhost:2024/threads/<thread-id>/runs \
  -H 'content-type: application/json' \
  -d '{"assistant_id": "triage", "command": {"resume": {"approved": false, "reason": "Too technical. Drop the stack trace."}}}'
```

Only an explicit `{"approved": true}` is approval. `"yes"`, `true`, `{}` and
deepagents' own `{"decisions":[…]}` envelope are all read as rejections. This
path carries no identity — anyone who can reach the port can use it — which is
exactly why approvals normally go through Slack.
