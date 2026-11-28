import { CfnOutput, CfnParameter, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

export class TonkatsuBusinessTaskRoleStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Tonkatsu business task role requires dev and an explicit regional account");
    }

    const pool = new CfnParameter(this, "CustomerUserPoolArn", {
      type: "String",
      allowedPattern: `^arn:aws:cognito-idp:${this.region}:${this.account}:userpool/${this.region}_[A-Za-z0-9]+$`,
    });
    const key = new CfnParameter(this, "AuthRefreshKeyArn", {
      type: "String",
      allowedPattern: `^arn:aws:kms:${this.region}:${this.account}:key/[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$`,
    });
    const role = new iam.Role(this, "BusinessTaskRole", {
      roleName: "foodtech-tonkatsu-business-api-dev",
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com").withConditions({
        StringEquals: { "aws:SourceAccount": this.account },
        ArnLike: { "aws:SourceArn": `arn:aws:ecs:${this.region}:${this.account}:*` },
      }),
    });
    const regional = { "aws:RequestedRegion": this.region };
    const allow = (actions: string[], resources: string[], conditions: Record<string, object>) => {
      role.addToPolicy(new iam.PolicyStatement({ actions, resources, conditions }));
    };
    const table = (name: string) => `arn:aws:dynamodb:${this.region}:${this.account}:table/foodtech-${name}-${stage}`;
    const items = (name: string, actions: string[], keys: string[], transactional = false) => {
      allow(actions.map(action => `dynamodb:${action}`), [table(name)], {
        "ForAllValues:StringLike": { "dynamodb:LeadingKeys": keys },
        Null: { "dynamodb:LeadingKeys": "false" },
        StringEquals: { ...regional, ...(transactional ? transaction : {}) },
        ...(transactional ? { StringEqualsIfExists: failure } : {}),
      });
    };
    const checkItems = (name: string, keys: string[]) => {
      allow(["dynamodb:ConditionCheckItem"], [table(name)], {
        "ForAllValues:StringLike": { "dynamodb:LeadingKeys": keys },
        Null: { "dynamodb:LeadingKeys": "false" },
        StringEquals: regional,
        StringEqualsIfExists: failure,
      });
    };
    const putAttributes = (name: string, keys: string[], attributes: string[]) => {
      allow(["dynamodb:PutItem"], [table(name)], {
        "ForAllValues:StringLike": { "dynamodb:LeadingKeys": keys },
        "ForAllValues:StringEquals": { "dynamodb:Attributes": attributes },
        Null: { "dynamodb:LeadingKeys": "false", "dynamodb:Attributes": "false" },
        StringEquals: { ...regional, ...transaction },
        StringEqualsIfExists: failure,
      });
    };

    const auth = "auth-sessions";
    const subject = "????????-????-????-????-????????????";
    const user = `USER#${subject}`;
    const enrollment = "CONTROL#PRINCIPAL_ENROLLMENT";
    const transaction = { "dynamodb:EnclosingOperation": "TransactWriteItems" };
    const failure = { "dynamodb:ReturnValues": "NONE" };
    items(auth, ["PutItem", "DeleteItem"], ["FLOW#*"]);
    items(auth, ["GetItem", "PutItem", "UpdateItem", "DeleteItem"], ["SESSION#*"]);
    items(auth, ["GetItem"], [user, enrollment]);
    checkItems(auth, [user, enrollment]);
    putAttributes(auth, [user], ["pk", "epoch", "suspended"]);

    const jobs = [`SCOPE#USER#${subject}#JOB#${subject}`, `SCOPE#USER#${subject}#JOBS`];
    const aggregates = `SCOPE#USER#${subject}#AGGREGATE#*`;
    items("cooking-jobs", ["GetItem", "Query"], [...jobs, aggregates]);
    items("cooking-jobs", ["PutItem", "UpdateItem", "DeleteItem"], [...jobs, aggregates], true);
    checkItems("cooking-jobs", [...jobs, aggregates]);
    const streams = ["?", "??"].map(length => `STREAM#14:synthetic-rest|21:mockprofile-100-a-001|${length}:batch-a-*`);
    putAttributes("cooking-jobs", streams, ["pk", "sk", "job_pk", "job_id"]);
    items("data-flow", ["GetItem", "Query"], streams);
    // Protected review adds independent condition checks through TonkatsuRecipeAccessStack.
    allow(["cognito-idp:AdminGetUser"], [pool.valueAsString], { StringEquals: regional });
    allow(["kms:GenerateDataKey", "kms:Decrypt"], [key.valueAsString], {
      StringEquals: {
        ...regional,
        "kms:EncryptionContext:purpose": "COGNITO_REFRESH_TOKEN",
        "kms:EncryptionContext:environment": stage,
        "kms:EncryptionContext:schema_version": "1",
        "kms:EncryptionContext:surface": "customer",
      },
      StringLike: { "kms:EncryptionContext:session_hash": "?".repeat(64) },
    });

    new CfnOutput(this, "BusinessTaskRoleArn", { value: role.roleArn });
    this.templateOptions.description = "Scoped customer task role for Tonkatsu development authentication and cooking";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
  }
}
