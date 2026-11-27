import { CfnOutput, CfnParameter, Fn, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";

/** Incremental broker metadata only, not a scoped customer source lease. */
export class CombiExecutionReadAccessStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Combi execution access requires dev and an explicit regional account");
    }
    const raw = readFileSync(join(__dirname, "combi-execution-read-baseline.json"), "utf8");
    const baseline = JSON.parse(raw);
    const generated = new Set<string>(baseline.Policies.flatMap((p: { Policy: { Statement: { Effect: string; Action: string[] }[] } }) =>
      p.Policy.Statement.filter(s => s.Effect === "Allow").flatMap(s => s.Action)));
    if (!generated.has("dynamodb:GetItem")) throw new Error("execution read action absent from generated baseline");
    const broker = new CfnParameter(this, "BrokerTaskRoleArn", { type: "String",
      allowedPattern: `^arn:aws:iam::${this.account}:role/foodtech-recipe-broker-${stage}$`,
      description: "Existing separately reviewed broker only; never Gateway, BFF or source-read identity" });
    const attributes = ["pk", "sk", "execution_id", "tenant_id", "feature", "data_classification", "job_id", "job_version",
      "prior_job_id", "prior_job_version", "comparison_id", "jeyuk_source_revision", "prior_source_revision",
      "combi_binding", "state", "version"];
    const policy = new iam.CfnPolicy(this, "BrokerExecutionMetadata", {
      policyName: `foodtech-combi-execution-reader-${stage}`,
      roles: [Fn.select(1, Fn.split("/", broker.valueAsString))],
      policyDocument: new iam.PolicyDocument({ statements: [new iam.PolicyStatement({
        actions: ["dynamodb:GetItem"],
        resources: [`arn:aws:dynamodb:${this.region}:${this.account}:table/foodtech-ai-execution-registry-${stage}`],
        conditions: {
          "ForAllValues:StringLike": { "dynamodb:LeadingKeys": ["EXEC#????????-????-????-????-????????????"] },
          "ForAllValues:StringEquals": { "dynamodb:Attributes": attributes },
          Null: { "dynamodb:LeadingKeys": "false", "dynamodb:Attributes": "false", "dynamodb:EnclosingOperation": "true" },
          StringEquals: { "aws:RequestedRegion": this.region, "dynamodb:Select": "SPECIFIC_ATTRIBUTES" },
        },
      })] }).toJSON(),
    });
    policy.applyRemovalPolicy(RemovalPolicy.RETAIN);
    policy.addMetadata("GeneratedBaselineSha256", createHash("sha256").update(raw).digest("hex"));
    policy.addMetadata("com.aws.cloudformation.Context", {
      why: "Broker validates stored execution binding without model, credential, result or browser session reads",
      must: ["Preserve existing role, trust and other policies", "Direct GetItem only; no transaction, index, query, scan or mutation",
        "Require explicit allowed attributes and EXEC UUID-shaped partition key", "Broker must validate canonical UUID, owner, epoch, PENDING state and complete binding",
        "Partition key does not constrain sort key or owner; this grant is not per-tenant isolation", "No coverage issuance, Task, network or model activation"],
      mutable: "review-required",
    });
    this.templateOptions.description = "Incremental broker execution metadata lookup with explicit attribute allowlist and existing role preservation";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
    new CfnOutput(this, "ReviewedBrokerTaskRoleArn", { value: broker.valueAsString });
  }
}
