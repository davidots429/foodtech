import { ArnFormat, CfnOutput, CfnParameter, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/** Opt-in IAM candidate. No Task, schedule, table, key or network is created. */
export class SourceGrantDeletionAccessStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("source grant deletion access requires dev and an explicit regional account");
    }
    const region = "ap-southeast-2";
    const arn = (name: string) => `arn:aws:dynamodb:${region}:${this.account}:table/foodtech-${name}-${stage}`;
    const auth = arn("auth-sessions"), registry = arn("deletion-registry"), catalog = arn("source-access");
    const key = (name: string, description: string) => new CfnParameter(this, name, { type: "String", description,
      allowedPattern: `^arn:aws:kms:${region}:${this.account}:key/[a-f0-9-]{36}$` });
    const registryKey = key("RegistryStorageKeyArn", "Verified existing Registry table SSE key; not an envelope key");
    const managedKey = key("DynamoDbManagedKeyArn", "Verified existing aws/dynamodb key for Auth and catalog SSE");
    const regional = { "aws:RequestedRegion": region };
    const boundary = new iam.ManagedPolicy(this, "TaskBoundary", {
      managedPolicyName: `foodtech-source-grant-deletion-boundary-${stage}`,
      description: "Catalog removal only; authorization and Registry are read/check only",
    });
    const purposes: Record<string, string> = {};
    const add = (statement: iam.PolicyStatementProps) => {
      const sid = `B${Object.keys(purposes).length + 1}`;
      purposes[sid] = statement.sid!;
      boundary.addStatements(new iam.PolicyStatement({ ...statement, sid }));
    };
    add({ sid: "MetadataOnlyForThreeTables", actions: ["dynamodb:DescribeTable"], resources: [auth, registry, catalog],
      conditions: { StringEquals: regional } });
    const authFields = ["pk", "epoch", "suspended", "deletion_lock_request", "withdrawal_request_id", "withdrawal_status",
      "withdrawal_requested_at", "withdrawal_deadline", "withdrawal_epoch", "source_read_grants"];
    const keys = (patterns: string[], exact = false) => ({
      [exact ? "ForAllValues:StringEquals" : "ForAllValues:StringLike"]: { "dynamodb:LeadingKeys": patterns },
      Null: { "dynamodb:LeadingKeys": "false" },
    });
    add({ sid: "ReadEnrollmentFence", actions: ["dynamodb:GetItem"], resources: [auth],
      conditions: { StringEquals: regional, ...keys(["CONTROL#PRINCIPAL_ENROLLMENT"], true) } });
    add({ sid: "ReadLockedPrincipalProjection", actions: ["dynamodb:GetItem"], resources: [auth], conditions: {
      StringEquals: { ...regional, "dynamodb:EnclosingOperation": "TransactGetItems", "dynamodb:Select": "SPECIFIC_ATTRIBUTES" },
      "ForAllValues:StringLike": { "dynamodb:LeadingKeys": ["USER#*"] },
      "ForAllValues:StringEquals": { "dynamodb:Attributes": authFields },
      Null: { "dynamodb:LeadingKeys": "false", "dynamodb:Attributes": "false" },
    } });
    add({ sid: "DiscoverWithdrawalMetadata", actions: ["dynamodb:Scan"], resources: [auth], conditions: {
      StringEquals: { ...regional, "dynamodb:Select": "SPECIFIC_ATTRIBUTES" },
      "ForAllValues:StringEquals": { "dynamodb:Attributes": authFields }, Null: { "dynamodb:Attributes": "false" },
    } });
    add({ sid: "CheckLockedPrincipalOnly", actions: ["dynamodb:ConditionCheckItem"], resources: [auth], conditions: {
      StringEquals: { ...regional, "dynamodb:ReturnValues": "NONE" },
      "ForAllValues:StringLike": { "dynamodb:LeadingKeys": ["USER#*"] },
      "ForAllValues:StringEquals": { "dynamodb:Attributes": authFields },
      Null: { "dynamodb:LeadingKeys": "false", "dynamodb:Attributes": "false" },
    } });
    add({ sid: "ReadRegistryStatusOnly", actions: ["dynamodb:GetItem"], resources: [registry], conditions: {
      StringEquals: { ...regional, "dynamodb:EnclosingOperation": "TransactGetItems", "dynamodb:Select": "SPECIFIC_ATTRIBUTES" },
      "ForAllValues:StringEquals": { "dynamodb:LeadingKeys": ["REGISTRY"], "dynamodb:Attributes": ["pk", "sk", "request_id", "status"] },
      Null: { "dynamodb:LeadingKeys": "false", "dynamodb:Attributes": "false" },
    } });
    add({ sid: "CheckRegistryStatusOnly", actions: ["dynamodb:ConditionCheckItem"], resources: [registry], conditions: {
      // ConditionCheckItem has no EnclosingOperation condition key.
      StringEquals: { ...regional, "dynamodb:ReturnValues": "NONE" }, ...keys(["REGISTRY"], true),
    } });
    add({ sid: "ReadGrantMetadataOnly", actions: ["dynamodb:GetItem"], resources: [catalog], conditions: {
      StringEquals: { ...regional, "dynamodb:EnclosingOperation": "TransactGetItems", "dynamodb:Select": "SPECIFIC_ATTRIBUTES" },
      "ForAllValues:StringLike": { "dynamodb:LeadingKeys": ["GRANT#*"] },
      "ForAllValues:StringEquals": { "dynamodb:Attributes": ["pk", "sk", "grant_id", "source_partition", "permission", "schema", "version"] },
      Null: { "dynamodb:LeadingKeys": "false", "dynamodb:Attributes": "false" },
    } });
    add({ sid: "RemoveCatalogGrantWithinTransaction", actions: ["dynamodb:DeleteItem"], resources: [catalog], conditions: {
      StringEquals: { ...regional, "dynamodb:EnclosingOperation": "TransactWriteItems", "dynamodb:ReturnValues": "NONE" },
      ...keys(["GRANT#*"]),
    } });
    // Explicit capability ceilings also prevent later identity-policy expansion
    // from turning this cleanup task into a principal or grant registrar.
    for (const [sid, resource, permitted] of [
      ["NeverWriteAuthorization", auth, ["dynamodb:GetItem", "dynamodb:Scan", "dynamodb:DescribeTable", "dynamodb:ConditionCheckItem"]],
      ["NeverWriteRegistry", registry, ["dynamodb:GetItem", "dynamodb:DescribeTable", "dynamodb:ConditionCheckItem"]],
      ["NeverCreateOrModifyGrant", catalog, ["dynamodb:GetItem", "dynamodb:DescribeTable", "dynamodb:DeleteItem"]],
    ] as const) add({ sid, effect: iam.Effect.DENY, notActions: [...permitted], resources: [resource] });
    for (const [sid, resource, tables] of [
      ["RegistrySseOnly", registryKey.valueAsString, [`foodtech-deletion-registry-${stage}`]],
      ["ManagedTableSseOnly", managedKey.valueAsString, [`foodtech-auth-sessions-${stage}`, `foodtech-source-access-${stage}`]],
    ] as const) add({ sid, actions: ["kms:Decrypt"], resources: [resource], conditions: { StringEquals: {
      ...regional, "kms:ViaService": `dynamodb.${region}.${this.urlSuffix}`,
      "kms:EncryptionContext:aws:dynamodb:subscriberId": this.account,
      "kms:EncryptionContext:aws:dynamodb:tableName": [...tables],
    } } });
    add({ sid: "ObserveOwnCallerIdentity", actions: ["sts:GetCallerIdentity"], resources: ["*"], conditions: { StringEquals: regional } });
    (boundary.node.defaultChild as iam.CfnManagedPolicy).addMetadata("PurposeBySid", purposes);
    const trust = () => new iam.ServicePrincipal("ecs-tasks.amazonaws.com", { conditions: {
      StringEquals: { "aws:SourceAccount": this.account },
      // A single-cluster SourceArn is not supported for ECS task-role trust.
      ArnLike: { "aws:SourceArn": `arn:aws:ecs:${region}:${this.account}:*` },
    } });
    const taskRole = new iam.Role(this, "TaskRole", { roleName: `foodtech-source-grant-deletion-task-${stage}`,
      assumedBy: trust(), permissionsBoundary: boundary,
      description: "Expired locked grant catalog cleanup; no principal/Registry mutation" });
    // The identity document is generated by IAM Policy Autopilot. Only its
    // account placeholder is materialized; the independent boundary caps it.
    const generated = JSON.parse(readFileSync(join(__dirname, "source-grant-deletion-baseline.json"), "utf8"));
    for (const statement of generated.Statement) {
      statement.Resource = statement.Resource.map((value: string) => value.replaceAll("${ACCOUNT}", this.account));
    }
    taskRole.attachInlinePolicy(new iam.Policy(this, "GeneratedIdentityPolicy", {
      document: iam.PolicyDocument.fromJson(generated),
    }));
    const execution = new iam.Role(this, "ExecutionRole", { roleName: `foodtech-source-grant-deletion-execution-${stage}`,
      assumedBy: trust(), description: "Pull existing maintenance image and write source grant cleanup logs only" });
    execution.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:GetAuthorizationToken"], resources: ["*"],
      conditions: { StringEquals: regional } }));
    execution.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
      resources: [`arn:aws:ecr:${region}:${this.account}:repository/foodtech/maintenance-${stage}`], conditions: { StringEquals: regional } }));
    execution.addToPolicy(new iam.PolicyStatement({ actions: ["logs:CreateLogStream", "logs:PutLogEvents"], resources: [this.formatArn({
      service: "logs", resource: "log-group", resourceName: `/foodtech/${stage}/source-grant-deletion:*`, arnFormat: ArnFormat.COLON_RESOURCE_NAME,
    })], conditions: { StringEquals: regional } }));
    new CfnOutput(this, "TaskRoleArn", { value: taskRole.roleArn });
    new CfnOutput(this, "ExecutionRoleArn", { value: execution.roleArn });
    new CfnOutput(this, "TaskBoundaryArn", { value: boundary.managedPolicyArn });
  }
}
