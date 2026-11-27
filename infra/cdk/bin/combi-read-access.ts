import { CfnOutput, CfnParameter, CfnRule, Fn, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";

/** Opt-in definition, not effective-access, writer or coverage attestation. */
export class CombiRecipeReadAccessStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("combi read access requires dev and an explicit regional account");
    }
    const source = readFileSync(join(__dirname, "combi-read-baseline.json"), "utf8");
    const baseline = JSON.parse(source);
    const generated = (kind: "source" | "issuer_history" | "conditions", wanted: string[]) => {
      const allowed = new Set<string>(baseline[kind].Policies.flatMap((p: { Policy: { Statement: { Effect: string; Action: string[] }[] } }) =>
        p.Policy.Statement.filter(s => s.Effect === "Allow").flatMap(s => s.Action)));
      if (!wanted.every(action => allowed.has(action))) throw new Error("action absent from generated combi baseline");
      return wanted;
    };
    const pattern = `^arn:aws:iam::${this.account}:role/[A-Za-z0-9+=,.@_-]{1,64}$`;
    const broker = new CfnParameter(this, "BrokerTaskRoleArn", { type: "String", allowedPattern: pattern });
    const gateway = new CfnParameter(this, "GatewayTaskRoleArn", { type: "String", allowedPattern: pattern });
    const name = `foodtech-combi-source-read-${stage}`;
    const readArn = `arn:aws:iam::${this.account}:role/${name}`;
    new CfnRule(this, "SeparateCombiAuthorities", { assertions: [{ assert: Fn.conditionAnd(
      Fn.conditionNot(Fn.conditionEquals(broker.valueAsString, gateway.valueAsString)),
      Fn.conditionNot(Fn.conditionEquals(broker.valueAsString, readArn)),
      Fn.conditionNot(Fn.conditionEquals(gateway.valueAsString, readArn))),
      assertDescription: "Broker, Gateway and Combi source read role must remain distinct" }] });
    const arn = (table: string) => `arn:aws:dynamodb:ap-southeast-2:${this.account}:table/foodtech-${table}-${stage}`;
    const regional = { "aws:RequestedRegion": "ap-southeast-2" };
    const keys = (values: string[]) => ({ "ForAllValues:StringLike": { "dynamodb:LeadingKeys": values },
      Null: { "dynamodb:LeadingKeys": "false" } });
    const meal = "SCOPE#USER#*#COMBI_MEAL#????????-????-????-????-????????????";
    const roster = "SCOPE#USER#*#COMBI_MEALS";
    const sourceRead = () => new iam.PolicyStatement({ actions: generated("source", ["dynamodb:GetItem", "dynamodb:Query"]),
      resources: [arn("cooking-jobs")], conditions: { ...keys([meal, meal + "#SOURCE"]), StringEquals: regional } });
    // The role's broad owner envelope is never a customer lease: the issuer must
    // intersect it with its exact four-key session policy. The boundary prevents
    // another attached Allow from expanding this read identity to other services.
    const boundary = new iam.ManagedPolicy(this, "ReadBoundary", { statements: [sourceRead()] });
    boundary.applyRemovalPolicy(RemovalPolicy.RETAIN);
    const role = new iam.Role(this, "ReadRole", { roleName: name, permissionsBoundary: boundary,
      description: "Combi source only; broker must always supply an exact-four-key session policy",
      assumedBy: new iam.ArnPrincipal(broker.valueAsString).withConditions({
        StringEquals: regional, StringLike: { "sts:RoleSessionName": "combi-*" } }),
    });
    // Attach the same reviewed document as the positive grant and boundary;
    // neither an inline role policy nor a separate broader managed grant exists.
    role.addManagedPolicy(boundary);
    role.applyRemovalPolicy(RemovalPolicy.RETAIN);
    role.assumeRolePolicy!.addStatements(new iam.PolicyStatement({ effect: iam.Effect.DENY,
      principals: [new iam.ArnPrincipal(gateway.valueAsString)], actions: ["sts:AssumeRole"] }),
    new iam.PolicyStatement({ effect: iam.Effect.DENY, principals: [new iam.AnyPrincipal()], actions: ["sts:TagSession"] }));
    const get = (table: string, values: string[]) => new iam.PolicyStatement({ actions: generated("issuer_history", ["dynamodb:GetItem"]),
      resources: [arn(table)], conditions: { ...keys(values), StringEquals: { ...regional, "dynamodb:EnclosingOperation": "TransactGetItems" } } });
    const query = new iam.PolicyStatement({ actions: generated("issuer_history", ["dynamodb:Query"]), resources: [arn("cooking-jobs")],
      conditions: { ...keys([roster, meal]), StringEquals: { ...regional, "dynamodb:Select": "SPECIFIC_ATTRIBUTES" } } });
    const check = (table: string, values: string[]) => new iam.PolicyStatement({ actions: generated("conditions", ["dynamodb:ConditionCheckItem"]),
      resources: [arn(table)], conditions: { ...keys(values), StringEquals: { ...regional, "dynamodb:ReturnValues": "NONE" } } });
    const policies = [new iam.CfnPolicy(this, "BrokerAccess", {
      policyName: `foodtech-combi-read-broker-${stage}`, roles: [Fn.select(1, Fn.split("/", broker.valueAsString))],
      policyDocument: new iam.PolicyDocument({ statements: [new iam.PolicyStatement({
        actions: generated("issuer_history", ["sts:AssumeRole"]), resources: [role.roleArn],
        conditions: { StringEquals: regional, StringLike: { "sts:RoleSessionName": "combi-*" } } }),
      get("auth-sessions", ["USER#*", "CONTROL#COMBI_HISTORY_WRITERS"]), get("cooking-jobs", [roster, meal]), query] }),
    }), new iam.CfnPolicy(this, "GatewayConditions", { policyName: `foodtech-combi-read-conditions-${stage}`,
      roles: [Fn.select(1, Fn.split("/", gateway.valueAsString))], policyDocument: new iam.PolicyDocument({ statements: [
        check("auth-sessions", ["USER#*", "CONTROL#COMBI_HISTORY_WRITERS"]), check("cooking-jobs", [roster, meal]),
      ] }) })];
    for (const policy of policies) {
      policy.applyRemovalPolicy(RemovalPolicy.RETAIN);
      policy.cfnOptions.metadata = { GeneratedBaselineSha256: createHash("sha256").update(source).digest("hex"),
        ReviewOnly: "Does not issue protected coverage, enable model calls or prove existing role policy intersections" };
    }
    new CfnOutput(this, "CombiSourceReadRoleArn", { value: role.roleArn });
    new CfnOutput(this, "ReviewedBrokerTaskRoleArn", { value: broker.valueAsString });
  }
}
