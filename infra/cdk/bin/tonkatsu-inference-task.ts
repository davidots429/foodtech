import { CfnOutput, CfnParameter, Fn, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { Construct } from "constructs";
import { tonkatsuListenerHealth } from "./tonkatsu-private-services.js";

const relayFields = {
  SERVER_CERT: "inference_server_cert_pem",
  SERVER_KEY: "inference_server_key_pem",
  CLIENT_CA: "inference_client_ca_pem",
  CLIENT_CERT: "inference_client_cert_pem",
};

export class TonkatsuInferenceTaskStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Tonkatsu inference task requires dev and an explicit regional account");
    }
    const parameter = (name: string, allowedPattern: string) =>
      new CfnParameter(this, name, { type: "String", allowedPattern }).valueAsString;
    const image = (name: string, repository: string) => parameter(name,
      `^${this.account}\\.dkr\\.ecr\\.${this.region}\\.amazonaws\\.com/foodtech/${repository}@sha256:[a-f0-9]{64}$`);
    const executionRole = parameter("InferenceExecutionRoleArn",
      `^arn:aws:iam::${this.account}:role/foodtech-tonkatsu-inference-execution-dev$`);
    const secret = parameter("InferenceTlsBundleArn",
      `^arn:aws:secretsmanager:${this.region}:${this.account}:secret:foodtech/tonkatsu-inference-dev/tls-[A-Za-z0-9]{6}$`);
    const version = parameter("InferenceTlsBundleVersion", "^[A-Za-z0-9-]{32,64}$");
    const container = (name: string, uri: string, user: string, directory: string,
                       environment: Record<string, string>): ecs.CfnTaskDefinition.ContainerDefinitionProperty => ({
      name, essential: true, image: uri, user, readonlyRootFilesystem: true, privileged: false,
      linuxParameters: { capabilities: { drop: ["ALL"] } },
      mountPoints: [{ sourceVolume: `${name}-runtime`, containerPath: directory, readOnly: false }],
      environment: Object.entries(environment).map(([envName, value]) => ({ name: envName, value })),
      logConfiguration: { logDriver: "awslogs", options: {
        "awslogs-group": "/foodtech/dev/tonkatsu-inference",
        "awslogs-region": this.region,
        "awslogs-stream-prefix": name,
      } },
    });
    const relay = container("relay", image("RelayImageUri", "tonkatsu-gateway-dev"), "10001:10001",
      "/run/foodtech-tls", {
        FOODTECH_SERVICE: "tonkatsu-inference",
        FOODTECH_TONKATSU_INFERENCE_ENABLED: "true",
        FOODTECH_TONKATSU_INFERENCE_BIND_HOST: "0.0.0.0",
        FOODTECH_TONKATSU_INFERENCE_PORT: "8448",
        PYTHONDONTWRITEBYTECODE: "1",
        TMPDIR: "/run/foodtech-tls",
      });
    const ollama = container("ollama", image("OllamaImageUri", "tonkatsu-ollama-dev"), "65532:65532",
      "/run/foodtech-ollama", {
        OLLAMA_HOST: "127.0.0.1:11434",
        OLLAMA_NO_CLOUD: "1",
        OLLAMA_NOPRUNE: "1",
        OLLAMA_MAX_LOADED_MODELS: "1",
        OLLAMA_NUM_PARALLEL: "1",
      });

    const task = new ecs.CfnTaskDefinition(this, "InferenceTask", {
      family: "foodtech-tonkatsu-inference-dev",
      requiresCompatibilities: ["FARGATE"], networkMode: "awsvpc", executionRoleArn: executionRole,
      runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
      cpu: "4096", memory: "12288",
      volumes: [{ name: "relay-runtime" }, { name: "ollama-runtime" }],
      containerDefinitions: [
        { ...ollama, healthCheck: {
          command: ["CMD-SHELL", "/bin/ollama show llama3.1:8b >/dev/null"],
          interval: 20, retries: 3, timeout: 5, startPeriod: 60,
        } },
        { ...relay, dependsOn: [{ containerName: "ollama", condition: "HEALTHY" }],
          portMappings: [{ containerPort: 8448, protocol: "tcp" }],
          healthCheck: tonkatsuListenerHealth(8448),
          secrets: Object.entries(relayFields).map(([suffix, field]) => ({
            name: `FOODTECH_TONKATSU_INFERENCE_${suffix}_PEM`,
            valueFrom: Fn.join("", [secret, `:${field}::`, version]),
          })),
        },
      ],
    });
    task.addMetadata("com.aws.cloudformation.Context", {
      why: "Isolate local inference from the Gateway business task role",
      must: ["Omit TaskRoleArn", "Bind Ollama to loopback and restrict relay ingress and task egress at launch"],
    });
    this.templateOptions.description = "Development Tonkatsu private CPU inference task without workload IAM";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
    new CfnOutput(this, "InferenceTaskDefinitionArn", { value: task.ref });
  }
}
