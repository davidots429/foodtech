import { CfnOutput, RemovalPolicy, Stack, StackProps, Token } from "aws-cdk-lib";
import * as kms from "aws-cdk-lib/aws-kms";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

/** Empty, isolated TLS/OAuth stores; no material generation or Task execution. */
export class PrivateRestSecretsStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, stage: string) {
    super(scope, id, props);
    if (stage !== "dev" || this.region !== "ap-southeast-2" || Token.isUnresolved(this.account) ||
        !/^\d{12}$/.test(this.account)) {
      throw new Error("private REST Secrets require dev and an explicit regional account");
    }
    this.templateOptions.description = "Separate empty synthetic REST Mock/collector Secrets and dedicated regional KMS keys; no secret values or Tasks";
    this.templateOptions.metadata = { AWSToolsMetrics: { AWSAgentToolkit: "aws-cloudformation@3" },
      "com.aws.cloudformation.Context": { ref: [{ at: "infra/cdk/README.md", has: "Private REST Secret preparation" }] } };
    for (const [kind, prefix] of [["mock", "Mock"], ["collector", "Collector"]] as const) {
      const name = `foodtech/dev/private-rest-${kind}`;
      // SM assigns an ARN suffix at creation. The key policy constrains its
      // name namespace; the separate execution IAM policy binds the exact ARN.
      const secretArnPattern = `arn:aws:secretsmanager:${this.region}:${this.account}:secret:${name}-*`;
      const root = `arn:aws:iam::${this.account}:root`;
      const key = new kms.CfnKey(this, `${prefix}Key`, {
        description: `Dedicated synthetic private REST ${kind} Secret storage key`,
        enabled: true, enableKeyRotation: true, multiRegion: false,
        keySpec: "SYMMETRIC_DEFAULT", keyUsage: "ENCRYPT_DECRYPT", pendingWindowInDays: 7,
        keyPolicy: { Version: "2012-10-17", Statement: [
          { Sid: "AccountAdministration", Effect: "Allow", Principal: { AWS: root },
            // Resource=* in a KMS key policy means this key. Account-principal
            // delegation still requires matching IAM permissions on the caller.
            Action: ["kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*", "kms:Put*", "kms:Update*",
              "kms:Revoke*", "kms:Disable*", "kms:Get*", "kms:Delete*", "kms:TagResource", "kms:UntagResource",
              "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion"], Resource: "*" },
          { Sid: "CryptographyThroughOwnSecretNamespace", Effect: "Allow", Principal: { AWS: root },
            Action: ["kms:Decrypt", "kms:GenerateDataKey"], Resource: "*",
            Condition: { StringEquals: { "kms:ViaService": `secretsmanager.${this.region}.amazonaws.com`,
              "kms:CallerAccount": this.account }, StringLike: { "kms:EncryptionContext:SecretARN": secretArnPattern } } },
        ] },
      });
      key.applyRemovalPolicy(RemovalPolicy.RETAIN);
      const secret = new secretsmanager.CfnSecret(this, `${prefix}Secret`, {
        name, description: `Synthetic private REST ${kind} material; initialized separately under explicit review`,
        kmsKeyId: key.attrArn,
        // Omitting BOTH properties creates an empty Secret, not a generated
        // password. Never serialize certificate/private-key material into CFN.
      });
      secret.applyRemovalPolicy(RemovalPolicy.RETAIN);
      secret.addMetadata("com.aws.cloudformation.Context", {
        why: "Keep Mock server private key inaccessible to the collector execution role",
        must: ["No SecretString/GenerateSecretString or plaintext output", "Material upload and actual ARN/role validation are separate reviewed actions",
          "No automatic custom-secret rotation, extra trail or alarms are configured by this preparation template"],
      });
      new CfnOutput(this, `${prefix}SecretArn`, { value: secret.ref });
      new CfnOutput(this, `${prefix}SecretKeyArn`, { value: key.attrArn });
    }
  }
}
