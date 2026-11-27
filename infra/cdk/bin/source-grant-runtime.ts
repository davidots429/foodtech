import { CfnOutput, CfnParameter, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";

/** Manual registration only; the reviewed roles, catalog and image are external. */
export class SourceGrantRuntimeStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("source grant runtime requires dev and an explicit regional account");
    }
    if (this.node.tryGetContext("principalProvisioningEnabled") !== "true") {
      throw new Error("source grant runtime requires principalProvisioningEnabled=true for coordinated rollout");
    }
    const reviewedImage = this.node.tryGetContext("sourceGrantImageUri");
    const imagePattern = new RegExp(`^${this.account}\\.dkr\\.ecr\\.ap-southeast-2\\.amazonaws\\.com/foodtech/maintenance-dev@sha256:[a-f0-9]{64}$`);
    if (typeof reviewedImage !== "string" || !imagePattern.test(reviewedImage)) {
      throw new Error("sourceGrantImageUri must be the reviewed immutable maintenance workload image");
    }
    const reviewedSubject = this.node.tryGetContext("sourceGrantTargetSubject");
    if (typeof reviewedSubject !== "string" || !reviewedSubject.length || reviewedSubject.length > 128 || /[^A-Za-z0-9_-]/.test(reviewedSubject)) {
      throw new Error("sourceGrantTargetSubject must be an explicit valid reviewed subject");
    }
    this.templateOptions.description = "Manual finite source grant catalog cleanup definition; execution requires separate review and approval";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" },
      "com.aws.cloudformation.Context": {
        arch: "Existing maintenance image, catalog-only task role and separate ECR/log agent role",
        must: ["Registration does not start a task", "No schedule, service, network or IAM creation",
          "Image workload, effective roles, enrollment fence and owned trial fixtures require live verification"],
      } };
    const role = (name: string, suffix: string) => new CfnParameter(this, name, { type: "String",
      allowedPattern: `^arn:aws:iam::${this.account}:role/foodtech-source-grant-deletion-${suffix}-${stage}$`,
      description: `Separately reviewed existing source-grant ${suffix} role` });
    const taskRole = role("TaskRoleArn", "task"), executionRole = role("ExecutionRoleArn", "execution");
    const image = new CfnParameter(this, "ImageUri", { type: "String", allowedValues: [reviewedImage],
      description: "Exact reviewed maintenance image manifest URI; no mutable tag or implicit latest image" });
    const target = new CfnParameter(this, "TargetSubject", { type: "String", allowedValues: [reviewedSubject],
      description: "Exact reviewed principal for this manual pass; requires a target-aware worker image" });
    const logMode = this.node.tryGetContext("sourceGrantLogGroupMode") ?? "create";
    if (logMode !== "create" && logMode !== "existing") throw new Error("sourceGrantLogGroupMode must be create or existing");
    const logName = `/foodtech/${stage}/source-grant-deletion`;
    const group = logMode === "existing" ? logs.LogGroup.fromLogGroupName(this, "ExistingLogs", logName)
      : new logs.LogGroup(this, "WorkerLogs", { logGroupName: logName,
        retention: logs.RetentionDays.ONE_WEEK, removalPolicy: RemovalPolicy.RETAIN });
    if (logMode === "create") (group.node.defaultChild as logs.CfnLogGroup).addMetadata("com.aws.cloudformation.Context", {
      why: "Keep bounded worker counts for reconciliation after a failed pass",
      must: ["Retain logs after definition removal", "Never log account keys, grant IDs, scan cursors or source data"],
    });
    const variables = {
      FOODTECH_SOURCE_GRANT_DELETION_ENABLED: "true",
      FOODTECH_SOURCE_GRANT_DELETION_ROLE_ARN: taskRole.valueAsString,
      FOODTECH_SOURCE_GRANT_TARGET_SUBJECT: target.valueAsString,
      FOODTECH_AUTH_TABLE: `foodtech-auth-sessions-${stage}`,
      FOODTECH_SOURCE_ACCESS_TABLE: `foodtech-source-access-${stage}`,
      FOODTECH_DELETION_REGISTRY_TABLE: `foodtech-deletion-registry-${stage}`,
      FOODTECH_SOURCE_GRANT_PAGE_SIZE: "20", FOODTECH_SOURCE_GRANT_MAX_PAGES: "50",
      FOODTECH_SOURCE_GRANT_MAX_CHANGES: "20", FOODTECH_SOURCE_GRANT_MAX_SECONDS: "600",
    };
    const task = new ecs.CfnTaskDefinition(this, "WorkerTask", {
      family: `foodtech-source-grant-deletion-${stage}`, requiresCompatibilities: ["FARGATE"], networkMode: "awsvpc",
      cpu: "256", memory: "512", taskRoleArn: taskRole.valueAsString, executionRoleArn: executionRole.valueAsString,
      runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
      containerDefinitions: [{ name: "worker", image: image.valueAsString, essential: true,
        user: "10001:10001", readonlyRootFilesystem: true, privileged: false, stopTimeout: 30,
        linuxParameters: { capabilities: { drop: ["ALL"] } },
        entryPoint: ["python", "-m", "foodtech_business_api.source_grant_deletion_worker"],
        environment: Object.entries(variables).map(([name, value]) => ({ name, value })),
        logConfiguration: { logDriver: "awslogs", options: { "awslogs-group": group.logGroupName,
          "awslogs-region": this.region, "awslogs-stream-prefix": "worker" } } }],
    });
    task.addMetadata("com.aws.cloudformation.Context", {
      why: "Run the existing finite catalog cleanup command with separately scoped application and agent identities",
      must: ["Use container-role credentials and verify actual STS identity before data access",
        "Each catalog delete rechecks locked expired account and DELETING Registry",
        "No Auth/Registry mutation, Cognito deletion, source payload deletion or whole-account completion",
        "Zero exit with no eligible rows is not proof of fixture processing or deletion completion"],
    });
    new CfnOutput(this, "WorkerTaskDefinitionArn", { value: task.ref });
    new CfnOutput(this, "WorkerLogGroupName", { value: group.logGroupName });
  }
}
