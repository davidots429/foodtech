import { CfnOutput, CfnParameter, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";

/** Compact canonical JSON prevents duplicate members or hidden normalization. */
export function reviewedMealRefreshTargets(raw: unknown): string {
  if (typeof raw !== "string" || !raw.length || raw.length > 8192) throw new Error("explicit bounded targets required");
  const values: unknown = JSON.parse(raw);
  if (!Array.isArray(values) || !values.length || values.length > 16 || JSON.stringify(values) !== raw)
    throw new Error("meal refresh targets must be compact canonical JSON with 1..16 entries");
  const seen = new Set<string>();
  for (const value of values) {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).sort().join() !== "meal_id,subject" ||
      typeof value.subject !== "string" || !value.subject.length || value.subject.length > 128 || /[^A-Za-z0-9_-]/.test(value.subject) ||
      typeof value.meal_id !== "string" || value.meal_id.length !== 36 || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value.meal_id))
      throw new Error("invalid meal refresh target");
    const key = `${value.subject}/${value.meal_id}`;
    if (seen.has(key)) throw new Error("duplicate meal refresh target");
    seen.add(key);
  }
  return raw;
}

/** Preserve the schedule's existing construct ID and make log ownership explicit. */
export function mealRefreshLogs(scope: Construct, stage: string): logs.ILogGroup {
  const mode = scope.node.tryGetContext("mealRefreshLogGroupMode") ?? "create";
  if (mode !== "create" && mode !== "existing") {
    throw new Error("mealRefreshLogGroupMode must be create or existing");
  }
  const name = `/foodtech/${stage}/meal-refresh`;
  return mode === "existing" ? logs.LogGroup.fromLogGroupName(scope, "ExistingMealRefreshLogs", name)
    : new logs.LogGroup(scope, "WorkerLogs", {
      logGroupName: name, retention: logs.RetentionDays.ONE_WEEK, removalPolicy: RemovalPolicy.RETAIN,
    });
}

/** Register one finite manual worker; no RunTask, schedule, network or IAM creation. */
export class MealRefreshRuntimeStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^\d{12}$/.test(this.account)) {
      throw new Error("meal refresh manual runtime requires dev and an explicit verified account");
    }
    if (this.node.tryGetContext("principalProvisioningEnabled") !== "true") {
      throw new Error("meal refresh manual runtime requires principalProvisioningEnabled=true");
    }
    const role = (name: string, kind: string) => new CfnParameter(this, name, {
      type: "String", allowedPattern: `^arn:aws:iam::${this.account}:role/foodtech-meal-refresh-${kind}-dev$`,
      description: `Reviewed existing meal refresh ${kind} role; not created here`,
    });
    const taskRole = role("TaskRoleArn", "task");
    const execution = role("ExecutionRoleArn", "execution");
    const rawTargets = this.node.tryGetContext("mealRefreshTargets");
    const targets = rawTargets === undefined ? undefined : reviewedMealRefreshTargets(rawTargets);
    const reviewedImage = this.node.tryGetContext("mealRefreshImageUri");
    const imagePattern = `^${this.account}\\.dkr\\.ecr\\.ap-southeast-2\\.amazonaws\\.com/foodtech/maintenance-dev@sha256:[a-f0-9]{64}$`;
    if (targets !== undefined && (typeof reviewedImage !== "string" || /\s/.test(reviewedImage) || !new RegExp(imagePattern).test(reviewedImage))) {
      throw new Error("targeted runtime requires the exact reviewed target-aware mealRefreshImageUri");
    }
    const image = new CfnParameter(this, "ImageUri", { type: "String",
      ...(targets === undefined ? { allowedPattern: imagePattern } : { allowedValues: [reviewedImage] }),
      description: "Reviewed maintenance manifest image URI containing strict account guards, pinned by digest" });
    const group = mealRefreshLogs(this, stage);
    const variables: Record<string, string> = {
      FOODTECH_MEAL_REFRESH_ENABLED: "true", FOODTECH_PRINCIPAL_PROVISIONING_ENABLED: "true",
      FOODTECH_MEAL_REFRESH_TASK_ROLE_ARN: taskRole.valueAsString,
      FOODTECH_MEAL_REFRESH_PAGE_SIZE: "25", FOODTECH_MEAL_REFRESH_MAX_PAGES_PER_SHARD: "100",
      FOODTECH_MEAL_REFRESH_MAX_SECONDS: "600",
      FOODTECH_COOKING_TABLE: `foodtech-cooking-jobs-${stage}`,
      FOODTECH_AUTH_TABLE: `foodtech-auth-sessions-${stage}`,
    };
    if (targets !== undefined) {
      const targetParameter = new CfnParameter(this, "RefreshTargets", { type: "String", allowedValues: [targets],
        description: "Exact reviewed subject/meal selection; requires a target-aware worker image" });
      variables.FOODTECH_MEAL_REFRESH_MODE = "targets";
      variables.FOODTECH_MEAL_REFRESH_TARGETS = targetParameter.valueAsString;
    }
    this.templateOptions.description = "Finite manual meal refresh definition; explicit execution approval required";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
    const task = new ecs.CfnTaskDefinition(this, "WorkerTask", {
      family: "foodtech-meal-refresh-dev", requiresCompatibilities: ["FARGATE"], networkMode: "awsvpc",
      cpu: "256", memory: "512", taskRoleArn: taskRole.valueAsString, executionRoleArn: execution.valueAsString,
      runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
      containerDefinitions: [{ name: "worker", image: image.valueAsString, essential: true,
        user: "10001:10001", readonlyRootFilesystem: true, privileged: false, stopTimeout: 30,
        linuxParameters: { capabilities: { drop: ["ALL"] } },
        entryPoint: ["python", "-m", "foodtech_business_api.meal_refresh_runtime"],
        environment: Object.entries(variables).map(([name, value]) => ({ name, value })),
        logConfiguration: { logDriver: "awslogs", options: { "awslogs-group": group.logGroupName,
          "awslogs-region": this.region, "awslogs-stream-prefix": "worker" } } }],
    });
    task.addMetadata("com.aws.cloudformation.Context", {
      why: targets === undefined ? "Register the existing bounded global pending sweep" : "Register an exact reviewed meal selection for manual reconciliation",
      must: ["Registration does not run tasks or create network, schedule or IAM",
        "Use target-aware reviewed image for targeted mode; older images may ignore targets",
        "Selection is not IAM isolation; dependent aggregate groups and other meal references are part of domain processing",
        "Never log subjects, meal IDs, keys, cursors or raw exceptions; empty success is not index or rollout completeness proof"],
    });
    new CfnOutput(this, "WorkerTaskDefinitionArn", { value: task.ref });
    new CfnOutput(this, "WorkerLogGroupName", { value: group.logGroupName });
  }
}
