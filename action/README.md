# The fix engine

Turns an `agent-fix`-labelled `sprint-tasks` issue into draft pull requests in the eight
Devtron code repositories — or, more often and by design, into a comment explaining why
it did not.

Four agent stages with hard gates between them:

```
issue labelled agent-fix
   │
   ├─ prepare        gh → ticket.md, with the fix-PR answer key redacted out
   ├─ clone          all eight repos, source only, no vendor/  (~150 MB)
   │
   ├─ 01 localize    read-only. which files own the broken behaviour?
   │      └── GATE   confidence == low, or no candidates → stop
   ├─ 02 rca         read-only. can the defect be explained? plan the change
   │      └── GATE   can_explain == false → stop
   ├─ clone build    materialise vendor/ for the 1–2 repos we will touch
   ├─ 03 fix         one session per repo, own diff each. build + test
   │      └── GATE   any repo not applied, or build red → stop
   ├─ 04 review      adversarial. tries to refute the fix
   │      └── GATE   verdict != upheld → stop
   └─ publish        branch → draft PR per repo → cross-link → comment on the issue
```

Every gate is a `jq` test in `bin/run.sh`, not a sentence in a prompt. The stage that
writes the fix has no GitHub token in its environment and no `gh` in its tool set, so it
cannot open a pull request even if it decides it should. Every stop still writes a full
report — the RCA, the candidate files, the failed attacks — and comments it on the
issue. "No speculative PRs" means no speculative PRs, not no output.

---

## Findings that shaped this, and where the original brief was wrong

**The Claude Code GitHub Action cannot open PRs in other repositories.** This was the
load-bearing question and the answer is unambiguous. `anthropics/claude-code-action@v1`
has no `repository` input — the complete `action.yml` input list is
`trigger_phrase`, `assignee_trigger`, `label_trigger`, `base_branch`, `branch_prefix`,
`branch_name_template`, `allowed_bots`, `allowed_non_write_users`,
`include_comments_by_actor`, `exclude_comments_by_actor`, `prompt`, `settings`,
`anthropic_api_key`, `claude_code_oauth_token`, the four
`anthropic_*federation*` inputs, `github_token`, `use_bedrock`, `use_vertex`,
`use_foundry`, `claude_args`, `additional_permissions`, `use_sticky_comment`,
`classify_inline_comments`, `use_commit_signing`, `ssh_signing_key`, `bot_id`,
`bot_name`, `track_progress`, `include_fix_links`,
`path_to_claude_code_executable`, `path_to_bun_executable`, `display_report`,
`show_full_output`, `plugins`, `plugin_marketplaces` — and its own security
documentation says the GitHub App token is "scoped specifically to the repository it's
operating in", that "each action invocation is limited to the repository where it was
triggered", and that it "cannot access other repositories".

You can pass your own `github_token`, and a PAT covering all eight repos would then be
in the agent's environment. That is the option this design specifically rejects: it puts
a credential that can push to `devtron-enterprise` inside the same process that is
writing an unreviewed authorisation patch. Instead, `gh` and `git push` run in a
separate workflow step, after the review gate, with a token the agent never sees.

**So this does not use the action at all.** Three reasons, in order of weight:

1. *The gates.* The action is one agent session. "A refuted fix stops the Action" and
   "cannot explain why this fix is correct is the stopping condition" can only be
   prompt instructions inside one session — and this project's own invariant is that a
   gate which is only an instruction is a gate the agent can walk past. Splitting the
   pipeline into four CLI sessions with `jq` between them makes each gate a property of
   the harness.
2. *Cross-repo.* Established above. A PAT is needed regardless, so the action's main
   convenience — minting a scoped App token — is not available to us anyway.
3. *The dry run.* The local harness and CI invoke the identical `bin/stage.sh`. If the
   workflow went through the action wrapper, iterating on a prompt locally would be
   testing something different from what runs in CI.

**`--max-turns` does not exist.** The action's documentation recommends
`--max-turns` in `claude_args` for cost control. Claude Code CLI 2.1.231 has no such
flag (`claude --help | grep max-turns` is empty). The bounds that do exist and that this
pipeline uses are `--max-budget-usd` per stage and `timeout-minutes` on the job.

**Everything else the brief said about the CLI checks out** on 2.1.231:
`--output-format json`, `--json-schema`, `--append-system-prompt`, `--tools`,
`--permission-mode`, `--max-budget-usd`, and `--setting-sources`. Confirmed by running
them: the envelope carries `structured_output` (the parsed object),
`total_cost_usd`, `num_turns`, `session_id`, and `permission_denials`.

**Two caveats on how hard the guardrails hold.**

- `--permission-mode bypassPermissions` skips permission prompts, so a `deny` rule is
  not something to rely on under it. The read-only stages therefore restrict tools
  rather than permissions: `--tools Read,Grep,Glob` cannot write a file or run a
  command, whatever the mode. The fix and review stages run under `acceptEdits`, where
  `deny` rules do apply.
- A Bash `deny` rule matches the command text the model writes. It does not match
  `/usr/bin/gh`, or `sh -c 'gh ...'`. `settings/fix.json` is a guardrail against an
  agent casually corrupting a vendored checkout, not a sandbox. The control that
  actually holds is the absent token.

**The no-merge-permission invariant needs a correction.** GitHub fine-grained PATs have
no separate "merge" permission — `pull_requests: write` is what lets you create a PR
*and* what lets you merge one. Token scope therefore cannot enforce it. What does
enforce it: **every PR is opened `--draft`, and a draft PR cannot be merged until a
human marks it ready.** Back that with branch protection on `main` in all eight repos
(require a review, and ideally a CODEOWNERS approval). Treat this as an open item for
the owner, not as something the workflow has solved.

---

## The clone problem

Measured against the clones already in `workspace/` (2,511 MB total):

| | MB | share | needed to *search*? | needed to *build*? |
|---|---:|---:|---|---|
| `vendor/` | 1,785 | 71% | no | yes |
| `docs/` + `assets/` | 296 | 12% | no | no |
| `.git` | 277 | 11% | no (shallow) | no |
| **everything else** | **~150** | **6%** | **yes** | yes |

The circularity in "clone only the repo you need" dissolves once you notice the
searchable projection of all eight repos is 150 MB, not 2.5 GB. So:

- **Tier 1, always:** all eight at `--depth 1 --filter=blob:none --sparse`, with
  `vendor/`, `docs/`, `assets/` and `node_modules/` excluded from the sparse set. A
  blobless partial clone never fetches a blob outside the sparse set, so the 1.8 GB of
  vendored dependencies is not downloaded at all. Localization sees every repo.
- **Tier 2, after the RCA names repos:** re-add `vendor/` for those repos only, which
  git fetches on demand. `devtron` + `devtron-enterprise`, the most common pair, costs
  332 MB at that point. For the services monorepos the RCA also names the *module*, so
  `devtron-services:git-sensor` materialises one module's vendor instead of all nine
  (637 MB → tens of MB).
- **`actions/cache`** on the tier-1 workspace, restored by prefix key, so the steady
  state is `git fetch --depth 1` per repo rather than eight clones. Credentials are
  never written into any `.git/config` — the token lives in a global `insteadOf` rule —
  so the cache and the uploaded artifact are safe.

Net: ~150 MB always, plus 175–340 MB in the common case, against ~2.5 GB naive.

---

## Layout

```
action/
  bin/
    lib.sh            layout, log/fail/halt, the pager-index slicer, per-repo build hints
    prepare.sh        gh issue → redacted ticket.md + area.txt
    clone.sh          `search` (tier 1) and `build <repo[:module]>` (tier 2)
    stage.sh          run ONE headless claude session → schema-checked JSON
    run.sh            the pipeline and all four gates
    publish.sh        the only script that writes to GitHub
    report.py         renders the PR body, the issue comment, and the stop comment
    dry-run.sh        local harness — any stage, no GitHub writes
    pin-to-ticket.sh  rewind the workspace to a closed ticket's pre-fix state
  prompts/            ← the product. Most of the quality is here.
    system-facts.md   appended to every stage's system prompt: the fork rule, the
                      services rule, the build traps, the standard of proof
    01-localize.md  02-rca.md  03-fix.md  04-review.md
  schemas/            one JSON Schema per stage; the CLI enforces them
  settings/           fix.json, review.json — Bash allow/deny for the writing stages
  workflows/
    pager-duty-fix.yml
```

`settings/*.json` carry no comments because Claude Code silently ignores a settings file
that fails validation in `-p` mode, and a silently-ignored deny list is worse than none.
What they do: deny `git push`, `gh`, `curl`, `go mod`/`go get` (which would rewrite a
vendored checkout and destroy the diff), `make build` (needs an unvendored `wire`
binary), `make dep-update-*`, and `yarn install`; allow `go build`/`vet`/`test`,
`yarn lint`, and read-only git.

### How the context artifacts are used

`context/repo-map.md` and `context/pager-index.md` are **not pasted into prompts
wholesale.** They sit on disk in the working directory and the prompts point at them by
path, so the agent reads the sections it needs.

The one exception is the past-PR index slice for the ticket's affected area, which
`lib.sh:index_slice` extracts deterministically (case-insensitively, since real tickets
say `RBAC Issues` and `PANIC IN CODE`) and inlines into the localize prompt. Leaving the
agent to find it made it optional; inlining it makes the prior unavoidable. When no
section matches — 20% of tickets carry no area at all — the prompt says so explicitly
rather than going quiet.

The prompt is blunt about how much the index is worth: counts are per-ticket-per-repo
and mostly 1, so a listed file is one prior incident. It is a prior over *search order*.
The prompt states that a file's presence in the index is never by itself grounds to list
it as a candidate, and its absence is never grounds to drop one.

---

## Dry-running locally

Everything runs from this repo with no GitHub writes. `PAGER_DRY_RUN=1` is forced.

Needs `bash` 4+ (`brew install bash` — macOS's `/bin/bash` is 3.2 and `lib.sh` will say
so), plus `jq`, `python3`, `gh`, `git` and `claude` on `PATH`.

```bash
# Read-only stages against the clones already in workspace/.
# Ticket text comes from data/corpus.json — no network, no gh auth.
action/bin/dry-run.sh --ticket 2960 --stage 01-localize --score
action/bin/dry-run.sh --ticket 2960 --stage 02-rca

# Iterate: edit action/prompts/01-localize.md, delete the stage output, re-run.
rm action/.runs/2960/01-localize.json && action/bin/dry-run.sh --ticket 2960 --stage 01-localize
```

Artifacts land in `action/.runs/<ticket>/`. Each stage leaves
`<stage>.prompt.md` (the exact text the agent received — read this first when a prompt
edit misbehaves), `<stage>.json` (the structured answer), and `<stage>.envelope.json`
(cost, turns, duration, permission denials).

`--score` prints `repo/path` lines in the form `tools/eval/score.py` consumes, so a
prompt edit can be scored against the corpus without leaving the harness.

**The writing stages need an expendable workspace.** `dry-run.sh` refuses to run
`03-fix` against this repo's `workspace/`, which is shared and read-only:

```bash
export PAGER_WORKSPACE=/tmp/pager-ws
action/bin/clone.sh search                                   # needs PAGER_GITHUB_TOKEN
action/bin/dry-run.sh --ticket 2960 --stage all              # full pipeline, publishes nothing
```

**Replaying a closed ticket needs a rewind.** The clones are at HEAD, which is *after*
the fix merged, so the RCA stage will correctly report that the defect is not present
and halt. That is the right answer and it never exercises stages 03–05. Rewind first:

```bash
PAGER_WORKSPACE=/tmp/pager-ws action/bin/pin-to-ticket.sh 2960   # → each fix PR's base commit
PAGER_WORKSPACE=/tmp/pager-ws action/bin/dry-run.sh --ticket 2960 --stage all
PAGER_WORKSPACE=/tmp/pager-ws action/bin/pin-to-ticket.sh --reset
```

### What one run costs

Observed on ticket 2960 (RBAC, Severity-1, the reference ticket), `opus`:

| Stage | Cost | Wall clock | Turns |
|---|---:|---:|---:|
| 01 localize | $2.33 | 4m 39s | 44 |
| 02 rca | $1.88 | 5m 34s | — |

Extrapolating, a full run that reaches a PR is roughly **$10–20 and 25–40 minutes**,
before GitHub Actions minutes. Per-stage caps are in `bin/stage.sh`
(`PAGER_BUDGET_LOCALIZE` and friends) and default to $4/$5/$10-per-repo/$6.

On that run localization returned all 8 ground-truth files (4 paths × 2 repos) in its
top 8, with 2 extra — recall 1.00, precision 0.80. **Do not quote that number.** It was
a plumbing smoke test: the clones are post-fix, and the past-PR index was built from a
corpus including 2960 itself. The honest measurement is `tools/eval/replay.py` with a
leave-one-out index, per the eval invariants in `CLAUDE.md`.

---

## Installing into `devtron-labs/sprint-tasks`

```bash
SPRINT=/path/to/sprint-tasks
mkdir -p "$SPRINT/.github/pager" "$SPRINT/.github/workflows"
cp -r action/bin action/prompts action/schemas action/settings "$SPRINT/.github/pager/"
mkdir -p "$SPRINT/.github/pager/context"
cp context/repo-map.md context/pager-index.md "$SPRINT/.github/pager/context/"
cp action/workflows/pager-duty-fix.yml "$SPRINT/.github/workflows/"
chmod +x "$SPRINT/.github/pager/bin/"*.sh
```

`context/` is copied rather than referenced because the workflow runs inside
`sprint-tasks`, which has no sibling `context/`. Re-copy it whenever the repo map or the
index is regenerated — `lib.sh` fails loudly if either file is missing, rather than
running without them.

### Secrets

| Secret | Scope | Used by |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude subscription token from `claude setup-token` — the same secret `claude.yml` uses in the code repos. Set `ANTHROPIC_API_KEY` instead to bill an API key; never both | the four agent stages |
| `PAGER_APP_ID` | the GitHub App's App ID | both token-minting steps |
| `PAGER_APP_PRIVATE_KEY` | the App's PEM private key | both token-minting steps |

**One App, two tokens, minted at different times.** The workflow calls
`actions/create-github-app-token@v3` twice: once before the agent stages with
`permission-contents/issues/metadata: read`, and once *after* them with
`contents`, `pull-requests` and `issues` at write. The write token therefore does not
exist in the environment of any step that runs an agent — the same property the two-PAT
design had, but with tokens that expire in an hour and are revoked when the job ends, so
there is nothing to rotate.

The App is granted write because publishing needs it; the `permission-*` inputs narrow
each minted token *below* that grant. Dropping them silently hands the fix stage a token
that can push to `devtron-enterprise`, which is exactly what this split exists to deny.

### Setting the App up

Create it **org-owned** (`devtron-labs` → Settings → Developer settings → GitHub Apps),
not under a personal account — a personal App dies with that account's access.

Repository permissions: **Contents** read+write, **Pull requests** read+write,
**Issues** read+write, **Metadata** read. Nothing else — in particular no Actions and no
Workflows, so the App cannot alter CI.

Install it on nine repositories: the eight code repos plus `sprint-tasks`. The
enterprise repos are private, so an org owner has to approve the installation.

The private key can mint a write token, so it is more powerful than either PAT was. It
must stay confined to the `with:` inputs of the two minting steps and never reach an
agent step's environment.

### Running it by hand

`workflow_dispatch` takes an issue number and a `dry_run` flag that defaults to **true**.
Use it against closed tickets first. With `dry_run` on, every stage runs, every artifact
is uploaded, and nothing is pushed or commented.

Automatic trigger: `issues.labeled` where the label is **`agent-fix`** — deliberately
not `pager-duty`, which the normal human process applies to every pager issue. `agent-fix`
is explicit opt-in and doubles as the rollout control: nothing runs until somebody labels
something. A human can add it to any existing pager issue, including ones the triage agent
never touched. Concurrency is
keyed per issue so a double-label cannot race.

---

## Design notes worth knowing before you change something

**Why one fix session per repository.** `devtron` and `devtron-enterprise` are a hard
fork at the same module path, and the `*_ent.go` files sit at *identical paths with
divergent bodies* — the same function with different signatures. A patch written against
one will not apply to the other. So the fix stage runs once per repo with only that
repo's checkout as its working directory, and each writes its own diff. There is no
cherry-pick or patch-replay step and there should not be one. (Localization on ticket
2960 found this unprompted: it reported the enterprise `EnforcerUtilHelm.go` has a call
site at lines 195-196 that OSS does not have.)

**Why the review stage sees the fix report.** It could be argued that showing the
reviewer the fix agent's self-assessment anchors it. The reverse choice was made
deliberately: line of attack 8 is "check whether `verified` is actually supported", and
`unsupported_claims` is a schema field. The reviewer's job includes auditing the claims,
not just the code.

**Why `applied: true` is not trusted.** After each fix stage, `run.sh` checks
`git status --porcelain` and halts if the tree is clean. An agent reporting a change it
did not make is caught there rather than at `gh pr create`.

**Why redaction is in the production path, not just the eval.** 119 of 135 corpus issue
bodies embed their own fix-PR URLs, because the PR template gets pasted into the issue
after the fix ships. On a live ticket that section is empty, so redacting costs nothing;
on a replay it is the difference between a measurement and a lookup. Same code path
either way means the replay tests what production does.

**Model selection.** Stages use the `opus` alias rather than a pinned model id, so they
track the current model without edits. Override per stage with `PAGER_MODEL_LOCALIZE`,
`PAGER_MODEL_RCA`, `PAGER_MODEL_FIX`, `PAGER_MODEL_REVIEW`.

## Open questions for the owner

1. **Branch protection on `main` in all eight repos.** The draft flag is the only
   mechanical block on merging an agent PR; a fine-grained PAT with
   `pull_requests: write` can merge. Required-review protection is the backstop the
   invariant actually needs.
2. **Is `agent-authored` an acceptable label to auto-create in eight repos?**
   `publish.sh` creates it if missing.
3. **`athena-be` and `notifier` have no recorded build commands.** The fix prompt tells
   the agent to inspect the repo and report exactly what it ran. Four corpus tickets
   landed in `athena-be`; if that continues, both deserve a row in repo-map §4.
4. **A GitHub App instead of two PATs.** Better shape — short-lived tokens, no rotation,
   per-repo scoping — but needs org admin. The two-token split is the stopgap.
5. **Should a refuted fix leave its branch pushed?** Today a refuted fix pushes nothing
   and the diff survives only as a workflow artifact, which expires in 14 days.
