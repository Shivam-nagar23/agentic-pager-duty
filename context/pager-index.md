# Pager bug history: where fixes have landed

Generated from closed `pager-duty` issues and the PRs that fixed them.
Use as a prior for where to look first, not as an answer.

## How to read the counts

`(3 tickets)` means **three separate pager tickets** in this area had a fix
touching that file *in that repo*. It is not a count of PRs or of file
appearances. This matters because `devtron-enterprise` is a hard fork of
`devtron`: most fixes land as two near-identical PRs on one ticket, so a bare
path count would double every mirrored fix and read as twice the evidence.
Files are therefore grouped by repo, and a mirrored fix counts once on each
side. Expect the same relative path under both repos — that is one incident,
and a fix will usually need a PR in each.

Excluded before counting: vendored dependencies, `go.mod`/`go.sum`,
`vendor/modules.txt`, generated `env_gen.*`, `CHANGELOG/`, `.github/`, and JS
lockfiles — build plumbing that was 56% of raw file hits. Also dropped: fix PRs
touching more than 12 non-plumbing files, which in this corpus are release
merges and whole-subsystem rewrites rather than targeted fixes. Both exclusions
are noted per area below.

Where several files in an area are tied at the same ticket count — most of
them are — they are ordered by how many pager tickets touched them anywhere in
the corpus, then by path. Ordering never depends on corpus order.

Areas: 19. Tickets attributed to an area: 116.

## CD

_5 ticket(s)_ · repos: devtron, devtron-enterprise, devtron-services, devtron-services-enterprise

### devtron
- `internal/sql/repository/bulkUpdate/BulkUpdateRepository.go` (2 tickets)
- `pkg/appStore/installedApp/service/AppStoreDeploymentService.go` (1 ticket)
- `pkg/bulkAction/BulkUpdateService.go` (1 ticket)

### devtron-enterprise
- `internal/sql/repository/bulkUpdate/BulkUpdateRepository.go` (2 tickets)
- `pkg/bulkAction/BulkUpdateService.go` (1 ticket)

### devtron-services
- `common-lib/utils/k8s/K8sUtil.go` (1 ticket)

> Dropped as a bulk change, not a targeted fix: https://github.com/devtron-labs/devtron-enterprise/pull/2075 (#1257, 15 non-plumbing files).

## CD (Blocking configs update)

_1 ticket(s)_ · repos: devtron, devtron-enterprise

### devtron-enterprise
- `api/bean/ConfigMapAndSecret.go` (1 ticket)
- `pkg/bean/configSecretData.go` (1 ticket)
- `pkg/infraConfig/config/infra_cs_config_ent.go` (1 ticket)

### devtron
- `api/bean/ConfigMapAndSecret.go` (1 ticket)
- `pkg/bean/configSecretData.go` (1 ticket)

## CD (Blocking new deployments)

_16 ticket(s)_ · repos: dashboard, devtron, devtron-enterprise, devtron-services, devtron-services-enterprise

### devtron-enterprise
- `pkg/appClone/AppCloneService.go` (3 tickets)
- `internal/sql/repository/CiArtifactsListingQueryBuilder.go` (2 tickets)
- `internal/sql/repository/deploymentConfig/repository.go` (2 tickets)
- `pkg/deployment/common/deploymentConfigService.go` (2 tickets)
- `wire_gen.go` (1 ticket)
- `pkg/pipeline/CdHandler.go` (1 ticket)
- `pkg/deployment/gitOps/git/GitOperationService.go` (1 ticket)
- `pkg/pipeline/DeploymentPipelineConfigService.go` (1 ticket)
- `internal/sql/repository/CiArtifactsListingQueryBuilder_test.go` (1 ticket)
- `internal/sql/repository/pipelineConfig/PipelineRepository.go` (1 ticket)
- _… 38 more file(s) not shown_

### devtron
- `internal/sql/repository/pipelineConfig/PipelineRepository.go` (1 ticket)
- `pkg/appClone/AppCloneService.go` (1 ticket)
- `pkg/appStore/installedApp/service/FullMode/deployment/InstalledAppGitOpsService.go` (1 ticket)
- `internal/sql/repository/deploymentConfig/repository.go` (1 ticket)
- `pkg/app/AppCrudOperationService.go` (1 ticket)
- `pkg/appStore/installedApp/read/InstalledAppReadEAService.go` (1 ticket)
- `pkg/appStore/installedApp/service/EAMode/InstalledAppDBService.go` (1 ticket)
- `pkg/appStore/installedApp/service/EAMode/deployment/EAModeDeploymentService.go` (1 ticket)
- `pkg/appStore/installedApp/service/common/AppStoreDeploymentCommonService.go` (1 ticket)
- `pkg/deployment/common/deploymentConfigService.go` (1 ticket)
- _… 6 more file(s) not shown_

### dashboard
- `src/components/app/details/triggerView/PipelineConfigDiff/usePipelineDeploymentConfig.ts` (1 ticket)

### devtron-services
- `ci-runner/helper/DockerHelper.go` (1 ticket)

## CD (Non-blocking but potential to impact Prod)

_6 ticket(s)_ · repos: dashboard, devtron, devtron-enterprise, devtron-services, devtron-services-enterprise

### devtron-services
- `kubewatch/pkg/informer/cluster/systemExec/helper.go` (1 ticket)
- `ci-runner/executor/stage/cdStages.go` (1 ticket)
- `ci-runner/executor/stage/ciStages.go` (1 ticket)
- `ci-runner/helper/EventHelper.go` (1 ticket)
- `ci-runner/helper/adaptor/CiStageAdaptor.go` (1 ticket)
- `kubelink/config/GlobalConfig.go` (1 ticket)
- `kubelink/pkg/service/commonHelmService/bean.go` (1 ticket)
- `kubelink/pkg/service/commonHelmService/k8sService.go` (1 ticket)
- `kubelink/pkg/service/commonHelmService/k8sService_benchmark_test.go` (1 ticket)
- `kubelink/pkg/service/commonHelmService/k8sService_integration_test.go` (1 ticket)
- _… 9 more file(s) not shown_

### devtron-enterprise
- `pkg/pipeline/CdHandler.go` (2 tickets)
- `pkg/eventProcessor/in/WorkflowEventProcessorService.go` (2 tickets)
- `pkg/cluster/environment/EnvironmentService.go` (1 ticket)
- `pkg/eventProcessor/bean/workflowEventBean.go` (1 ticket)

### devtron
- `pkg/pipeline/CdHandler.go` (2 tickets)
- `pkg/eventProcessor/in/WorkflowEventProcessorService.go` (2 tickets)
- `pkg/eventProcessor/bean/workflowEventBean.go` (1 ticket)

> Fix PRs whose file list could not be read: https://github.com/devtron-labs/devtron-fe-lib/pull/803.

## CD (Non-blocking)

_3 ticket(s)_ · repos: devtron, devtron-enterprise, devtron-services-enterprise

### devtron-enterprise
- `pkg/appWorkflow/AppWorkflowService.go` (1 ticket)
- `pkg/pipeline/DeploymentPipelineConfigService.go` (1 ticket)
- `internal/sql/repository/appWorkflow/AppWorkflowRepository.go` (1 ticket)
- `pkg/deployment/manifest/deploymentTemplate/read/chartEnvConfigOverride_ent.go` (1 ticket)

### devtron
- `pkg/appWorkflow/AppWorkflowService.go` (1 ticket)
- `pkg/pipeline/DeploymentPipelineConfigService.go` (1 ticket)
- `internal/sql/repository/appWorkflow/AppWorkflowRepository.go` (1 ticket)

## CI

_1 ticket(s)_ · repos: devtron, devtron-enterprise

### devtron
- `pkg/app/AppListingViewBuilder.go` (1 ticket)
- `pkg/auth/user/UserService.go` (1 ticket)
- `pkg/auth/user/helper/helper.go` (1 ticket)
- `pkg/pipeline/pipelineStageVariableParser.go` (1 ticket)

### devtron-enterprise
- `pkg/app/AppListingViewBuilder.go` (1 ticket)
- `pkg/auth/user/UserService.go` (1 ticket)
- `pkg/auth/user/helper/helper.go` (1 ticket)
- `pkg/pipeline/pipelineStageVariableParser.go` (1 ticket)

## CI (Blocking)

_12 ticket(s)_ · repos: devtron, devtron-enterprise, devtron-services, devtron-services-enterprise

### devtron-enterprise
- `wire_gen.go` (2 tickets)
- `client/gitSensor/GitSensorGrpcClient.go` (2 tickets)
- `pkg/pipeline/CiCdPipelineOrchestrator.go` (2 tickets)
- `cmd/external-app/wire_gen.go` (1 ticket)
- `api/restHandler/app/pipeline/configure/BuildPipelineRestHandler.go` (1 ticket)
- `api/helm-app/gRPC/applicationClient.go` (1 ticket)
- `client/gitSensor/GitSensorClient.go` (1 ticket)
- `client/gitSensor/GitSensorRestClient.go` (1 ticket)
- `internal/middleware/instrument.go` (1 ticket)
- `internal/sql/repository/pipelineConfig/CiPipelineRepository.go` (1 ticket)
- _… 9 more file(s) not shown_

### devtron-services
- `git-sensor/pkg/RepoManages.go` (3 tickets)
- `git-sensor/pkg/git/GitBaseManager.go` (2 tickets)
- `git-sensor/pkg/git/RepositoryManager.go` (2 tickets)
- `git-sensor/wire_gen.go` (2 tickets)
- `git-sensor/pkg/git/Util.go` (1 ticket)
- `image-scanner/pkg/security/ImageScanService.go` (1 ticket)
- `image-scanner/wire_gen.go` (1 ticket)
- `common-lib/utils/grpc/GrpcConfig.go` (1 ticket)
- `git-sensor/api/GrpcHandler.go` (1 ticket)
- `git-sensor/internals/Configuration.go` (1 ticket)
- _… 7 more file(s) not shown_

### devtron
- `client/gitSensor/GitSensorGrpcClient.go` (2 tickets)
- `pkg/pipeline/CiCdPipelineOrchestrator.go` (2 tickets)
- `wire_gen.go` (1 ticket)
- `api/helm-app/gRPC/applicationClient.go` (1 ticket)
- `client/gitSensor/GitSensorClient.go` (1 ticket)
- `client/gitSensor/GitSensorRestClient.go` (1 ticket)
- `pkg/bean/app.go` (1 ticket)
- `pkg/infraConfig/service/infraConfigService.go` (1 ticket)
- `pkg/infraConfig/units/bean/memory_unit_type.go` (1 ticket)

### devtron-services-enterprise
- `image-scanner/wire_gen.go` (1 ticket)
- `Makefile` (1 ticket)
- `image-scanner/App.go` (1 ticket)

> Not ranked: fixes in `protos` (#1956), which is outside the eight target repos. Noted rather than deleted — if pager fixes keep landing there, the target list is wrong.

## CI (Non blocking)

_10 ticket(s)_ · repos: dashboard, devtron, devtron-enterprise, devtron-fe-common-lib, devtron-services, devtron-services-enterprise

### devtron-services
- `ci-runner/Dockerfile` (2 tickets)
- `ci-runner/helper/DockerHelper.go` (1 ticket)
- `kubewatch/pkg/informer/cluster/systemExec/helper.go` (1 ticket)
- `ci-runner/executor/StageExecutor.go` (1 ticket)
- `image-scanner/pkg/security/ImageScanService.go` (1 ticket)
- `image-scanner/wire_gen.go` (1 ticket)
- `ci-runner/Dockerfile-v27` (1 ticket)
- `ci-runner/executor/StageExecutor_test.go` (1 ticket)
- `ci-runner/executor/scriptExecutor_test.go` (1 ticket)
- `ci-runner/executor/stage/cdStages_test.go` (1 ticket)
- _… 13 more file(s) not shown_

### devtron-services-enterprise
- `ci-runner/Dockerfile` (2 tickets)
- `ci-runner/helper/StageExecutor/StageExecutorExtended.go` (1 ticket)
- `image-scanner/wire_gen.go` (1 ticket)
- `casbin/wire_gen.go` (1 ticket)
- `chart-sync/wire_gen.go` (1 ticket)
- `ci-runner/Dockerfile-v27` (1 ticket)
- `git-sensor/Dockerfile` (1 ticket)
- `git-sensor/wire_gen.go` (1 ticket)
- `image-scanner/Wire.go` (1 ticket)
- `image-scanner/pkg/security/ImageScanServiceExtended.go` (1 ticket)
- _… 1 more file(s) not shown_

### devtron
- `internal/sql/repository/pipelineConfig/CiTemplateOverrideRepository.go` (1 ticket)
- `pkg/pipeline/CiMaterialConfigService.go` (1 ticket)

### devtron-enterprise
- `internal/sql/repository/pipelineConfig/CiTemplateOverrideRepository.go` (1 ticket)
- `pkg/pipeline/CiMaterialConfigService.go` (1 ticket)

### dashboard
- `src/css/base.scss` (1 ticket)

### devtron-fe-common-lib
- `src/Common/Common.service.ts` (1 ticket)

> Dropped as a bulk change, not a targeted fix: https://github.com/devtron-labs/devtron-services/pull/226 (#2095, 28 non-plumbing files).
> Fix PRs whose file list could not be read: https://github.com/devtron-labs/devtron-fe-lib/pull/655, https://github.com/devtron-labs/devtron-fe-lib/pull/839.

## CI/CD Plugins

_2 ticket(s)_ · repos: dashboard, devtron-services, devtron-services-enterprise

### dashboard
- `src/components/CIPipelineN/VariableDataTable/VariableDataTable.component.tsx` (1 ticket)
- `src/components/CIPipelineN/VariableDataTable/constants.ts` (1 ticket)
- `src/components/CIPipelineN/VariableDataTable/utils.tsx` (1 ticket)
- `src/components/CIPipelineN/VariableDataTable/validations.ts` (1 ticket)

### devtron-services
- `ci-runner/executor/StageExecutor.go` (1 ticket)
- `ci-runner/executor/stage/cdStages.go` (1 ticket)
- `ci-runner/executor/stage/ciStages.go` (1 ticket)
- `ci-runner/executor/util/envUtils.go` (1 ticket)

### devtron-services-enterprise
- `ci-runner/helper/StageExecutor/StageExecutorExtended.go` (1 ticket)

> Fix PRs whose file list could not be read: https://github.com/devtron-labs/devtron-fe-lib/pull/759.

## Deployment from Chart store

_1 ticket(s)_ · repos: devtron, devtron-enterprise

### devtron
- `pkg/appStore/installedApp/service/FullMode/deployment/InstalledAppGitOpsService.go` (1 ticket)
- `pkg/appStore/bean/bean.go` (1 ticket)
- `pkg/deployment/gitOps/git/GitOperationService.go` (1 ticket)
- `pkg/deployment/manifest/deploymentTemplate/chartRef/bean/bean.go` (1 ticket)

### devtron-enterprise
- `pkg/deployment/gitOps/git/GitOperationService.go` (1 ticket)
- `pkg/appStore/installedApp/service/FullMode/deployment/InstalledAppGitOpsService.go` (1 ticket)
- `pkg/appStore/bean/bean.go` (1 ticket)
- `pkg/deployment/manifest/deploymentTemplate/chartRef/bean/bean.go` (1 ticket)

> Dropped as a bulk change, not a targeted fix: https://github.com/devtron-labs/devtron-enterprise/pull/3235 (#2750, 49 non-plumbing files).

## Devtron dashboard completely down

_5 ticket(s)_ · repos: devtron, devtron-enterprise, devtron-services-enterprise

### devtron-enterprise
- `wire_gen.go` (1 ticket)
- `cmd/external-app/wire_gen.go` (1 ticket)
- `api/restHandler/ImageScanRestHandler.go` (1 ticket)
- `pkg/policyGovernance/security/imageScanning/repository/ImageScanResultRepository.go` (1 ticket)
- `api/connector/Connector.go` (1 ticket)
- `api/connector/connector_test.go` (1 ticket)
- `api/restHandler/OverviewRestHandler.go` (1 ticket)

### devtron
- `wire_gen.go` (1 ticket)
- `cmd/external-app/wire_gen.go` (1 ticket)
- `api/connector/Connector.go` (1 ticket)
- `api/connector/connector_test.go` (1 ticket)
- `internal/sql/repository/pipelineConfig/CiWorkflowRepository.go` (1 ticket)
- `pkg/pipeline/CiHandler.go` (1 ticket)

### devtron-services-enterprise
- `common-lib/utils/k8s/proxy/InterClusterServiceCommunicationManager.go` (1 ticket)
- `common-lib/utils/k8s/proxy/PortForwardManager.go` (1 ticket)

> Dropped as a bulk change, not a targeted fix: https://github.com/devtron-labs/devtron-enterprise/pull/2604 (#2023, 26 non-plumbing files).
> Dropped as a bulk change, not a targeted fix: https://github.com/devtron-labs/devtron-enterprise/pull/2589 (#2023, 25 non-plumbing files).

## Login issues

_2 ticket(s)_ · repos: devtron-services-enterprise

### devtron-services-enterprise
- `license-manager/pkg/auth/user/MagicLinkService.go` (1 ticket)
- `license-manager/pkg/auth/user/UserRegistrationService.go` (1 ticket)
- `license-manager/pkg/auth/user/UserService.go` (1 ticket)
- `license-manager/pkg/service/licenseService.go` (1 ticket)

> Dropped as a bulk change, not a targeted fix: https://github.com/devtron-labs/devtron-enterprise/pull/3254 (#2765, 19 non-plumbing files).

## Other CRITICAL Devtron functionality

_24 ticket(s)_ · repos: dashboard, devtron, devtron-enterprise, devtron-services, devtron-services-enterprise

### devtron-enterprise
- `pkg/k8s/application/k8sApplicationService.go` (3 tickets)
- `api/k8s/application/k8sApplicationRestHandler.go` (2 tickets)
- `client/scoop/scoopClientGetter.go` (2 tickets)
- `wire_gen.go` (1 ticket)
- `cmd/external-app/wire_gen.go` (1 ticket)
- `internal/sql/repository/CiArtifactsListingQueryBuilder.go` (1 ticket)
- `client/scoop/scoopClient.go` (1 ticket)
- `internal/sql/repository/CiArtifactsListingQueryBuilder_test.go` (1 ticket)
- `pkg/cluster/environment/EnvironmentService.go` (1 ticket)
- `pkg/k8s/proxy/InterClusterServiceCommunicationManager.go` (1 ticket)
- _… 28 more file(s) not shown_

### dashboard
- `.env` (2 tickets)
- `src/components/app/details/appDetails/utils.tsx` (2 tickets)
- `src/config/constants.ts` (2 tickets)
- `src/index.tsx` (2 tickets)
- `.env.development` (1 ticket)
- `.env.production` (1 ticket)
- `config.md` (1 ticket)
- `src/Pages/GlobalConfigurations/Authorization/Shared/components/AppPermissions/AppOrJobSelector.tsx` (1 ticket)
- `src/Pages/GlobalConfigurations/Authorization/Shared/components/AppPermissions/AppPermissions.component.tsx` (1 ticket)
- `src/Pages/GlobalConfigurations/Authorization/authorization.service.ts` (1 ticket)
- _… 20 more file(s) not shown_

### devtron-services-enterprise
- `common-lib/utils/k8s/proxy/InterClusterServiceCommunicationManager.go` (1 ticket)
- `common-lib/utils/k8s/proxy/PortForwardManager.go` (1 ticket)
- `common-lib/utils/k8s/proxy/bean/bean.go` (1 ticket)
- `kubelink/pkg/service/commonHelmService/ResourceTreeService.go` (1 ticket)
- `resource-optimizer/fetchAllEnv/fetchAllEnv.go` (1 ticket)
- `resource-optimizer/main.go` (1 ticket)
- `resource-optimizer/pkg/asyncProvider/asyncProvider.go` (1 ticket)
- `resource-optimizer/pkg/asyncProvider/wire_AsyncProvider.go` (1 ticket)
- `resource-optimizer/pkg/cluster/adapter.go` (1 ticket)
- `resource-optimizer/pkg/cluster/repository.go` (1 ticket)
- _… 7 more file(s) not shown_

### devtron
- `wire_gen.go` (1 ticket)
- `cmd/external-app/wire_gen.go` (1 ticket)
- `api/cluster/EnvironmentRestHandler.go` (1 ticket)
- `api/cluster/EnvironmentRouter.go` (1 ticket)
- `client/grafana/GrafanaClient.go` (1 ticket)
- `cmd/external-app/wire.go` (1 ticket)
- `internal/sql/repository/NotificationSettingsRepository.go` (1 ticket)
- `pkg/cluster/ClusterServiceExtended.go` (1 ticket)
- `pkg/cluster/environment/EnvironmentService.go` (1 ticket)
- `pkg/cluster/environment/bean/bean.go` (1 ticket)
- _… 5 more file(s) not shown_

### devtron-services
- `kubewatch/pkg/informer/cluster/systemExec/helper.go` (1 ticket)
- `common-lib/timeRangeLib/constant.go` (1 ticket)
- `common-lib/timeRangeLib/validator.go` (1 ticket)
- `common-lib/utils/SqlUtil.go` (1 ticket)
- `kubewatch/main.go` (1 ticket)
- `kubewatch/pkg/cluster/ClusterRepository.go` (1 ticket)
- `kubewatch/pkg/informer/bean/bean.go` (1 ticket)
- `kubewatch/pkg/informer/cluster/systemExec/util.go` (1 ticket)

> Dropped as a bulk change, not a targeted fix: https://github.com/devtron-labs/athena-be/pull/442 (#2952, 128 non-plumbing files).
> Fix PRs whose file list could not be read: https://github.com/devtron-labs/devtron-fe-lib/pull/1201, https://github.com/devtron-labs/devtron-fe-lib/pull/717, https://github.com/devtron-labs/devtron-fe-lib/pull/974.

## Other CRITICAL functionality

_1 ticket(s)_ · repos: devtron-enterprise

### devtron-enterprise
- `pkg/clusterTerminalAccess/UserTerminalAccessService.go` (1 ticket)

## Other CRITICAL issue but potential to impact Prod

_5 ticket(s)_ · repos: dashboard, devtron, devtron-enterprise

### devtron-enterprise
- `pkg/k8s/application/k8sApplicationService.go` (1 ticket)
- `api/restHandler/ImageScanRestHandler.go` (1 ticket)
- `client/scoop/scoopClient.go` (1 ticket)
- `internal/sql/repository/pipelineConfig/PipelineRepository.go` (1 ticket)
- `pkg/k8s/proxy/InterClusterServiceCommunicationManager.go` (1 ticket)
- `pkg/policyGovernance/security/imageScanning/repository/ImageScanResultRepository.go` (1 ticket)
- `internal/util/ChartTemplateService.go` (1 ticket)
- `pkg/deployment/manifest/deploymentTemplate/DeploymentTemplateHistoryService.go` (1 ticket)
- `pkg/k8s/proxy/PortForwardManager.go` (1 ticket)
- `pkg/overview/SecurityOverviewService.go` (1 ticket)
- _… 7 more file(s) not shown_

### devtron
- `internal/sql/repository/pipelineConfig/PipelineRepository.go` (1 ticket)
- `api/restHandler/ImageScanRestHandler.go` (1 ticket)
- `internal/util/ChartTemplateService.go` (1 ticket)
- `pkg/deployment/manifest/deploymentTemplate/DeploymentTemplateHistoryService.go` (1 ticket)
- `pkg/overview/SecurityOverviewService.go` (1 ticket)
- `pkg/pipeline/history/repository/DeploymentTemplateHistoryRepository.go` (1 ticket)
- `pkg/policyGovernance/security/imageScanning/ImageScanService.go` (1 ticket)
- `pkg/policyGovernance/security/imageScanning/repository/ImageScanDeployInfoRepository.go` (1 ticket)
- `pkg/policyGovernance/security/imageScanning/repository/ImageScanResultRepository.go` (1 ticket)
- `scripts/devtron-reference-helm-charts/reference-chart-proxy/Chart.yaml` (1 ticket)
- _… 2 more file(s) not shown_

### dashboard
- `src/components/ApplicationGroup/Details/EnvCIDetails/EnvCIDetails.tsx` (1 ticket)
- `src/components/app/details/triggerView/workflow/nodes/TriggerLinkedCINode.tsx` (1 ticket)
- `src/config/routes.ts` (1 ticket)

## Other NON-CRITICAL Devtron functionality

_5 ticket(s)_ · repos: dashboard, devtron-enterprise, devtron-services, devtron-services-enterprise

### devtron-enterprise
- `wire_gen.go` (1 ticket)
- `api/k8s/application/k8sApplicationRestHandler.go` (1 ticket)
- `charts/devtron/templates/timescale-db.yaml` (1 ticket)
- `pkg/cluster/ClusterService.go` (1 ticket)
- `pkg/finops/adaptor/adapter.go` (1 ticket)
- `pkg/finops/mocks/repository/CostRepository.go` (1 ticket)
- `pkg/finops/repository/CostModel.go` (1 ticket)
- `pkg/finops/repository/CostRepository.go` (1 ticket)
- `pkg/finops/repository/bean.go` (1 ticket)
- `pkg/finops/service/CostHelper.go` (1 ticket)
- _… 5 more file(s) not shown_

### devtron-services
- `git-sensor/pkg/RepoManages.go` (1 ticket)
- `git-sensor/pkg/git/Util.go` (1 ticket)
- `git-sensor/internals/sql/WebhookEventDataMappingRepository.go` (1 ticket)
- `git-sensor/internals/sql/WebhookEventParsedDataRepository.go` (1 ticket)
- `git-sensor/internals/sql/mocks/WebhookEventDataMappingRepository.go` (1 ticket)
- `git-sensor/internals/sql/mocks/WebhookEventParsedDataRepository.go` (1 ticket)
- `git-sensor/pkg/RepoManages_test.go` (1 ticket)
- `git-sensor/pkg/git/WebhookEventService.go` (1 ticket)
- `git-sensor/pkg/git/WebhookEventService_test.go` (1 ticket)
- `git-sensor/pkg/git/WebhookHandler.go` (1 ticket)
- _… 2 more file(s) not shown_

### dashboard
- `src/components/v2/appDetails/k8Resource/nodeDetail/NodeDetailTabs/Manifest.component.tsx` (1 ticket)

## PANIC IN CODE

_12 ticket(s)_ · repos: devtron, devtron-enterprise, devtron-services

### devtron-enterprise
- `pkg/appWorkflow/AppWorkflowService.go` (2 tickets)
- `.nojekyll` (2 tickets)
- `api/restHandler/TelemetryRestHandler.go` (2 tickets)
- `index.html` (2 tickets)
- `wire_gen.go` (1 ticket)
- `pkg/k8s/application/k8sApplicationService.go` (1 ticket)
- `cmd/external-app/wire_gen.go` (1 ticket)
- `pkg/appClone/AppCloneService.go` (1 ticket)
- `pkg/pipeline/CdHandler.go` (1 ticket)
- `api/restHandler/app/pipeline/configure/BuildPipelineRestHandler.go` (1 ticket)
- _… 8 more file(s) not shown_

### devtron
- `pkg/pipeline/CdHandler.go` (2 tickets)
- `pkg/appWorkflow/AppWorkflowService.go` (2 tickets)
- `api/restHandler/TelemetryRestHandler.go` (2 tickets)
- `wire_gen.go` (1 ticket)
- `pkg/appClone/AppCloneService.go` (1 ticket)
- `pkg/pipeline/DeploymentPipelineConfigService.go` (1 ticket)
- `api/k8s/capacity/k8sCapacityRestHandler.go` (1 ticket)
- `pkg/bulkAction/service/BulkUpdateService.go` (1 ticket)
- `pkg/deployment/gitOps/git/GitServiceBitbucket.go` (1 ticket)
- `pkg/k8s/application/k8sApplicationService.go` (1 ticket)
- _… 2 more file(s) not shown_

### devtron-services
- `common-lib/middlewares/recovery.go` (1 ticket)

## Pre-CD (Non blocking)

_1 ticket(s)_ · repos: devtron-services, devtron-services-enterprise

### devtron-services
- `ci-runner/helper/DockerHelper.go` (1 ticket)

## RBAC Issues

_4 ticket(s)_ · repos: devtron, devtron-enterprise

### devtron
- `api/auth/user/UserRestHandler_ent.go` (1 ticket)
- `internal/sql/repository/app/AppRepository.go` (1 ticket)
- `pkg/auth/user/UserCommonService.go` (1 ticket)
- `util/rbac/EnforcerUtil.go` (1 ticket)
- `util/rbac/EnforcerUtilHelm.go` (1 ticket)
- `util/rbac/EnforcerUtilHelmObject_test.go` (1 ticket)

### devtron-enterprise
- `api/restHandler/app/pipeline/configure/BuildPipelineRestHandler.go` (1 ticket)
- `api/auth/user/UserRestHandler_ent.go` (1 ticket)
- `internal/sql/repository/app/AppRepository.go` (1 ticket)
- `util/rbac/EnforcerUtil.go` (1 ticket)
- `util/rbac/EnforcerUtilHelm.go` (1 ticket)
- `util/rbac/EnforcerUtilHelmObject_test.go` (1 ticket)

> Dropped as a bulk change, not a targeted fix: https://github.com/devtron-labs/devtron-enterprise/pull/2394 (#1848, 17 non-plumbing files).

