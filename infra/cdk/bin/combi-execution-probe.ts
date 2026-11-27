import { CfnOutput, CfnParameter, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { Construct } from "constructs";

/** Manual read-only probe definition: no RunTask, network, IAM or new log group. */
export class CombiExecutionReadProbeStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Combi execution read probe requires dev and an explicit regional account");
    }
    const param = (id: string, pattern: string, description: string) =>
      new CfnParameter(this, id, { type: "String", allowedPattern: pattern, description });
    const role = param("TaskRoleArn", `^arn:aws:iam::${this.account}:role/foodtech-recipe-broker-dev$`, "Existing separately reviewed broker task identity");
    const execution = param("ExecutionRoleArn", `^arn:aws:iam::${this.account}:role/foodtech-meal-refresh-execution-dev$`, "Existing maintenance image/log execution role");
    const roleId = param("BrokerRoleId", "^[A-Z0-9]{16,128}$", "Independently checked immutable broker RoleId; re-created roles require a fresh review");
    const image = param("ImageUri", `^${this.account}\\.dkr\\.ecr\\.ap-southeast-2\\.amazonaws\\.com/foodtech/maintenance-dev@sha256:[a-f0-9]{64}$`, "Exact reviewed main manifest image containing the probe module");
    const probe = param("ProbeId", "^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$", "New reviewed UUIDv4; operator must first confirm absent EXEC/MODEL probe keys");
    const variables = {
      FOODTECH_COMBI_EXECUTION_READ_PROBE_ENABLED: "true", FOODTECH_STAGE: "dev",
      FOODTECH_COMBI_EXECUTION_PROBE_ACCOUNT: this.account,
      FOODTECH_COMBI_EXECUTION_PROBE_ROLE_ID: roleId.valueAsString,
      FOODTECH_COMBI_EXECUTION_PROBE_ID: probe.valueAsString,
      PYTHONDONTWRITEBYTECODE: "1",
    };
    const task = new ecs.CfnTaskDefinition(this, "ProbeTask", {
      family: "foodtech-combi-execution-read-probe-dev", requiresCompatibilities: ["FARGATE"], networkMode: "awsvpc",
      cpu: "256", memory: "512", taskRoleArn: role.valueAsString, executionRoleArn: execution.valueAsString,
      runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
      containerDefinitions: [{ name: "worker", image: image.valueAsString, essential: true, user: "10001:10001",
        readonlyRootFilesystem: true, privileged: false, stopTimeout: 30,
        linuxParameters: { capabilities: { drop: ["ALL"] } },
        entryPoint: ["python", "-m", "foodtech_business_api.combi_execution_read_probe"],
        environment: Object.entries(variables).map(([name,value]) => ({ name,value })),
        logConfiguration: { logDriver: "awslogs", options: { "awslogs-group": "/foodtech/dev/meal-refresh",
          "awslogs-region": this.region, "awslogs-stream-prefix": "worker" } } }],
    });
    task.addMetadata("com.aws.cloudformation.Context", {
      why: "Prove GetItem projection/Select/direct-only authorization with the intended broker, not a local developer identity",
      must: ["No IAM, log group, network, schedule, service, secret, data write or model resources",
        "Use existing maintenance worker log path; no keys, fields, values or role sessions in output",
        "New UUIDv4 keys must be independently checked absent; unexpected data aborts before broader requests",
        "Fixed 180 second script deadline; 2 empty allowed reads and 6 exact AccessDenied cases; no SDK retries",
        "Digest-pinned reviewed main image and independently verified broker RoleId",
        "Actual definition application, RunTask and network require separate approval"], mutable: "review-required",
    });
    this.templateOptions.description = "Finite broker execution metadata authorization probe definition; existing roles and logs only, no automatic execution";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
    new CfnOutput(this, "ProbeTaskDefinitionArn", { value: task.ref });
  }
}
