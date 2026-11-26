import { CfnOutput, CfnParameter, CfnRule, Fn, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";

/** Explicit incremental reader/checker policies, never an attestation issuer.
 * Existing role policies, SSE access and ordinary-writer fencing need separate
 * effective-access review. This stack does not prove those controls exist.
 */
export class RecipeHistoryAccessStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("recipe history access requires dev and an explicit regional account");
    }
    const source = readFileSync(join(__dirname, "recipe-history-baseline.json"), "utf8");
    const baseline = JSON.parse(source);
    const generated = (kind: "reader" | "conditions", wanted: string[]) => {
      const allowed = new Set<string>(baseline[kind].Policies.flatMap((p: { Policy: { Statement: iam.PolicyStatementProps[] } }) =>
        p.Policy.Statement.filter((s: iam.PolicyStatementProps & { Effect?: string }) => s.Effect === "Allow")
          .flatMap((s: iam.PolicyStatementProps & { Action?: string[] }) => s.Action ?? [])));
      if (!wanted.every(action => allowed.has(action))) throw new Error("action missing from generated history baseline");
      return wanted;
    };
    const rolePattern = `^arn:aws:iam::${this.account}:role/[A-Za-z0-9+=,.@_-]{1,64}$`;
    const broker = new CfnParameter(this, "BrokerTaskRoleArn", { type: "String", allowedPattern: rolePattern });
    const gateway = new CfnParameter(this, "GatewayTaskRoleArn", { type: "String", allowedPattern: rolePattern });
    const sourceRole = `arn:aws:iam::${this.account}:role/foodtech-recipe-source-read-${stage}`;
    new CfnRule(this, "SeparateHistoryAuthorities", { assertions: [{ assert: Fn.conditionAnd(
      Fn.conditionNot(Fn.conditionEquals(broker.valueAsString, gateway.valueAsString)),
      Fn.conditionNot(Fn.conditionEquals(broker.valueAsString, sourceRole)),
      Fn.conditionNot(Fn.conditionEquals(gateway.valueAsString, sourceRole))),
      assertDescription: "History reader, checker and scoped source reader must remain distinct" }] });
    const arn = (name: string) => `arn:aws:dynamodb:ap-southeast-2:${this.account}:table/foodtech-${name}-${stage}`;
    const keys = (values: string[]) => ({ "ForAllValues:StringLike": { "dynamodb:LeadingKeys": values },
      Null: { "dynamodb:LeadingKeys": "false" } });
    const regional = { "aws:RequestedRegion": "ap-southeast-2" };
    const read = (table: string, values: string[]) => new iam.PolicyStatement({
      actions: generated("reader", ["dynamodb:GetItem"]), resources: [arn(table)], conditions: {
        ...keys(values), StringEquals: { ...regional, "dynamodb:EnclosingOperation": "TransactGetItems" },
      } });
    const check = (table: string, values: string[]) => new iam.PolicyStatement({
      actions: generated("conditions", ["dynamodb:ConditionCheckItem"]), resources: [arn(table)], conditions: {
        // ConditionCheckItem does not support EnclosingOperation. Failure data
        // is explicitly disabled; never permit ALL_OLD as a read side channel.
        ...keys(values), StringEquals: { ...regional, "dynamodb:ReturnValues": "NONE" },
      } });
    const reader = new iam.PolicyDocument({ statements: [
      read("auth-sessions", ["USER#*", "CONTROL#COOKING_HISTORY_WRITERS"]),
      read("cooking-jobs", ["SCOPE#USER#*#JOBS", "SCOPE#USER#*#JOB#*"]),
      new iam.PolicyStatement({ actions: generated("reader", ["dynamodb:Query"]), resources: [arn("cooking-jobs")],
        conditions: { ...keys(["SCOPE#USER#*#JOBS"]), StringEquals: {
          ...regional, "dynamodb:Select": "SPECIFIC_ATTRIBUTES" } } }),
    ] });
    const checker = new iam.PolicyDocument({ statements: [
      check("auth-sessions", ["USER#*", "CONTROL#COOKING_HISTORY_WRITERS"]),
      check("cooking-jobs", ["SCOPE#USER#*#JOBS"]),
      check("source-access", ["GRANT#*"]),
    ] });
    for (const [name, role, document] of [["Reader", broker, reader], ["Checker", gateway, checker]] as const) {
      const policy = new iam.CfnPolicy(this, `${name}Policy`, {
        policyName: `foodtech-recipe-history-${name.toLowerCase()}-${stage}`,
        roles: [Fn.select(1, Fn.split("/", role.valueAsString))], policyDocument: document.toJSON(),
      });
      policy.addMetadata("GeneratedBaselineSha256", createHash("sha256").update(source).digest("hex"));
      policy.addMetadata("Scope", "Incremental read/check only; no issuance, mutation, source credential or runtime activation");
    }
    new CfnOutput(this, "ReviewedHistoryReaderRoleArn", { value: broker.valueAsString });
    new CfnOutput(this, "ReviewedHistoryCheckerRoleArn", { value: gateway.valueAsString });
  }
}
