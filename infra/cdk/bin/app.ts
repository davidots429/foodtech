import { App, Duration, RemovalPolicy, Stack, StackProps, Tags } from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
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
      preventUserExistenceErrors: true,
      enableTokenRevocation: true,
      refreshTokenValidity: Duration.days(1),
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      oAuth: {
        flows: { authorizationCodeGrant: true },
        callbackUrls: [callback], logoutUrls: [web],
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
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

const sessions = new AuthSessionsStack(app, `FoodtechAuthSessions-${stage}`, { env });
Tags.of(sessions).add("Project", "foodtech");
if (app.node.tryGetContext("provisionIdentity") === "true") {
  const identity = new AuthIdentityStack(app, `FoodtechAuthIdentity-${stage}`, { env });
  Tags.of(identity).add("Project", "foodtech");
}
app.synth();

