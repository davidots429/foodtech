import { CfnOutput, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { tonkatsuBrokerTlsName } from "./tonkatsu-broker-task.js";

export class TonkatsuBrokerTlsStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || this.region !== "ap-southeast-2" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account)) {
      throw new Error("Tonkatsu broker TLS requires dev and an explicit regional account");
    }

    const keyPolicy = JSON.parse(
      readFileSync(join(__dirname, "../../containers/private-rest-key-policy.json"), "utf8")
        .replaceAll("__ACCOUNT__", this.account)
        .replaceAll("__REGION__", this.region)
        .replace("__SECRET_NAME__-*", `${tonkatsuBrokerTlsName}-??????`),
    );
    const key = new kms.CfnKey(this, "BrokerTlsKey", {
      description: "Regional storage key for the dedicated Tonkatsu broker TLS bundle",
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
      why: "Keep broker TLS encryption isolated to the dedicated Secret namespace",
      must: ["Delegate cryptography only through regional Secrets Manager for this Secret name",
        "The execution role policy binds the complete ARN after stack creation"],
    });

    const secret = new secretsmanager.CfnSecret(this, "BrokerTlsBundle", {
      name: tonkatsuBrokerTlsName,
      description: "Empty Tonkatsu broker TLS store initialized separately under explicit review",
      kmsKeyId: key.attrArn,
    });
    secret.applyRemovalPolicy(RemovalPolicy.RETAIN);
    secret.addMetadata("com.aws.cloudformation.Context", {
      why: "Store only the broker TLS bundle under the separate broker execution boundary",
      must: ["Supply no Secret value or automatic rotation in this stack",
        "Review initialization and pin the resulting version ID before Task execution"],
    });
    this.templateOptions.description = "Empty development Tonkatsu broker TLS bundle and dedicated KMS key";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" } };
    new CfnOutput(this, "BrokerTlsBundleArn", { value: secret.ref });
    new CfnOutput(this, "BrokerTlsKeyArn", { value: key.attrArn });
  }
}
