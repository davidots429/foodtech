import { CfnOutput, CfnParameter, CfnRule, Fn, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

type Boundary = "broker" | "scoped" | "gateway" | "bff";

/** Separate fryer broker and delegated source roles plus incremental existing-task policies. */
export class TonkatsuRecipeAccessStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Tonkatsu access requires dev and an explicit regional account");
    }
    const raw = readFileSync(join(__dirname, "tonkatsu-access-baseline.json"), "utf8");
    const baseline = JSON.parse(raw) as Record<Boundary, string[]>;
    const baselineDigest = createHash("sha256").update(raw).digest("hex");
    const actions = (kind: Boundary, ...wanted: string[]) => {
      if (!wanted.every(action => baseline[kind].includes(action))) throw new Error("Action absent from generated Tonkatsu baseline");
      return wanted;
    };
    const roleArn = (name: string) => `arn:aws:iam::${this.account}:role/${name}`;
    const tableArn = (table: string) => `arn:aws:dynamodb:${this.region}:${this.account}:table/foodtech-${table}-${stage}`;
    const taskPattern = `^arn:aws:iam::${this.account}:role/[A-Za-z0-9+=,.@_-]{1,64}$`;
    const gateway = new CfnParameter(this, "GatewayTaskRoleArn", { type: "String", allowedPattern: taskPattern });
    const bff = new CfnParameter(this, "BusinessTaskRoleArn", { type: "String", allowedPattern: taskPattern });
    const brokerName = `foodtech-tonkatsu-broker-${stage}`;
    const readName = `foodtech-tonkatsu-source-read-${stage}`;
    const distinct = [
      [gateway.valueAsString, bff.valueAsString],
      [gateway.valueAsString, roleArn(brokerName)], [bff.valueAsString, roleArn(brokerName)],
      [gateway.valueAsString, roleArn(readName)], [bff.valueAsString, roleArn(readName)],
    ].map(([left, right]) => Fn.conditionNot(Fn.conditionEquals(left, right)));
    new CfnRule(this, "DistinctTonkatsuRoles", { assertions: [{
      assert: Fn.conditionAnd(...distinct),
      assertDescription: "Gateway, business, Tonkatsu broker and delegated source roles must remain distinct",
    }] });

    const region = { "aws:RequestedRegion": this.region };
    const job = "SCOPE#USER#*#JOB#????????-????-????-????-????????????";
    const jobs = "SCOPE#USER#*#JOBS";
    const execution = "EXEC#????????-????-????-????-????????????";
    const streams = ["?", "??"].map(length => `STREAM#14:synthetic-rest|21:mockprofile-100-a-001|${length}:batch-a-*`);
    const controls = ["USER#*", "CONTROL#COOKING_HISTORY_WRITERS"];
    const keys = (values: string[], extra: Record<string, string> = {}) => ({
      "ForAllValues:StringLike": { "dynamodb:LeadingKeys": values },
      Null: { "dynamodb:LeadingKeys": "false", ...extra },
    });
    const projected = (kind: Boundary, table: string, values: string[], attributes: string[]) => new iam.PolicyStatement({
      actions: actions(kind, "dynamodb:GetItem"),
      resources: [tableArn(table)],
      conditions: {
        ...keys(values, { "dynamodb:Attributes": "false", "dynamodb:EnclosingOperation": "true" }),
        "ForAllValues:StringEquals": { "dynamodb:Attributes": attributes },
        StringEquals: { ...region, "dynamodb:Select": "SPECIFIC_ATTRIBUTES" },
      },
    });
    const historyReads = (kind: "broker" | "gateway") => {
      const transactionRead = (table: string, values: string[]) => new iam.PolicyStatement({
        actions: actions(kind, "dynamodb:GetItem"), resources: [tableArn(table)], conditions: {
          ...keys(values), StringEquals: { ...region, "dynamodb:EnclosingOperation": "TransactGetItems" },
        },
      });
      return [
        transactionRead("auth-sessions", controls),
        transactionRead("cooking-jobs", [jobs, job, ...streams]),
        transactionRead("data-flow", streams),
        new iam.PolicyStatement({
          actions: actions(kind, "dynamodb:Query"), resources: [tableArn("cooking-jobs")],
          conditions: { ...keys([jobs]), StringEquals: { ...region, "dynamodb:Select": "SPECIFIC_ATTRIBUTES" } },
        }),
      ];
    };
    const checker = (kind: "gateway" | "bff", table: string, values: string[]) => new iam.PolicyStatement({
      actions: actions(kind, "dynamodb:ConditionCheckItem"), resources: [tableArn(table)],
      conditions: { ...keys(values), StringEquals: { ...region, "dynamodb:ReturnValues": "NONE" } },
    });
    const sourceChecks = (kind: "gateway" | "bff") => [
      checker(kind, "auth-sessions", controls),
      checker(kind, "cooking-jobs", [jobs, job, ...streams]),
      checker(kind, "data-flow", streams),
      checker(kind, "ai-execution-registry", [execution]),
    ];

    const broker = new iam.Role(this, "BrokerRole", {
      roleName: brokerName,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com").withConditions({
        StringEquals: { "aws:SourceAccount": this.account },
        ArnLike: { "aws:SourceArn": `arn:aws:ecs:${this.region}:${this.account}:*` },
      }),
    });
    const scopedRead = (table: string, values: string[]) => new iam.PolicyStatement({
      actions: actions("scoped", "dynamodb:GetItem", "dynamodb:Query"), resources: [tableArn(table)],
      conditions: { ...keys(values), StringEquals: region },
    });
    const boundary = new iam.ManagedPolicy(this, "ReadBoundary", {
      statements: [scopedRead("cooking-jobs", [job]), scopedRead("data-flow", streams)],
    });
    const read = new iam.Role(this, "ReadRole", {
      roleName: readName, permissionsBoundary: boundary, managedPolicies: [boundary],
      assumedBy: new iam.ArnPrincipal(broker.roleArn).withConditions({
        StringEquals: region, StringLike: { "sts:RoleSessionName": "tonkatsu-*" },
      }),
    });
    read.assumeRolePolicy!.addStatements(new iam.PolicyStatement({
      effect: iam.Effect.DENY, principals: [new iam.ArnPrincipal(gateway.valueAsString), new iam.ArnPrincipal(bff.valueAsString)],
      actions: ["sts:AssumeRole"],
    }), new iam.PolicyStatement({
      effect: iam.Effect.DENY, principals: [new iam.AnyPrincipal()], actions: ["sts:TagSession"],
    }));
    const roleName = (parameter: CfnParameter) => Fn.select(1, Fn.split("/", parameter.valueAsString));
    const policy = (id: string, roles: string[], statements: iam.PolicyStatement[]) => {
      const resource = new iam.CfnPolicy(this, id, {
        policyName: `foodtech-tonkatsu-${id.toLowerCase()}-${stage}`, roles,
        policyDocument: new iam.PolicyDocument({ statements }).toJSON(),
      });
      resource.applyRemovalPolicy(RemovalPolicy.RETAIN);
      resource.addMetadata("GeneratedBaselineSha256", baselineDigest);
    };
    policy("BrokerPermissions", [broker.roleName], [
      new iam.PolicyStatement({
        actions: actions("broker", "sts:AssumeRole"), resources: [read.roleArn],
        conditions: { StringEquals: region, StringLike: { "sts:RoleSessionName": "tonkatsu-*" } },
      }),
      projected("broker", "ai-execution-registry", [execution], [
        "pk", "sk", "execution_id", "tenant_id", "feature", "data_classification", "state", "version",
        "job_id", "job_version", "prior_job_id", "prior_job_version", "comparison_id",
        "jeyuk_source_revision", "prior_source_revision", "combi_binding", "tonkatsu_binding",
      ]),
      ...historyReads("broker"),
    ]);
    policy("GatewayPermissions", [roleName(gateway)], [
      ...historyReads("gateway"), ...sourceChecks("gateway"),
      projected("gateway", "auth-sessions", ["USER#*"], ["pk", "epoch", "suspended", "deletion_lock_request"]),
    ]);
    policy("BusinessConditions", [roleName(bff)], sourceChecks("bff"));

    for (const resource of [broker, boundary, read]) resource.applyRemovalPolicy(RemovalPolicy.RETAIN);
    new CfnOutput(this, "TonkatsuBrokerTaskRoleArn", { value: broker.roleArn });
    new CfnOutput(this, "TonkatsuSourceReadRoleArn", { value: read.roleArn });
    this.templateOptions.description = "Tonkatsu broker and delegated reads with incremental Gateway and business checks";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
  }
}
