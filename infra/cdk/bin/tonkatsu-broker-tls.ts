import { CfnOutput, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { tonkatsuBrokerTlsName } from "./tonkatsu-broker-task.js";

function createTlsStore(stack: Stack, prefix: string, name: string, service: string): void {
  const keyPolicy = JSON.parse(
    readFileSync(join(__dirname, "../../containers/private-rest-key-policy.json"), "utf8")
      .replaceAll("__ACCOUNT__", stack.account)
      .replaceAll("__REGION__", stack.region)
      .replace("__SECRET_NAME__-*", `${name}-??????`),
  );
  const key = new kms.CfnKey(stack, `${prefix}TlsKey`, {
    description: `Regional storage key for the dedicated Tonkatsu ${service} TLS bundle`,
    enabled: true,
    enableKeyRotation: true,
    multiRegion: false,
    keyPolicy,
    keySpec: "SYMMETRIC_DEFAULT",
    keyUsage: "ENCRYPT_DECRYPT",
    pendingWindowInDays: 7,
  });
  key.applyRemovalPolicy(RemovalPolicy.RETAIN);
  key.addMetadata("com.aws.cloudformation.Context", {
    why: `Keep ${service} TLS encryption isolated to the dedicated Secret namespace`,
    must: ["Delegate cryptography only through regional Secrets Manager for this Secret name",
      "The execution role policy binds the complete ARN after stack creation"],
  });

  const secret = new secretsmanager.CfnSecret(stack, `${prefix}TlsBundle`, {
    name: name,
    description: `Empty Tonkatsu ${service} TLS store initialized separately under explicit review`,
    kmsKeyId: key.attrArn,
  });
  secret.applyRemovalPolicy(RemovalPolicy.RETAIN);
  secret.addMetadata("com.aws.cloudformation.Context", {
    why: `Store only the ${service} TLS bundle under the separate ${service} execution boundary`,
    must: ["Supply no Secret value or automatic rotation in this stack",
      "Review initialization and pin the resulting version ID before Task execution"],
  });
  new CfnOutput(stack, `${prefix}TlsBundleArn`, { value: secret.ref });
  new CfnOutput(stack, `${prefix}TlsKeyArn`, { value: key.attrArn });
}

export class TonkatsuBrokerTlsStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || this.region !== "ap-southeast-2" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account)) {
      throw new Error("Tonkatsu broker TLS requires dev and an explicit regional account");
    }

    createTlsStore(this, "Broker", tonkatsuBrokerTlsName, "broker");

    this.templateOptions.description = "Empty development Tonkatsu broker TLS bundle and dedicated KMS key";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
  }
}

export class TonkatsuPeerTlsStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || this.region !== "ap-southeast-2" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account)) {
      throw new Error("Tonkatsu peer TLS requires dev and an explicit regional account");
    }
    createTlsStore(this, "BusinessApi", "foodtech/tonkatsu-business-api-dev/tls", "business API");
    createTlsStore(this, "Gateway", "foodtech/tonkatsu-gateway-dev/tls", "Gateway");
    this.templateOptions.description = "Isolated empty development TLS bundles and keys for the Tonkatsu business API and Gateway";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
  }
}

export class TonkatsuInferenceTlsStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || this.region !== "ap-southeast-2" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account)) {
      throw new Error("Tonkatsu inference TLS requires dev and an explicit regional account");
    }
    createTlsStore(this, "Inference", "foodtech/tonkatsu-inference-dev/tls", "inference host");
    this.templateOptions.description = "Empty isolated development Tonkatsu inference TLS store and key";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
  }
}
