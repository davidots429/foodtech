import { CfnOutput, CfnParameter, Fn, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as discovery from "aws-cdk-lib/aws-servicediscovery";
import { Construct } from "constructs";

const namespaceName = "tonkatsu.foodtech.internal";
const serviceNames = ["business-api", "broker", "gateway", "inference"];

export class TonkatsuPrivateNetworkStack extends Stack {
  override get availabilityZones(): string[] { return [Fn.ref("AvailabilityZone")]; }

  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Tonkatsu private network requires dev and an explicit regional account");
    }
    const mode = this.node.tryGetContext("tonkatsuPrivateNetworkMode") ?? "isolated";
    if (mode !== "isolated" && mode !== "managed-login") {
      throw new Error("tonkatsuPrivateNetworkMode must be isolated or managed-login");
    }
    const managedLogin = mode === "managed-login";
    new CfnParameter(this, "AvailabilityZone", {
      type: "AWS::EC2::AvailabilityZone::Name", allowedPattern: "^ap-southeast-2[a-z]$",
    });
    const s3Prefix = this.prefixList("S3PrefixListId");
    const dynamoPrefix = this.prefixList("DynamoDbPrefixListId");

    const isolated: ec2.SubnetConfiguration = { name: "isolated", subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 26 };
    const vpc = new ec2.Vpc(this, "Vpc", {
      ipAddresses: ec2.IpAddresses.cidr("10.44.0.0/24"),
      availabilityZones: this.availabilityZones,
      enableDnsSupport: true, enableDnsHostnames: true,
      natGateways: managedLogin ? 1 : 0,
      subnetConfiguration: managedLogin ? [
        isolated,
        { name: "nat", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 27, mapPublicIpOnLaunch: false },
        { name: "business", subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 27 },
      ] : [isolated],
    });
    const gatewaySubnets = [{ subnetType: ec2.SubnetType.PRIVATE_ISOLATED }];
    if (managedLogin) gatewaySubnets.push({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS });
    const securityGroup = (name: string, description: string) => {
      const group = new ec2.CfnSecurityGroup(this, name, {
        vpcId: vpc.vpcId, groupDescription: description,
        // CloudFormation installs allow-all egress when the group has no inline rule.
        // Keep the CDK no-traffic ICMP placeholder inline and add real rules separately
        // so mutual group references do not create CloudFormation dependency cycles.
        securityGroupEgress: [{ ipProtocol: "icmp", fromPort: 252, toPort: 86,
          cidrIp: "255.255.255.255/32", description: "Suppress default allow-all egress" }],
      });
      return ec2.SecurityGroup.fromSecurityGroupId(this, `${name}Rules`, group.attrGroupId, {
        allowAllOutbound: false, mutable: true,
      });
    };
    const tester = securityGroup("Tester", "Private test client to business API only");
    const business = securityGroup("Business", "Customer BFF with private broker and record connections");
    const broker = securityGroup("Broker", "Private source access broker");
    const gateway = securityGroup("Gateway", "Private invocation and records listeners");
    const inference = securityGroup("Inference", "Private TLS relay with loopback-only inference");
    const imageAccess = securityGroup("ImageAccess", "ECS image acquisition, logs and TLS Secret VPC endpoints");
    const workflowAccess = securityGroup("WorkflowAccess", "STS endpoint for broker and Gateway");
    const connect = (source: ec2.ISecurityGroup, destination: ec2.ISecurityGroup, port: number, reason: string) => {
      source.addEgressRule(destination, ec2.Port.tcp(port), reason);
      destination.addIngressRule(source, ec2.Port.tcp(port), reason);
    };
    connect(tester, business, 8080, "Private test client");
    connect(business, broker, 8446, "BFF broker mTLS");
    connect(business, gateway, 8447, "BFF records mTLS");
    connect(broker, gateway, 8445, "Broker invocation mTLS");
    connect(gateway, inference, 8448, "Gateway inference mTLS");
    for (const task of [business, broker, gateway, inference]) {
      connect(task, imageAccess, 443, "Regional image, logs and TLS endpoint");
      task.addEgressRule(ec2.Peer.prefixList(s3Prefix), ec2.Port.tcp(443), "ECR image layers through S3 endpoint");
    }
    for (const task of [business, broker, gateway]) {
      task.addEgressRule(ec2.Peer.prefixList(dynamoPrefix), ec2.Port.tcp(443), "Private DynamoDB endpoint");
    }
    connect(broker, workflowAccess, 443, "Broker scoped STS delegation");
    connect(gateway, workflowAccess, 443, "Gateway verifies scoped STS identities");

    const scopedPolicy = (actions: string[], resources: string[]) => new iam.PolicyStatement({
      principals: [new iam.AnyPrincipal()], actions, resources,
      conditions: { StringEquals: { "aws:PrincipalAccount": this.account } },
    });
    const addEndpoint = (name: string, service: ec2.InterfaceVpcEndpointService, group: ec2.ISecurityGroup,
                         policies: iam.PolicyStatement[]) => {
      const endpoint = vpc.addInterfaceEndpoint(name, {
        service, open: false, privateDnsEnabled: true, securityGroups: [group],
        subnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      });
      for (const policy of policies) endpoint.addToPolicy(policy);
    };
    const repos = ["broker", "gateway", "ollama"].map(name =>
      `arn:aws:ecr:${this.region}:${this.account}:repository/foodtech/tonkatsu-${name}-dev`);
    const pulls = scopedPolicy(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], repos);
    addEndpoint("EcrApi", ec2.InterfaceVpcEndpointAwsService.ECR, imageAccess, [
      scopedPolicy(["ecr:GetAuthorizationToken"], ["*"]), pulls,
    ]);
    addEndpoint("EcrDocker", ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER, imageAccess, [pulls]);
    const streams = serviceNames.flatMap(service => {
      const names = service === "gateway" ? ["gateway", "records"] : service === "inference" ? ["relay", "ollama"] : [service];
      return names.map(name =>
        `arn:aws:logs:${this.region}:${this.account}:log-group:/foodtech/dev/tonkatsu-${service}:log-stream:${name}/${name}/*`);
    });
    addEndpoint("Logs", ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS, imageAccess, [
      scopedPolicy(["logs:CreateLogStream", "logs:PutLogEvents"], streams),
    ]);
    addEndpoint("Secrets", ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER, imageAccess, [
      scopedPolicy(["secretsmanager:GetSecretValue"], serviceNames.map(name =>
        `arn:aws:secretsmanager:${this.region}:${this.account}:secret:foodtech/tonkatsu-${name}-dev/tls-??????`)),
    ]);
    addEndpoint("Sts", ec2.InterfaceVpcEndpointAwsService.STS, workflowAccess, [
      scopedPolicy(["sts:GetCallerIdentity"], ["*"]),
      scopedPolicy(["sts:AssumeRole"], [`arn:aws:iam::${this.account}:role/foodtech-tonkatsu-source-read-dev`]),
    ]);
    if (managedLogin) {
      const authAccess = securityGroup("AuthAccess", "Customer BFF refresh encryption VPC endpoint");
      const refreshKey = new CfnParameter(this, "AuthRefreshKeyArn", {
        type: "String", allowedPattern: `^arn:aws:kms:${this.region}:${this.account}:key/[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$`,
      });
      connect(business, authAccess, 443, "Customer session refresh encryption");
      addEndpoint("Kms", ec2.InterfaceVpcEndpointAwsService.KMS, authAccess, [
        scopedPolicy(["kms:GenerateDataKey", "kms:Decrypt"], [refreshKey.valueAsString]),
      ]);
      business.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), "Cognito hosted OAuth and issuer HTTPS via NAT");
    }
    const s3 = vpc.addGatewayEndpoint("S3", { service: ec2.GatewayVpcEndpointAwsService.S3, subnets: gatewaySubnets });
    s3.addToPolicy(new iam.PolicyStatement({
      principals: [new iam.AnyPrincipal()], actions: ["s3:GetObject"],
      resources: [`arn:aws:s3:::prod-${this.region}-starport-layer-bucket/*`],
    }));
    const dynamo = vpc.addGatewayEndpoint("Dynamo", { service: ec2.GatewayVpcEndpointAwsService.DYNAMODB, subnets: gatewaySubnets });
    dynamo.addToPolicy(scopedPolicy(
      ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"],
      ["auth-sessions", "cooking-jobs", "data-flow", "ai-execution-registry"].map(name =>
        `arn:aws:dynamodb:${this.region}:${this.account}:table/foodtech-${name}-dev`),
    ));

    const cluster = new ecs.CfnCluster(this, "Cluster", {
      clusterName: "foodtech-tonkatsu-private-dev", clusterSettings: [{ name: "containerInsights", value: "disabled" }],
    });
    const namespace = new discovery.CfnPrivateDnsNamespace(this, "Namespace", { vpc: vpc.vpcId, name: namespaceName });
    for (const [label, name] of [["Business", "business-api"], ["Broker", "broker"],
                                 ["Gateway", "gateway"], ["Inference", "inference"]]) {
      const registration = new discovery.CfnService(this, `${label}Discovery`, {
        name, namespaceId: namespace.attrId, dnsConfig: { routingPolicy: "MULTIVALUE", dnsRecords: [{ type: "A", ttl: 10 }] },
        // ECS publishes private task health when the service attaches this registry.
        healthCheckCustomConfig: { failureThreshold: 1 },
      });
      new CfnOutput(this, `${label}DiscoveryArn`, { value: registration.attrArn });
      new CfnOutput(this, `${label}Host`, { value: `${name}.${namespaceName}` });
    }
    new CfnOutput(this, "VpcId", { value: vpc.vpcId });
    new CfnOutput(this, "ClusterArn", { value: cluster.attrArn });
    new CfnOutput(this, "IsolatedSubnetId", { value: vpc.isolatedSubnets[0].subnetId });
    new CfnOutput(this, "BusinessSubnetId", { value: managedLogin ? vpc.privateSubnets[0].subnetId : vpc.isolatedSubnets[0].subnetId });
    for (const [name, group] of Object.entries({ Tester: tester, Business: business, Broker: broker, Gateway: gateway, Inference: inference })) {
      new CfnOutput(this, `${name}SecurityGroupId`, { value: group.securityGroupId });
    }
    this.templateOptions.description = `Development Tonkatsu private network with ${mode} egress and no running tasks`;
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
  }

  private prefixList(name: string): string {
    return new CfnParameter(this, name, { type: "String", allowedPattern: "^pl-[0-9a-f]{8,17}$" }).valueAsString;
  }
}
