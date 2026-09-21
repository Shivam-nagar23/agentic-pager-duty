# Agentic Pager Duty — Design

**Date:** 2026-09-16
**Status:** Approved, pending implementation plan

## Problem

Devtron's DevOps team handles customer support through Zoho Desk. Tickets fall into
three buckets: platform queries, ordinary Kubernetes issues, and platform bugs. A
platform bug becomes a sprint-tasks issue tagged `pager-duty`; a developer picks it
up, debugs, writes the fix, tests it, fills in the sprint ticket, and merges.

The expensive parts are triage and localization. A bug can live in any of eight
repositories, and finding *where* costs more developer time than writing the fix.

Reference ticket: [sprint-tasks#2960](https://github.com/devtron-labs/sprint-tasks/issues/2960)
— a Severity-1 RBAC bug (pager score 832) fixed by PRs into both `devtron` and
`devtron-enterprise`.

## Goal

Automate the chain from incoming Zoho ticket through to an open draft PR. Humans
keep two decisions: what gets said to a customer, and whether something is really
a platform bug. Merging stays human.

## Scope

**In:** Zoho triage, customer-reply drafting, pager classification, sprint issue
creation, bug localization, root-cause analysis, code fix, build + unit tests,
verification plan, draft PR.

**Out:** Merging. Live customer cluster access — debugging works from ticket text,
code reading, and reproduction, not from production logs or metrics.

**Repositories in scope:** `devtron`, `devtron-enterprise`, `dashboard`,
`devtron-services`, `devtron-services-enterprise`, `athena-be`, `notifier`,
`devtron-fe-common-lib`. Pager fixes target `main` in all of them. A bug may be in any of them,
so component localization is part of the problem, not an input to it.

## Architecture

Two systems joined at one seam: the sprint-tasks issue. That is already the handoff
in the human process, so it stays the handoff here.

```
  ZOHO DESK                          LANGGRAPH PLATFORM
  ┌─────────┐    cron ~2min    ┌──────────────────────────────┐
  │ tickets │ ───────────────► │  deep agent, 1 thread/ticket │
  └─────────┘                  │                              │
       ▲                       │  triage-analyst              │
       │                       │  responder                   │
       │  [GATE 1: send reply] │  pager-scribe                │
       └───────────────────────┤                              │
                               └──────────────┬───────────────┘
                                 [GATE 2: confirm platform bug]
                                              ▼
                               ┌──────────────────────────────┐
                               │ sprint-tasks issue           │
                               │ + pager-duty label           │
                               └──────────────┬───────────────┘
                                              ▼
  GITHUB ACTIONS                ┌──────────────────────────────┐
                                │ Claude Code                  │
                                │  localize → RCA → fix        │
                                │  go build / go test          │
                                │  push branch → draft PR      │
                                └──────────────┬───────────────┘
                                               ▼
                                     human reviews & merges
                                               │
                                     ┌─────────┴──────────┐
                                     │ back to Zoho:      │
                                     │ ticket updated     │
                                     └────────────────────┘
```

### Why this split

The two halves want opposite things. Triage is a judgment problem over unstructured
customer text with human approval points and long waits between turns — that is
LangGraph's shape: durable per-ticket state, resumable threads, `interrupt()` at
the gates. Fixing is a code problem needing a checkout, a Go toolchain, and a place
to run `go build ./...` — that is Claude Code's shape, and GitHub Actions already
has the repos and the credentials.

An important consequence of choosing GitHub Actions over a hosted sandbox: **no
enterprise source code moves anywhere new.** The Action runs where the code already
lives. LangGraph Platform only ever sees Zoho ticket text and its own state.

### LangGraph side

A single `deepagents` graph on LangGraph Platform, Python. **One thread per Zoho
ticket**, so a ticket parked for three days awaiting a human gate resumes with full
context on the managed Postgres checkpointer.

**Entry point:** a LangGraph Platform cron polls Zoho Desk every ~2 minutes and
opens a thread per unseen ticket. Polling rather than webhooks — Zoho would
otherwise need a public authenticated ingress, for no gain.

The main agent holds only the planning tool, the virtual filesystem, and `task`
delegation. It never reads source code directly. Three subagents:

| Subagent | Job |
|---|---|
| `triage-analyst` | Read ticket, thread, and attachments. Search docs and the past-ticket index. Classify: platform query / k8s issue / platform bug. |
| `responder` | Draft the customer reply for queries and k8s issues. |
| `pager-scribe` | Fill the sprint-tasks pager template: affected areas, impact %, prod/non-prod, client count, "why this is pager", repro steps, expected vs actual. |

Subagents pass work through the virtual filesystem (`ticket.md`, `triage.md`,
`draft_reply.md`, `pager_issue.md`), returning a short summary plus a filename.
The main agent's context stays thin and each ticket leaves a readable artifact trail.

### Human gates

Two, and **both are enforced inside the tools, not in the system prompt**. In a
single-deep-agent design the agent chooses its own route, so a gate that is only a
sentence of instruction is a gate it can walk past.

- `zoho_send_reply` calls `interrupt()` before sending. Agent drafts; a human sends.
- `create_sprint_issue` calls `interrupt()` with the proposed classification and the
  filled template. A human confirms before an issue is opened.

Everything after gate 2 runs unattended, including opening the draft PR.

### GitHub Actions side

A workflow in `devtron-labs/sprint-tasks`, triggered on `issues.labeled` with
`pager-duty`. It clones the eight code repositories with a PAT, hands Claude Code the
filled pager template, and runs: localize → root-cause analysis → fix → `go build` +
`go test` on touched packages → write a step-by-step verification plan for QA → push
a branch → open a draft PR in whichever repository the bug turned out to be in →
comment the RCA and PR link back on the sprint issue.

Guardrails:

- The PR is always `draft` and labelled `agent-authored`.
- The workflow token has **no merge permission**. A wrong auth fix therefore cannot
  become a merged auth change.
- Bounded iteration and a runner timeout.
- The PR body must state explicitly what was verified and what was not.
- **An adversarial self-review runs before the PR opens.** A second Claude Code pass
  gets the ticket, RCA, and diff and tries to refute the fix — wrong root cause, missed
  call path, auth regression, enterprise half not updated. Its verdict is recorded in
  the PR body, and a refuted fix stops the Action with a comment instead of opening a
  PR. This is the automated half of "the reviewer is the gate".

## Verification strategy

Build the affected service, run `go test` on touched packages, and write an explicit
reproduction and verification plan into the sprint ticket for QA. No live environment
is required, which suits the bug profile — most pager bugs here are RBAC or logic,
not visual.

**The reviewer is the gate, not the test suite** (confirmed by Shivam). Devtron's real
quality bar is a logical reviewer reasoning about the change: `devtron`'s `make test-unit`
runs only `go test ./pkg/pipeline` and `dashboard` CI never runs tests at all. Build and
tests are therefore a smoke check that the change compiles, not evidence it is correct.

What the agent optimizes for instead is a *reviewable* change — the RCA, the causal chain
from reported symptom to the specific line, an explicit statement of what was and was not
verified, and a verification plan QA can execute. The stopping condition shifts with it:
not "tests went red" but "cannot articulate why this fix is correct". That is the better
gate for the bug profile here, where a wrong auth fix compiles and passes everything.

This is deliberately weaker than a test that fails on `main` and passes with the
patch. Many RBAC and casbin bugs resist unit testing, and requiring one would stall
the agent on exactly the tickets that matter most. The draft-PR-plus-human-review
gate carries that weight instead.

## The past-PR index

The highest-leverage artifact, and it comes from data that already exists.

Every closed `pager-duty` issue in sprint-tasks links the PRs that fixed it. That is
a labelled dataset mapping *symptom* to *files changed*. A `gh`-driven script turns
it into `affected_area → historically touched files` — "RBAC Issues" resolves to the
casbin and enforcer paths, "ci (blocking)" to the CI orchestrator paths, and so on.

Two consumers: LangGraph's `triage-analyst` uses it to sharpen classification, and
Claude Code reads it as repository context so localization starts warm rather than
grepping seven large repos cold. It lives in the workflow repository next to a
`CLAUDE.md` describing the repo map and how to build each service.

## Error handling

- **Zoho unreachable:** cron skips the tick; no thread is opened. Tickets are picked
  up on the next successful poll. Polling is idempotent on ticket ID.
- **A gate is never answered:** the thread parks indefinitely on the checkpointer.
  No timeout auto-approves — an unanswered gate must never become an implicit yes.
- **Localization finds nothing:** the Action comments its RCA and candidate files on
  the issue and stops. It does not open a speculative PR.
- **Build or tests fail after a bounded number of fix attempts:** the Action comments
  the diff, the failure output, and its analysis, then stops. A human takes over with
  the work already done rather than from zero.
- **Wrong repository:** localization names a component; if the fix spans two repos
  (as the reference ticket did — `devtron` plus `devtron-enterprise`), the Action
  opens a draft PR in each and cross-links them on the issue.

## Post-v1: Slack approval for the gates

Both human gates are `interrupt()` calls inside tools, which makes the approval
surface swappable without touching the graph. Post-v1, gate requests render into a
Slack channel: the reviewer sees the ticket context and the exact proposed action —
the customer reply text, or the classification plus filled pager template — and
approves or rejects inline, resuming the parked thread.

Design consequences to honour in v1:

- The `interrupt()` payload is a structured dict (ticket id, gate type, proposed
  action, reasoning, Zoho link), not a prose string, so it renders into Slack blocks
  without re-parsing.
- Approval is an authorization decision, not a notification. Gate 1 sends text to a
  paying customer and gate 2 begins an unattended chain ending in an open PR, so the
  channel must be scoped and the approver's identity checked.
- Rejection carries a reason back into the thread so the agent revises rather than
  halting.

## Build order

Each phase is independently useful, and the first needs no new infrastructure.

1. **The GitHub Action alone**, run by hand against past pager tickets. Compare its
   RCA and file list against the PR that actually fixed each one. No Zoho work, no
   LangGraph, immediate signal on the hardest part of the problem.
2. **The past-PR index and `CLAUDE.md` repo map.** Re-run phase 1 and measure the
   change in localization accuracy.
3. **LangGraph Zoho triage**, ending at issue creation, with both gates live.
4. **Wire the seam.** The label triggers the Action; the PR link flows back to the
   Zoho ticket.

## Open questions

- **Zoho Desk MCP.** MCP is preferred over raw API calls. If no usable Zoho Desk MCP
  server exists, a thin MCP wrapper over their REST API is needed — small, but real
  work that belongs in the plan.
- **Exact API shapes.** The Claude Code GitHub Action and Agent SDK surfaces move.
  Confirm current call shapes from documentation at implementation time rather than
  writing them from memory.
- **Credentials.** Zoho OAuth, a GitHub PAT with access to all seven repositories
  including the private enterprise ones, and an Anthropic key. Storage and rotation
  to be settled in the plan.

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Scope | Full loop excluding merge | |
| Trigger | New Zoho ticket, full chain | |
| Agent structure | Single deep agent with subagents | Chosen over an explicit state machine |
| Fix engine | Claude Code GitHub Action | Repos and credentials already there; no new infra |
| LangGraph role | Zoho brain, gates, handoff | Each half does what it is best at |
| Runtime | LangGraph Platform (managed) | |
| Verification | Build + unit tests + written repro plan | |
| Human gates | Customer reply; bug classification | PR opening is unattended |
| Rollout | Straight to the two gates | No shadow-mode phase |

## Noted risk

Unattended draft PRs on Severity-1 RBAC bugs in infrastructure software carry real
risk: a plausible-but-wrong auth fix is a security hole that passes a green build.
This was raised and the decision to proceed was explicit. The mitigations in the
design are the always-draft PR, the `agent-authored` label, the no-merge-permission
token, and human review at merge.
