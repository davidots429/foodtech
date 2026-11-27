import { CfnCondition, CfnOutput, CfnParameter, Fn, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/** Separate finite Mock/collector definitions; never starts or schedules tasks. */
export class PrivateRestTrialStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^\d{12}$/.test(this.account) ||
        this.region !== "ap-southeast-2") {
      throw new Error("private REST trial requires dev and an explicit regional account");
    }
    if (this.node.tryGetContext("provisionDataFlow") === "true" &&
        this.node.tryGetContext("collectionInfrastructureMode") !== "deferred") {
      throw new Error("private REST trial must not duplicate legacy collection infrastructure");
    }
    this.templateOptions.description = "Separate bounded private REST trial Tasks, collector access and retained quarantine; no RunTask or network";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" },
      "com.aws.cloudformation.Context": { ref: [{ at: "infra/cdk/README.md", has: "Private REST trial preparation" }] } };
    const account = this.account, region = this.region;
    const arn = (service: string, resource: string) => `arn:aws:${service}:${region}:${account}:${resource}`;
    const regional = { "aws:RequestedRegion": region };
    const trust = () => new iam.ServicePrincipal("ecs-tasks.amazonaws.com", { conditions: {
      StringEquals: { "aws:SourceAccount": account },
      // ECS does not support restricting this trust to a particular cluster.
      ArnLike: { "aws:SourceArn": arn("ecs", "*") },
    } });
    const image = (name: string, repository: string) => new CfnParameter(this, name, {
      type: "String", allowedPattern: `^${account}\\.dkr\\.ecr\\.ap-southeast-2\\.amazonaws\\.com/foodtech/${repository}-dev@sha256:[a-f0-9]{64}$`,
      description: `Reviewed immutable ${repository} image with private REST trial capabilities`,
    });
    const mockImage = image("MockImageUri", "mock"), collectorImage = image("CollectorImageUri", "collection");
    const mockCommand: unknown = JSON.parse(readFileSync(join(__dirname, "../../containers/private-rest-trial-command.json"), "utf8"));
    if (!Array.isArray(mockCommand) || !mockCommand.every(value => typeof value === "string")) {
      throw new Error("private REST Mock command must be a string array");
    }
    // Provision/populate the two independent Secrets under a separate reviewed
    // procedure. No plaintext, generated credential, or Secret creation here.
    const secret = (name: string, suffix: string) => new CfnParameter(this, name, {
      type: "String", noEcho: true, allowedPattern: `^arn:aws:secretsmanager:${region}:${account}:secret:foodtech/dev/private-rest-${suffix}-[A-Za-z0-9]{6}$`,
      description: `Complete ARN of separately provisioned private REST ${suffix} Secret`,
    });
    const key = (name: string) => new CfnParameter(this, name, { type: "String", noEcho: true,
      allowedPattern: `^arn:aws:kms:${region}:${account}:key/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$`,
      description: "Exact customer-managed key protecting the matching Secret; decrypt context is restricted",
    });
    const mockSecret = secret("MockSecretArn", "mock"), collectorSecret = secret("CollectorSecretArn", "collector");
    const materialVersion = new CfnParameter(this, "MaterialVersionId", { type: "String",
      allowedPattern: "^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$",
      description: "Verified initializer request UUID used as the exact VersionId in BOTH Secrets; no AWSCURRENT fallback",
    });
    const mockKey = key("MockSecretKeyArn"), collectorKey = key("CollectorSecretKeyArn");
    const writes = new CfnParameter(this, "QuarantineWritesEnabled", {
      type: "String", allowedValues: ["false", "true"], default: "false",
      description: "false explicitly denies collector PutObject; changing to true requires reviewed retry approval",
    });
    const blockWrites = new CfnCondition(this, "QuarantineWritesBlocked", {
      expression: Fn.conditionEquals(writes.valueAsString, "false"),
    });
    const collector = new iam.Role(this, "CollectorRole", { roleName: "foodtech-collection-task-dev",
      description: "Read-only equipment collector with scoped pipeline writes; no secret retrieval", assumedBy: trust() });
    const mock = new iam.Role(this, "MockRole", { roleName: "foodtech-rest-mock-task-dev",
      description: "Synthetic REST Mock with no application AWS permissions", assumedBy: trust() });
    const bucket = new s3.Bucket(this, "Quarantine", {
      bucketName: `foodtech-ingestion-quarantine-${account}-dev`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true, objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      removalPolicy: RemovalPolicy.RETAIN, autoDeleteObjects: false, versioned: false,
    });
    bucket.addToResourcePolicy(new iam.PolicyStatement({ sid: "DenyTrialWritesUntilApprovedRetry",
      effect: iam.Effect.DENY, principals: [new iam.AnyPrincipal()], actions: ["s3:PutObject"],
      resources: [bucket.arnForObjects("ingestion/*")],
      conditions: { ArnEquals: { "aws:PrincipalArn": collector.roleArn } },
    }));
    // Conditionalize only the deliberate trial denial. The permanent TLS deny
    // and IAM restrictions must survive the enabled-writes stage unchanged.
    const bucketPolicy = bucket.policy!.node.defaultChild as s3.CfnBucketPolicy;
    const policy = bucketPolicy.policyDocument as { toJSON(): { Statement: unknown[] } };
    const resolved = policy.toJSON();
    const statements = resolved.Statement;
    resolved.Statement = statements.map(statement => {
      const item = statement as { Sid?: string };
      return item.Sid === "DenyTrialWritesUntilApprovedRetry"
        ? Fn.conditionIf(blockWrites.logicalId, statement, Fn.ref("AWS::NoValue")) : statement;
    });
    bucketPolicy.policyDocument = resolved;
    const table = arn("dynamodb", "table/foodtech-data-flow-dev");
    const leadingKeys = (patterns: string[]) => ({ StringEquals: regional,
      "ForAllValues:StringLike": { "dynamodb:LeadingKeys": patterns }, Null: { "dynamodb:LeadingKeys": "false" } });
    // Derived from the source-code Autopilot baseline: selected table operations
    // only. Replication, ACL/ObjectLock, KMS storage and other optional variants
    // are not selected by these clients and are deliberately excluded.
    collector.addToPolicy(new iam.PolicyStatement({ actions: ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
      resources: [table], conditions: leadingKeys(["STREAM#*", "MOCK#*", "INGESTION_DLQ#*"]) }));
    collector.addToPolicy(new iam.PolicyStatement({ actions: ["dynamodb:Query", "dynamodb:DeleteItem"],
      resources: [table], conditions: leadingKeys(["STREAM#*"]) }));
    collector.addToPolicy(new iam.PolicyStatement({ actions: ["sqs:SendMessage"],
      resources: [arn("sqs", "foodtech-ordered-dev.fifo")], conditions: { StringEquals: regional } }));
    collector.addToPolicy(new iam.PolicyStatement({ actions: ["s3:PutObject"], resources: [bucket.arnForObjects("ingestion/*")],
      conditions: { StringEquals: { ...regional, "aws:ResourceAccount": account,
        "s3:x-amz-server-side-encryption": "AES256" }, Bool: { "aws:SecureTransport": "true" } } }));
    const definition = (name: string, repository: string, role: iam.IRole, secretArn: string, keyArn: string,
                        imageUri: string, environment: Record<string, string>, secrets: Record<string, string>, command: string[]) => {
      const logName = `/foodtech/dev/private-rest-${name}`;
      const group = new logs.LogGroup(this, `${name}Logs`, { logGroupName: logName,
        retention: logs.RetentionDays.ONE_WEEK, removalPolicy: RemovalPolicy.RETAIN });
      const execution = new iam.Role(this, `${name}ExecutionRole`, {
        roleName: `foodtech-private-rest-${name}-execution-dev`, assumedBy: trust(),
        description: "ECS agent: one ECR repository, own logs and own Secret only",
      });
      execution.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:GetAuthorizationToken"], resources: ["*"],
        conditions: { StringEquals: regional } }));
      execution.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
        resources: [arn("ecr", `repository/foodtech/${repository}-dev`)], conditions: { StringEquals: regional } }));
      execution.addToPolicy(new iam.PolicyStatement({ actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
        resources: [arn("logs", `log-group:${logName}:log-stream:trial/${name}/*`)], conditions: { StringEquals: regional } }));
      execution.addToPolicy(new iam.PolicyStatement({ actions: ["secretsmanager:GetSecretValue"], resources: [secretArn],
        conditions: { StringEquals: regional, Bool: { "aws:SecureTransport": "true" } } }));
      execution.addToPolicy(new iam.PolicyStatement({ actions: ["kms:Decrypt"], resources: [keyArn],
        conditions: { StringEquals: { ...regional, "kms:ViaService": `secretsmanager.${region}.amazonaws.com`,
          "kms:EncryptionContext:SecretARN": secretArn } } }));
      const task = new ecs.CfnTaskDefinition(this, `${name}Task`, {
        family: `foodtech-private-rest-${name}-dev`, networkMode: "awsvpc", requiresCompatibilities: ["FARGATE"],
        cpu: "256", memory: "512", taskRoleArn: role.roleArn, executionRoleArn: execution.roleArn,
        runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
        volumes: name === "collector" ? [{ name: "collector-ca" }] : undefined,
        containerDefinitions: [{ name, image: imageUri, essential: true, user: "10001:10001",
          readonlyRootFilesystem: true, stopTimeout: 30, command,
          linuxParameters: { capabilities: { drop: ["ALL"] } },
          mountPoints: name === "collector" ? [{ sourceVolume: "collector-ca", containerPath: "/run/foodtech-collection", readOnly: false }] : undefined,
          portMappings: name === "mock" ? [{ containerPort: 8443, protocol: "tcp" }] : undefined,
          environment: Object.entries(environment).map(([name, value]) => ({ name, value })),
          secrets: Object.entries(secrets).map(([name, field]) => ({ name, valueFrom: `${secretArn}:${field}::${materialVersion.valueAsString}` })),
          logConfiguration: { logDriver: "awslogs", options: { "awslogs-group": group.logGroupName,
            "awslogs-region": region, "awslogs-stream-prefix": "trial" } },
        }],
      });
      task.node.addDependency(execution.node.findChild("DefaultPolicy"));
      new CfnOutput(this, `${name}TaskDefinitionArn`, { value: task.ref });
      new CfnOutput(this, `${name}ExecutionRoleArn`, { value: execution.roleArn });
    };
    definition("mock", "mock", mock, mockSecret.valueAsString, mockKey.valueAsString, mockImage.valueAsString,
      { DOTNET_EnableDiagnostics: "0", DOTNET_CLI_TELEMETRY_OPTOUT: "1" },
      { FOODTECH_MOCK_SERVER_CERT_PEM: "server_cert_pem", FOODTECH_MOCK_SERVER_KEY_PEM: "server_key_pem",
        FOODTECH_MOCK_CLIENT_ID: "client_id", FOODTECH_MOCK_CLIENT_SECRET: "client_secret" },
      mockCommand);
    definition("collector", "collection", collector, collectorSecret.valueAsString, collectorKey.valueAsString, collectorImage.valueAsString,
      { FOODTECH_STAGE: "dev", FOODTECH_COLLECTION_PROTOCOL: "rest", FOODTECH_COLLECTION_RUN_MODE: "private-rest-trial",
        FOODTECH_REST_TRIAL_ENABLED: "true", FOODTECH_REST_TRIAL_MAX_SECONDS: "60",
        FOODTECH_COLLECTION_TASK_ROLE_ARN: collector.roleArn, FOODTECH_DATA_TABLE: "foodtech-data-flow-dev",
        FOODTECH_ORDERED_QUEUE_URL: `https://sqs.${region}.amazonaws.com/${account}/foodtech-ordered-dev.fifo`,
        FOODTECH_INGESTION_DLQ_BUCKET: bucket.bucketName },
      { FOODTECH_MOCK_CA_PEM: "ca_pem", FOODTECH_MOCK_CLIENT_ID: "client_id", FOODTECH_MOCK_CLIENT_SECRET: "client_secret" }, ["--trial"]);
    new CfnOutput(this, "CollectorTaskRoleArn", { value: collector.roleArn });
    new CfnOutput(this, "MockTaskRoleArn", { value: mock.roleArn });
    new CfnOutput(this, "QuarantineBucketName", { value: bucket.bucketName });
  }
}
