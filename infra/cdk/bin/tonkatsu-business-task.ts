import { CfnOutput, CfnParameter, Fn, Stack, StackProps, Token } from "aws-cdk-lib";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { Construct } from "constructs";
import { tonkatsuListenerHealth } from "./tonkatsu-private-services.js";

const tlsFields = {
  BROKER_SERVER_CERT: "broker_server_cert_pem",
  BFF_CERT: "bff_cert_pem",
  BFF_KEY: "bff_key_pem",
  GATEWAY_RECORDS_SERVER_CERT: "records_server_cert_pem",
  GATEWAY_RECORDS_CLIENT_CERT: "records_client_cert_pem",
  GATEWAY_RECORDS_CLIENT_KEY: "records_client_key_pem",
} as const;

export class TonkatsuBusinessTaskStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account) || this.region !== "ap-southeast-2") {
      throw new Error("Tonkatsu business API task requires dev and an explicit regional account");
    }
    const parameter = (name: string, allowedPattern: string, description: string) =>
      new CfnParameter(this, name, { type: "String", allowedPattern, description }).valueAsString;

    const service = "business-api";
    const name = `foodtech-tonkatsu-${service}-dev`;
    const repository = "foodtech/tonkatsu-broker-dev";
    const iamRole = `^arn:aws:iam::${this.account}:role/`;
    const taskRole = parameter("BusinessTaskRoleArn", `${iamRole}${name}$`, "Reviewed business API task role");
    const executionRole = parameter("BusinessExecutionRoleArn", `${iamRole}foodtech-tonkatsu-business-api-execution-dev$`,
      "Reviewed image, logging and dedicated TLS execution role");
    const image = parameter("BusinessImageUri",
      `^${this.account}\\.dkr\\.ecr\\.${this.region}\\.amazonaws\\.com/${repository}@sha256:[a-f0-9]{64}$`,
      "Reviewed shared business package image digest");
    const secret = parameter("BusinessTlsBundleArn",
      `^arn:aws:secretsmanager:${this.region}:${this.account}:secret:foodtech/tonkatsu-business-api-dev/tls-[A-Za-z0-9]{6}$`,
      "Dedicated business API TLS bundle");
    const version = parameter("BusinessTlsBundleVersion", "^[A-Za-z0-9-]{32,64}$",
      "Reviewed immutable version containing the broker and records client credentials");

    const label = "[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?";
    const privateOrigin = (port: number) => `^https://${label}(?:[.]${label})*[.]internal:${port}$`;
    const broker = parameter("BrokerPrivateOrigin", privateOrigin(8446), "Broker hostname covered by its TLS certificate");
    const records = parameter("RecordsPrivateOrigin", privateOrigin(8447), "Records hostname covered by its TLS certificate");
    const webOrigin = `https://${label}(?:[.]${label})+`;
    const callbackPattern = `^${webOrigin}/[A-Za-z0-9/_-]+$`;
    const issuer = parameter("CognitoIssuer",
      "^https://cognito-idp[.]ap-southeast-2[.]amazonaws[.]com/ap-southeast-2_[A-Za-z0-9]+$", "Reviewed development pool issuer");
    const domain = parameter("CognitoDomain", `^${webOrigin}$`, "Reviewed Cognito HTTPS host");
    const refreshKey = parameter("AuthRefreshKeyArn",
      `^arn:aws:kms:${this.region}:${this.account}:key/[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$`,
      "Existing session refresh encryption key");

    const tlsDirectory = "/run/foodtech-tls";
    const variables = {
      FOODTECH_SERVICE: "tonkatsu-business-api",
      FOODTECH_STAGE: stage,
      FOODTECH_ENVIRONMENT: stage,
      FOODTECH_SECURE_COOKIES: "true",
      FOODTECH_PRINCIPAL_PROVISIONING_ENABLED: "true",
      FOODTECH_TONKATSU_CUSTOMER_INVOCATION_ENABLED: "true",
      FOODTECH_TONKATSU_GATEWAY_RECORDS_ENABLED: "true",
      FOODTECH_TONKATSU_BUSINESS_BIND_HOST: "0.0.0.0",
      FOODTECH_TONKATSU_BUSINESS_PORT: "8080",
      FOODTECH_TONKATSU_BROKER_URL: broker,
      FOODTECH_TONKATSU_GATEWAY_RECORDS_URL: records,
      FOODTECH_AUTH_TABLE: "foodtech-auth-sessions-dev",
      FOODTECH_AUTH_REFRESH_KEY_ARN: refreshKey,
      FOODTECH_COOKING_TABLE: "foodtech-cooking-jobs-dev",
      FOODTECH_DATA_TABLE: "foodtech-data-flow-dev",
      FOODTECH_COGNITO_ISSUER: issuer,
      FOODTECH_COGNITO_DOMAIN: domain,
      FOODTECH_CUSTOMER_CLIENT_ID: parameter("CustomerClientId", "^[A-Za-z0-9]{1,128}$", "Development customer client"),
      FOODTECH_ADMIN_CLIENT_ID: parameter("AdminClientId", "^[A-Za-z0-9]{1,128}$", "Development administrator client"),
      FOODTECH_CUSTOMER_CALLBACK: parameter("CustomerCallback", callbackPattern, "Registered customer callback"),
      FOODTECH_ADMIN_CALLBACK: parameter("AdminCallback", callbackPattern, "Registered administrator callback"),
      FOODTECH_CUSTOMER_WEB: parameter("CustomerWeb", `^${webOrigin}$`, "Customer web origin"),
      FOODTECH_ADMIN_WEB: parameter("AdminWeb", `^${webOrigin}$`, "Administrator web origin"),
      PYTHONDONTWRITEBYTECODE: "1",
      TMPDIR: tlsDirectory,
    };
    const task = new ecs.CfnTaskDefinition(this, "BusinessTask", {
      family: name,
      requiresCompatibilities: ["FARGATE"],
      networkMode: "awsvpc",
      runtimePlatform: { operatingSystemFamily: "LINUX", cpuArchitecture: "X86_64" },
      cpu: "256",
      memory: "512",
      taskRoleArn: taskRole,
      executionRoleArn: executionRole,
      volumes: [{ name: "business-tls" }],
      containerDefinitions: [{
        name: service,
        essential: true,
        image,
        user: "10001:10001",
        readonlyRootFilesystem: true,
        privileged: false,
        stopTimeout: 30,
        linuxParameters: { capabilities: { drop: ["ALL"] } },
        mountPoints: [{ sourceVolume: "business-tls", containerPath: tlsDirectory, readOnly: false }],
        portMappings: [{ containerPort: 8080, protocol: "tcp" }],
        healthCheck: tonkatsuListenerHealth(8080),
        environment: Object.entries(variables).map(([envName, value]) => ({ name: envName, value })),
        secrets: Object.entries(tlsFields).map(([suffix, key]) => ({
          name: `FOODTECH_TONKATSU_${suffix}_PEM`,
          valueFrom: Fn.join("", [secret, `:${key}::`, version]),
        })),
        logConfiguration: { logDriver: "awslogs", options: {
          "awslogs-group": "/foodtech/dev/tonkatsu-business-api",
          "awslogs-region": this.region,
          "awslogs-stream-prefix": service,
        } },
      }],
    });
    this.templateOptions.description = "Manual Tonkatsu business API task with pinned image and TLS version";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
    new CfnOutput(this, "BusinessTaskDefinitionArn", { value: task.ref });
  }
}
