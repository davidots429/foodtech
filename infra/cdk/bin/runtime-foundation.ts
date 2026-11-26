import { CfnOutput, CfnParameter, Fn, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

/** Development network and image destination only; never starts a workload. */
export class RuntimeFoundationStack extends Stack {
  // The deployment parameter is authoritative. Do not resolve account AZs via
  // synth-time credentials, including inside downstream L2 constructs.
  override get availabilityZones(): string[] { return [Fn.ref("AvailabilityZone")]; }

  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (Token.isUnresolved(this.account) || !/^\d{12}$/.test(this.account)) {
      throw new Error("runtime foundation requires an explicit verified CDK account");
    }
    if (this.node.tryGetContext("runtimeEgressMode") !== "nat") {
      throw new Error("runtimeEgressMode=nat is required: the NAT gateway incurs idle charges");
    }
    const repositoryMode = this.node.tryGetContext("runtimeImageRepositoryMode") ?? "create";
    if (repositoryMode !== "create" && repositoryMode !== "existing") {
      throw new Error("runtimeImageRepositoryMode must be create or existing");
    }
    const zone = new CfnParameter(this, "AvailabilityZone", {
      type: "AWS::EC2::AvailabilityZone::Name",
      description: "Reviewed ap-southeast-2 availability zone for the single-AZ development runtime",
      allowedPattern: "^ap-southeast-2[a-z]$",
    });
    const vpc = new ec2.Vpc(this, "RuntimeVpc", {
      vpcName: `foodtech-runtime-${stage}`,
      ipAddresses: ec2.IpAddresses.cidr("10.42.0.0/24"),
      availabilityZones: [zone.valueAsString], natGateways: 1,
      enableDnsSupport: true, enableDnsHostnames: true,
      subnetConfiguration: [
        { name: "egress", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 27, mapPublicIpOnLaunch: false },
        { name: "tasks", subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 27 },
      ],
    });
    const tasks = new ec2.SecurityGroup(this, "TaskSecurityGroup", {
      vpc, description: "No inbound access; HTTPS egress for reviewed maintenance tasks",
      allowAllOutbound: false, allowAllIpv6Outbound: false,
    });
    // Port restriction only: this is not an AWS-hostname or provider allowlist.
    tasks.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), "HTTPS via NAT or gateway endpoints");

    const dynamo = vpc.addGatewayEndpoint("DynamoDbEndpoint", {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
      subnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
    });
    const tableNames = ["auth-sessions", "cooking-jobs", "data-flow", "ai-execution-registry",
      "ai-credential-vault", "identity-vault", "link-capsule", "deletion-registry", "deletion-progress", "tier-requests"];
    // Endpoint resource policies are an additional boundary, not role grants.
    dynamo.addToPolicy(new iam.PolicyStatement({
      principals: [new iam.AnyPrincipal()],
      actions: ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:PutItem",
        "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"],
      resources: tableNames.flatMap(name => {
        const arn = this.formatArn({ service: "dynamodb", resource: "table", resourceName: `foodtech-${name}-${stage}` });
        return [arn, `${arn}/index/*`];
      }), conditions: { StringEquals: { "aws:PrincipalAccount": this.account } },
    }));
    const s3 = vpc.addGatewayEndpoint("ImageLayerEndpoint", {
      service: ec2.GatewayVpcEndpointAwsService.S3,
      subnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
    });
    // ECR image layer downloads use the regional ECR-owned S3 bucket.
    s3.addToPolicy(new iam.PolicyStatement({ principals: [new iam.AnyPrincipal()],
      actions: ["s3:GetObject"], resources: [`arn:${this.partition}:s3:::prod-${this.region}-starport-layer-bucket/*`] }));

    const cluster = new ecs.Cluster(this, "RuntimeCluster", {
      vpc, clusterName: `foodtech-runtime-${stage}`,
      containerInsightsV2: ecs.ContainerInsights.DISABLED,
    });
    // Explicit existing mode references the retained image destination without
    // taking ownership, changing its policy/settings, or recreating its name.
    const repository = repositoryMode === "existing"
      ? ecr.Repository.fromRepositoryName(this, "RetainedMaintenanceImageRepository", `foodtech/maintenance-${stage}`)
      : new ecr.Repository(this, "MaintenanceImageRepository", {
      repositoryName: `foodtech/maintenance-${stage}`,
      encryption: ecr.RepositoryEncryption.AES_256,
      imageTagMutability: ecr.TagMutability.IMMUTABLE,
      imageScanOnPush: true, removalPolicy: RemovalPolicy.RETAIN,
      emptyOnDelete: false,
    });
    new CfnOutput(this, "VpcId", { value: vpc.vpcId });
    new CfnOutput(this, "ClusterArn", { value: cluster.clusterArn });
    new CfnOutput(this, "PrivateSubnetIds", { value: vpc.privateSubnets.map(subnet => subnet.subnetId).join(",") });
    new CfnOutput(this, "TaskSecurityGroupId", { value: tasks.securityGroupId });
    new CfnOutput(this, "ImageRepositoryArn", { value: repository.repositoryArn });
    new CfnOutput(this, "ImageRepositoryUri", { value: repository.repositoryUri });
    new CfnOutput(this, "DynamoDbEndpointId", { value: dynamo.vpcEndpointId });
    new CfnOutput(this, "ImageLayerEndpointId", { value: s3.vpcEndpointId });
  }
}
