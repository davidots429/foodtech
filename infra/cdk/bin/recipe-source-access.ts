import { CfnOutput, CfnParameter, CfnRule, Fn, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { Construct } from "constructs";

/** Separate authorization metadata; not source payload or user identity storage. */
export class RecipeSourceCatalogStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account)) {
      throw new Error("recipe source catalog requires dev and an explicit verified account");
    }
    const table = new dynamodb.Table(this, "SourceAccess", {
      tableName: `foodtech-source-access-${stage}`,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true, recoveryPeriodInDays: 7 },
      deletionProtection: true, timeToLiveAttribute: "expires_at", removalPolicy: RemovalPolicy.RETAIN,
    });
    new CfnOutput(this, "SourceAccessTableName", { value: table.tableName });
  }
}

/** Opt-in IAM candidate only. No source read, credential issue, Task or schedule. */
export class RecipeSourceAccessStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("recipe source access requires dev and an explicit verified regional account");
    }
    const rolePattern = `^arn:aws:iam::${this.account}:role/[A-Za-z0-9+=,.@_-]{1,64}$`;
    const brokerMode = this.node.tryGetContext("recipeBrokerRoleMode") ?? "existing";
    if (brokerMode !== "existing" && brokerMode !== "create") {
      throw new Error("recipeBrokerRoleMode must be existing or create");
    }
    // This identity is only the trusted read issuer. Authentication/session,
    // ordinary BFF writes and history access require separately reviewed grants.
    const ownedBroker = brokerMode === "create" ? new iam.Role(this, "BrokerTaskRole", {
      roleName: `foodtech-recipe-broker-${stage}`,
      description: "Dedicated ECS recipe read issuer; not a general customer API or Gateway role",
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com", { conditions: {
        StringEquals: { "aws:SourceAccount": this.account },
        ArnLike: { "aws:SourceArn": `arn:aws:ecs:ap-southeast-2:${this.account}:*` },
      } }),
    }) : undefined;
    const existingBroker = ownedBroker ? undefined : new CfnParameter(this, "BrokerTaskRoleArn", {
      type: "String", allowedPattern: rolePattern,
      description: "Reviewed existing trusted BFF/broker task role; never the Gateway role",
    });
    // CloudFormation Rules cannot refer to resource GetAtt values. The owned
    // role has an explicit name, so compare its account-bound literal ARN.
    const brokerArn = ownedBroker
      ? `arn:aws:iam::${this.account}:role/foodtech-recipe-broker-${stage}`
      : existingBroker!.valueAsString;
    const gateway = new CfnParameter(this, "GatewayTaskRoleArn", { type: "String", allowedPattern: rolePattern,
      description: "Existing Gateway task role, explicitly excluded from credential issuance" });
    const name = `foodtech-recipe-source-read-${stage}`;
    const readArn = `arn:aws:iam::${this.account}:role/${name}`;
    new CfnRule(this, "DistinctSourceAuthorities", { assertions: [{ assert: Fn.conditionAnd(
      Fn.conditionNot(Fn.conditionEquals(brokerArn, gateway.valueAsString)),
      ...(ownedBroker ? [] : [Fn.conditionNot(Fn.conditionEquals(brokerArn, readArn))]),
      Fn.conditionNot(Fn.conditionEquals(gateway.valueAsString, readArn))),
      assertDescription: "Broker, Gateway and scoped read roles must be different" }] });
    const role = new iam.Role(this, "ReadRole", { roleName: name,
      description: "Broker-issued bounded recipe reads; every issue must supply an exact-key session policy",
      assumedBy: new iam.ArnPrincipal(brokerArn).withConditions({
        StringEquals: { "aws:PrincipalArn": brokerArn, "aws:RequestedRegion": "ap-southeast-2" },
        StringLike: { "sts:RoleSessionName": "recipe-*" },
      }) });
    if (ownedBroker) role.node.addDependency(ownedBroker);
    role.assumeRolePolicy!.addStatements(new iam.PolicyStatement({ effect: iam.Effect.DENY,
      principals: [new iam.ArnPrincipal(gateway.valueAsString)], actions: ["sts:AssumeRole"] }));
    role.assumeRolePolicy!.addStatements(new iam.PolicyStatement({ effect: iam.Effect.DENY,
      principals: [new iam.AnyPrincipal()], actions: ["sts:TagSession"] }));
    // Autopilot's source-read baseline is deliberately narrowed: these existing
    // regional AWS-managed tables need no KMS/replication grants or index access.
    for (const [table, keys] of [
      ["cooking-jobs", ["SCOPE#USER#*#JOB#*", "JEYUK#jeyuk-*"]],
      ["data-flow", ["JEYUK#jeyuk-*"]],
    ] as const) {
      role.addToPolicy(new iam.PolicyStatement({ actions: ["dynamodb:GetItem", "dynamodb:Query"],
        resources: [`arn:aws:dynamodb:ap-southeast-2:${this.account}:table/foodtech-${table}-${stage}`],
        conditions: { "ForAllValues:StringLike": { "dynamodb:LeadingKeys": [...keys] },
          Null: { "dynamodb:LeadingKeys": "false" }, StringEquals: { "aws:RequestedRegion": "ap-southeast-2" } } }));
    }
    // Attach only the generated AssumeRole call, refined to this one role.
    // The existing broker must separately retain its own application permissions.
    new iam.CfnPolicy(this, "BrokerAssumeReadRole", { policyName: `foodtech-recipe-source-assume-${stage}`,
      roles: [ownedBroker ? ownedBroker.roleName : Fn.select(1, Fn.split("/", brokerArn))], policyDocument: {
        Version: "2012-10-17", Statement: [{ Effect: "Allow", Action: "sts:AssumeRole", Resource: role.roleArn,
          Condition: { StringEquals: { "aws:RequestedRegion": "ap-southeast-2" },
            StringLike: { "sts:RoleSessionName": "recipe-*" } } }, {
          Effect: "Allow", Action: "dynamodb:GetItem",
          Resource: `arn:aws:dynamodb:ap-southeast-2:${this.account}:table/foodtech-source-access-${stage}`,
          Condition: { StringEquals: { "aws:RequestedRegion": "ap-southeast-2",
            "dynamodb:EnclosingOperation": "TransactGetItems" },
            "ForAllValues:StringLike": { "dynamodb:LeadingKeys": ["GRANT#*"] },
            Null: { "dynamodb:LeadingKeys": "false" } },
        }],
      } });
    new CfnOutput(this, "RecipeSourceReadRoleArn", { value: role.roleArn });
    new CfnOutput(this, "ReviewedBrokerTaskRoleArn", { value: brokerArn });
  }
}
