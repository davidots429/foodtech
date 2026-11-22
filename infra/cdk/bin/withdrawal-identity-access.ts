import { ArnFormat, CfnOutput, CfnParameter, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

// Reviewed refinement of the rejected Autopilot baseline. This opt-in stack
// defines policies only; deployment and actual-role verification are separate.
export class WithdrawalIdentityAccessStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account)) {
      throw new Error("withdrawal identity access requires an explicit verified CDK account");
    }
    const region = "ap-southeast-2";
    const parameter = (name: string, allowedPattern: string, description: string) =>
      new CfnParameter(this, name, { type: "String", allowedPattern, description });
    const pool = parameter("CognitoUserPoolId", "^ap-southeast-2_[A-Za-z0-9]+$", "Reviewed existing Cognito Pool ID");
    const storageKey = parameter("RegistryStorageKeyArn",
      `^arn:aws:kms:${region}:${this.account}:key/[a-f0-9-]{36}$`, "Exact Deletion Registry table SSE key, never the locator envelope key");
    const repository = parameter("ImageRepositoryArn",
      `^arn:aws:ecr:${region}:${this.account}:repository/[a-z0-9._/-]+$`, "Existing private repository containing the pinned worker image");
    const cluster = parameter("ClusterArn", `^arn:aws:ecs:${region}:${this.account}:cluster/[A-Za-z0-9_-]+$`,
      "Exact cluster for the separately defined disabled schedule");
    const registryName = `foodtech-deletion-registry-${stage}`;
    const authArn = this.formatArn({ service: "dynamodb", resource: "table", resourceName: `foodtech-auth-sessions-${stage}` });
    const registryArn = this.formatArn({ service: "dynamodb", resource: "table", resourceName: registryName });
    const poolArn = this.formatArn({ service: "cognito-idp", resource: "userpool", resourceName: pool.valueAsString });
    const scheduleArn = this.formatArn({ service: "events", resource: "rule", resourceName: `foodtech-withdrawal-identity-${stage}` });
    const taskFamilyArn = this.formatArn({ service: "ecs", resource: "task-definition",
      resourceName: `foodtech-withdrawal-identity-${stage}:*` });
    const logArn = this.formatArn({ service: "logs", resource: "log-group",
      resourceName: `/foodtech/${stage}/withdrawal-identity:*`, arnFormat: ArnFormat.COLON_RESOURCE_NAME });
    const taskTrust = () => new iam.ServicePrincipal("ecs-tasks.amazonaws.com", { conditions: {
      StringEquals: { "aws:SourceAccount": this.account },
      // ECS does not support a single-cluster SourceArn for task-role trust.
      ArnLike: { "aws:SourceArn": `arn:${this.partition}:ecs:${region}:${this.account}:*` },
    } });
    const taskRole = new iam.Role(this, "IdentityTaskRole", {
      roleName: `foodtech-withdrawal-identity-task-${stage}`, assumedBy: taskTrust(),
      description: "Expired locked Cognito identities; no registry mutation or envelope decryption",
    });
    const regional = { "aws:RequestedRegion": region };
    taskRole.addToPolicy(new iam.PolicyStatement({ sid: "ReadLockedPrincipals", actions: ["dynamodb:GetItem"], resources: [authArn],
      conditions: { StringEquals: { ...regional, "dynamodb:EnclosingOperation": "TransactGetItems" },
        "ForAllValues:StringLike": { "dynamodb:LeadingKeys": ["USER#*"] }, Null: { "dynamodb:LeadingKeys": "false" } } }));
    taskRole.addToPolicy(new iam.PolicyStatement({ sid: "ReadEnrollmentFence", actions: ["dynamodb:GetItem"], resources: [authArn],
      conditions: { StringEquals: regional, "ForAllValues:StringEquals": { "dynamodb:LeadingKeys": ["CONTROL#PRINCIPAL_ENROLLMENT"] },
        Null: { "dynamodb:LeadingKeys": "false" } } }));
    taskRole.addToPolicy(new iam.PolicyStatement({ sid: "DiscoverOnlyWithdrawalMetadata", actions: ["dynamodb:Scan"], resources: [authArn],
      conditions: { StringEquals: { ...regional, "dynamodb:Select": "SPECIFIC_ATTRIBUTES" },
        "ForAllValues:StringEquals": { "dynamodb:Attributes": ["pk", "withdrawal_request_id", "epoch", "withdrawal_status",
          "suspended", "withdrawal_deadline", "deletion_lock_request", "identity_retired_request"] },
        Null: { "dynamodb:Attributes": "false" } } }));
    taskRole.addToPolicy(new iam.PolicyStatement({ sid: "MarkIdentityWithinLockedTransaction", actions: ["dynamodb:UpdateItem"], resources: [authArn],
      conditions: { StringEquals: { ...regional, "dynamodb:EnclosingOperation": "TransactWriteItems" },
        "ForAllValues:StringLike": { "dynamodb:LeadingKeys": ["USER#*"] },
        "ForAllValues:StringEquals": { "dynamodb:Attributes": ["pk", "identity_retired_request", "identity_retired_at",
          "deletion_lock_request", "suspended", "epoch", "withdrawal_request_id", "withdrawal_status",
          "withdrawal_requested_at", "withdrawal_deadline"] },
        StringEqualsIfExists: { "dynamodb:ReturnValues": "NONE" },
        Null: { "dynamodb:LeadingKeys": "false", "dynamodb:Attributes": "false" } } }));
    for (const [sid, action, operation] of [
      ["ReadDeletionRegistry", "dynamodb:GetItem", "TransactGetItems"],
      ["CheckDeletionRegistry", "dynamodb:ConditionCheckItem", "TransactWriteItems"],
    ]) {
      taskRole.addToPolicy(new iam.PolicyStatement({ sid, actions: [action], resources: [registryArn],
        // ConditionCheckItem is a transaction permission, and its supported
        // condition keys do not include dynamodb:EnclosingOperation.
        conditions: { StringEquals: { ...regional, ...(action === "dynamodb:GetItem" ? { "dynamodb:EnclosingOperation": operation } : {}) },
          "ForAllValues:StringEquals": { "dynamodb:LeadingKeys": ["REGISTRY"] }, Null: { "dynamodb:LeadingKeys": "false" } } }));
    }
    taskRole.addToPolicy(new iam.PolicyStatement({ sid: "RemoveOnlyFromReviewedPool",
      actions: ["cognito-idp:AdminGetUser", "cognito-idp:AdminDeleteUser"], resources: [poolArn], conditions: { StringEquals: regional } }));
    taskRole.addToPolicy(new iam.PolicyStatement({ sid: "ReadRegistryStorageEncryptionOnly", actions: ["kms:Decrypt"], resources: [storageKey.valueAsString],
      conditions: { StringEquals: { ...regional, "kms:ViaService": `dynamodb.${region}.${this.urlSuffix}`,
        "kms:EncryptionContext:aws:dynamodb:tableName": registryName,
        "kms:EncryptionContext:aws:dynamodb:subscriberId": this.account } } }));

    const executionRole = new iam.Role(this, "IdentityExecutionRole", {
      roleName: `foodtech-withdrawal-identity-execution-${stage}`, assumedBy: taskTrust(),
      description: "Pull one existing worker repository and write only identity worker logs",
    });
    executionRole.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:GetAuthorizationToken"], resources: ["*"], conditions: { StringEquals: regional } }));
    executionRole.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
      resources: [repository.valueAsString], conditions: { StringEquals: regional } }));
    executionRole.addToPolicy(new iam.PolicyStatement({ actions: ["logs:CreateLogStream", "logs:PutLogEvents"], resources: [logArn],
      conditions: { StringEquals: regional } }));
    const invocationRole = new iam.Role(this, "IdentityInvocationRole", {
      roleName: `foodtech-withdrawal-identity-invoke-${stage}`,
      assumedBy: new iam.ServicePrincipal("events.amazonaws.com", { conditions: {
        StringEquals: { "aws:SourceAccount": this.account }, ArnEquals: { "aws:SourceArn": scheduleArn },
      } }), description: "Invoke only the withdrawal identity family in the reviewed cluster",
    });
    invocationRole.addToPolicy(new iam.PolicyStatement({ actions: ["ecs:RunTask"], resources: [taskFamilyArn],
      conditions: { StringEquals: regional, ArnEquals: { "ecs:cluster": cluster.valueAsString } } }));
    invocationRole.addToPolicy(new iam.PolicyStatement({ actions: ["iam:PassRole"], resources: [taskRole.roleArn, executionRole.roleArn],
      conditions: { StringEquals: { "iam:PassedToService": "ecs-tasks.amazonaws.com" } } }));
    new CfnOutput(this, "TaskRoleArn", { value: taskRole.roleArn });
    new CfnOutput(this, "ExecutionRoleArn", { value: executionRole.roleArn });
    new CfnOutput(this, "InvokeRoleArn", { value: invocationRole.roleArn });
  }
}
