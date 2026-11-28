import { CfnOutput, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

export class TonkatsuGatewayTaskRoleStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Tonkatsu Gateway task role requires dev and an explicit regional account");
    }
    const role = new iam.Role(this, "GatewayTaskRole", {
      roleName: "foodtech-tonkatsu-gateway-dev",
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com").withConditions({
        StringEquals: { "aws:SourceAccount": this.account },
        ArnLike: { "aws:SourceArn": `arn:aws:ecs:${this.region}:${this.account}:*` },
      }),
    });
    const registry = `arn:aws:dynamodb:${this.region}:${this.account}:table/foodtech-ai-execution-registry-${stage}`;
    const uuid = "????????-????-????-????-????????????";
    const execution = `EXEC#${uuid}`;
    const active = `ACTIVE#${uuid}`;
    const model = "MODEL#llama3.1:8b";
    const limits = [`LIMIT#????-??#TENANT#${uuid}`, "LIMIT#????-??#PROVIDER#ollama", "LIMIT#????-??#PROJECT#GLOBAL"];
    const permit = (action: string, leadingKeys: string[], transactionalWrite = false) => {
      role.addToPolicy(new iam.PolicyStatement({
        actions: [`dynamodb:${action}`],
        resources: [registry],
        conditions: {
          "ForAllValues:StringLike": { "dynamodb:LeadingKeys": leadingKeys },
          Null: { "dynamodb:LeadingKeys": "false" },
          StringEquals: {
            "aws:RequestedRegion": this.region,
            ...(transactionalWrite ? { "dynamodb:EnclosingOperation": "TransactWriteItems" } : {}),
          },
          ...(action !== "GetItem" ? { StringEqualsIfExists: { "dynamodb:ReturnValues": "NONE" } } : {}),
        },
      }));
    };

    // The recipe access stack adds the guarded source reads and source checks.
    permit("GetItem", [execution, model, ...limits]);
    permit("ConditionCheckItem", [model, ...limits]);
    permit("PutItem", [execution, active], true);
    permit("UpdateItem", [execution, ...limits], true);
    permit("DeleteItem", [active], true);

    new CfnOutput(this, "GatewayTaskRoleArn", { value: role.roleArn });
    this.templateOptions.description = "Tonkatsu Gateway application role scoped to local-model execution state and limits";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
  }
}
