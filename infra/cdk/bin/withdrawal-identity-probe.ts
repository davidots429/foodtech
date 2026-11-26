import { CfnOutput, CfnParameter, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";

/** Shared path required by the already scoped ECS execution role. */
export function identityWorkerLogs(scope: Construct, stage: string): logs.ILogGroup {
  const mode = scope.node.tryGetContext("identityLogGroupMode") ?? "create";
  if (mode !== "create" && mode !== "existing") {
    throw new Error("identityLogGroupMode must be create or existing");
  }
  const name = `/foodtech/${stage}/withdrawal-identity`;
  return mode === "existing"
    ? logs.LogGroup.fromLogGroupName(scope, "ExistingIdentityLogs", name)
    : new logs.LogGroup(scope, "WorkerLogs", {
      logGroupName: name, retention: logs.RetentionDays.ONE_WEEK, removalPolicy: RemovalPolicy.RETAIN,
    });
}

/** Register a manual probe only. No RunTask, schedule, network or role creation. */
export class WithdrawalIdentityProbeStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^\d{12}$/.test(this.account)) {
      throw new Error("identity read probe requires dev and an explicit verified account");
    }
    const param = (name: string, pattern: string, description: string) =>
      new CfnParameter(this, name, { type: "String", allowedPattern: pattern, description });
    const taskRole = param("TaskRoleArn", `^arn:aws:iam::${this.account}:role/foodtech-withdrawal-identity-task-dev$`,
      "Exact reviewed existing identity task role");
    const execution = param("ExecutionRoleArn", `^arn:aws:iam::${this.account}:role/foodtech-withdrawal-identity-execution-dev$`,
      "Exact reviewed existing image and log execution role");
    const image = param("ImageUri", `^${this.account}\\.dkr\\.ecr\\.ap-southeast-2\\.amazonaws\\.com/foodtech/maintenance-dev@sha256:[a-f0-9]{64}$`,
      "Reviewed immutable image containing the read-only probe command");
    const probe = param("ProbeId", "^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$",
      "New UUIDv4 for non-business probe keys; never a real account/request identifier");
    const group = identityWorkerLogs(this, stage);
    const task = new ecs.CfnTaskDefinition(this, "ProbeTask", {
      family: "foodtech-withdrawal-identity-probe-dev", requiresCompatibilities: ["FARGATE"], networkMode: "awsvpc",
      cpu: "256", memory: "512", taskRoleArn: taskRole.valueAsString, executionRoleArn: execution.valueAsString,
      runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
      containerDefinitions: [{ name: "probe", image: image.valueAsString, essential: true,
        user: "10001:10001", readonlyRootFilesystem: true,
        entryPoint: ["python", "-m", "foodtech_business_api.withdrawal_identity_probe"],
        environment: [{ name: "FOODTECH_IDENTITY_READ_PROBE_ENABLED", value: "true" },
          { name: "FOODTECH_PROBE_ACCOUNT", value: this.account },
          { name: "FOODTECH_PROBE_ID", value: probe.valueAsString }],
        logConfiguration: { logDriver: "awslogs", options: { "awslogs-group": group.logGroupName,
          "awslogs-region": this.region, "awslogs-stream-prefix": "read-probe" } } }],
    });
    new CfnOutput(this, "ProbeTaskDefinitionArn", { value: task.ref });
    new CfnOutput(this, "IdentityLogGroupName", { value: group.logGroupName });
  }
}
