import { CfnOutput, CfnParameter, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";

/** Manual, finite worker definition; grants no permissions and never runs a Task. */
export class StandardizationRuntimeStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^\d{12}$/.test(this.account)) {
      throw new Error("standardization runtime requires dev and an explicit verified account");
    }
    if (this.node.tryGetContext("principalProvisioningEnabled") !== "true") {
      throw new Error("standardization runtime requires principalProvisioningEnabled=true for coordinated rollout");
    }
    const reviewed = this.node.tryGetContext("standardizationTaskRoleArn");
    const rolePattern = new RegExp(`^arn:aws:iam::${this.account}:role/FoodtechMinimalDataFlow-[A-Za-z0-9-]*StandardizationWorkerTask[A-Za-z0-9-]*$`);
    if (typeof reviewed !== "string" || !rolePattern.test(reviewed)) {
      throw new Error("standardizationTaskRoleArn must be the reviewed existing data-flow worker role");
    }
    const taskRole = new CfnParameter(this, "TaskRoleArn", { type: "String", allowedValues: [reviewed],
      description: "Exact reviewed existing StandardizationWorkerTaskRoleArn output" });
    const execution = new CfnParameter(this, "ExecutionRoleArn", { type: "String",
      allowedPattern: `^arn:aws:iam::${this.account}:role/foodtech-standardization-execution-dev$`,
      description: "Separately reviewed existing ECR/log execution role; not created here" });
    const image = new CfnParameter(this, "ImageUri", { type: "String",
      allowedPattern: `^${this.account}\\.dkr\\.ecr\\.ap-southeast-2\\.amazonaws\\.com/foodtech/maintenance-dev@sha256:[a-f0-9]{64}$`,
      description: "Reviewed standardization workload manifest image URI, pinned by digest" });
    const logName = `/foodtech/${stage}/standardization`;
    const logMode = this.node.tryGetContext("standardizationLogGroupMode") ?? "create";
    if (logMode !== "create" && logMode !== "existing") throw new Error("standardizationLogGroupMode must be create or existing");
    const group = logMode === "existing" ? logs.LogGroup.fromLogGroupName(this, "ExistingLogs", logName)
      : new logs.LogGroup(this, "WorkerLogs", { logGroupName: logName,
        retention: logs.RetentionDays.ONE_WEEK, removalPolicy: RemovalPolicy.RETAIN });
    const variables = {
      FOODTECH_STANDARDIZATION_RUNTIME_ENABLED: "true", FOODTECH_PRINCIPAL_PROVISIONING_ENABLED: "true",
      FOODTECH_WORKER_ACCOUNT: this.account, FOODTECH_WORKER_STAGE: stage,
      FOODTECH_WORKER_TASK_ROLE_ARN: taskRole.valueAsString, FOODTECH_WORKER_MAX_SECONDS: "600",
      FOODTECH_DATA_TABLE: `foodtech-data-flow-${stage}`, FOODTECH_COOKING_TABLE: `foodtech-cooking-jobs-${stage}`,
      FOODTECH_AUTH_TABLE: `foodtech-auth-sessions-${stage}`,
      FOODTECH_ORDERED_QUEUE_URL: `https://sqs.ap-southeast-2.amazonaws.com/${this.account}/foodtech-ordered-${stage}.fifo`,
      FOODTECH_STANDARDIZATION_DLQ_URL: `https://sqs.ap-southeast-2.amazonaws.com/${this.account}/foodtech-standardizationdlq-${stage}.fifo`,
    };
    const task = new ecs.CfnTaskDefinition(this, "WorkerTask", {
      family: "foodtech-standardization-dev", requiresCompatibilities: ["FARGATE"], networkMode: "awsvpc",
      cpu: "256", memory: "512", taskRoleArn: taskRole.valueAsString, executionRoleArn: execution.valueAsString,
      runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
      containerDefinitions: [{ name: "worker", image: image.valueAsString, essential: true,
        user: "10001:10001", readonlyRootFilesystem: true, stopTimeout: 30,
        entryPoint: ["python", "-m", "foodtech_standardization_worker.runtime"],
        environment: Object.entries(variables).map(([name, value]) => ({ name, value })),
        logConfiguration: { logDriver: "awslogs", options: { "awslogs-group": group.logGroupName,
          "awslogs-region": this.region, "awslogs-stream-prefix": "worker" } } }],
    });
    new CfnOutput(this, "WorkerTaskDefinitionArn", { value: task.ref });
    new CfnOutput(this, "WorkerLogGroupName", { value: group.logGroupName });
  }
}
