import { CfnOutput, CfnParameter, Duration, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

class TonkatsuPublishingStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string, service: "broker" | "gateway") {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error(`Tonkatsu ${service} publishing requires dev and an explicit regional account`);
    }
    const prefix = service === "broker" ? "Broker" : "Gateway";
    const provider = new CfnParameter(this, "GitHubOidcProviderArn", {
      type: "String",
      allowedPattern: `^arn:aws:iam::${this.account}:oidc-provider/token[.]actions[.]githubusercontent[.]com$`,
    });
    const subject = new CfnParameter(this, "VerifiedGitHubSubject", {
      type: "String",
      allowedValues: [
        "repo:davidots429/foodtech:ref:refs/heads/main",
        "repo:davidots429@182712727/foodtech@1439543152:ref:refs/heads/main",
      ],
    });
    const repository = new ecr.Repository(this, `${prefix}Repository`, {
      repositoryName: `foodtech/tonkatsu-${service}-dev`,
      encryption: ecr.RepositoryEncryption.AES_256,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      imageScanOnPush: true,
      emptyOnDelete: false,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    (repository.node.defaultChild as ecr.CfnRepository).encryptionConfiguration = { encryptionType: "AES256" };
    const role = new iam.Role(this, `${prefix}Publisher`, {
      roleName: `foodtech-tonkatsu-${service}-publish-dev`,
      maxSessionDuration: Duration.hours(1),
      assumedBy: new iam.FederatedPrincipal(provider.valueAsString, { StringEquals: {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": subject.valueAsString,
      } }, "sts:AssumeRoleWithWebIdentity"),
    });
    const conditions = { StringEquals: { "aws:RequestedRegion": this.region } };
    role.addToPolicy(new iam.PolicyStatement({
      actions: ["ecr:GetAuthorizationToken"], resources: ["*"], conditions,
    }));
    role.addToPolicy(new iam.PolicyStatement({
      actions: ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages", "ecr:DescribeRepositories"],
      resources: [repository.repositoryArn], conditions,
    }));
    this.templateOptions.description = `Manual main-branch publication for the private Tonkatsu ${service} image`;
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
    new CfnOutput(this, `${prefix}PublishRoleArn`, { value: role.roleArn });
    new CfnOutput(this, `${prefix}RepositoryUri`, { value: repository.repositoryUri });
  }
}

export class TonkatsuBrokerPublishingStack extends TonkatsuPublishingStack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props, stage, "broker");
  }
}

export class TonkatsuGatewayPublishingStack extends TonkatsuPublishingStack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props, stage, "gateway");
  }
}
