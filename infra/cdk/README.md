# AWS CDK 개발 인프라

`-c provisionTierRequests=true`는 시연용 티어 신청을 위한 `FoodtechTierRequests-dev` 스택을 합성한다. `foodtech-tier-requests-dev` DynamoDB On-demand 테이블에 고객별 현재 상태, 신청 기록과 검토 기록을 함께 저장하고, `PendingRequests` GSI로 관리자 검토 대기 항목을 조회한다. 현재 상태 변경과 이력 쓰기는 조건부 트랜잭션을 사용한다. PITR 7일, AWS 관리형 암호화, `RETAIN` 정책을 설정했다. 이는 배포되지 않은 코드이며 `FOODTECH_TIER_TABLE` 환경변수와 업무 API 역할 권한을 연결해야 실제 운영할 수 있다.

## 개발 이미지 게시 경로

`-c provisionAiImagePublishing=true`는 `FoodtechAiImagePublishing-dev`에 등록기·Gateway 전용 ECR 저장소와 명시적으로 시작하는 CodeBuild 프로젝트를 합성한다. 두 저장소는 태그 변경을 금지하고 각각 최근 이미지 5개를 유지한다. CodeBuild 역할은 이 두 저장소의 이미지 업로드와 전용 로그 그룹 기록에 한정한다. 빌드 로그는 별도 KMS 키로 암호화한다. ECR 저장 용량, CodeBuild 실행 시간과 KMS 키 유지에는 비용이 발생할 수 있다.

빌드는 공개 저장소의 `main`을 받아 시작 요청의 40자리 `SOURCE_SHA`와 정확히 일치할 때만 진행한다. 이미지는 `sha-<커밋 앞 12자리>` 태그로 게시한다. 프로젝트 생성만으로 빌드가 시작되지 않는다. 병합된 main의 검사 결과와 합성·차이를 확인한 뒤 `StartBuild` 요청에 `SOURCE_SHA`를 지정해 한 번 실행한다. 게시 후 두 ECR 저장소에 해당 태그와 digest가 있는지 확인한다. 이미지에 고객 자격증명·TLS 개인키를 포함하지 않는다.

현재 계정의 `iam:ListOpenIDConnectProviders` 및 `iam:GetOpenIDConnectProvider`는 조직 정책의 명시적 거부로 확인되었고 GitHub용 배포 역할도 없다. 따라서 GitHub Actions에는 AWS 자격증명을 추가하지 않는다. CodeBuild의 명시적 일회성 게시로 개발용 이미지를 준비하고, 조직 정책이 OIDC 공급자 설정을 허용하면 승인된 GitHub Actions OIDC 방식으로 전환한다. 공개 GitHub 소스 다운로드에는 배포 비밀값을 사용하지 않는다.

## AI 서비스 Task Definition 초안

`-c provisionCredentialVault=true -c provisionAiServiceTasks=true`는 별도 `FoodtechAiServiceTasks-dev` 스택에 자격증명 등록기와 AI Gateway의 Fargate Task Definition을 만든다. 각 Task는 기존 Vault 스택의 **서로 다른** 업무 역할을 사용한다. 실행 역할은 해당 ECR 이미지와 Secrets Manager TLS 자료를 읽고 CloudWatch Logs에 기록한다. 등록기는 Vault 쓰기와 KMS 전송·저장·가명화 권한, Gateway는 Vault 읽기와 저장 키 복호화 권한을 유지한다. 외부 공급자 실행은 기본 비활성이다.

필수 CDK context: `credentialKeyPeriod`, `credentialHmacVersion`, `registrationImageRepository`, `registrationImageTag`, `gatewayImageRepository`, `gatewayImageTag`, `registrationTlsSecretArn`, `gatewayTlsSecretArn`. 이미지 태그는 `latest`를 허용하지 않는다. TLS Secret 두 개는 각각 JSON 키 `server_cert_pem`, `server_key_pem`, `business_ca_pem`을 가져야 한다. 인증서 개인키와 CA는 저장소와 Task Definition의 일반 환경변수에 넣지 않는다. ECS 실행 역할이 Secret 값을 주입하고 컨테이너 시작기가 권한 `0600`의 임시 파일로 작성한다. 이미지 빌드는 `infra/containers/ai-service.Dockerfile`에 `FOODTECH_PACKAGE=foodtech-credential-registration` 또는 `foodtech-ai-gateway`를 전달한다. CI는 두 이미지를 빌드만 하며 게시하지 않는다.

두 Task의 리스너는 컨테이너 안에서 `0.0.0.0`에 결합한다. 실제 실행 전에 업무 API가 두 서비스에만 접근할 수 있는 **사설 서브넷·보안 그룹**, 서비스 DNS와 일치하는 서버 인증서 SAN, 업무 API 전용 클라이언트 인증서를 마련해야 한다. 현재 Task Definition은 ECS Service, Cluster, 네트워크, 이미지 게시, TLS Secret 생성, 업무 API 주소 설정을 만들지 않는다. 따라서 합성 성공만으로 종단간 배포가 완료된 것은 아니다. Gateway의 Ollama 기본 주소도 같은 Task의 loopback이다. 서버 GPU·Ollama 배치가 결정되기 전에는 로컬 모델 호출을 배포 검증 결과로 간주하지 않는다.

현재 개발 계정에는 서비스용 ECR 저장소와 TLS Secret이 아직 없다. 실행 전에 이미지와 Secret을 준비하고 실제 ARN·태그로 합성해야 한다. Fargate 상시 실행은 요금이 발생하므로 Task Definition 합성과 서비스 배포를 분리한다.

`-c provisionCredentialVault=true -c credentialKeyPeriod=2026-11`은 `FoodtechCredentialVault-dev` 스택을 합성한다. 2026-11-07 개발 계정 `ap-southeast-2`에 Vault DynamoDB On-demand 테이블, PITR 7일, 저장용 대칭 KMS 키와 별도 등록·Gateway ECS Task 역할을 배포했다. KMS 키·테이블은 `RETAIN`이다. 등록 역할은 Vault `PutItem`·저장 키 `GenerateDataKey`, Gateway 역할은 Vault `GetItem`·저장 키 `Decrypt` 및 AI 실행 레지스트리 접근만 허용한다. KMS 역할 정책은 `purpose=EXTERNAL_AI_CREDENTIAL`, `environment=dev` context를 요구한다. KMS 키와 PITR은 리소스를 유지하는 동안 비용이 발생할 수 있다. 실제 서비스 Task Definition 연결은 후속이다.

같은 날 두 역할을 각각 임시 ECS Fargate Task의 `taskRoleArn`에 연결해 실제 AWS API를 시험했다. 등록 역할은 Vault `PutItem`·KMS `GenerateDataKey`가 성공하고 `GetItem`·`Decrypt`가 `AccessDeniedException`으로 거부됐다. Gateway 역할은 Vault `GetItem`·KMS `Decrypt`가 성공하고 `PutItem`·`GenerateDataKey`가 거부됐다. 합성 Vault 항목과 임시 클러스터·Task 정의·실행 역할·보안 그룹·로그 그룹은 시험 후 정리했다. IAM 시뮬레이터는 실제 Vault 읽기에 성공한 개발 역할까지 조직 정책 거부로 표시했으므로, 이 계정의 해당 시뮬레이션 결과를 실제 Task 요청 결과보다 우선하지 않는다. 이 시험은 역할의 AWS API 권한을 검증한 것이며 실제 서비스 배포·외부 공급자 호출 증거는 아니다.

자격증명 등록 경로를 위해 같은 개발 스택에 RSA-4096 전송 KMS 키와 HMAC-256 가명 KMS 키를 추가했다. 등록 역할에는 전송 키 `GetPublicKey`·`Decrypt(RSAES_OAEP_SHA_256)`, 가명 키 `GenerateMac`, AI 레지스트리 `PutItem`을 추가했다. 기존 Vault·저장 KMS 키·Gateway 역할은 교체하지 않았다. 두 새 키의 ARN은 스택 출력에 있으며 저장 키와 마찬가지로 `RETAIN`이다. 임시 Fargate Task에 등록 역할을 부여한 실제 요청에서 새 KMS 권한과 Vault·AI 레지스트리 원자적 기록이 성공했고 Vault `GetItem`은 거부됐다. 시험 데이터와 Task 리소스는 정리했다. 실제 서비스 Task 배포는 후속이다.

`-c provisionAiExecutionRegistry=true`는 별도 `FoodtechAiExecutionRegistry-dev` 스택을 합성한다. DynamoDB On-demand 테이블 `foodtech-ai-execution-registry-dev`는 실행·모델·자격증명 메타데이터·동의·월별 한도를 저장한다. `pk=EXEC#<실행 ID>, sk=META`로 Gateway가 실행 ID를 직접 조회한다. AWS 관리형 저장 암호화, 시점 복구, 결과 항목의 `expires` TTL과 `RETAIN` 제거 정책을 적용한다. TTL은 비동기 물리 삭제이므로 90일 Hard Delete의 최종 보장은 별도 삭제 작업이 필요하다. 키 원문·암호문은 이 테이블에 저장하지 않는다. 2026-11-07에 개발 계정 `ap-southeast-2`에 이 스택을 배포했다. Gateway 실행 역할은 아직 배포되지 않았으며, 로컬 검증은 개발자 AWS 세션으로 수행했다.

`FoodtechAuthSessions-dev`는 만료 시각(`expires`)이 있는 인증 흐름·세션과 관리자 권한 회수 상태를 저장하는 DynamoDB 테이블을 만든다. 앱은 `FOODTECH_AUTH_TABLE=foodtech-auth-sessions-dev`로 연결한다. TTL은 물리 삭제용이며 애플리케이션이 만료를 직접 검사한다.

2026-11-01에 `foodtech` 프로파일의 `ap-southeast-2`에 `CDKToolkit`과 `FoodtechAuthSessions-dev`를 배포했다. 시험용 합성 식별자로 두 DynamoDB 저장소 인스턴스의 흐름 단일 소비·세션 공유·관리자 차단 상태 전파를 확인하고 시험 항목을 삭제했다. 이 스택은 Cognito User Pool을 만들지 않았다.

`-c provisionIdentity=true`를 주면 Cognito Essentials User Pool, 고객·관리자 App Client, 관리형 로그인 v2, 선택적 TOTP MFA, 관리자 그룹을 정의하는 `FoodtechAuthIdentity-dev`도 합성한다. 두 callback URL·두 웹 URL·고유 도메인 접두어를 context로 전달해야 한다. 현재 CLI로 만든 `foodtech-auth-poc-20261101` Pool은 이 스택의 소유 대상이 아니므로 `provisionIdentity` 없이 세션 스택만 배포한다. PoC Pool을 이 스택으로 자동 편입하거나 같은 이름으로 다시 만들지 않는다. 향후 별도 환경에 새 Pool을 배포할 때 이 정의를 사용하고, 기존 PoC는 사용자·연동 전환을 확인한 후 별도 정리한다. 시험용 Pool의 TOTP MFA도 `OPTIONAL`로 설정했다.

스택은 `ap-southeast-2`에 한정한다. 배포 전 `cdk synth --strict`와 `cdk diff`를 확인한다. AuthSessions 테이블과 User Pool의 제거 정책은 `RETAIN`이다. 현재 배포된 API 실행 역할은 없으며, 실제 API 배포 시 테이블의 `GetItem`·`PutItem`·`DeleteItem`·`UpdateItem`과 Pool의 `AdminGetUser`·`AdminListGroupsForUser`만 실행 역할에 부여한다. CI 검사에는 AWS 권한을 부여하지 않는다. 기본 CDK bootstrap의 CloudFormation 실행 정책은 `AdministratorAccess`로 생성되었으므로, 향후 GitHub OIDC 배포 역할을 연결하기 전에 신뢰 조건과 실행 권한을 별도로 제한한다.

`-c provisionDataFlow=true`로 `FoodtechMinimalDataFlow-dev`를 합성한다. 2026-11-01에 `foodtech` 프로파일의 `ap-southeast-2`에 배포했다. `foodtech-data-flow-dev` DynamoDB On-demand 테이블, `foodtech-ordered-dev.fifo`, `foodtech-processingdlq-dev.fifo`, `foodtech-standardizationdlq-dev.fifo`를 생성한다. 테이블은 AWS 관리형 저장 암호화와 `RETAIN` 제거 정책을 사용한다. 큐에는 TLS만 허용하고 content-based deduplication을 끈다. Ordered 큐의 처리 오류는 Processing DLQ로, 품질 격리는 Worker가 Standardization DLQ로 보낸다.

첫 합성 fixture 3건을 실제 테이블·큐에 넣어 순번 복원 `1, 2, 3`, 품질 `VALID` 2건·`QUARANTINED` 1건, `HARD_LIMIT_EXCEEDED` Finding 1건과 격리 큐 1건을 확인했다. 이는 단일 스트림의 최소 기능 증거이며 다중 Task 동시성·부하 기준 달성 증거가 아니다. 스택에는 상시 실행 컴퓨트가 없지만 테이블·큐는 계속 남아 저장·요청 사용량이 발생할 수 있다.

`-c provisionCookingJobs=true`는 조리작업·안전·평가 컬렉션용 별도 `FoodtechCookingJobs-dev` 스택과 `foodtech-cooking-jobs-dev` DynamoDB On-demand 테이블을 합성한다. `PK=SCOPE#USER#<서버 세션 subject>#JOB#<UUID>`에 `META`, `ASSESSMENT#REV#<12자리 버전>`, `ASSESSMENT#CURRENT`, `EVENT#JOB#...`, `EVENT#SAFETY#...`를 둔다. 작업 메타와 현재 포인터의 변경, 새 완료 평가 버전 및 상태 변경 이력은 조건부 트랜잭션으로 묶는다. 2026-11-02에 승인된 개발 스택을 `ap-southeast-2`에 생성했고 테이블이 `ACTIVE`임을 확인했다. 합성 레코드로 동시 평가 제출의 단일 성공·단일 충돌, 평가 버전과 포인터의 원자적 일치, 안전 차단·재평가·재조리 연결을 확인한 뒤 시험 레코드를 삭제했다. 테이블은 향후 개발을 위해 유지하며 저장·요청 사용량이 발생할 수 있다.

`FoodtechMinimalDataFlow-dev`에는 표준화 Worker용 ECS Task Role도 정의한다. 이 역할은 수집 테이블에 `GetItem`·`PutItem`·`ConditionCheckItem`, 조리작업 테이블에 `GetItem`·`Query`·`PutItem`·`ConditionCheckItem`, Ordered 큐에 `ReceiveMessage`·`DeleteMessage`, Standardization DLQ에 `SendMessage`만 허용한다. 조리작업의 `Query`는 늦은 이벤트 파생 계산에서 기존 공정 사건과 완료된 요청을 조회하는 데 필요하다. 신뢰 정책은 같은 계정·리전의 ECS Task로 제한한다. 역할 ARN은 `StandardizationWorkerTaskRoleArn` 출력으로 제공한다. 역할 생성만으로 Worker가 배포되거나 실행되지는 않으며, 후속 Task Definition의 `taskRole`에 연결해야 한다. 이 역할은 컨테이너 이미지 다운로드와 로그 전송에 쓰는 ECS Task Execution Role과 별개다. 2026-11-14 `cdk synth --strict`와 배포 스택 대상 `cdk diff`에서 역할·정책·출력 추가만 확인했고 기존 테이블·큐 교체는 나타나지 않았다. 현재 개발 스택에는 이 역할 변경이 배포되지 않았으므로 실제 역할 자격증명으로 실행한 검증은 아직 없다.

역할 배포 후 `AWS_PROFILE=foodtech .venv/bin/python tests/live/worker_role_iam_check.py`로 두 테이블과 두 큐의 허용·거부 조합을 IAM 정책 시뮬레이터에서 확인한다. 이는 역할의 **ID 기반 정책** 확인이며 ECS Task가 실제로 역할을 받아 Worker를 실행했다는 증거는 아니다. 실제 실행 검증은 Task Definition에 역할을 연결한 뒤 수행한다.

