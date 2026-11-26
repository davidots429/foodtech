import { CfnOutput, CfnParameter, Duration, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

/** Image publisher only: never an infrastructure deployer or workload role. */
export class MaintenancePublishingStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (Token.isUnresolved(this.account) || !/^\d{12}$/.test(this.account)) {
      throw new Error("maintenance publisher requires an explicit verified CDK account");
    }
    const subject = new CfnParameter(this, "VerifiedGitHubSubject", {
      type: "String",
      allowedValues: ["repo:davidots429/foodtech:ref:refs/heads/main",
        "repo:davidots429@182712727/foodtech@1439543152:ref:refs/heads/main"],
      description: "Exact subject observed by main-branch inspect workflow; no wildcard or default",
    });
    let providerArn: string;
    if (this.node.tryGetContext("createGitHubOidcProvider") === "true") {
      const provider = new iam.CfnOIDCProvider(this, "GitHubOidc", {
        url: "https://token.actions.githubusercontent.com", clientIdList: ["sts.amazonaws.com"],
      });
      provider.applyRemovalPolicy(RemovalPolicy.RETAIN);
      providerArn = provider.attrArn;
    } else {
      providerArn = new CfnParameter(this, "GitHubOidcProviderArn", {
        type: "String",
        allowedPattern: `^arn:aws:iam::${this.account}:oidc-provider/token[.]actions[.]githubusercontent[.]com$`,
        description: "Reviewed existing GitHub OIDC provider in this account; never create a duplicate",
      }).valueAsString;
    }
    const role = new iam.Role(this, "MaintenancePublisher", {
      roleName: `foodtech-maintenance-publish-${stage}`,
      description: "Manual main-branch maintenance image publishing; no deployment or data access",
      maxSessionDuration: Duration.hours(1),
      assumedBy: new iam.FederatedPrincipal(providerArn, { StringEquals: {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": subject.valueAsString,
      } }, "sts:AssumeRoleWithWebIdentity"),
    });
    role.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:GetAuthorizationToken"], resources: ["*"],
      conditions: { StringEquals: { "aws:RequestedRegion": "ap-southeast-2" } } }));
    role.addToPolicy(new iam.PolicyStatement({
      actions: ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages"],
      resources: [this.formatArn({ service: "ecr", resource: "repository", resourceName: `foodtech/maintenance-${stage}` })],
      conditions: { StringEquals: { "aws:RequestedRegion": "ap-southeast-2" } },
    }));
    new CfnOutput(this, "MaintenancePublishRoleArn", { value: role.roleArn });
    new CfnOutput(this, "ReviewedGitHubOidcProviderArn", { value: providerArn });
  }
}
