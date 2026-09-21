# Repo map

Context artifact for the localization agent. Answers: *given a pager bug report, which
repo is it in, what does that repo own, and how do I build and test it?*

Companion artifact: the past-PR index (`affected_area → hot files`, mined from closed
pager tickets). This document is the structural half; that one is the empirical half.

## Provenance

Everything below was read from the shallow clones in `workspace/` at these commits.
Nothing here is from memory or from the public internet.

| Repo | Branch | HEAD | Date |
|---|---|---|---|
| `devtron` | `main` | `72dcd49` | 2026-09-15 |
| `devtron-enterprise` | `main` | `c1e8992` | 2026-09-15 |
| `dashboard` | `develop` | `d4ebf10` | 2026-09-10 |
| `devtron-services` | `main` | `7f31e7b` | 2026-08-04 |
| `devtron-services-enterprise` | `main` | `b5fc8e5` | 2026-08-04 |

Note the branch asymmetry: the two Go orchestrator repos and the two services repos
are cloned on `main`, but **`dashboard` is cloned on `develop`**. The CI workflows in
all five repos gate on `main`, `develop`, `rc-*`, and `hotfix-*`, so more than one
branch is live at a time. Confirm with the repo owner which branch a pager fix should
target before branching.

Statements marked **[inferred]** were derived from directory/package names without a
corroborating ticket. Statements marked **[unknown]** could not be established from
the clones at all — treat those as questions for the repo owner, not as gaps to guess
past.

---

## 1. The five repos

### 1.1 `devtron` — OSS orchestrator

**Purpose.** The open-source Devtron orchestrator: the monolithic Go API server behind
the dashboard. It owns application and pipeline CRUD, CI/CD triggering, deployment
(GitOps via ArgoCD/FluxCD and Helm), RBAC enforcement, the chart store, user/SSO
management, notifications config, and the Kubernetes resource browser APIs. The README
describes the product as an "AI-Native Kubernetes Management Platform". It ships as two
binaries from one codebase: the full `devtron` image and the EA-only `hyperion` image
built from `cmd/external-app`.

**Language.** Go 1.25.0, module `github.com/devtron-labs/devtron`. Dependencies are
**vendored** (`vendor/` is committed), so builds work offline.

**Build.**
```
make build          # = clean + wire + go build -o devtron
make build-ea       # EA-only binary, delegates to ./cmd/external-app
make wire           # regenerate DI code; required after changing Wire.go
```
Two caveats a localization agent must know:
- `make build` does `include scripts/dev-conf/envfile.env` at the top of the Makefile.
  That file **is** committed (437 bytes) so the include succeeds, but `make build` also
  exports every var in it.
- `make wire` shells out to a `wire` binary that is not vendored. The repo's own
  `enterprise-repo-sync` workflow installs it with
  `go install github.com/google/wire/cmd/wire@latest`.
- For a pure "does my edit still compile" check, `go build ./...` avoids both the env
  file and the wire binary. Prefer it for verification; use `make build` only when
  reproducing the release artifact.

**Test.**
```
make test-all       # = make test-unit
make test-unit      # go test ./pkg/pipeline   <- this is ALL the Makefile runs
make test-integration   # needs docker:dind; runs the wire-nil checker
```
`make test-unit` only covers `./pkg/pipeline`. There are `_test.go` files across the
tree (e.g. `util/rbac/EnforcerUtilHelmObject_test.go`, `api/connector/connector_test.go`)
that the Makefile target never reaches. After a fix, run `go test ./<changed-package>/...`
explicitly rather than trusting `make test-all`.

**Top-level directories that matter.**

| Path | Holds |
|---|---|
| `api/` | HTTP layer: routers + rest handlers, grouped by domain (`auth/`, `restHandler/`, `helm-app/`, `k8s/`, `cluster/`, `connector/`, `webhook/`, `terminal/`) |
| `pkg/` | Business logic, the bulk of the repo. Domain packages: `pipeline/`, `deployment/`, `build/`, `appStore/`, `auth/`, `policyGovernance/`, `bulkAction/`, `plugin/`, `notifier/`, `eventProcessor/`, `variables/`, `k8s/`, `cluster/` |
| `internal/sql/repository/` | DB access layer (go-pg). Most "wrong data returned" bugs bottom out here |
| `util/` | Cross-cutting helpers. **`util/rbac/` is the RBAC enforcement helper layer** (`EnforcerUtil.go`, `EnforcerUtilHelm.go`) |
| `client/` | Outbound clients to the other services: `gitSensor/`, `lens/`, `argocdServer/`, `events/`, `grafana/`, `telemetry/`, `dashboard/`, `fluxcd/` |
| `cmd/external-app/` | The EA-only (`hyperion`) entrypoint with its own wire graph |
| `scripts/sql/` | Numbered up/down DB migrations — a schema change means a file here |
| `scripts/devtron-reference-helm-charts/` | The reference Helm charts users deploy with; chart-store and deployment-template bugs land here |
| `charts/devtron/` | The Helm chart that installs Devtron itself, including `devtron-bom.yaml` (the pinned image list for every component) |
| `manifests/` | Installer manifests and `version.txt` / `release.txt` |
| `specs/` | OpenAPI specs per domain |
| `tests/` | e2e, integration, api-spec-validation harnesses |
| `Wire.go`, `wire_gen.go`, `WiringNilCheck.go` | Google Wire DI graph. A new dependency means editing `Wire.go` and regenerating |
| `env_gen.json`, `env_gen.md` | Generated env-var catalogue (`make fetch-all-env`). Changing a config struct regenerates these — they show up in PR diffs as noise |

### 1.2 `devtron-enterprise` — enterprise orchestrator

**Purpose.** The enterprise build of the same orchestrator. It is a **hard fork of
`devtron` at the same module path**, carrying every OSS file plus the enterprise-only
subsystems: global policies and approvals, config drafts and protection, deployment
windows, artifact promotion, lock configuration, licensing, FinOps/cost, cluster
backup/upgrade, audit logs, drift detection, the Scoop integration, and the chat/AI
surfaces. See §2 for the exact relationship — it is the single most important fact in
this document.

**Language.** Go 1.25.0, module `github.com/devtron-labs/devtron` (**identical to OSS**).
Vendored.

**Build / test.** Same Makefile as OSS with two additions:
```
make build / make build-ea / make wire      # identical to OSS
make test-all / make test-unit              # identical: go test ./pkg/pipeline
make test-integration                       # slightly different dind invocation
make wire-nil-checker                       # extra target, not present in OSS
make dep-update-oss TARGET_BRANCH=<branch>  # repoint common-lib + authenticator
make dep-update-ent TARGET_BRANCH=<branch>  # repoint common-lib-private, scoop, license-manager
```

**Directories, beyond the OSS set above.**

| Path | Holds |
|---|---|
| `enterprise/api/`, `enterprise/pkg/` | Self-contained enterprise features: `artifactPromotionPolicy/`, `artifactPromotionApprovalRequest/`, `deploymentWindow/`, `drafts/`, `globalTag/`, `lockConfiguation/` (sic), `protect/`, `commonPolicyActions/`, `resourceScan/` |
| `pkg/globalPolicy/` | The global policy data manager + history + repository |
| `pkg/policyGovernance/` | Much wider than OSS: `approvalConfig/`, `artifactApproval/`, `artifactPromotion/`, `lockConfiguration/`, `plugin/`, `validator/`, plus the OSS `security/` |
| `pkg/finops/`, `pkg/currency/` | Cost/FinOps reporting (paired with the `cost-sync` service and TimescaleDB) |
| `pkg/auditLog/`, `pkg/operationAudit/`, `api/auditEnrichment/` | Enterprise audit trail |
| `pkg/clusterBackup/`, `pkg/clusterUpgrade/`, `pkg/InfrastructureInstallationService/` | Infrastructure management |
| `pkg/drift/`, `api/drift/` | Config drift detection |
| `licensing/` | License handler + license client (talks to `license-manager`) |
| `api/chat/`, `api/scoop/`, `api/featureFlag/`, `api/fileUploader/` | Enterprise API surfaces with no OSS counterpart |
| `cel/` | CEL expression evaluator (also present in OSS) |
| `scripts/sql-ent/`, `scripts/timescale/`, `scripts/seed/` | Enterprise-only migrations. **Enterprise schema changes go in `sql-ent/`, not `sql/`** |
| `util/rbac/accessManagerUtil/`, `util/rbac/filter/` | Enterprise RBAC extensions on top of the shared `util/rbac/` |

**CI.** `devtron-enterprise` runs `golangci-lint` on PRs (`--modules-download-mode=vendor`,
`--tests=false`, pinned to golangci-lint v1.54.0 / `--go=1.21` — note the lint config
still claims Go 1.21 while `go.mod` says 1.25.0). It also carries `claude.yml`,
`claude-code-review.yml`, `code-review.yml`, and `on-demand-review.yaml` workflows that
OSS does not have. OSS `devtron` has **no** lint or test workflow at all.

### 1.3 `dashboard` — React frontend

**Purpose.** The client-side web app for Devtron. React 19 + TypeScript, built with
Vite, served by nginx in production. It is the only frontend for both OSS and
enterprise; enterprise-only UI is not forked but loaded at runtime from an optional npm
package (see below). "Devtron dashboard completely down" as a pager symptom is usually
*not* this repo — see §3.

**Language.** TypeScript / React 19.2.4, Node `v24.14.1` (`.nvmrc`), Yarn 4.9.2
(`packageManager: yarn@4.9.2`). Package version 1.22.0.

**Build.**
```
yarn install --immutable   # what CI runs
yarn build                 # NODE_OPTIONS=--max_old_space_size=8192 vite build
yarn build-light           # same, no source maps
yarn build-k8s-app         # VITE_K8S_CLIENT=true build
yarn start                 # dev server (vite --open)
```
The 8 GB heap flag is not optional — the build OOMs on the default heap.

**Test / lint.**
```
yarn lint          # tsc --noEmit && eslint 'src/**/*.{js,jsx,ts,tsx}' --max-warnings 0
yarn test          # vitest
yarn test-coverage
```
**The CI workflow (`ci.yml`) runs only `yarn install --immutable` and `yarn lint`.** It
does not run `yarn test`. So `yarn lint` is the real gate: a type error or a single
ESLint warning fails the PR. Run `yarn lint` before proposing any dashboard change.

**Top-level directories that matter.**

| Path | Holds |
|---|---|
| `src/components/` | The legacy component tree — still where most code lives: `app/`, `cdPipeline/`, `ciPipeline/`, `CIPipelineN/`, `workflowEditor/`, `ApplicationGroup/`, `ResourceBrowser/`, `ClusterNodes/`, `globalConfigurations/`, `security/`, `notifications/`, `login/`, `gitOps/`, `dockerRegistry/`, `charts/`, `bulkEdits/`, `v2/` |
| `src/Pages/` | Newer page modules: `App/`, `Applications/`, `ChartStore/`, `GlobalConfigurations/`, `License/`, `Releases/`, `Shared/` |
| `src/Pages-Devtron-2.0/` | Devtron 2.0 domain routers: `ApplicationManagement/`, `Automation&Enablement/`, `InfrastructureManagement/`, `SecurityCenter/`, `Shared/` |
| `src/services/`, `src/config/` | API client layer and route constants (`src/config/routes.ts`) |
| `src/css/`, `src/assets/` | Styling and icons |
| `patches/` | `patch-package` patches applied on `postinstall` |

Two frontend facts that matter for localization:
- Shared UI comes from the npm package `@devtron-labs/devtron-fe-common-lib` (4.0.13-pre-1).
  A bug in a shared component is **not fixable in this repo** — it lives in the
  `devtron-fe-common-lib` repo, which is not cloned in `workspace/`. **[gap]**
- Enterprise-only UI comes from the optional `@devtron-labs/devtron-fe-lib`, loaded via
  `importComponentFromFELibrary(...)` from `src/components/common/helpers/Helpers.tsx`,
  which returns `null` when the package is absent. That repo is also not cloned. **[gap]**

### 1.4 `devtron-services` — OSS microservices monorepo

**Purpose.** A monorepo of the OSS supporting microservices that the orchestrator talks
to over gRPC/NATS/HTTP. **There is no top-level `go.mod`** — each subdirectory is its own
independent Go module with its own `go.mod`, `Makefile`, `Dockerfile`, and committed
`vendor/`. The top-level `Makefile` is a fan-out that `cd`s into each service.

**Language.** Go 1.25.0 (kubewatch is 1.25.7). All services vendored.

**Build.**
```
make                      # top level: dep-update-oss then build, every service in turn
make build                # fan-out: cd <svc> && $(MAKE), for chart-sync ci-runner
                          #   git-sensor kubelink kubewatch lens image-scanner
cd <service> && make      # build one service (this is what you want for a fix)
```
Per-service `make build` is typically `clean` + `wire` + `go build -o <name>`. `ci-runner`
skips wire and builds to `-o cirunner`. `kubewatch` uses
`CGO_ENABLED=0 GOOS=linux go build`.

**Test.** **[unknown — needs confirmation from the team.]** No service in this repo has
a `test` target in its Makefile, and there is no test workflow. The only Go CI is
`golangci-lint.yml`, which lints a fixed matrix —
`[common-lib, authenticator, chart-sync, kubewatch, git-sensor, kubelink, lens]` — with
`--tests=false`, i.e. test files are explicitly excluded from linting. Note `ci-runner`
and `image-scanner` are **not** in the lint matrix. `go test ./...` inside a service
directory is the obvious fallback but is unverified as a supported command here.

**The services.**

| Directory | Module | What it is |
|---|---|---|
| `authenticator/` | `github.com/devtron-labs/authenticator` | Auth/JWT/API-token library + dex init container. Consumed as a Go module by the orchestrator |
| `chart-sync/` | `github.com/devtron-labs/chart-sync` | Syncs Helm chart repositories into the Devtron DB |
| `ci-runner/` | `github.com/devtron-labs/ci-runner` | The container that executes CI, pre-CD and post-CD stages inside the cluster |
| `common-lib/` | `github.com/devtron-labs/common-lib` | Shared library — pubsub (NATS), blob storage, k8s helpers, `securestore/` (encryption/secrets), `middlewares/` (incl. `recovery.go`), telemetry, git-manager, imageScan |
| `git-sensor/` | `github.com/devtron-labs/git-sensor` | Watches git repositories and publishes commit/webhook material for CI |
| `image-scanner/` | `github.com/devtron-labs/image-scanner` | Container image vulnerability scanning |
| `kubelink/` | `github.com/devtron-labs/kubelink` | gRPC service that performs all Helm operations and app-status reads against target clusters |
| `kubewatch/` | `github.com/devtron-labs/kubewatch` | Kubernetes informer/watcher; publishes cluster + ArgoCD + workflow events to NATS. `pkg/informer/` is the heart of it |
| `lens/` | `github.com/devtron-labs/lens` | Deployment metrics / DORA-style analytics over git and release data |

`authenticator` and `common-lib` are **libraries, not deployed services** — the
orchestrator `require`s them and `replace`s them to this monorepo (§2.3).

### 1.5 `devtron-services-enterprise` — enterprise microservices monorepo

**Purpose.** Same shape: no top-level `go.mod`, one module per subdirectory, fan-out
Makefile. But the relationship to the OSS monorepo is **not** a fork — most of these are
thin wrapper modules that import the OSS service as a library and add enterprise
behaviour. See §2.3.

**Language.** Go 1.25.0. All vendored.

**Build.**
```
make / make build     # fan-out: casbin chart-sync ci-runner git-sensor kubelink
                      #   scoop image-scanner license-manager  (devtctl + common-lib commented out)
cd <service> && make
make dep-update-oss / dep-update-ent / dep-update-all TARGET_BRANCH=<branch>
```

**Test.** Only two services define a `test` target: `audit-logs`
(`go test -race ./... -coverpkg=./... -coverprofile=...` plus a `coverage` target) and
`cost-sync` (`go test ./...`). Everything else: **[unknown — needs confirmation from
the team.]** There is no lint workflow and no test workflow in this repo; CI is
`release.yaml` (goreleaser, `workdir: devtctl/`), `semantic.yml`, `pr-issue-validator`,
and `tag-patch-incre`.

**The services.**

| Directory | Module | Non-vendor `.go` files | What it is |
|---|---|---|---|
| `kubelink/` | `kubelink-enterprise` | 10 | Wrapper over OSS `kubelink` + `common-lib-private` (inter-cluster proxy, port-forward) |
| `git-sensor/` | `git-sensor-enterprise` | 6 | Wrapper over OSS `git-sensor` |
| `chart-sync/` | `chart-sync-enterprise` | 6 | Wrapper over OSS `chart-sync` |
| `ci-runner/` | `ci-runner-enterprise` | 8 | Wrapper over OSS `ci-runner` |
| `image-scanner/` | `image-scanner-enterprise` | 36 | Wraps OSS `image-scanner`, adds enterprise scanning |
| `common-lib/` | `common-lib-private` | 43 | Enterprise shared lib layered on OSS `common-lib`: ssh tunnel, k8s proxy, recommendations |
| `casbin/` | `casbin-enterprise` | 16 | Standalone Casbin authorization service. Does **not** import an OSS counterpart |
| `scoop/` | `scoop` | 73 | In-cluster agent (imported by the enterprise orchestrator as `github.com/devtron-labs/scoop`) |
| `license-manager/` | `license-manager` | 122 | Licensing service; `pkg/auth/` here is in the login path |
| `audit-logs/` | `audit-log` | 36 | Audit log sink incl. blob cold storage |
| `cost-sync/` | `devtron-services-enterprise/cost-sync` | 31 | Pulls cost data from OpenCost into PostgreSQL/TimescaleDB (pairs with `pkg/finops/`) |
| `resource-optimizer/` | `resource-optimizer` | 38 | Analyzes Prometheus/VictoriaMetrics pod metrics, recommends CPU/memory requests+limits |
| `devtctl/` | `devtron-cli/devtctl` | 145 | CLI binary, released via goreleaser |

The file counts are the tell: a 6-file "service" is a `main.go` plus wiring. A fix to
`git-sensor-enterprise` behaviour is almost always a fix in **OSS `git-sensor`** followed
by a version bump here (§2.3).

---

## 2. How the repos relate

### 2.1 `devtron` ↔ `devtron-enterprise`: a hard fork, continuously merged

This is established fact, not inference, from three independent pieces of evidence:

1. **Same module path.** Both `go.mod` files declare `module github.com/devtron-labs/devtron`.
   Enterprise does **not** `require` or `replace` the OSS repo — it *is* the OSS repo,
   with commits on top. There is no dependency edge to follow.
2. **Path-level superset.** Excluding `vendor/`, OSS tracks 5,500 files and enterprise
   6,416. **5,460 of the 5,500 OSS paths (99.3%) exist at the same path in enterprise**,
   with 956 enterprise-only files and only 40 OSS-only paths (a handful of files that
   moved or were renamed on the enterprise side). The READMEs are byte-identical.
3. **An automated merge workflow.** `devtron/.github/workflows/enterprise-repo-sync.yaml`
   runs on every push to OSS `main`: it clones both repos, adds OSS as a remote named
   `oss-devtron`, runs `git merge oss-devtron/main`, auto-resolves a `wire_gen.go`
   conflict by regenerating it with `wire`, **fails hard on any other conflict**, then
   opens a PR titled `SYNC: OSS sync for <sha>` against `devtron-enterprise` `main`.

**Consequences for localization — read these carefully:**

- **A bug in shared code needs two PRs, not one.** Merging into OSS alone leaves
  enterprise unfixed until the next sync PR lands (and that sync can conflict). The
  reference ticket sprint-tasks#2960 was fixed by
  [devtron#7034](https://github.com/devtron-labs/devtron/pull/7034) and
  [devtron-enterprise#3402](https://github.com/devtron-labs/devtron-enterprise/pull/3402),
  **with an identical four-file diff in each**. The corpus confirms this is the norm,
  not the exception: 12 of 41 tickets touched exactly the pair
  `(devtron, devtron-enterprise)`.
- **The file path is usually identical in both.** When localization lands on
  `pkg/foo/Bar.go` in one repo, check the same path in the other before assuming it is
  single-repo. The main exception is `enterprise/**` and the enterprise-only `pkg/`
  packages (`globalPolicy/`, `finops/`, `drift/`, `clusterBackup/`, …), which have no
  OSS counterpart at all.
- **Beware `*_ent.go`.** There are 48 files matching `*_ent.go` in OSS and 157 in
  enterprise. These are **same-path files whose bodies diverge**: OSS
  `api/auth/user/UserRestHandler_ent.go` and enterprise
  `api/auth/user/UserRestHandler_ent.go` both define
  `checkRBACForUserCreate`, but with **different signatures** — enterprise adds
  `accessRoleFilters` and `requestCanManageAllAccess` parameters and the file is 494
  lines against ~330 in OSS. This is the designated OSS/enterprise seam: shared callers
  live in the non-`_ent` file, the divergent implementation lives in `_ent.go`.
  **A patch written against the OSS `_ent.go` will not apply to enterprise and may not
  even compile there.** Write the enterprise diff separately.
- **Enterprise-only counts as single-repo.** 12 of 41 corpus tickets touched
  `devtron-enterprise` alone.

### 2.2 Where enterprise diverges most

Ranked by enterprise-only file count (excluding `vendor/`):
`pkg/` 591 · `api/` 93 · `enterprise/` 76 · `scripts/` 54 · `specs/` 49 · `internal/` 22 ·
`util/` 16 · `licensing/` 15 · `docs/` 13 · `client/` 12.

So the enterprise delta is overwhelmingly *new business logic in `pkg/`*, not a rewrite
of OSS logic. That is why same-path diffs work so often.

### 2.3 Orchestrators ↔ services: module `replace`, not vendoring of source

The orchestrator repos consume the services monorepos as **Go modules pointing at
subdirectories**, wired up with `replace`:

`devtron/go.mod`:
```
github.com/devtron-labs/authenticator => github.com/devtron-labs/devtron-services/authenticator v0.0.0-20260803100101-66fcb35e4b0e
github.com/devtron-labs/common-lib   => github.com/devtron-labs/devtron-services/common-lib   v0.0.0-20260803100101-66fcb35e4b0e
```

`devtron-enterprise/go.mod` adds three more:
```
github.com/devtron-labs/common-lib-private => github.com/devtron-labs/devtron-services-enterprise/common-lib
github.com/devtron-labs/license-manager    => github.com/devtron-labs/devtron-services-enterprise/license-manager
github.com/devtron-labs/scoop              => github.com/devtron-labs/devtron-services-enterprise/scoop
```

And the enterprise services do the same to their OSS counterparts —
`devtron-services-enterprise/kubelink/go.mod`:
```
require github.com/devtron-labs/kubelink v0.0.0-...
replace github.com/devtron-labs/kubelink => github.com/devtron-labs/devtron-services/kubelink v0.0.0-20260803100101-...
```
Same pattern for `git-sensor`, `chart-sync`, `ci-runner`, `image-scanner`, and for
`common-lib-private → common-lib`. `casbin-enterprise`, `scoop`, `license-manager`,
`resource-optimizer`, `cost-sync`, and `devtctl` have no OSS counterpart to wrap.

**Consequences for localization:**

- **A fix in `common-lib` or in an OSS service is not deployed by merging it.** The
  consumer's `go.mod`/`go.sum`/`vendor/` must be bumped to the new pseudo-version. Two
  corpus tickets show exactly this shape: a CI (Blocking) ticket changed
  `git-sensor/{go.mod,go.sum,vendor/modules.txt}` in **both** `devtron-services` and
  `devtron-services-enterprise`; a PANIC-IN-CODE ticket changed
  `common-lib/middlewares/recovery.go` in `devtron-services` and then
  `vendor/github.com/devtron-labs/common-lib/middlewares/recovery.go` + `go.mod`/`go.sum`
  in `devtron-enterprise`. The real fix is one file; the rest is propagation.
- **`make dep-update-oss` / `dep-update-ent` are the propagation commands.** Both take
  `TARGET_BRANCH` and run `go mod edit -replace=… @$(TARGET_BRANCH)` then
  `go mod tidy && go mod vendor`. They exist at the top level of both services repos, in
  each service, and in both orchestrators.
- **A changed `vendor/` path is never the root cause.** If localization lands on
  `vendor/github.com/devtron-labs/...`, translate it to the owning repo and fix it there.
- `devtron-services` also has `multi-gitter-pr-sync.yml`, which on PR-open fans out a
  version-bump PR across repos via `multi-gitter` + `.github/scripts/update-version.sh`
  (targeting a **Gitea** host, using `GITEA_TOKEN`). The exact set of repos it targets is
  in `.github/config/multi-gitter-config`, which is not present in the shallow clone.
  **[unknown — needs confirmation from the team.]**

### 2.4 Repos the pager touches that are NOT in `workspace/`

Derived from `data/corpus.json` fix-PR URLs and from `devtron/devtron-images.txt.source`:

| Repo / image | Evidence | Why it matters |
|---|---|---|
| `athena-be` | **4 fix PRs across 4 corpus tickets**; images `athena-api`, `athena-mcp` | Python (`components/chatbot/`, `components/devtron_mcp_engine/`, `components/llm_engine/`). This is the AI/MCP backend and it is the single largest file-churn source under "Other CRITICAL Devtron functionality". **The localization agent cannot reach it today.** |
| `notifier` | 1 fix PR; image `quay.io/devtron/notifier` | The notification delivery service. `devtron/pkg/notifier/` is only the *config* side |
| `silver-surfer` | image `quay.io/devtron/silver-surfer` | Listed in the pager vocabulary's microservice set; no code in either services repo |
| `devtron-fe-common-lib` | `dashboard/package.json` dependency | Shared frontend components — many dashboard bugs are actually here |
| `devtron-fe-lib` | `importComponentFromFELibrary` pattern | Enterprise-only frontend components |

Also named in the sprint-task microservice list but **not found** in either services
repo: **Central-API**, **App-Sync**. (`pkg/chartRepo/ManualAppSyncYaml.go` in the
orchestrator generates an app-sync job, which may be what "App-Sync" refers to — but
that is a guess, not a finding.) **[unknown — needs confirmation from the team.]**

---

## 3. Which repo owns what

Mapping the pager template's "Affected areas" vocabulary to repos and subsystems.

**Confidence key.** ✅ = at least one corpus ticket's fix PR actually changed files here.
🔶 = **[inferred]** from directory/package names only — no ticket evidence; correct me.
All rows previously marked 🔶 were reviewed by the repo owner on 2026-09-16 and are now ✅.
❓ = **[unknown]**.

| Affected area | Primary repo(s) | Where to look | Conf. |
|---|---|---|---|
| **Devtron dashboard completely down** | `devtron` + `devtron-enterprise` (**not** `dashboard`) | The one corpus ticket was a **backend startup/wiring failure**: `api/connector/Connector.go`, `wire_gen.go`, `cmd/external-app/wire_gen.go`, `env_gen.*`, `api/restHandler/{ImageScan,Overview}RestHandler.go`. If the UI is blank, suspect the orchestrator failing to boot (wire/DI, env parsing) before suspecting React. Only then `dashboard/src/index.tsx`, `src/App.tsx`, `src/components/common/navigation/NavigationRoutes.tsx` | ✅ |
| **Security issue (secrets leak/log/visible etc)** | `devtron-services/common-lib/securestore/` + `devtron` | **CONFIRMED by repo owner: this row means secret *handling* — secrets leaked, logged, or rendered visible — NOT image-scan findings.** Route to `devtron-services/common-lib/securestore/` (`EncryptionKeyService.go`, `AttributesRepository.go`) and `devtron/pkg/pipeline/ConfigMapService.go`. Do *not* route here for CVE/scan-result tickets; those are image scanning (`devtron/pkg/policyGovernance/security/imageScanning/`, `devtron-services/image-scanner/`) and arrive under a different area. | ✅ |
| **Policies** | `devtron-enterprise` (enterprise-only) | `pkg/globalPolicy/`, `pkg/policyGovernance/{approvalConfig,artifactApproval,artifactPromotion,lockConfiguration,plugin,validator}/`, `enterprise/api/{artifactPromotionPolicy,commonPolicyActions,deploymentWindow,lockConfiguation,protect,drafts}/`, `cel/EvaluatorService.go`. OSS `pkg/policyGovernance/` contains only `security/` — **CONFIRMED by repo owner: policy bugs are enterprise-only — ONE PR, not two.** This is the standing exception to the two-PR rule; do not open an OSS PR for a Policies ticket | ✅ |
| **Login issues** | `devtron` + `devtron-enterprise`, and `devtron-services*` | `pkg/auth/sso/`, `pkg/auth/authentication/UserAuthOidcHelper.go`, `api/auth/{sso,user}/`, `pkg/dex/`, `devtron-services/authenticator/`. Enterprise adds `devtron-services-enterprise/license-manager/pkg/auth/` (license check in the login path — evidenced) and `devtron-enterprise/licensing/`. **Caution:** one of the two corpus "Login issues" tickets was fixed entirely in `charts/devtron/templates/`, `manifests/`, `devtron-images.txt.source` — i.e. a **release/chart version bump, not a code fix** | ✅ |
| **RBAC issues** | `devtron` **and** `devtron-enterprise` (two PRs) | `util/rbac/{EnforcerUtil.go,EnforcerUtilHelm.go}`, `pkg/auth/authorisation/casbin/{Adapter.go,rbac.go,rbacpolicy.go}`, `api/auth/user/UserRestHandler_ent.go`, `internal/sql/repository/app/AppRepository.go`, `util/commonEnforcementFunctionsUtil/`. Enterprise adds `util/rbac/accessManagerUtil/` and `util/rbac/filter/`, plus the `casbin-enterprise` service. **Both RBAC tickets in the corpus produced identical diffs in both orchestrator repos** | ✅ |
| **CI (blocking)** / **CI (non blocking)** | `devtron-services` + `devtron-enterprise` | Pipeline config in the orchestrator: `pkg/pipeline/{CiHandler.go,CiService.go,BuildPipelineConfigService.go,CiCdPipelineOrchestrator.go}`, `pkg/build/`, `pkg/variables/` (scoped-variable resolution — evidenced). Execution side: `devtron-services/ci-runner/`, `devtron-services/git-sensor/pkg/git/` (evidenced), `devtron-services/kubewatch/pkg/informer/` for workflow status | ✅ |
| **CD (blocking new deployments)** | `devtron` + `devtron-enterprise` | `pkg/deployment/trigger/devtronApps/`, `pkg/deployment/gitOps/git/GitOperationService.go`, `pkg/deployment/manifest/`, `internal/sql/repository/CiArtifactsListingQueryBuilder.go`, and a matching pair of `scripts/sql/*.up.sql`/`.down.sql` migrations | ✅ |
| **CD (blocking configs update)** | `devtron` + `devtron-enterprise`; config *protection* is enterprise-only | `pkg/deployment/manifest/deploymentTemplate/`, `pkg/pipeline/ConfigMapService.go`, `pkg/config/`, `internal/sql/repository/chartConfig/`. Enterprise: `enterprise/api/drafts/`, `enterprise/api/protect/`, `enterprise/api/lockConfiguation/` | ✅ (owner reviewed, no correction) |
| **CD (non-blocking but potential to impact prod)** | `devtron-services` | The one corpus ticket was entirely `kubewatch/pkg/informer/` (9 files: `cluster/`, `argoCD/`, `argoWf/{ci,cd}/`, `systemExec/`) — i.e. app-status/event drift, not the deploy path itself | ✅ |
| **CD (non-blocking)** | `devtron-services-enterprise` / `devtron-services` | The one corpus ticket was `kubelink/` + vendored `common-lib-private/utils/k8s/proxy/` (`InterClusterServiceCommunicationManager.go`, `PortForwardManager.go`) — i.e. the real fix was in `devtron-services-enterprise/common-lib/`, propagated into kubelink's vendor dir | ✅ |
| **Pre-CD (blocking)** / **pre-CD (non blocking)** | `devtron` + `devtron-enterprise`, and `devtron-services*/ci-runner` | `pkg/deployment/trigger/devtronApps/preStageHandlerCode.go` **and** `preStageHandlerCode_ent.go` (both files exist in both repos — the `_ent.go` bodies differ). Actual stage execution is `ci-runner`. **CONFIRMED by repo owner: failures land in the orchestrator and `ci-runner` roughly evenly — search both, let ticket symptoms decide; there is no useful prior.** `preStageHandlerCode.go` is evidenced by a CD-blocking ticket; the pre-CD attribution itself is inferred from the filename | ✅ |
| **Bulk update** | `devtron` + `devtron-enterprise` | `pkg/bulkAction/{service,repository,adapter,bean}/`, `api/restHandler/BulkUpdateRestHandler.go` **and** `BulkUpdateRestHandler_ent.go`, `api/restHandler/BatchOperationRestHandler.go`, `specs/bulk/`, `specs/bulkEdit/`. Frontend: `dashboard/src/components/bulkEdits/` | ✅ (owner reviewed, no correction) |
| **App creation** | `devtron` + `devtron-enterprise` | `pkg/app/{AppCrudOperationService.go,AppListingService.go}` (+ `AppListingService_ent.go`), `pkg/appClone/`, `pkg/pipeline/DevtronAppConfigService.go`, `api/restHandler/app/`, `internal/sql/repository/app/AppRepository.go`. Frontend: `dashboard/src/components/app/` | ✅ (owner reviewed, no correction) |
| **Deployment from chart store** | `devtron` + `devtron-enterprise`, and `devtron-services/chart-sync` | Evidenced: `pkg/appStore/bean/bean.go`, `pkg/appStore/installedApp/service/FullMode/deployment/InstalledAppGitOpsService.go`, `pkg/deployment/gitOps/git/GitOperationService.go`, `pkg/deployment/manifest/deploymentTemplate/chartRef/bean/bean.go`. Chart-version bumps show up as a large `scripts/devtron-reference-helm-charts/reference-chart_X-Y-Z/` tree plus paired `scripts/sql/*_reference-X-Y-Z.{up,down}.sql`. Repo sync: `devtron-services/chart-sync/`, `pkg/chartRepo/` | ✅ |
| **CI/CD plugins** | `devtron` + `devtron-enterprise`, and `ci-runner` | `pkg/plugin/` (`GlobalPluginService.go`, `repository/`, `adaptor/`), `api/restHandler/GlobalPluginRestHandler.go`, `pkg/pipeline/PipelineStageService.go` (+ `PipelineStagePipelineConditions_test.go`, `PipelineStagePluginConditions_test.go` — these were touched by a real ticket, and the HEAD commits of both orchestrator repos are literally "support-trigger-conditions-pipeline-level"). Enterprise: `pkg/policyGovernance/plugin/`. Execution: `devtron-services/ci-runner/`. **CONFIRMED by repo owner: orchestrator and `ci-runner` split roughly evenly — search both.** | ✅ |
| **Other critical Devtron functionality** | Widest spread — **assume nothing** | Corpus: 11 tickets hitting 6 different repos. `athena-be` dominates by file count (`components/devtron_mcp_engine/`, `components/chatbot/`), then `devtron-enterprise` (`internal/sql/repository/NotificationSettingsRepository.go`, `pkg/notifier/NotificationConfigService.go`, `pkg/overview/ClusterOverviewService.go`), `devtron-services/kubewatch/`, `devtron-services-enterprise/resource-optimizer/`. **Notifications are the most frequent concrete subsystem in this bucket** (`pkg/notifier/`, `api/restHandler/NotificationRestHandler.go`, `client/events/`, plus the external `notifier` repo) | ✅ |
| **Other non-critical Devtron functionality** | Anything | Corpus: `devtron-enterprise/pkg/finops/` + `scripts/timescale/`, and `dashboard/src/components/v2/` | ✅ |
| **Other critical issue but potential to impact prod** | Anything | Corpus: image scanning (`pkg/policyGovernance/security/imageScanning/`, `pkg/overview/SecurityOverviewService.go`), chart templating (`internal/util/ChartTemplateService.go`, `reference-chart-proxy/`), and `dashboard/src/components/` | ✅ |

### 3.1 Vocabulary drift — read before matching on the area string

The affected-area strings in real tickets **do not match the template vocabulary
exactly**. Across the 41 corpus tickets:

- Casing differs: tickets say `RBAC Issues`, `CD (Blocking new deployments)`,
  `Deployment from Chart store`, `Other CRITICAL Devtron functionality`. Match
  case-insensitively.
- One value is **not in the template list at all**: `PANIC IN CODE` (1 ticket).
- **8 of 41 tickets (20%) have no affected-area value at all.** For those the map is
  useless and the past-PR index / ticket body is the only signal.
- 11 of 41 (27%) are tagged `Other CRITICAL Devtron functionality`, which is a catch-all
  spanning six repos. Roughly half the corpus is therefore in a bucket this table cannot
  narrow. **Do not let a confident-looking table row override the ticket text.**

### 3.2 Repo frequency baseline (41 corpus tickets, 71 fix PRs)

| Repo | Fix PRs | Notes |
|---|---|---|
| `devtron-enterprise` | 30 | Touched by more tickets than any other repo |
| `devtron` | 16 | Almost always alongside `devtron-enterprise` |
| `devtron-services` | 8 | |
| `devtron-services-enterprise` | 8 | |
| `athena-be` | 4 | **Not cloned** |
| `dashboard` | 4 | |
| `notifier` | 1 | **Not cloned** |

Repo-set per ticket: `(devtron, devtron-enterprise)` 12 · `(devtron-enterprise)` alone 12
· `(dashboard)` alone 3 · `(athena-be)` alone 3 · `(devtron-services-enterprise)` alone 3
· `(devtron-services, devtron-services-enterprise)` 3 · `(devtron-enterprise, devtron-services)` 2
· one 6-repo ticket · one `(dashboard, devtron, devtron-enterprise)` · one `(devtron-services)` alone.

**Prior for a new pager ticket: it involves `devtron-enterprise` (~73% of tickets), and
if it involves `devtron` at all it needs a second, near-identical PR in
`devtron-enterprise` (12 of 13 `devtron` tickets).**

---

## 4. Build/test cheat sheet

| Repo | Verify a change compiles | Verify tests |
|---|---|---|
| `devtron` | `go build ./...` (vendored, offline OK). `make build` also needs the `wire` binary | `go test ./<changed-pkg>/...`; `make test-unit` only covers `./pkg/pipeline` |
| `devtron-enterprise` | same | same; `make wire-nil-checker` additionally validates the DI graph |
| `dashboard` | `yarn install --immutable && yarn lint` — **this is the CI gate** | `yarn test` (vitest) — **not run by CI** |
| `devtron-services` | `cd <service> && go build ./...` or `make` | **[unknown]** — no `test` target anywhere; CI lints 7 of 9 services with `--tests=false` |
| `devtron-services-enterprise` | `cd <service> && go build ./...` or `make` | `make test` exists only in `audit-logs` and `cost-sync`; otherwise **[unknown]** |

Every Go module in all four Go repos has a committed `vendor/` directory, so
`go build`/`go test` work without network access and **must not** be run with
`-mod=mod`. Never run `go mod download`, `go mod tidy`, or `go mod vendor` inside
`workspace/` — those rewrite the clone.

Noise to expect in a Go diff and not to be alarmed by: `env_gen.json` / `env_gen.md`
(regenerated by `make fetch-all-env` whenever a config struct changes), `wire_gen.go`
(regenerated by `make wire`), and `vendor/modules.txt` + `vendor/**` (regenerated by
`make dep-update-*`).

---

## 5. Open questions for the repo owner

These are the gaps that would most improve this map. Each is a place where I found no
answer in the clones rather than a place where I guessed.

1. **Which branch should a pager fix target?** `devtron`/`devtron-enterprise`/services
   are cloned on `main`, `dashboard` on `develop`, and CI gates on `main`, `develop`,
   `rc-*`, `hotfix-*`. `devtron-enterprise` also has a `hotfix-main-pr.yaml` workflow.
   What is the actual pager branch convention?
2. **How do you test `devtron-services` and `devtron-services-enterprise`?** No Makefile
   `test` target exists outside `audit-logs` and `cost-sync`, and the lint matrix runs
   with `--tests=false`. Is `go test ./...` in a service directory the right command, or
   is testing done elsewhere?
3. **Should `athena-be` be cloned into `workspace/`?** It is the largest single source of
   changed files under "Other CRITICAL Devtron functionality" (4 PRs / 4 tickets) and
   the agent currently cannot see it. Same question for `notifier`,
   `devtron-fe-common-lib`, `devtron-fe-lib`, and `silver-surfer`.
4. **What are "Central-API" and "App-Sync"?** Both appear in the sprint-task microservice
   list; neither exists as a directory in either services repo. Is App-Sync
   `pkg/chartRepo/ManualAppSyncYaml.go`? Where does Central-API live?
5. **Is "Security issue (secrets leak/log/visible etc)" about secret handling or about
   image-scan findings?** They live in completely different places
   (`common-lib/securestore/` vs `pkg/policyGovernance/security/imageScanning/`) and the
   routing changes entirely depending on the answer.
6. **When a fix lands in OSS `common-lib` or an OSS service, who bumps the consumers?**
   Is the `multi-gitter-pr-sync` workflow expected to do it automatically, or does the
   pager fixer run `make dep-update-oss` by hand in each consumer? And is the Gitea
   target in that workflow still live?
7. **Should the agent open the `devtron-enterprise` PR itself, or wait for
   `enterprise-repo-sync` to carry the OSS fix across?** The sync workflow fails on any
   conflict outside `wire_gen.go`, and the corpus shows humans consistently opening both
   PRs by hand — but confirming this fixes the agent's default behaviour.
8. **The `*_ent.go` convention** — is "shared logic in the plain file, divergent logic in
   `_ent.go`" the intended rule, and is it reliable enough for the agent to use as a
   routing signal?
9. ~~Correct the 🔶 rows in §3.~~ **RESOLVED 2026-09-16.** Security = secret handling, not image scanning. Policies = enterprise-only, one PR (standing exception to the two-PR rule). Pre-CD and CI/CD plugins split evenly between orchestrator and `ci-runner` — search both. CD configs update / Bulk update / App creation reviewed, no correction needed.

---

*Sources: `workspace/{devtron,devtron-enterprise,dashboard,devtron-services,devtron-services-enterprise}`
(READMEs, `Makefile`s, `go.mod`, `package.json`, `.github/workflows/`, `devtron-images.txt.source`,
`charts/devtron/devtron-bom.yaml`, directory listings, `git ls-files` set comparison) and
`data/corpus.json` (41 closed pager tickets, 71 fix PRs).*
