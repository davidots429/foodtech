import { CfnOutput, CfnParameter, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";

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
    const image = new CfnParameter(this, "ImageUri", { type: "String",
      allowedPattern: `^${this.account}\\.dkr\\.ecr\\.ap-southeast-2\\.amazonaws\\.com/foodtech/maintenance-dev@sha256:[a-f0-9]{64}$`,
      description: "Reviewed maintenance manifest image URI containing strict account guards, pinned by digest" });
    const group = mealRefreshLogs(this, stage);
    const variables = {
      FOODTECH_MEAL_REFRESH_ENABLED: "true", FOODTECH_PRINCIPAL_PROVISIONING_ENABLED: "true",
      FOODTECH_MEAL_REFRESH_TASK_ROLE_ARN: taskRole.valueAsString,
      FOODTECH_MEAL_REFRESH_PAGE_SIZE: "25", FOODTECH_MEAL_REFRESH_MAX_PAGES_PER_SHARD: "100",
      FOODTECH_MEAL_REFRESH_MAX_SECONDS: "600",
      FOODTECH_COOKING_TABLE: `foodtech-cooking-jobs-${stage}`,
      FOODTECH_AUTH_TABLE: `foodtech-auth-sessions-${stage}`,
    };
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
    new CfnOutput(this, "WorkerTaskDefinitionArn", { value: task.ref });
    new CfnOutput(this, "WorkerLogGroupName", { value: group.logGroupName });
  }
}
