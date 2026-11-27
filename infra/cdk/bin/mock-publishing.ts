import { CfnOutput, CfnParameter, Duration, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

/** Image destination and main-only publisher; no workload/deployment permissions. */
export class MockPublishingStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) || !/^[0-9]{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("mock publisher requires dev and an explicit regional account");
    }
    this.templateOptions.description = "Mock image destination and manual main-only ECR publisher; no workload resources";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" },
      "com.aws.cloudformation.Context": { arch: "Reuse existing GitHub OIDC provider; keep mock and maintenance publishing separate",
        must: ["Explicit approval before applying IAM/repository", "No Task, network, scheduler, PassRole or business data access"] } };
    const provider = new CfnParameter(this, "GitHubOidcProviderArn", { type: "String",
      allowedPattern: `^arn:aws:iam::${this.account}:oidc-provider/token[.]actions[.]githubusercontent[.]com$`,
      description: "Verified existing GitHub OIDC provider; do not create another provider" });
    const subject = new CfnParameter(this, "VerifiedGitHubSubject", { type: "String",
      allowedValues: ["repo:davidots429/foodtech:ref:refs/heads/main",
        "repo:davidots429@182712727/foodtech@1439543152:ref:refs/heads/main"],
      description: "Exact subject from the main mock inspect workflow, without wildcard or default" });
    const mode = this.node.tryGetContext("mockImageRepositoryMode") ?? "create";
    if (mode !== "create" && mode !== "existing") throw new Error("mockImageRepositoryMode must be create or existing");
    const name = `foodtech/mock-${stage}`;
    const repository = mode === "existing" ? ecr.Repository.fromRepositoryName(this, "RetainedMockRepository", name)
      : new ecr.Repository(this, "MockRepository", { repositoryName: name,
        encryption: ecr.RepositoryEncryption.AES_256, imageTagMutability: ecr.TagMutability.IMMUTABLE,
        imageScanOnPush: true, emptyOnDelete: false, removalPolicy: RemovalPolicy.RETAIN });
    if (mode === "create") {
      const definition = repository.node.defaultChild as ecr.CfnRepository;
      definition.encryptionConfiguration = { encryptionType: "AES256" };
      definition.addMetadata("com.aws.cloudformation.Context", {
        why: "Retain reviewed mock image digests independently of temporary runtime networks",
        must: ["No automatic image deletion", "Existing mode references retained destination without importing or changing it"] });
    }
    const role = new iam.Role(this, "MockPublisher", { roleName: `foodtech-mock-publish-${stage}`,
      maxSessionDuration: Duration.hours(1),
      description: "Manual main-only mock image publication; no workload or infrastructure deployment",
      assumedBy: new iam.FederatedPrincipal(provider.valueAsString, { StringEquals: {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": subject.valueAsString,
      } }, "sts:AssumeRoleWithWebIdentity") });
    role.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:GetAuthorizationToken"], resources: ["*"],
      conditions: { StringEquals: { "aws:RequestedRegion": "ap-southeast-2" } } }));
    // Official ECR push policy plus metadata checks; no pull-through cache variant.
    role.addToPolicy(new iam.PolicyStatement({ actions: ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages", "ecr:DescribeRepositories"],
      resources: [repository.repositoryArn], conditions: { StringEquals: { "aws:RequestedRegion": "ap-southeast-2" } } }));
    (role.node.defaultChild as iam.CfnRole).addMetadata("com.aws.cloudformation.Context", {
      why: "Keep publishing authority separate from ECS execution, maintenance publisher and deployment",
      must: ["Exact repository-main subject and audience", "Existing provider only; never create resources from this role"] });
    ((role.node.findChild("DefaultPolicy") as iam.Policy).node.defaultChild as iam.CfnPolicy).addMetadata(
      "com.aws.cloudformation.Context", { why: "Official ECR single-repository push actions and destination/digest readback",
        must: ["No pull-through cache import, repository creation or tagging", "Token permission cannot be repository-scoped"] });
    new CfnOutput(this, "MockPublishRoleArn", { value: role.roleArn });
    new CfnOutput(this, "MockRepositoryUri", { value: repository.repositoryUri });
  }
}
