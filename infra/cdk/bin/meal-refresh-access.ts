import { CfnOutput, CfnParameter, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";

/** IAM definitions only. No table/index, Task, network or schedule activation. */
export class MealRefreshAccessStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("meal refresh access requires dev and an explicit regional account");
    }
    const source = readFileSync(join(__dirname, "meal-refresh-baseline.json"), "utf8");
    const baseline = JSON.parse(source);
    const available = new Set<string>(baseline.Statement.flatMap((s: { Effect: string; Action: string[] }) =>
      s.Effect === "Allow" ? s.Action : []));
    const generated = (...wanted: string[]) => {
      if (!wanted.every(action => available.has(action))) throw new Error("action missing from generated meal refresh baseline");
      return wanted;
    };
    const region = "ap-southeast-2";
    const table = (name: string) => `arn:aws:dynamodb:${region}:${this.account}:table/foodtech-${name}-${stage}`;
    const auth = table("auth-sessions"), cooking = table("cooking-jobs");
    const regional = { "aws:RequestedRegion": region };
    const keys = (...patterns: string[]) => ({
      "ForAllValues:StringLike": { "dynamodb:LeadingKeys": patterns },
      Null: { "dynamodb:LeadingKeys": "false" },
    });
    const meals = "SCOPE#USER#*#COMBI_MEAL#*", results = "SCOPE#USER#*#MEAL_AGGREGATE#*";
    const statements = [
      new iam.PolicyStatement({ actions: generated("dynamodb:DescribeTable"), resources: [auth, cooking],
        conditions: { StringEquals: regional } }),
      new iam.PolicyStatement({ actions: generated("dynamodb:GetItem", "dynamodb:ConditionCheckItem"), resources: [auth],
        conditions: { ...keys("USER#*"), StringEquals: regional } }),
      new iam.PolicyStatement({ actions: generated("dynamodb:GetItem", "dynamodb:ConditionCheckItem"), resources: [cooking],
        conditions: { ...keys(meals, results), StringEquals: regional } }),
      new iam.PolicyStatement({ actions: generated("dynamodb:Query"), resources: [cooking],
        conditions: { ...keys(meals), StringEquals: regional } }),
      new iam.PolicyStatement({ actions: generated("dynamodb:Query"), resources: [`${cooking}/index/MealAggregatePending`],
        conditions: { ...keys(...Array.from({ length: 16 }, (_, n) => `PENDING#${n.toString(16).padStart(2, "0")}`)),
          StringEquals: regional } }),
      new iam.PolicyStatement({ actions: generated("dynamodb:PutItem"), resources: [cooking],
        conditions: { ...keys(meals, results, "SCOPE#USER#*#MEAL_AGGREGATE_GROUPS"),
          StringEquals: { ...regional, "dynamodb:EnclosingOperation": "TransactWriteItems" } } }),
      // IAM LeadingKeys scopes partitions, not individual sort keys. The runtime
      // must enforce DIRTY-only mutations and compare-and-set versions/cursors.
      new iam.PolicyStatement({ actions: generated("dynamodb:UpdateItem", "dynamodb:DeleteItem"), resources: [cooking],
        conditions: { ...keys(meals), StringEquals: { ...regional, "dynamodb:EnclosingOperation": "TransactWriteItems" } } }),
      new iam.PolicyStatement({ actions: generated("sts:GetCallerIdentity"), resources: ["*"], conditions: { StringEquals: regional } }),
    ];
    const trust = () => new iam.ServicePrincipal("ecs-tasks.amazonaws.com", { conditions: {
      StringEquals: { "aws:SourceAccount": this.account },
      ArnLike: { "aws:SourceArn": `arn:aws:ecs:${region}:${this.account}:*` },
    } });
    const task = new iam.Role(this, "TaskRole", { roleName: `foodtech-meal-refresh-task-${stage}`,
      assumedBy: trust(), description: "Meal aggregate refresh; USER read/check only, scoped transactional derived writes" });
    const policy = new iam.Policy(this, "TaskPolicy", { document: new iam.PolicyDocument({ statements }) });
    task.attachInlinePolicy(policy);
    (policy.node.defaultChild as iam.CfnPolicy).addMetadata("GeneratedBaselineSha256", createHash("sha256").update(source).digest("hex"));
    const execution = new iam.Role(this, "ExecutionRole", { roleName: `foodtech-meal-refresh-execution-${stage}`,
      assumedBy: trust(), description: "Pull maintenance image and write meal refresh logs only" });
    execution.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:GetAuthorizationToken"], resources: ["*"], conditions: { StringEquals: regional } }));
    execution.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
      resources: [`arn:aws:ecr:${region}:${this.account}:repository/foodtech/maintenance-${stage}`], conditions: { StringEquals: regional } }));
    execution.addToPolicy(new iam.PolicyStatement({ actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
      resources: [`arn:aws:logs:${region}:${this.account}:log-group:/foodtech/${stage}/meal-refresh:log-stream:worker/worker/*`],
      conditions: { StringEquals: regional } }));
    const cluster = new CfnParameter(this, "ClusterArn", { type: "String",
      allowedPattern: `^arn:aws:ecs:${region}:${this.account}:cluster/[A-Za-z0-9_-]+$`, description: "Exact reviewed existing cluster" });
    const rule = new CfnParameter(this, "ScheduleRuleArn", { type: "String",
      allowedPattern: `^arn:aws:events:${region}:${this.account}:rule/[A-Za-z0-9_.-]+$`, description: "Exact reviewed default-bus meal refresh rule; activation is separate" });
    const invoke = new iam.Role(this, "InvokeRole", { roleName: `foodtech-meal-refresh-invoke-${stage}`,
      assumedBy: new iam.ServicePrincipal("events.amazonaws.com", { conditions: {
        StringEquals: { "aws:SourceAccount": this.account }, ArnEquals: { "aws:SourceArn": rule.valueAsString },
      } }) });
    invoke.addToPolicy(new iam.PolicyStatement({ actions: ["ecs:RunTask"],
      resources: [`arn:aws:ecs:${region}:${this.account}:task-definition/foodtech-meal-refresh-${stage}:*`],
      conditions: { StringEquals: regional, ArnEquals: { "ecs:cluster": cluster.valueAsString } } }));
    invoke.addToPolicy(new iam.PolicyStatement({ actions: ["iam:PassRole"], resources: [task.roleArn, execution.roleArn],
      conditions: { StringEquals: { "iam:PassedToService": "ecs-tasks.amazonaws.com" } } }));
    new CfnOutput(this, "TaskRoleArn", { value: task.roleArn });
    new CfnOutput(this, "ExecutionRoleArn", { value: execution.roleArn });
    new CfnOutput(this, "InvokeRoleArn", { value: invoke.roleArn });
    this.templateOptions.description = "Opt-in development meal refresh IAM only; no runtime, GSI, writer rollout or schedule activation";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
    for (const child of this.node.findAll()) {
      if (child instanceof iam.CfnRole || child instanceof iam.CfnPolicy) child.addMetadata("com.aws.cloudformation.Context", {
        why: "Separate business refresh, image/log agent and scheduled invocation authorities",
        must: "No authentication writes, source credentials, table/index changes or runtime activation; explicit deployment review required",
      });
    }
  }
}
