import { App, CfnOutput, Duration, RemovalPolicy, Stack, StackProps, Tags } from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as kms from "aws-cdk-lib/aws-kms";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
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
    table.grant(workerRole, "dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:ConditionCheckItem");
    const cookingTable = dynamodb.Table.fromTableName(
      this, "CookingJobsForWorker", `foodtech-cooking-jobs-${stage}`,
    );
    cookingTable.grant(workerRole, "dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:ConditionCheckItem");
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

class TierRequestsStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);
    const table = new dynamodb.Table(this, "TierRequests", {
      tableName: `foodtech-tier-requests-${stage}`,
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true, recoveryPeriodInDays: 7 },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    table.addGlobalSecondaryIndex({
      indexName: "PendingRequests",
      partitionKey: { name: "queue_status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "queue_sort", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    new CfnOutput(this, "TierRequestsTableName", { value: table.tableName });
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
  readonly registrationRole: iam.Role;
  readonly gatewayRole: iam.Role;
  readonly vaultTable: dynamodb.Table;
  readonly storageKey: kms.Key;
  readonly transferKey: kms.Key;
  readonly hmacKey: kms.Key;
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
    const transferKey = new kms.Key(this, "CredentialTransferKey", {
      alias: `alias/foodtech/ai-credential-transfer-${stage}-${keyPeriod}`,
      description: "Foodtech browser-to-registration RSA-OAEP transfer key",
      keySpec: kms.KeySpec.RSA_4096,
      keyUsage: kms.KeyUsage.ENCRYPT_DECRYPT,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const hmacKey = new kms.Key(this, "CredentialPseudonymKey", {
      alias: `alias/foodtech/ai-credential-hmac-${stage}-${keyPeriod}`,
      description: "Foodtech credential tenant pseudonym HMAC key",
      keySpec: kms.KeySpec.HMAC_256,
      keyUsage: kms.KeyUsage.GENERATE_VERIFY_MAC,
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
    this.registrationRole = registration;
    this.gatewayRole = gateway;
    this.vaultTable = table;
    this.storageKey = key;
    this.transferKey = transferKey;
    this.hmacKey = hmacKey;
    table.grant(registration, "dynamodb:PutItem");
    table.grant(gateway, "dynamodb:GetItem");
    const registry = dynamodb.Table.fromTableName(
      this, "ExecutionRegistryForGateway", `foodtech-ai-execution-registry-${stage}`,
    );
    registry.grant(gateway, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:ConditionCheckItem");
    registry.grant(registration, "dynamodb:PutItem");
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
    transferKey.grant(registration, "kms:GetPublicKey");
    const transferDecrypt = transferKey.grant(registration, "kms:Decrypt");
    for (const statement of [...transferDecrypt.principalStatements, ...transferDecrypt.resourceStatements]) {
      statement.addCondition("StringEquals", { "kms:EncryptionAlgorithm": "RSAES_OAEP_SHA_256" });
    }
    hmacKey.grant(registration, "kms:GenerateMac");
    new CfnOutput(this, "CredentialVaultTableName", { value: table.tableName });
    new CfnOutput(this, "CredentialStorageKeyArn", { value: key.keyArn });
    new CfnOutput(this, "CredentialTransferKeyArn", { value: transferKey.keyArn });
    new CfnOutput(this, "CredentialPseudonymKeyArn", { value: hmacKey.keyArn });
    new CfnOutput(this, "CredentialRegistrationTaskRoleArn", { value: registration.roleArn });
    new CfnOutput(this, "AiGatewayTaskRoleArn", { value: gateway.roleArn });
  }
}

class AiServiceTasksStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps, vault: CredentialVaultStack) {
    super(scope, id, props);
    const required = (name: string): string => {
      const value = app.node.tryGetContext(name);
      if (typeof value !== "string" || !value.trim()) throw new Error(`${name} context is required`);
      return value.trim();
    };
    const image = (name: string): ecs.ContainerImage => {
      const repositoryName = required(`${name}Repository`);
      const tag = required(`${name}Tag`);
      if (!/^[a-z0-9]+(?:[._/-][a-z0-9]+)*$/.test(repositoryName) ||
          !/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(tag) || tag === "latest") {
        throw new Error(`${name} requires a valid ECR repository and immutable version tag`);
      }
      const repository = ecr.Repository.fromRepositoryName(this, `${name}Ecr`, repositoryName);
      return ecs.ContainerImage.fromEcrRepository(repository, tag);
    };
    const registrationImage = image("registrationImage");
    const gatewayImage = image("gatewayImage");
    const tlsSecret = (name: string) => {
      const arn = required(name);
      if (!/^arn:[^:]+:secretsmanager:[^:]+:[0-9]{12}:secret:.+/.test(arn)) {
        throw new Error(`${name} must be a complete Secrets Manager ARN`);
      }
      return secretsmanager.Secret.fromSecretCompleteArn(this, name, arn);
    };
    const registrationTls = tlsSecret("registrationTlsSecretArn");
    const gatewayTls = tlsSecret("gatewayTlsSecretArn");
    const definition = (name: string, role: iam.IRole, memory: number, secret: secretsmanager.ISecret) => {
      const task = new ecs.FargateTaskDefinition(this, `${name}Task`, {
        cpu: 512, memoryLimitMiB: memory, taskRole: role,
        runtimePlatform: { operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
          cpuArchitecture: ecs.CpuArchitecture.X86_64 },
      });
      const logGroup = new logs.LogGroup(this, `${name}Logs`, {
        logGroupName: `/foodtech/${stage}/${name.toLowerCase()}`,
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: RemovalPolicy.RETAIN,
      });
      const tlsSecrets = {
        FOODTECH_TLS_SERVER_CERT_PEM: ecs.Secret.fromSecretsManager(secret, "server_cert_pem"),
        FOODTECH_TLS_SERVER_KEY_PEM: ecs.Secret.fromSecretsManager(secret, "server_key_pem"),
        FOODTECH_TLS_BUSINESS_CA_PEM: ecs.Secret.fromSecretsManager(secret, "business_ca_pem"),
      };
      return { task, logGroup, tlsSecrets };
    };
    const registration = definition("CredentialRegistration", vault.registrationRole, 1024, registrationTls);
    registration.task.addContainer("App", {
      image: registrationImage,
      essential: true,
      user: "10001",
      environment: {
        FOODTECH_SERVICE: "credential-registration",
        FOODTECH_REGISTRATION_BIND_HOST: "0.0.0.0",
        FOODTECH_STAGE: stage,
        FOODTECH_CREDENTIAL_VAULT_TABLE: vault.vaultTable.tableName,
        FOODTECH_AI_REGISTRY_TABLE: `foodtech-ai-execution-registry-${stage}`,
        FOODTECH_CREDENTIAL_STORAGE_KEY_ARN: vault.storageKey.keyArn,
        FOODTECH_CREDENTIAL_TRANSFER_KEY_ARN: vault.transferKey.keyArn,
        FOODTECH_CREDENTIAL_TRANSFER_KEY_VERSION: required("credentialKeyPeriod"),
        FOODTECH_CREDENTIAL_HMAC_KEY_ARN: vault.hmacKey.keyArn,
        FOODTECH_CREDENTIAL_HMAC_KEY_VERSION: required("credentialHmacVersion"),
      },
      secrets: registration.tlsSecrets,
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: "app", logGroup: registration.logGroup }),
      portMappings: [{ containerPort: 8444, protocol: ecs.Protocol.TCP }],
    });
    registrationTls.grantRead(registration.task.executionRole!);
    const gateway = definition("AiGateway", vault.gatewayRole, 2048, gatewayTls);
    gateway.task.addContainer("App", {
      image: gatewayImage,
      essential: true,
      user: "10001",
      environment: {
        FOODTECH_SERVICE: "ai-gateway",
        FOODTECH_GATEWAY_BIND_HOST: "0.0.0.0",
        FOODTECH_STAGE: stage,
        FOODTECH_AI_REGISTRY_TABLE: `foodtech-ai-execution-registry-${stage}`,
        FOODTECH_CREDENTIAL_VAULT_TABLE: vault.vaultTable.tableName,
        FOODTECH_CREDENTIAL_KMS_KEY_ARNS: vault.storageKey.keyArn,
        FOODTECH_EXTERNAL_EXECUTION_ENABLED: "false",
      },
      secrets: gateway.tlsSecrets,
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: "app", logGroup: gateway.logGroup }),
      portMappings: [{ containerPort: 8443, protocol: ecs.Protocol.TCP }],
    });
    gatewayTls.grantRead(gateway.task.executionRole!);
    new CfnOutput(this, "CredentialRegistrationTaskArn", { value: registration.task.taskDefinitionArn });
    new CfnOutput(this, "AiGatewayTaskArn", { value: gateway.task.taskDefinitionArn });
  }
}

class AiImagePublishingStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);
    const repository = (id: string, name: string) => {
      const repo = new ecr.Repository(this, id, {
        repositoryName: `foodtech/${name}`,
        imageTagMutability: ecr.TagMutability.IMMUTABLE,
        encryption: ecr.RepositoryEncryption.AES_256,
        removalPolicy: RemovalPolicy.RETAIN,
      });
      repo.addLifecycleRule({ maxImageCount: 5, rulePriority: 1 });
      new CfnOutput(this, `${id}Name`, { value: repo.repositoryName });
      return repo;
    };
    const registration = repository("CredentialRegistrationImages", "credential-registration");
    const gateway = repository("AiGatewayImages", "ai-gateway");
    const logKey = new kms.Key(this, "ImageBuildLogKey", {
      enableKeyRotation: true,
      removalPolicy: RemovalPolicy.RETAIN,
      description: "Encrypt Foodtech image build logs",
    });
    logKey.addToResourcePolicy(new iam.PolicyStatement({
      principals: [new iam.ServicePrincipal(`logs.${this.region}.${this.urlSuffix}`)],
      actions: ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
      resources: ["*"],
      conditions: { ArnEquals: {
        "kms:EncryptionContext:aws:logs:arn":
          `arn:${this.partition}:logs:${this.region}:${this.account}:log-group:/foodtech/${stage}/ai-image-builds`,
      } },
    }));
    const logGroup = new logs.LogGroup(this, "ImageBuildLogs", {
      logGroupName: `/foodtech/${stage}/ai-image-builds`,
      encryptionKey: logKey,
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const project = new codebuild.Project(this, "AiImageBuilder", {
      projectName: `foodtech-ai-images-${stage}`,
      description: "Explicit, commit-pinned development image publishing without GitHub AWS credentials",
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        computeType: codebuild.ComputeType.MEDIUM,
        privileged: true,
      },
      timeout: Duration.minutes(30),
      logging: { cloudWatch: { logGroup } },
      environmentVariables: {
        SOURCE_SHA: { value: "REQUIRED_AT_START_BUILD" },
        REGISTRATION_REPOSITORY: { value: registration.repositoryUri },
        GATEWAY_REPOSITORY: { value: gateway.repositoryUri },
      },
      buildSpec: codebuild.BuildSpec.fromObject({
        version: "0.2",
        phases: {
          pre_build: { commands: [
            "echo \"$SOURCE_SHA\" | grep -Eq '^[0-9a-f]{40}$'",
            "git clone --depth 1 --branch main https://github.com/davidots429/foodtech.git source",
            "test \"$(git -C source rev-parse HEAD)\" = \"$SOURCE_SHA\"",
            "aws ecr get-login-password --region \"$AWS_DEFAULT_REGION\" | docker login --username AWS --password-stdin \"${REGISTRATION_REPOSITORY%%/*}\"",
            "IMAGE_TAG=sha-$(echo \"$SOURCE_SHA\" | cut -c1-12)",
          ] },
          build: { commands: [
            "docker build -f source/infra/containers/ai-service.Dockerfile --build-arg FOODTECH_PACKAGE=foodtech-credential-registration -t \"$REGISTRATION_REPOSITORY:$IMAGE_TAG\" source",
            "docker build -f source/infra/containers/ai-service.Dockerfile --build-arg FOODTECH_PACKAGE=foodtech-ai-gateway -t \"$GATEWAY_REPOSITORY:$IMAGE_TAG\" source",
            "docker push \"$REGISTRATION_REPOSITORY:$IMAGE_TAG\"",
            "docker push \"$GATEWAY_REPOSITORY:$IMAGE_TAG\"",
          ] },
        },
      }),
    });
    registration.grantPullPush(project);
    gateway.grantPullPush(project);
    const logGrant = logKey.grant(project, "kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*",
      "kms:GenerateDataKey*", "kms:Describe*");
    for (const statement of logGrant.principalStatements) {
      statement.addCondition("StringEquals", { "kms:ViaService": `logs.${this.region}.${this.urlSuffix}` });
    }
    new CfnOutput(this, "ImageBuildProjectName", { value: project.projectName });
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
if (app.node.tryGetContext("provisionTierRequests") === "true") {
  const tierRequests = new TierRequestsStack(app, `FoodtechTierRequests-${stage}`, { env });
  Tags.of(tierRequests).add("Project", "foodtech");
}
if (app.node.tryGetContext("provisionAiExecutionRegistry") === "true") {
  const registry = new AiExecutionRegistryStack(app, `FoodtechAiExecutionRegistry-${stage}`, { env });
  Tags.of(registry).add("Project", "foodtech");
}
if (app.node.tryGetContext("provisionCredentialVault") === "true") {
  const vault = new CredentialVaultStack(app, `FoodtechCredentialVault-${stage}`, { env });
  Tags.of(vault).add("Project", "foodtech");
  if (app.node.tryGetContext("provisionAiServiceTasks") === "true") {
    const tasks = new AiServiceTasksStack(app, `FoodtechAiServiceTasks-${stage}`, { env }, vault);
    Tags.of(tasks).add("Project", "foodtech");
  }
} else if (app.node.tryGetContext("provisionAiServiceTasks") === "true") {
  throw new Error("provisionAiServiceTasks requires provisionCredentialVault=true");
}
if (app.node.tryGetContext("provisionAiImagePublishing") === "true") {
  const publishing = new AiImagePublishingStack(app, `FoodtechAiImagePublishing-${stage}`, { env });
  Tags.of(publishing).add("Project", "foodtech");
}
app.synth();

