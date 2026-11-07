import { App, CfnOutput, Duration, RemovalPolicy, Stack, StackProps, Tags } from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as sqs from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";

const app = new App();
const stage = app.node.tryGetContext("stage") ?? "dev";
if (!/^[a-z0-9-]+$/.test(stage)) throw new Error("stage must be lowercase letters, numbers or hyphens");
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: "ap-southeast-2" };

class AuthSessionsStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);
    new dynamodb.Table(this, "AuthSessions", {
      tableName: `foodtech-auth-sessions-${stage}`,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "expires",
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: RemovalPolicy.RETAIN,
    });
  }
}

class AuthIdentityStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);
    const customerCallback = app.node.tryGetContext("customerCallback");
    const adminCallback = app.node.tryGetContext("adminCallback");
    const customerWeb = app.node.tryGetContext("customerWeb");
    const adminWeb = app.node.tryGetContext("adminWeb");
    const domainPrefix = app.node.tryGetContext("cognitoDomainPrefix");
    if (![customerCallback, adminCallback, customerWeb, adminWeb, domainPrefix].every(Boolean)) {
      throw new Error("identity stack requires both callback/web URLs and cognitoDomainPrefix contexts");
    }
    const pool = new cognito.UserPool(this, "UserPool", {
      userPoolName: `foodtech-auth-${stage}`,
      featurePlan: cognito.FeaturePlan.ESSENTIALS,
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: { otp: true, sms: false },
      passwordPolicy: {
        minLength: 12, requireLowercase: true, requireUppercase: true,
        requireDigits: true, requireSymbols: true,
      },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const domain = pool.addDomain("Domain", {
      cognitoDomain: { domainPrefix },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });
    const createClient = (name: string, callback: string, web: string) => pool.addClient(name, {
      userPoolClientName: `foodtech-${name.toLowerCase()}-${stage}`,
      generateSecret: false,
      authFlows: name === "Admin" ? { userPassword: true, userSrp: true } : undefined,
      preventUserExistenceErrors: true,
      enableTokenRevocation: true,
      refreshTokenValidity: Duration.days(1),
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      oAuth: {
        flows: { authorizationCodeGrant: true },
        callbackUrls: [callback], logoutUrls: [web],
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE,
          ...(name === "Customer" ? [cognito.OAuthScope.COGNITO_ADMIN] : [])],
      },
    });
    const customer = createClient("Customer", customerCallback, customerWeb);
    const admin = createClient("Admin", adminCallback, adminWeb);
    new cognito.UserPoolGroup(this, "AdminGroup", { userPool: pool, groupName: "foodtech-admin" });
    for (const [name, client] of [["Customer", customer], ["Admin", admin]] as const) {
      const branding = new cognito.CfnManagedLoginBranding(this, `${name}Branding`, {
        userPoolId: pool.userPoolId, clientId: client.userPoolClientId, useCognitoProvidedValues: true,
      });
      branding.node.addDependency(domain);
    }
  }
}

class MinimalDataFlowStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);
    const table = new dynamodb.Table(this, "DataFlow", {
      tableName: `foodtech-data-flow-${stage}`,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const queue = (id: string) => new sqs.Queue(this, id, {
      queueName: `foodtech-${id.toLowerCase()}-${stage}.fifo`,
      fifo: true,
      contentBasedDeduplication: false,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      visibilityTimeout: Duration.seconds(30),
    });
    const processingDlq = queue("ProcessingDlq");
    const quarantine = queue("StandardizationDlq");
    const ordered = new sqs.Queue(this, "Ordered", {
      queueName: `foodtech-ordered-${stage}.fifo`,
      fifo: true,
      contentBasedDeduplication: false,
      encryption: sqs.QueueEncryption.SQS_MANAGED,
      enforceSSL: true,
      visibilityTimeout: Duration.seconds(30),
      deadLetterQueue: { queue: processingDlq, maxReceiveCount: 3 },
    });
    const workerRole = new iam.Role(this, "StandardizationWorkerTaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com", {
        conditions: {
          ArnLike: {
            "aws:SourceArn": `arn:${this.partition}:ecs:${this.region}:${this.account}:*`,
          },
          StringEquals: { "aws:SourceAccount": this.account },
        },
      }),
      description: "ECS task access for Foodtech standardization and process assessment",
    });
    table.grant(workerRole, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:ConditionCheckItem");
    const cookingTable = dynamodb.Table.fromTableName(
      this, "CookingJobsForWorker", `foodtech-cooking-jobs-${stage}`,
    );
    cookingTable.grant(workerRole, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:ConditionCheckItem");
    ordered.grant(workerRole, "sqs:ReceiveMessage", "sqs:DeleteMessage");
    quarantine.grant(workerRole, "sqs:SendMessage");
    new CfnOutput(this, "DataTableName", { value: table.tableName });
    new CfnOutput(this, "OrderedQueueUrl", { value: ordered.queueUrl });
    new CfnOutput(this, "StandardizationDlqUrl", { value: quarantine.queueUrl });
    new CfnOutput(this, "StandardizationWorkerTaskRoleArn", { value: workerRole.roleArn });
  }
}

class CookingJobsStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);
    const table = new dynamodb.Table(this, "CookingJobs", {
      tableName: `foodtech-cooking-jobs-${stage}`,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    new CfnOutput(this, "CookingJobsTableName", { value: table.tableName });
  }
}

class AiExecutionRegistryStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);
    const table = new dynamodb.Table(this, "AiExecutionRegistry", {
      tableName: `foodtech-ai-execution-registry-${stage}`,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      timeToLiveAttribute: "expires",
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    new CfnOutput(this, "AiExecutionRegistryTableName", { value: table.tableName });
  }
}

class CredentialVaultStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);
    const keyPeriod = app.node.tryGetContext("credentialKeyPeriod");
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(keyPeriod ?? "")) {
      throw new Error("credentialKeyPeriod must be YYYY-MM for the initial key pool");
    }
    const table = new dynamodb.Table(this, "CredentialVault", {
      tableName: `foodtech-ai-credential-vault-${stage}`,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true, recoveryPeriodInDays: 7 },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const key = new kms.Key(this, "CredentialStorageKey", {
      alias: `alias/foodtech/ai-credential-${stage}-${keyPeriod}`,
      description: "Foodtech external AI credential envelope wrapping key",
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const taskTrust = () => new iam.ServicePrincipal("ecs-tasks.amazonaws.com", {
      conditions: {
        ArnLike: { "aws:SourceArn": `arn:${this.partition}:ecs:${this.region}:${this.account}:*` },
        StringEquals: { "aws:SourceAccount": this.account },
      },
    });
    const registration = new iam.Role(this, "CredentialRegistrationTaskRole", {
      assumedBy: taskTrust(),
      description: "Write-only credential vault and data-key generation for registration",
    });
    const gateway = new iam.Role(this, "AiGatewayTaskRole", {
      assumedBy: taskTrust(),
      description: "Credential vault read and storage-key decrypt for AI Gateway only",
    });
    table.grant(registration, "dynamodb:PutItem");
    table.grant(gateway, "dynamodb:GetItem");
    const registry = dynamodb.Table.fromTableName(
      this, "ExecutionRegistryForGateway", `foodtech-ai-execution-registry-${stage}`,
    );
    registry.grant(gateway, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:ConditionCheckItem");
    const restrictContext = (grant: iam.Grant) => {
      for (const statement of [...grant.principalStatements, ...grant.resourceStatements]) {
        statement.addCondition("StringEquals", {
          "kms:EncryptionContext:purpose": "EXTERNAL_AI_CREDENTIAL",
          "kms:EncryptionContext:environment": stage,
        });
      }
    };
    restrictContext(key.grant(registration, "kms:GenerateDataKey"));
    restrictContext(key.grant(gateway, "kms:Decrypt"));
    new CfnOutput(this, "CredentialVaultTableName", { value: table.tableName });
    new CfnOutput(this, "CredentialStorageKeyArn", { value: key.keyArn });
    new CfnOutput(this, "CredentialRegistrationTaskRoleArn", { value: registration.roleArn });
    new CfnOutput(this, "AiGatewayTaskRoleArn", { value: gateway.roleArn });
  }
}

const sessions = new AuthSessionsStack(app, `FoodtechAuthSessions-${stage}`, { env });
Tags.of(sessions).add("Project", "foodtech");
if (app.node.tryGetContext("provisionIdentity") === "true") {
  const identity = new AuthIdentityStack(app, `FoodtechAuthIdentity-${stage}`, { env });
  Tags.of(identity).add("Project", "foodtech");
}
if (app.node.tryGetContext("provisionDataFlow") === "true") {
  const dataFlow = new MinimalDataFlowStack(app, `FoodtechMinimalDataFlow-${stage}`, { env });
  Tags.of(dataFlow).add("Project", "foodtech");
}
if (app.node.tryGetContext("provisionCookingJobs") === "true") {
  const cookingJobs = new CookingJobsStack(app, `FoodtechCookingJobs-${stage}`, { env });
  Tags.of(cookingJobs).add("Project", "foodtech");
}
if (app.node.tryGetContext("provisionAiExecutionRegistry") === "true") {
  const registry = new AiExecutionRegistryStack(app, `FoodtechAiExecutionRegistry-${stage}`, { env });
  Tags.of(registry).add("Project", "foodtech");
}
if (app.node.tryGetContext("provisionCredentialVault") === "true") {
  const vault = new CredentialVaultStack(app, `FoodtechCredentialVault-${stage}`, { env });
  Tags.of(vault).add("Project", "foodtech");
}
app.synth();

