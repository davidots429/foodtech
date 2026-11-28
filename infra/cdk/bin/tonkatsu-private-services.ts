import { CfnOutput, CfnParameter, CfnRule, Fn, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { Construct } from "constructs";

const workloads = [
  { label: "Inference", name: "inference" },
  { label: "Gateway", name: "gateway" },
  { label: "Broker", name: "broker" },
  { label: "Business", name: "business-api" },
];

export function tonkatsuListenerHealth(port: number): ecs.CfnTaskDefinition.HealthCheckProperty {
  return {
    command: ["CMD", "python", "-c",
      `import socket; socket.create_connection(("127.0.0.1", ${port}), timeout=3).close()`],
    interval: 15, timeout: 5, retries: 3, startPeriod: 45,
  };
}

export class TonkatsuPrivateServicesStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Tonkatsu private services require dev and an explicit regional account");
    }
    const pattern = (name: string, allowedPattern: string, description: string) =>
      new CfnParameter(this, name, { type: "String", allowedPattern, description }).valueAsString;
    const ecsArn = `arn:aws:ecs:${this.region}:${this.account}`;
    const cluster = pattern("ClusterArn", `^${ecsArn}:cluster/foodtech-tonkatsu-private-dev$`,
      "ClusterArn from the reviewed private network");
    const isolatedSubnet = pattern("IsolatedSubnetId", "^subnet-(?:[a-f0-9]{8}|[a-f0-9]{17})$",
      "IsolatedSubnetId from the reviewed private network");
    const businessSubnet = pattern("BusinessSubnetId", "^subnet-(?:[a-f0-9]{8}|[a-f0-9]{17})$",
      "BusinessSubnetId from the reviewed private network");
    const count = new CfnParameter(this, "DesiredCountPerService", {
      type: "Number", allowedValues: ["0", "1"], default: 0,
      description: "Start or stop the four development services together during the approved trial",
    });
    const groups: string[] = [];
    const registries: string[] = [];
    let precedingService: ecs.CfnService | undefined;
    for (const { label, name } of workloads) {
      const serviceName = `foodtech-tonkatsu-${name}-dev`;
      const task = pattern(`${label}TaskDefinitionArn`, `^${ecsArn}:task-definition/${serviceName}:[1-9][0-9]*$`,
        "Exact reviewed task definition revision with pinned images and TLS version");
      const securityGroup = pattern(`${label}SecurityGroupId`, "^sg-(?:[a-f0-9]{8}|[a-f0-9]{17})$",
        "Corresponding service security group from the reviewed private network");
      const registry = pattern(`${label}DiscoveryArn`, `^arn:aws:servicediscovery:${this.region}:${this.account}:service/srv-[a-z0-9]+$`,
        "Corresponding A-record Cloud Map service from the reviewed private network");
      groups.push(securityGroup);
      registries.push(registry);
      const service = new ecs.CfnService(this, `${label}Service`, {
        serviceName, cluster, taskDefinition: task,
        desiredCount: count.valueAsNumber, launchType: "FARGATE", platformVersion: "1.4.0",
        schedulingStrategy: "REPLICA",
        deploymentController: { type: "ECS" },
        deploymentConfiguration: {
          minimumHealthyPercent: 0, maximumPercent: 100,
          deploymentCircuitBreaker: { enable: true, rollback: true },
        },
        networkConfiguration: { awsvpcConfiguration: {
          assignPublicIp: "DISABLED",
          subnets: [label === "Business" ? businessSubnet : isolatedSubnet],
          securityGroups: [securityGroup],
        } },
        serviceRegistries: [{ registryArn: registry }],
        enableExecuteCommand: false, enableEcsManagedTags: true, propagateTags: "SERVICE",
      });
      if (precedingService) service.addResourceDependency(precedingService);
      precedingService = service;
      new CfnOutput(this, `${label}ServiceArn`, { value: service.ref });
    }
    for (const [name, values] of [["SecurityGroups", groups], ["DiscoveryRegistries", registries]] as const) {
      new CfnRule(this, `Distinct${name}`, {
        assertions: values.flatMap((value, index) => values.slice(index + 1).map(other => ({
          assert: Fn.conditionNot(Fn.conditionEquals(value, other)),
          assertDescription: `${name} must be distinct across the four services`,
        }))),
      });
    }
    this.templateOptions.description = "Tonkatsu development ECS services with fixed task revisions and private discovery";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
  }
}
