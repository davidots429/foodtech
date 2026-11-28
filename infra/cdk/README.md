# AWS CDK 개발 인프라

`-c provisionTierRequests=true`는 시연용 티어 신청을 위한 `FoodtechTierRequests-dev` 스택을 합성한다. `foodtech-tier-requests-dev` DynamoDB On-demand 테이블에 고객별 현재 상태, 신청 기록과 검토 기록을 함께 저장하고, `PendingRequests` GSI로 관리자 검토 대기 항목을 조회한다. 현재 상태 변경과 이력 쓰기는 조건부 트랜잭션을 사용한다. PITR 7일, AWS 관리형 암호화, `RETAIN` 정책을 설정했다. 이는 배포되지 않은 코드이며 `FOODTECH_TIER_TABLE` 환경변수와 업무 API 역할 권한을 연결해야 실제 운영할 수 있다.

## 개발 이미지 게시 경로

`-c provisionAiImagePublishing=true`는 `FoodtechAiImagePublishing-dev`에 등록기·Gateway 전용 ECR 저장소와 명시적으로 시작하는 CodeBuild 프로젝트를 합성한다. 두 저장소는 태그 변경을 금지하고 각각 최근 이미지 5개를 유지한다. CodeBuild 역할은 이 두 저장소의 이미지 업로드와 전용 로그 그룹 기록에 한정한다. 빌드 로그는 별도 KMS 키로 암호화한다. ECR 저장 용량, CodeBuild 실행 시간과 KMS 키 유지에는 비용이 발생할 수 있다.

빌드는 공개 저장소의 `main`을 받아 시작 요청의 40자리 `SOURCE_SHA`와 정확히 일치할 때만 진행한다. 이미지는 `sha-<커밋 앞 12자리>` 태그로 게시한다. 프로젝트 생성만으로 빌드가 시작되지 않는다. 병합된 main의 검사 결과와 합성·차이를 확인한 뒤 `StartBuild` 요청에 `SOURCE_SHA`를 지정해 한 번 실행한다. 게시 후 두 ECR 저장소에 해당 태그와 digest가 있는지 확인한다. 이미지에 고객 자격증명·TLS 개인키를 포함하지 않는다.

과거 OIDC 조회 제한 당시에는 아래 CodeBuild 후보를 준비했다. 이후 개발용 GitHub main 전용 OIDC provider·유지보수 게시 역할과 실제 게시 검증을 완료했으므로 이 제한을 현재 상태로 취급하지 않는다. 새 수집기 게시 준비는 아래의 GitHub Actions 수동 경로를 따른다. 기존 CodeBuild 후보는 자동 선택하거나 이번 경로와 함께 적용하지 않으며 실제 새 자원·권한 적용에는 별도 검토와 승인이 필요하다. 공개 GitHub 소스 다운로드에는 배포 비밀값을 사용하지 않는다.

`-c provisionCollectionImagePublishing=true`는 수집기 전용 불변 태그 ECR 저장소와 별도의 명시적 CodeBuild 프로젝트를 합성한다. 이 프로젝트도 `main`의 HEAD가 지정한 40자리 `SOURCE_SHA`와 일치할 때만 `sha-<앞 12자리>` 이미지를 빌드·게시하며 자동 트리거는 없다. AI 이미지 게시와 독립적으로 실행한다. 아직 배포·실행되지 않았으며 ECR 저장, CodeBuild 실행과 로그 암호화 KMS 키에 비용이 발생할 수 있다. 이 CodeBuild 후보는 과거 제약을 위한 준비본이며, 현재의 승인된 GitHub Actions 전환 후보는 이 문서의 수집기 이미지 수동 게시 준비 절을 따른다.

## AI 서비스 Task Definition 초안

`-c provisionCredentialVault=true -c provisionAiServiceTasks=true`는 별도 `FoodtechAiServiceTasks-dev` 스택에 자격증명 등록기와 AI Gateway의 Fargate Task Definition을 만든다. 각 Task는 기존 Vault 스택의 **서로 다른** 업무 역할을 사용한다. 실행 역할은 해당 ECR 이미지와 Secrets Manager TLS 자료를 읽고 CloudWatch Logs에 기록한다. 등록기는 Vault 쓰기와 KMS 전송·저장·가명화 권한, Gateway는 Vault 읽기와 저장 키 복호화 권한을 유지한다. 외부 공급자 실행은 기본 비활성이다.

`-c principalProvisioningEnabled=true`는 AI 서비스 두 Task에 같은 `FOODTECH_PRINCIPAL_PROVISIONING_ENABLED=true`를 전달한다. 기본값은 false이며 `true`/`false` 이외의 값은 합성에서 거부한다. 업무 API의 PR #229 초기화·최종 제거 계약과 함께 준비해 모든 관련 프로세스를 같은 모드로 재시작한 뒤 적용해야 한다. CI는 두 모드의 합성 결과에서 등록기·Gateway의 일치 및 외부 공급자 실행의 비활성을 확인한다. 이 설정은 실제 배포나 계정 초기화 제어 항목 생성을 수행하지 않는다.

필수 CDK context: `credentialKeyPeriod`, `credentialHmacVersion`, `registrationImageRepository`, `registrationImageDigest`, `gatewayImageRepository`, `gatewayImageDigest`, `registrationTlsSecretArn`, `gatewayTlsSecretArn`. 두 digest는 `sha256:`와 소문자 16진수 64자리다. 종전 `registrationImageTag`·`gatewayImageTag`는 digest와 함께 지정해도 거부한다. 저장소는 같은 계정·리전의 이름만 받고 레지스트리 URI는 받지 않는다. TLS Secret 두 개는 각각 JSON 키 `server_cert_pem`, `server_key_pem`, `business_ca_pem`을 가져야 한다. 인증서 개인키와 CA는 저장소와 Task Definition의 일반 환경변수에 넣지 않는다. ECS 실행 역할이 Secret 값을 주입하고 컨테이너 시작기가 권한 `0600`의 임시 파일로 작성한다. 이미지 빌드는 `infra/containers/ai-service.Dockerfile`에 `FOODTECH_PACKAGE=foodtech-credential-registration` 또는 `foodtech-ai-gateway`를 전달한다. CI는 두 이미지를 빌드만 하며 게시하지 않는다.

두 Task의 리스너는 컨테이너 안에서 `0.0.0.0`에 결합한다. 실제 실행 전에 업무 API가 두 서비스에만 접근할 수 있는 **사설 서브넷·보안 그룹**, 서비스 DNS와 일치하는 서버 인증서 SAN, 업무 API 전용 클라이언트 인증서를 마련해야 한다. 현재 Task Definition은 ECS Service, Cluster, 네트워크, 이미지 게시, TLS Secret 생성, 업무 API 주소 설정을 만들지 않는다. 따라서 합성 성공만으로 종단간 배포가 완료된 것은 아니다. Gateway의 Ollama 기본 주소도 같은 Task의 loopback이다. 서버 GPU·Ollama 배치가 결정되기 전에는 로컬 모델 호출을 배포 검증 결과로 간주하지 않는다.

현재 개발 계정에는 서비스용 ECR 저장소와 TLS Secret이 아직 없다. 실행 전에 이미지와 Secret을 준비하고 실제 ARN·검토한 digest로 합성해야 한다. Fargate 상시 실행은 요금이 발생하므로 Task Definition 합성과 서비스 배포를 분리한다.

`-c provisionCredentialVault=true -c credentialKeyPeriod=2026-11`은 `FoodtechCredentialVault-dev` 스택을 합성한다. 2026-11-07 개발 계정 `ap-southeast-2`에 Vault DynamoDB On-demand 테이블, PITR 7일, 저장용 대칭 KMS 키와 별도 등록·Gateway ECS Task 역할을 배포했다. KMS 키·테이블은 `RETAIN`이다. 등록 역할은 Vault `PutItem`·저장 키 `GenerateDataKey`, Gateway 역할은 Vault `GetItem`·저장 키 `Decrypt` 및 AI 실행 레지스트리 접근만 허용한다. KMS 역할 정책은 `purpose=EXTERNAL_AI_CREDENTIAL`, `environment=dev` context를 요구한다. KMS 키와 PITR은 리소스를 유지하는 동안 비용이 발생할 수 있다. 실제 서비스 Task Definition 연결은 후속이다.

같은 날 두 역할을 각각 임시 ECS Fargate Task의 `taskRoleArn`에 연결해 실제 AWS API를 시험했다. 등록 역할은 Vault `PutItem`·KMS `GenerateDataKey`가 성공하고 `GetItem`·`Decrypt`가 `AccessDeniedException`으로 거부됐다. Gateway 역할은 Vault `GetItem`·KMS `Decrypt`가 성공하고 `PutItem`·`GenerateDataKey`가 거부됐다. 합성 Vault 항목과 임시 클러스터·Task 정의·실행 역할·보안 그룹·로그 그룹은 시험 후 정리했다. IAM 시뮬레이터는 실제 Vault 읽기에 성공한 개발 역할까지 조직 정책 거부로 표시했으므로, 이 계정의 해당 시뮬레이션 결과를 실제 Task 요청 결과보다 우선하지 않는다. 이 시험은 역할의 AWS API 권한을 검증한 것이며 실제 서비스 배포·외부 공급자 호출 증거는 아니다.

자격증명 등록 경로를 위해 같은 개발 스택에 RSA-4096 전송 KMS 키와 HMAC-256 가명 KMS 키를 추가했다. 등록 역할에는 전송 키 `GetPublicKey`·`Decrypt(RSAES_OAEP_SHA_256)`, 가명 키 `GenerateMac`, AI 레지스트리 `PutItem`을 추가했다. 기존 Vault·저장 KMS 키·Gateway 역할은 교체하지 않았다. 두 새 키의 ARN은 스택 출력에 있으며 저장 키와 마찬가지로 `RETAIN`이다. 임시 Fargate Task에 등록 역할을 부여한 실제 요청에서 새 KMS 권한과 Vault·AI 레지스트리 원자적 기록이 성공했고 Vault `GetItem`은 거부됐다. 시험 데이터와 Task 리소스는 정리했다. 실제 서비스 Task 배포는 후속이다.

`-c provisionAiExecutionRegistry=true`는 별도 `FoodtechAiExecutionRegistry-dev` 스택을 합성한다. DynamoDB On-demand 테이블 `foodtech-ai-execution-registry-dev`는 실행·모델·자격증명 메타데이터·동의·월별 한도를 저장한다. `pk=EXEC#<실행 ID>, sk=META`로 Gateway가 실행 ID를 직접 조회한다. AWS 관리형 저장 암호화, 7일 시점 복구, 결과 항목의 `expires` TTL과 `RETAIN` 제거 정책을 적용한다. TTL은 비동기 물리 삭제이므로 90일 Hard Delete의 최종 보장은 별도 삭제 작업이 필요하다. 키 원문·암호문은 이 테이블에 저장하지 않는다. 2026-11-07에 개발 계정 `ap-southeast-2`에 이 스택을 배포했다. Gateway 실행 역할은 아직 배포되지 않았으며, 로컬 검증은 개발자 AWS 세션으로 수행했다. 2026-11-22 읽기 전용 점검에서 실제 PITR 기간이 기본값 35일인 것을 확인하여 코드에 7일을 명시했다. 준비한 CloudFormation 변경 세트는 이 속성만 수정하며 테이블 교체는 없다. 사용자 승인 후 해당 변경 세트 실행이 UPDATE_COMPLETE로 종료되었고 DescribeContinuousBackups에서 실제 RecoveryPeriodInDays=7을 확인했다. 이 확인은 해당 개발 테이블의 PITR 설정에 한정되며 전체 삭제·복원 후 재삭제 검증을 대신하지 않는다.

`FoodtechAuthSessions-dev`는 만료 시각(`expires`)이 있는 인증 흐름·세션과 관리자 권한 회수 상태를 저장하는 DynamoDB 테이블을 만든다. 앱은 `FOODTECH_AUTH_TABLE=foodtech-auth-sessions-dev`로 연결한다. TTL은 물리 삭제용이며 애플리케이션이 만료를 직접 검사한다.

Vault의 등록기·Gateway 기본 정책은 인증 표의 `USER#*` 조건 검사만 지정 리전에서 허용하고 `LeadingKeys`의 존재와 실패 반환 `NONE`을 요구한다. 두 서비스의 인증 조건 검사 요청도 `ReturnValuesOnConditionCheckFailure=NONE`을 명시한다. 별도 이력 정책의 보호된 `CONTROL` 검사는 그 정책에서만 승인하며, 기본 정책의 광범위한 Allow로 제한을 무효화하지 않는다. 이는 CDK 배포 후보의 계약이며 실제 적용·실행 검증을 뜻하지 않는다.

`provisionAuthRefreshKey=true`를 명시하면 별도 `FoodtechAuthRefreshKey-dev` 스택에 세션 갱신 토큰용 대칭 KMS 키와 ARN 출력을 합성한다. 이 키는 기존 AI 자격증명 키와 분리되며 자동 회전과 삭제 시 보존을 사용한다. 키를 실제 배포하면 KMS 비용이 발생한다. 현재 업무 API 실행 역할·키 사용 권한 및 환경 변수 연결은 구성되지 않았고, 합성은 실제 배포나 역할 검증이 아니다.

새 Cognito Identity 스택의 고객·관리자 App Client는 갱신 토큰 회전과 10초 재시도 유예를 정의한다. 회전과 호환되지 않는 `REFRESH_TOKEN_AUTH`는 사용하지 않는다. 이는 CDK 정의이며 기존 시험 Pool의 App Client 설정을 변경하지 않는다.

2026-11-01에 `foodtech` 프로파일의 `ap-southeast-2`에 `CDKToolkit`과 `FoodtechAuthSessions-dev`를 배포했다. 시험용 합성 식별자로 두 DynamoDB 저장소 인스턴스의 흐름 단일 소비·세션 공유·관리자 차단 상태 전파를 확인하고 시험 항목을 삭제했다. 이 스택은 Cognito User Pool을 만들지 않았다.

`-c provisionIdentity=true`를 주면 Cognito Essentials User Pool, 고객·관리자 App Client, 관리형 로그인 v2, 선택적 TOTP MFA, 관리자 그룹을 정의하는 `FoodtechAuthIdentity-dev`도 합성한다. 두 callback URL·두 웹 URL·고유 도메인 접두어를 context로 전달해야 한다. 현재 CLI로 만든 `foodtech-auth-poc-20261101` Pool은 이 스택의 소유 대상이 아니므로 `provisionIdentity` 없이 세션 스택만 배포한다. PoC Pool을 이 스택으로 자동 편입하거나 같은 이름으로 다시 만들지 않는다. 향후 별도 환경에 새 Pool을 배포할 때 이 정의를 사용하고, 기존 PoC는 사용자·연동 전환을 확인한 후 별도 정리한다. 시험용 Pool의 TOTP MFA도 `OPTIONAL`로 설정했다.

스택은 `ap-southeast-2`에 한정한다. 배포 전 `cdk synth --strict`와 `cdk diff`를 확인한다. AuthSessions 테이블과 User Pool의 제거 정책은 `RETAIN`이다. 현재 배포된 API 실행 역할은 없으며, 실제 API 배포 시 테이블의 `GetItem`·`PutItem`·`DeleteItem`·`UpdateItem`과 Pool의 `AdminGetUser`·`AdminListGroupsForUser`만 실행 역할에 부여한다. CI 검사에는 AWS 권한을 부여하지 않는다. 기본 CDK bootstrap의 CloudFormation 실행 정책은 `AdministratorAccess`로 생성되었으므로, 향후 GitHub OIDC 배포 역할을 연결하기 전에 신뢰 조건과 실행 권한을 별도로 제한한다.

`-c provisionDataFlow=true`로 `FoodtechMinimalDataFlow-dev`를 합성한다. 2026-11-01에 `foodtech` 프로파일의 `ap-southeast-2`에 배포했다. `foodtech-data-flow-dev` DynamoDB On-demand 테이블, `foodtech-ordered-dev.fifo`, `foodtech-processingdlq-dev.fifo`, `foodtech-standardizationdlq-dev.fifo`를 생성한다. 테이블은 AWS 관리형 저장 암호화와 `RETAIN` 제거 정책을 사용한다. 큐에는 TLS만 허용하고 content-based deduplication을 끈다. Ordered 큐의 처리 오류는 Processing DLQ로, 품질 격리는 Worker가 Standardization DLQ로 보낸다.

첫 합성 fixture 3건을 실제 테이블·큐에 넣어 순번 복원 `1, 2, 3`, 품질 `VALID` 2건·`QUARANTINED` 1건, `HARD_LIMIT_EXCEEDED` Finding 1건과 격리 큐 1건을 확인했다. 이는 단일 스트림의 최소 기능 증거이며 다중 Task 동시성·부하 기준 달성 증거가 아니다. 스택에는 상시 실행 컴퓨트가 없지만 테이블·큐는 계속 남아 저장·요청 사용량이 발생할 수 있다.

`-c provisionCookingJobs=true`는 조리작업·안전·평가 컬렉션용 별도 `FoodtechCookingJobs-dev` 스택과 `foodtech-cooking-jobs-dev` DynamoDB On-demand 테이블을 합성한다. `PK=SCOPE#USER#<서버 세션 subject>#JOB#<UUID>`에 `META`, `ASSESSMENT#REV#<12자리 버전>`, `ASSESSMENT#CURRENT`, `EVENT#JOB#...`, `EVENT#SAFETY#...`를 둔다. 작업 메타와 현재 포인터의 변경, 새 완료 평가 버전 및 상태 변경 이력은 조건부 트랜잭션으로 묶는다. 2026-11-02에 승인된 개발 스택을 `ap-southeast-2`에 생성했고 테이블이 `ACTIVE`임을 확인했다. 합성 레코드로 동시 평가 제출의 단일 성공·단일 충돌, 평가 버전과 포인터의 원자적 일치, 안전 차단·재평가·재조리 연결을 확인한 뒤 시험 레코드를 삭제했다. 테이블은 향후 개발을 위해 유지하며 저장·요청 사용량이 발생할 수 있다.

`FoodtechMinimalDataFlow-dev`에는 표준화 Worker용 ECS Task Role도 정의한다. 이 역할은 수집 테이블에 `GetItem`·`Query`·`PutItem`·`ConditionCheckItem`, 조리작업 테이블에 `GetItem`·`Query`·`PutItem`·`ConditionCheckItem`, Ordered 큐에 `ReceiveMessage`·`DeleteMessage`, Standardization DLQ에 `SendMessage`만 허용한다. 수집 테이블의 `Query`는 늦은 이벤트 파생 계산에서 같은 배치의 원본 목록을 조회하는 데 필요하고, 조리작업 테이블의 `Query`는 기존 공정 사건과 완료된 요청 조회에 필요하다. 신뢰 정책은 같은 계정·리전의 ECS Task로 제한한다. 역할 ARN은 `StandardizationWorkerTaskRoleArn` 출력으로 제공한다. 역할 생성만으로 Worker가 배포되거나 실행되지는 않으며, 후속 Task Definition의 `taskRole`에 연결해야 한다. 이 역할은 컨테이너 이미지 다운로드와 로그 전송에 쓰는 ECS Task Execution Role과 별개다. 2026-11-14 승인된 일회성 로컬 CDK 배포로 최초 역할·정책·출력을 생성했다. 이후 추가한 수집 테이블 `Query`는 별도 배포가 필요하다.

`AWS_PROFILE=foodtech .venv/bin/python tests/live/worker_role_iam_check.py`로 배포된 역할의 두 테이블과 두 큐에 대한 ID 기반 정책 및 실제 주체 시뮬레이션을 분리해 확인한다. 2026-11-14 실행에서는 요청 리전 조건을 전달하지 않아 필요한 권한 10건이 조직 정책에 의해 차단된 것으로 평가됐다. 2026-11-22 실제 요청 리전 `ap-southeast-2`을 지정해 재검증한 결과, 배포된 역할의 필요 권한 10건은 허용되고 제한 대상 4건은 거부되었으며 조직 정책 차단은 관찰되지 않았다. 이 결과는 현재 배포된 정책을 대상으로 한 시뮬레이션으로, 아직 배포되지 않은 수집 테이블 `Query` 권한이나 실제 ECS Task의 AWS API 호출을 검증하지 않는다. 후속 배포와 Task Definition 연결·실행 검증이 남아 있다.


`-c provisionPrivacyStorage=true`는 별도 `FoodtechPrivacyStorage-dev`에 Identity Vault·Link Capsule·Deletion Registry의 독립 DynamoDB 테이블과 각 테이블의 KMS 저장 암호화 키, Registry 연결정보 암호화를 위한 별도 KMS 키를 합성한다. 세 테이블은 On-demand, 7일 PITR, 삭제 보호 및 RETAIN을 사용한다. Registry만 `ttl_delete_after` TTL을 보조 정리 수단으로 설정하며 TTL을 삭제 완료 증거로 사용하지 않는다. Registry는 복원 세트와 함께 과거 상태로 되돌리지 않는다. 해당 스택은 기본적으로 생성되지 않는다.

이 구성은 저장소 정의에 한정되며 아직 배포되지 않았다. 각 사용자에 대한 IAM/STS 신뢰 경계, Vault·Capsule 조회 및 Registry 암호화 전용 역할과 JIT 복호화 역할, Capsule 사용자별 DEK와 감사, Gateway Endpoint와 삭제·복원 절차는 제공하지 않는다. KMS 기본 키 정책은 계정 IAM 위임을 허용하므로 실제 권한 분리는 후속 정책 설계·실제 역할 검증이 필요하다. 합성 검사는 `CDK_DEFAULT_ACCOUNT=573693340100 npx cdk --app 'node --import tsx bin/app.ts' synth FoodtechPrivacyStorage-dev --strict -c provisionPrivacyStorage=true --output /tmp/foodtech-privacy-cdk`를 `infra/cdk`에서 실행한 뒤 `python3 tests/config/check_privacy_storage.py /tmp/foodtech-privacy-cdk/FoodtechPrivacyStorage-dev.template.json`으로 확인한다. 배포 시 별도 비용과 삭제 보호·RETAIN 자원 정리가 필요하며, 합성 성공을 배포 승인이나 보안 검증으로 취급하지 않는다.

개인정보 스택에는 삭제 진행 체크포인트용 별도 DynamoDB 테이블과 테이블 저장 암호화 키, 체크포인트 봉투 암호화용 별도 KMS 키도 정의한다. 테이블은 다른 개인정보 테이블과 같은 On-demand·7일 PITR·삭제 보호·RETAIN을 적용한다. 이 정의는 역할 부여나 실제 배포를 포함하지 않으며 진행 기록의 보존·복원 절차와 전용 역할 검증은 남아 있다.
수집 격리 버킷은 `FoodtechMinimalDataFlow-dev`에 S3 관리형 암호화·공개 차단·TLS 강제·RETAIN으로 정의하고 `IngestionDlqBucketName`으로 출력한다. 이 정의는 아직 배포되지 않았다.

수집 어댑터 Task 역할은 데이터 흐름 테이블의 조회·기록, `STREAM#` 파티션의 순서 복구 항목 삭제, Ordered FIFO 전송과 격리 버킷 `ingestion/` 객체 저장 권한을 갖도록 정의한다. CDK 합성과 합성 정책의 허용·거부 시뮬레이션만 확인했으며 아직 배포되지 않았다. 실제 ECS 실행 및 조직 정책 검증도 남았다.

배포 후 `AWS_PROFILE=foodtech .venv/bin/python tests/live/collector_role_iam_check.py`로 배포된 역할의 정책과 Organizations를 포함한 유효 권한을 분리해 시뮬레이션한다. 개발 스택에 역할 또는 버킷 출력이 없으면 변경 없이 중단한다. 정책 시뮬레이션은 실제 ECS Task 실행을 대체하지 않는다.

`infra/containers/collection-adapter.Dockerfile`은 잠금된 Python 의존성으로 비관리자 수집기 이미지를 만든다. 진입점은 주입된 인증서 자료를 검증해 권한 `0600` 임시 파일로 저장한 뒤 `FOODTECH_COLLECTION_PROTOCOL`에 따라 REST·MQTT·OPC UA JSON·OPC UA Binary 수집 명령을 실행한다. CI는 이미지를 빌드해 각 명령의 도움말 실행을 확인한다.

`provisionRestCollectionTask=true`를 `provisionDataFlow=true`와 함께 사용하면 위 역할을 사용하는 단발 Fargate Task Definition을 합성한다. `collectionImageRepository`, `collectionImageDigest`(`sha256:`와 소문자 16진수 64자리), `collectionMockBaseUrl`(HTTPS origin), `collectionMockSecretArn`이 필요하다. Secrets Manager JSON 필드 `client_id`, `client_secret`, `ca_pem`은 Task 실행 역할을 통해 주입하며 일반 환경변수로 저장하지 않는다. 기본 수집은 한 번 실행 후 종료하며 실행 시 허용된 CLI 인수를 지정할 수 있다.

`provisionMqttCollectionTask=true`는 같은 이미지와 역할을 사용하는 별도 단발 MQTT Task를 합성한다. 이미지 설정 외에 `collectionMqttHost`, `collectionMqttPort`, `collectionMqttSecretArn`이 필요하며 비밀 JSON 필드 `username`, `password`, `client_id`, `ca_pem`을 사용한다.

`provisionOpcUaJsonCollectionTask=true`는 프로젝트 전용 HTTPS Browse/Read 수집 Task를 합성한다. 이미지 설정 외에 `collectionUaJsonBaseUrl`(HTTPS origin), `collectionUaJsonSecretArn`이 필요하며 비밀 JSON 필드 `client_id`, `client_secret`, `ca_pem`을 사용한다.

`provisionOpcUaBinaryCollectionTask=true`는 별도 OPC UA Binary Task를 합성한다. `collectionUaBinaryEndpoint`(`opc.tcp` 주소)와 `collectionUaBinarySecretArn`이 필요하며 비밀 JSON 필드 `client_cert_pem`, `client_key_pem`, `server_cert_der_base64`를 사용한다. 클라이언트 인증서와 개인키 일치 및 서버 인증서 DER 형식을 시작 시 검사하고, 기존 수집기가 서버 인증서의 유효기간·호스트·애플리케이션 URI를 확인한다. 이미지 게시, 네트워크·Mock 배치, 실제 Task 실행은 별도 작업이다.

표준화 Worker 역할의 인증 테이블 `dynamodb:ConditionCheckItem` 권한은 제육 작업의 삭제 잠금 확인을 위한 CDK 변경안이다. 적용을 위해서는 승인된 배포 workflow와 역할 검증이 필요하며, 배포하지 않은 합성 결과를 실제 권한으로 취급하지 않는다.
자격증명 등록 Task의 인증 테이블 조건 검사 권한과 `FOODTECH_AUTH_TABLE` 설정은 삭제 중 신규 자격증명 생성을 막기 위한 변경안이다. 합성 결과이며 배포 및 실제 Task 역할 검증은 별도로 수행한다.
Gateway Task의 인증 테이블 조건 검사 권한과 `FOODTECH_AUTH_TABLE` 설정은 삭제 중 새 AI 호출 예약을 막기 위한 변경안이다. 합성 결과이며 배포와 실제 역할 검증은 별도로 수행한다.

Gateway의 실행 정산은 레지스트리의 `ACTIVE#` 진행 표식을 같은 트랜잭션에서 삭제한다. 이를 위한 `DeleteItem` 권한은 해당 파티션 접두사와 `TransactWriteItems`로 제한하며, 개발 계정의 정책 시뮬레이션에서 허용 및 다른 키·단건 삭제 거부를 확인했다. 실제 배포와 실행 역할 검증은 남았다.

2026-11-22 기존 배포 Gateway 역할에 대해 활성 표식 삭제를 시뮬레이션한 결과 `explicitDeny`, `AllowedByOrganizations=false`였으나 요청 리전 조건이 빠져 있었다. 따라서 이 결과만으로 실제 조직 정책 차단을 단정할 수 없다. 위 정책 변경은 아직 배포되지 않았고 정책 시뮬레이션은 실제 Task 실행이 아니다. 리전을 포함한 재검증과 실제 정산 트랜잭션 검증이 필요하다.

재확인은 `AWS_PROFILE=foodtech .venv/bin/python tests/live/ai_gateway_role_iam_check.py`를 사용한다. 시뮬레이션에 실제 요청 리전과 필요한 DynamoDB 조건을 전달하며 결과는 실제 실행을 대체하지 않는다.

2026-11-22 리전 조건을 포함한 기존 배포 역할 재검증에서는 네 사례 모두 조직 정책 허용으로 평가됐다. 필요한 활성 표식의 트랜잭션 삭제는 역할 정책에서 `implicitDeny`였고 다른 파티션·키 없음·단건 삭제도 거부됐다. 필요한 역할 정책 변경은 미배포 상태이므로 배포 후 권한 및 실제 Task 정산을 검증해야 한다.

## 만료 신원 제거 작업자의 비활성 배포 초안

`provisionWithdrawalIdentitySchedule=true`는 별도 `FoodtechWithdrawalIdentitySchedule-<stage>` 스택에 신원 제거 전용 Fargate Task와 **DISABLED** EventBridge 예약 규칙을 합성한다. `ImageUri`는 ECR의 `@sha256` 고정 이미지이고 Task 진입점은 `python -m foodtech_business_api.withdrawal_identity_worker`다. 비루트 사용자·읽기 전용 루트 파일시스템·프라이빗 서브넷·공인 IP 미할당을 사용한다. 재집계와 Task family·group·로그·실패 지표를 분리하며 기존 재집계 스택의 합성 템플릿은 변경하지 않는다.

예약 스택의 배포 입력은 검토한 `TaskRoleArn`, `ExecutionRoleArn`, `InvokeRoleArn`, `ClusterArn`, `ImageUri`, `SubnetIds`, `SecurityGroupIds`, `DeliveryDlqArn`, 실측·검토할 `IntervalMinutes` 및 `CognitoUserPoolId`다. 세 역할의 ARN이 서로 같으면 CloudFormation 규칙에서 거부한다. ARN이 다르다는 것만으로 최소 권한을 증명하지 않으며 실제 정책·신뢰 관계·PassRole 범위·계정/리전·네트워크를 별도로 검증해야 한다. 예약 스택은 역할·사용자·개인정보 저장소를 생성하거나 기존 역할에 실행 권한을 부여하지 않는다. 별도의 역할 정의 초안은 아래 Access 스택으로 제공한다. 같은 stage의 인증·삭제 Registry 테이블과 검증된 Cognito Pool이 준비되어야 한다.

신원 제거 예약의 합성에는 검증할 계정을 명시한다. 역할 입력은 같은 계정/stage의 기존 `foodtech-withdrawal-identity-task/execution/invoke-<stage>` 이름에 각각 제한하고 TaskRoleArn을 명령의 기대 역할로 전달한다. 명령은 ECS container-role provider와 STS의 실제 역할을 추가 확인한다. Task 환경의 전체 실행 제한은 600초이고 종료 유예는 30초다. 이 입력 제약은 최소 권한·실제 배포 적합성의 증명이 아니며 예약은 계속 DISABLED다.

Task에는 신원 제거 작업자의 명시적 활성화와 공개 자원 식별자만 전달한다. 웹 API의 탈퇴·초기화 기능 플래그는 변경하지 않는다. 실행 시 이미 준비된 초기화 제어 항목을 읽으며 자동 생성하지 않는다. Task를 수동으로 실행하면 잠금·기한이 검증된 이전 Cognito 주체를 실제로 제거하므로, 비활성 예약 규칙만으로 수동 실행까지 차단된다고 간주하지 않는다.

실행 요청 전달 실패는 지정한 EventBridge DLQ로 보내고, 해당 Task group의 STOPPED 이벤트에서 비정상·누락 종료 코드를 별도 지표·경보로 관찰한다. 경보의 알림 대상·실제 전달·중복/동시 실행·재시도는 배포 검증 전제다. 경보와 로그는 작업 미실행이나 관찰 경로 자체의 실패를 모두 증명하지 않으며 전체 데이터 삭제·백업 제거의 완료 증거가 아니다. 제어 항목·실행 역할·이미지 게시·프라이빗 연결·실제 합성 Task 검증과 전체 삭제 조정기 연결 전에는 예약 규칙을 활성화하지 않는다.

`infra/cdk`에서 `npx cdk --app 'node --import tsx bin/app.ts' synth FoodtechWithdrawalIdentitySchedule-dev --strict -c provisionWithdrawalIdentitySchedule=true --output /tmp/foodtech-withdrawal-identity-cdk`로 합성하고, 저장소 루트에서 `python3 tests/config/check_withdrawal_identity_schedule.py /tmp/foodtech-withdrawal-identity-cdk/FoodtechWithdrawalIdentitySchedule-dev.template.json`으로 확인한다. TypeScript 검사·신원 제거 및 기존 재집계 계약 검사·기존 템플릿 전후 동일성 비교가 통과했다. 실제 스택 배포나 예약·수동 Task 실행은 수행하지 않았다.

### 신원 제거 권한 후보의 검증

2026-11-22 IAM Policy Autopilot에 신원 제거 명령·경계·계정 제어의 실제 Python 소스와 개발 계정·리전을 입력했다. 아래는 저장소 루트에서 실행할 재현 명령이다. `CDK_DEFAULT_ACCOUNT`에는 확인된 개발 계정이 필요하며 출력은 저장소 밖의 비공개 파일로 둔다. 이 명령은 AWS 정책을 업로드하지 않는다.

```sh
task_repo_root="$(pwd)"
umask 077
DISABLE_IAM_POLICY_AUTOPILOT_TELEMETRY=true uvx iam-policy-autopilot@latest generate-policies \
  "$task_repo_root/apps/business-api/src/foodtech_business_api/withdrawal_identity_worker.py" \
  "$task_repo_root/apps/business-api/src/foodtech_business_api/withdrawal_identity.py" \
  "$task_repo_root/apps/business-api/src/foodtech_business_api/principal_enrollment.py" \
  --region ap-southeast-2 --account "${CDK_DEFAULT_ACCOUNT:?Set the verified development account}" \
  --service-hints dynamodb cognito-idp --pretty \
  > /tmp/foodtech-withdrawal-identity-autopilot.json
```

생성 후보는 트랜잭션의 항목별 목적과 호출하지 않는 계정 관리 메서드를 구분하지 못해 광범위한 DynamoDB 생성·삭제 및 KMS 권한을 포함했다. 실제 IAM `SimulateCustomPolicy`에서 인증·삭제 Registry·무관한 테이블을 각각 검사해 과도한 허용을 확인했다. 이 후보는 사용하지 않는다.

`tests/live/withdrawal_identity_iam_check.py --policy-file <비공개 후보 파일> --pool-id <개발 Pool ID>`는 입력 정책을 수정하지 않고 허용·거부 시나리오를 검사한다. 계정·제어 항목의 필요한 읽기, 제한된 Scan과 이전 계정의 트랜잭션 갱신, Registry 읽기·조건 검사, 지정 Pool의 조회·제거가 동작하는지 확인하는 동시에 세션 읽기·임의 제어 변경·직접 갱신·키 누락·Registry 변경·다른 테이블/Pool·임의 저장 키 복호화를 거부해야 한다. 허용 전체·거부 전체·불완전한 시뮬레이션 결과도 검증 실패다. 출력에는 시나리오명·판정만 담고 정책·ARN·주체·커서는 출력하지 않는다. 현재 속성 제한까지 포함해 37개를 검사하며 `--registry-storage-key-arn`을 지정하면 저장 키의 서비스·테이블·계정 문맥을 포함한 41개를 검사한다.

자동 생성 후보는 최초 34개 검사에서 22개 차단 조건을 충족하지 못해 종료 코드 1로 거부됐다. 검사는 정책 생성·수정·연결·리소스 쓰기를 하지 않았다. 검사기의 `runtime_verified`는 항상 false이며 전체 삭제 완료를 표시하지 않는다.

### 승인된 권한 세분화 초안

2026-11-22 사용자 승인에 따라 자동 생성 후보를 실제 호출 계약에 맞춰 세분화했다. `provisionWithdrawalIdentityAccess=true`는 별도 `FoodtechWithdrawalIdentityAccess-<stage>` 스택에 아래 세 역할을 정의한다. 합성에는 확인된 `CDK_DEFAULT_ACCOUNT`가 필요하고, `CognitoUserPoolId`, `RegistryStorageKeyArn`, `ImageRepositoryArn`, `ClusterArn`은 기본값 없는 입력이다. 키·저장소·클러스터 ARN은 같은 계정·`ap-southeast-2` 범위로 제한한다. 실제 역할 생성·정책 적용은 수행하지 않았다.

| 역할 | 허용 범위 |
| --- | --- |
| 작업 역할 | `USER#`의 트랜잭션 조회·제한된 갱신, 초기화 제어 항목의 조회, 속성을 지정한 탈퇴 메타데이터 Scan, Registry의 조회·조건 검사, 지정 Pool의 사용자 조회·제거, 지정 Registry SSE 키의 DynamoDB 문맥 복호화 |
| ECS 실행 역할 | 기존 지정 ECR 저장소의 이미지 읽기와 작업자 로그 기록. ECR 인증 토큰만 API 특성상 리소스 `*`를 사용하며 리전을 제한 |
| 예약 호출 역할 | 지정 클러스터에서 신원 제거 Task family 실행과 그 Task/실행 역할만 ECS로 PassRole |

작업 역할은 계정/Registry 생성·삭제, Registry 변경, 초기화 제어 항목 변경, 세션 읽기와 locator 봉투 복호화를 허용하지 않는다. Scan은 `SPECIFIC_ATTRIBUTES`와 허용 속성을 함께 요구하며, 코드도 Select를 명시한다. 갱신은 트랜잭션·`USER#` 키·참조 속성 범위를 요구한다. `ForAllValues` 조건에는 해당 문맥 키의 `Null=false` 검사를 함께 둔다. 조건식이 참인지, 요청·기한·sub가 실제로 일치하는지는 신뢰된 작업자 코드가 검증하며 IAM만으로 이를 증명하지 않는다. 갱신 허용 속성에는 조건식에서 읽는 상태 필드도 포함되므로 IAM을 '표식 필드만 쓰도록 강제하는 장치'로 해석하지 않는다.

AWS 공개 Service Authorization Reference와 [DynamoDB 항목별 트랜잭션 인가](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/specifying-conditions.html)를 확인했다. 트랜잭션 후보의 모든 동작을 모든 테이블에 부여하지 않고 실제 항목별 호출로 나눈다. `ConditionCheckItem`은 `EnclosingOperation` 조건 키를 지원하지 않으므로 해당 조건을 사용하지 않으며 정확한 테이블·`REGISTRY` 키·리전을 검사한다. 저장 키 복호화는 [DynamoDB의 테이블명·계정 암호화 문맥](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html)을 요구한다. ECR pull-through 생성·복제와 ECS 태그 생성은 현재 이미지 읽기·태그 없는 실행 경로에 포함하지 않는다.

Task/실행 역할의 신뢰는 ECS 서비스와 같은 계정·리전의 SourceArn으로 제한한다. ECS Task 신뢰의 [클러스터 단위 SourceArn 제한 미지원](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html)을 반영해 문서가 요구하는 ECS ARN 범위를 사용한다. 예약 호출 역할은 고정한 비활성 예약 규칙 ARN만 신뢰하며 RunTask에서 별도로 클러스터를 대조한다. 이 역할은 전체 삭제 조정기·계정 최종 제거 역할이 아니다.

합성 후 `python3 tests/config/check_withdrawal_identity_access.py <Access 템플릿> --review-output /tmp/foodtech-identity-policy-review`로 정책을 변경하지 않은 채 CloudFormation 참조만 합성 값에 결합해 비공개 검토 파일을 만들 수 있다. 이 출력의 Pool·키·저장소·클러스터는 명시적 합성 바인딩이며 배포된 자원을 나타내지 않는다. `tests/live/withdrawal_identity_access_iam_check.py --review-dir /tmp/foodtech-identity-policy-review`는 이 후보를 실제 IAM 시뮬레이터로 검사한다.

실제 정책 시뮬레이터에서 작업 역할 41개·실행 역할 17개·호출 역할 12개, 총 70개가 통과했다. IAM Access Analyzer `ValidatePolicy`도 세 후보 모두 지적 사항 0개였다. TypeScript·역할/예약 템플릿 계약 검사도 통과했다. CI의 Access 스택 합성은 가상 계정 번호를 사용해 실제 계정에 결합한 원문 정책을 공개 로그에 출력하지 않는다. 정책 시뮬레이션은 실제 역할·SCP/RCP·키 정책·이미지/네트워크·ECS 실행을 증명하지 않는다. 검증된 실제 자원 바인딩과 명시적 배포 절차를 마련한 뒤 실제 역할의 허용·거부 및 서비스 호출을 확인해야 한다. 예약 규칙은 계속 비활성이며 전체 삭제 완료 증거는 별도다.
`dynamodb:EnclosingOperation`은 문자열 문맥 키이므로 단일 값 `StringEquals`로 제한하고 시뮬레이션에도 `string`으로 전달한다. 배열용 `ForAnyValue`를 사용하지 않는다. 수정 후보의 실제 IAM 시뮬레이션 6건에서 활성 표식 트랜잭션 삭제만 허용하고 다른 파티션·키 누락·직접 삭제·혼합 키·다른 연산은 거부했다. 합성·시뮬레이션은 실제 역할에 정책을 적용하거나 실제 ECS 정산을 검증한 결과가 아니다.

2026-11-22 리전 조건을 포함한 기존 배포 역할 재검증에서는 네 사례 모두 조직 정책 허용으로 평가됐다. 필요한 활성 표식의 트랜잭션 삭제는 역할 정책에서 `implicitDeny`였고 다른 파티션·키 없음·단건 삭제도 거부됐다. 필요한 역할 정책 변경은 미배포 상태이므로 배포 후 권한 및 실제 Task 정산을 검증해야 한다.

## 신원 제거 역할의 수동 읽기 검증

`provisionWithdrawalIdentityProbe=true`는 개발용 `FoodtechWithdrawalIdentityProbe-dev`에 검증 명령의 Fargate Task 정의와 7일 보존 로그 그룹만 등록한다. Role·VPC·클러스터·예약·Service를 생성하거나 `RunTask`를 실행하지 않는다. 기본값 없는 `TaskRoleArn`, `ExecutionRoleArn`은 같은 계정의 이미 검토한 identity 작업/실행 역할 이름으로 제한한다. `ImageUri`도 해당 계정·ap-southeast-2의 `foodtech/maintenance-dev@sha256`로 제한하고, `ProbeId`에는 새 UUIDv4를 지정한다.

명령은 `python -m foodtech_business_api.withdrawal_identity_probe`다. Fargate 환경과 명시적 플래그·계정·검증 ID가 없으면 SDK 생성 전 종료 코드 2로 거부한다. SDK 자격증명 provider가 container-role이어야 하고, STS가 반환한 계정/ARN이 지정 identity 작업 역할과 일치해야 읽기를 시작한다. 정적 키·프로필·일반 AssumeRole 자격증명을 대체 사용하지 않으며 사용자 지정 AWS endpoint 설정을 무시한다. 환경 변수 자체를 인증 근거로 삼지 않는다. 정상 경로는 제어 키의 pk 조회, 실제 업무 ID가 될 수 없는 `ROLEPROBE#` 키의 인증/Registry 트랜잭션 조회, DynamoDB 레코드를 최대 1개 검사하는 메타데이터 Scan의 요청 수락을 확인한다. 이어 세션 읽기·트랜잭션 밖의 계정 읽기·다른 업무 테이블의 트랜잭션 읽기에서 정확한 IAM AccessDeniedException을 요구한다. throttling·테이블 없음·예상 밖 허용은 통과로 간주하지 않는다.

이 검증 명령에는 데이터 변경·Cognito·직접 KMS 호출이 없다. 기존 작업 역할 자체에는 승인된 삭제/갱신 권한이 있으므로 이를 '읽기 전용 IAM 역할'로 표현하지 않는다. 성공 로그는 고정된 검사명과 완료 범위만 담고 계정/세션 ARN·키·반환 항목·SDK 오류·커서는 출력하지 않는다. 한 번의 제한된 Scan은 전체 검색이 아니며, 읽기 요청 수락만으로 제어 항목/삭제 레코드 존재·초기화 준비·KMS 복호화·Cognito 삭제·전체 삭제 완료를 증명하지 않는다.

실행 역할이 허용한 `/foodtech/dev/withdrawal-identity` 로그 그룹은 실제 작업자와 공유한다. 기본 `identityLogGroupMode=create`는 이 그룹을 RETAIN으로 생성한다. 한 스택에서 생성한 뒤 다른 스택이나 후속 재생성에서는 존재/설정/소유권을 확인하고 `identityLogGroupMode=existing`을 명시해 참조만 한다. 두 스택이 동시에 생성하려는 합성은 거부한다. 이는 CloudFormation import나 존재 확인이 아니며, 기본 신원 제거 예약 스택과 기존 재집계 스택의 생성 계약은 유지한다.

실제 실행 전에는 이 명령을 포함한 main 커밋의 이미지를 새로 게시하고, digest·스캔 finding·실행 적합성을 검토해야 한다. 이전 게시 이미지를 새 모듈이 들어 있는 것처럼 사용하지 않는다. Task 정의 배포와 실제 실행·임시 네트워크 재생성은 별도 승인 범위다. 실행할 때 검토한 private subnet/송신 SG와 `assignPublicIp=DISABLED`를 명시하고 종료·로그·네트워크 정리를 확인한다. Task 정의의 awsvpc 설정만으로 이런 RunTask 입력이 강제됐다고 주장하지 않는다. 예약 호출 역할을 probe family에 맞춰 확대하지 않으며, 제어/삭제 시험 전용 업무 기록을 만들거나 사용자 삭제를 수행하는 단계도 포함하지 않는다.

로컬에서는 `infra/cdk`에서 아래처럼 AWS 조회 없이 합성하고 저장소 루트의 검사기로 등록 범위를 확인한다. 합성은 실제 로그·이미지·역할·프라이빗 연결의 적합성 증거가 아니다.

```sh
CDK_DEFAULT_ACCOUNT=123456789012 npx cdk --app 'node --import tsx bin/app.ts' synth FoodtechWithdrawalIdentityProbe-dev --strict --no-lookups -c provisionWithdrawalIdentityProbe=true --output /tmp/foodtech-identity-read-probe-cdk
python3 ../../tests/config/check_withdrawal_identity_probe.py /tmp/foodtech-identity-read-probe-cdk/FoodtechWithdrawalIdentityProbe-dev.template.json
```

## 개발용 유지보수 실행 기반

`provisionRuntimeFoundation=true`는 별도 `FoodtechRuntimeFoundation-dev`에 새 VPC, 공개 egress 서브넷·프라이빗 Task 서브넷 각 1개, NAT Gateway·Elastic IP 각 1개, DynamoDB·S3 Gateway Endpoint, HTTPS 송신 전용 Task 보안 그룹, ECS 클러스터와 비공개 ECR 저장소 `foodtech/maintenance-dev`를 합성한다. 기존 업무 테이블·Cognito·키·IAM 역할은 변경하지 않으며 Task·Service·예약 작업을 생성하지 않는다. 이 구성은 단일 AZ 개발 검증용이며 장애 시 가용성이나 운영 배포 완료를 보장하지 않는다.

최초 생성의 저장소 모드는 `runtimeImageRepositoryMode=create`가 기본이다. 비용 정리를 위해 기반 스택을 삭제하면 RETAIN ECR 저장소와 이미지는 남는다. 이후 기반을 재생성할 때는 먼저 같은 계정·리전의 `foodtech/maintenance-dev` 존재, 불변 태그·스캔·암호화 설정과 사용할 digest를 확인한 뒤 `-c runtimeImageRepositoryMode=existing`을 명시한다. 이 모드는 해당 이름의 저장소를 참조만 하며 ECR 생성·변경·삭제·정책 갱신이나 CloudFormation 소유권 가져오기를 하지 않는다. 합성에는 AWS 조회가 없으므로 저장소 존재나 설정 적합성을 검증했다고 간주하지 않는다. 저장소가 없으면 기존 모드를 선택하지 않으며, 존재하는 저장소에 기본 생성 모드를 적용하면 이름 충돌로 실패한다. 소유권 이관이 필요하면 별도의 CloudFormation import 절차로 검토한다.

기존 기반 스택에 저장소를 여전히 소유한 채 이 모드만 변경하는 것을 재생성 절차로 사용하지 않는다. 삭제가 완료된 기반을 다시 만드는 경우에 사용하고, 변경 세트에서 ECR 추가·변경·삭제가 없는지 확인한다. 두 모드 모두 NAT 비용 확인과 실제 배포 승인은 동일하게 필요하며, 이미지의 스캔·실행 적합성은 별도다. 2026-11-26 게시 시험의 OpenSSL finding은 [이슈 #247](https://github.com/davidots429/foodtech/issues/247)에서 추적한다.

NAT Gateway와 공인 IPv4는 Task가 없어도 비용이 발생한다. `runtimeEgressMode=nat`를 함께 명시하지 않으면 합성을 거부한다. 배포 AZ는 기본값 없는 `AvailabilityZone` 파라미터로 검토·지정한다. 실제 배포 전에 계정의 가용 AZ·Free Plan 제한·크레딧·정리 시점과 CloudFormation 변경 세트를 확인한다. 이번 코드는 배포나 자원 생성 승인을 의미하지 않는다.

두 서브넷 모두 자동 공인 IP 할당을 끈다. 인터넷 기본 경로는 공개 egress 서브넷의 IGW와 프라이빗 서브넷의 NAT로 분리한다. Task 보안 그룹에는 인바운드 규칙이 없고 TCP 443 송신만 허용한다. 이는 포트 제한이며 목적지 호스트 allowlist나 외부 AI 전송 승인, MQTT·OPC UA 수집기 네트워크를 제공하지 않는다.

DynamoDB Endpoint는 같은 계정의 명시된 프로젝트 테이블/인덱스와 항목 작업 및 시작 시 검증에 필요한 `DescribeTable`만 통과시킨다. 원천 권한 정리 작업의 `foodtech-source-access-dev`도 명시된 목록에 포함한다. `ListTables`·테이블 생성/삭제·다른 계정/임의 테이블은 허용하지 않는다. [Gateway Endpoint 정책](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-ddb.html)은 추가 접근 경계이며 역할 권한을 부여하지 않는다. 서비스별 역할·사용자 키 범위는 별도로 제한해야 한다. 합성 검사 통과는 실제 프라이빗 Task의 접근 검증이 아니며 적용·실행은 별도 승인 대상이다. S3 Endpoint는 지역 ECR 이미지 계층 버킷의 GetObject만 허용하므로 애플리케이션 격리 버킷 읽기/쓰기를 제공하지 않는다. 수집기를 이 서브넷에 연결하려면 실제 격리 버킷과 권한을 검토해 Endpoint 정책을 확장해야 한다.

Cognito 도메인을 사용하는 기존 호스팅 로그인과의 호환성을 유지하기 위해 Cognito Interface Endpoint를 자동 추가하지 않는다. [Cognito PrivateLink 공식 제약](https://docs.aws.amazon.com/cognito/latest/developerguide/vpc-interface-endpoints.html)을 배포 때 다시 확인한다. ECR 이미지 계층의 S3 Gateway Endpoint 사용은 [ECR 공식 문서](https://docs.aws.amazon.com/AmazonECR/latest/userguide/vpc-endpoints.html)를 따른다.

ECR은 불변 태그·기본 스캔·AES256 저장 암호화·RETAIN이다. 스택을 삭제해도 이미지 저장소는 남으므로 중단 시 별도 정리한다. 이 스택은 이미지를 빌드하거나 업로드하지 않는다. 기존 `infra/containers/combi-refresh-worker.Dockerfile`의 업무 패키지를 게시한 뒤 검증된 digest를 사용하고, 역할 스택·비활성 Task/예약 스택은 이 스택의 출력에 실제 자원을 결합해 별도로 검토한다. 일반 업무 API와 삭제 작업자를 같은 실행 역할로 합치지 않는다.

```sh
# infra/cdk에서 합성; 가상 계정은 CI 전용이다.
CDK_DEFAULT_ACCOUNT=123456789012 npx cdk --app 'node --import tsx bin/app.ts' synth FoodtechRuntimeFoundation-dev --strict --no-lookups -c provisionRuntimeFoundation=true -c runtimeEgressMode=nat --output /tmp/foodtech-runtime-foundation-cdk
python3 ../../tests/config/check_runtime_foundation.py /tmp/foodtech-runtime-foundation-cdk/FoodtechRuntimeFoundation-dev.template.json
```

TypeScript·strict synth와 네트워크/저장소 경계 검사를 통과했다. 명시적 NAT 선택이 없는 합성의 거부도 확인했다. 실제 AWS 배포·Task 역할·이미지 pull·Cognito 접근·작업 완료는 아직 검증하지 않았다.

## 유지보수 이미지의 수동 게시와 OIDC

유지보수 이미지는 Python 3.12의 Debian 13(trixie) 변형을 명시하고 빌드·실행 단계가 같은 갱신된 base를 공유한다. [DSA-6657-1](https://security-tracker.debian.org/tracker/DSA-6657-1)에 따라 stable security 저장소의 libzstd1을 갱신하며, `1.5.7+dfsg-1+deb13u1` 이상이 아니면 빌드를 실패시킨다. 불안정 저장소·자체 패치로 OpenSSL을 교체하지 않는다. CI와 실제 게시할 이미지에서 이 버전을 다시 확인하고, 새 digest의 실제 ECR 스캔으로 수정 여부를 검증한다. 이 최소 버전 조건만으로 나머지 OpenSSL·dpkg finding이나 이미지 전체를 합격 처리하지 않는다. 상세 항목은 [이슈 #247](https://github.com/davidots429/foodtech/issues/247)을 따른다.

`publish-maintenance-image.yml`은 `workflow_dispatch` 전용이며 기본 operation은 `inspect`다. 저장소·불변 저장소/소유자 ID·main ref·실행 SHA·수동 이벤트를 확인한 뒤 GitHub가 전달한 토큰에서 필요한 공개 claim만 추출한다. JWT 원문·요청 토큰·AWS 자격증명은 출력하거나 artifact에 넣지 않는다. 이 점검은 서명 검증을 대신하지 않으며 실제 역할 사용 시 AWS STS가 서명·issuer·audience·subject를 검증한다. 임의 JWT를 인증 근거로 받아들이는 애플리케이션 경로가 아니다.

먼저 main에서 `operation=inspect`를 명시 실행하고 `maintenance-oidc-<run>-<attempt>` artifact의 subject를 확인한다. 새 저장소는 [GitHub 문서](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)에 따라 불변 ID가 포함된 subject를 사용할 수 있다. 옛 형식을 추측해 신뢰 정책에 넣지 않는다.

`provisionMaintenancePublishing=true`는 별도 `FoodtechMaintenancePublishing-dev`에 ECR 게시 역할만 정의한다. 기본값 없는 `VerifiedGitHubSubject` 파라미터는 이 프로젝트의 main subject 두 형식 중 실제로 관찰한 정확한 값이어야 한다. 신뢰는 GitHub OIDC provider, `aud=sts.amazonaws.com`, 정확한 subject로 제한한다. AWS가 지원하지 않는 사용자 정의 claim에 workflow 파일 제한을 걸었다고 주장하지 않는다. 같은 조건을 만족하는 신뢰된 main의 다른 workflow도 역할을 사용할 수 있다.

기존 GitHub OIDC provider가 있으면 URL·client ID·계정과 기존 사용자를 확인한 후 `GitHubOidcProviderArn`으로 결합한다. 없을 때만 `createGitHubOidcProvider=true`를 명시해 네이티브 IAM OIDC provider를 생성한다. 새 provider는 다른 역할이 공유할 수 있어 RETAIN이며 별도 정리해야 한다. 기존 provider를 수정하거나 중복 생성하지 않는다. 기본 합성과 provider 생성 모드는 모두 로컬에서 검증했다.

게시 역할은 `foodtech/maintenance-dev` 저장소의 업로드·필요한 조회와 ECR 인증 토큰만 허용한다. [ECR push 권한](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-push-iam.html)에 기록된 작업에 digest 확인용 DescribeImages를 더했으며 리전은 ap-southeast-2다. CloudFormation·IAM 변경·PassRole·ECS 실행·DynamoDB·KMS 권한은 부여하지 않는다. 이미지 게시 역할과 배포·Task/실행 역할을 분리한다.

실제 역할·저장소를 검토·생성한 뒤 저장소 변수 `FOODTECH_AWS_ACCOUNT_ID`, `FOODTECH_MAINTENANCE_PUBLISH_ROLE_ARN`을 설정한다. 장기 AWS 키를 GitHub Secrets에 저장하지 않는다. `operation=publish`는 SHA로 고정한 main 소스의 유지보수 Dockerfile을 빌드하고, 그 뒤에만 900초 OIDC 세션을 받아 게시한다. 태그는 source SHA·run·attempt로 구분해 불변 태그의 덮어쓰기를 피한다. Docker 인증 파일은 runner 임시 경로에서 사용하고 단계 종료 때 정리하며 AWS 세션은 action의 종료 정리를 사용한다.

게시 후 ECR에서 조회한 digest·source SHA·run·attempt·플랫폼을 `maintenance-image-<run>-<attempt>` artifact에 기록한다. 실행에는 태그 대신 이 manifest의 `image_uri`(`@sha256`)를 사용한다. 재시도마다 다른 빌드가 생길 수 있고 base 이미지 태그는 변할 수 있으므로 같은 source SHA의 모든 빌드가 같은 digest라고 가정하지 않는다. Push 응답 유실·실패 때는 성공 manifest를 만들지 않으며 기존 태그를 임의 삭제하거나 덮어쓰지 않는다. 기본 스캔 설정이나 digest 기록만으로 이미지 보안 검토·실제 Task 실행 완료를 표시하지 않는다.

```sh
# infra/cdk에서 가상 계정으로 합성; 실제 계정 생성/권한 적용이 아니다.
CDK_DEFAULT_ACCOUNT=123456789012 npx cdk --app 'node --import tsx bin/app.ts' synth FoodtechMaintenancePublishing-dev --strict --no-lookups -c provisionMaintenancePublishing=true --output /tmp/foodtech-maintenance-publish
python3 ../../tests/config/check_maintenance_publishing.py /tmp/foodtech-maintenance-publish/FoodtechMaintenancePublishing-dev.template.json
```

공개 claim·컨텍스트 불일치·만료·비밀값 출력 방지·고정 digest·계정 불일치 검사를 포함한 로컬 시험 9건과 두 provider 모드의 타입/합성 검사를 통과했다. 실제 OIDC 발급·역할 인수·ECR 게시·이미지 pull은 아직 수행하지 않았다. workflow·역할 코드를 병합하는 것은 실제 생성·권한 적용·게시 실행의 승인이 아니다.


### 조리·공정 쓰기의 초기화 계정 경계

`FOODTECH_PRINCIPAL_PROVISIONING_ENABLED=true`에서는 조리 저장소의 고객 쓰기에 정확한 epoch 일치를 요구하고, 배경 파생 쓰기와 표준화 Worker의 제육/튀김/재판정 쓰기에도 계정 항목과 유효한 epoch를 요구한다. 상태 항목 부재·잘못된 epoch·정지·삭제 잠금은 같은 DynamoDB 트랜잭션에서 거부한다. 기본 호환 모드는 유지한다.

`principalProvisioningEnabled` context는 AI 서비스와 식사 재집계 Task에 같은 값으로 전달한다. 별도로 시작하는 업무 API·표준화 Worker도 같은 환경변수로 시작해야 한다. 이는 환경변수 전파 계약이며 현재 배포나 기존 프로세스 종료를 수행하지 않는다. 초기화 제어 항목과 모든 작성자의 rollout/권한을 확인하기 전 삭제 조정기의 QUIESCENCE 차단을 해제하지 않는다.

`tests/live/principal_writer_fence_check.py --allow-synthetic-writes`는 foodtech/ap-southeast-2의 기존 개발 테이블에 고유 합성 키만 사용해 초기화·잠금·계정 제거·오래된 epoch 조건과 정리를 검증한다. `AWS_PROFILE=foodtech` 및 위 엄격 모드를 명시해야 한다. 개발자 자격증명에 의한 조건 검증이며 실제 배포 역할·legacy writer 중지·전체 삭제 증거가 아니다.


### 표준화 Worker의 수동 Fargate 실행 정의

`provisionStandardizationRuntime=true`와 `principalProvisioningEnabled=true`를 함께 지정하면 개발용 `FoodtechStandardizationRuntime-dev`에 Task 정의와 RETAIN 7일 로그(`/foodtech/dev/standardization`)만 등록한다. 새 IAM·네트워크·테이블·큐·Service·예약·RunTask는 만들지 않는다. `standardizationTaskRoleArn` context에는 실제 MinimalDataFlow 스택의 검토한 `StandardizationWorkerTaskRoleArn` 출력을 지정한다. 템플릿의 TaskRoleArn 매개변수는 이 정확한 ARN만 허용한다. ExecutionRoleArn은 별도로 검토·생성할 `foodtech-standardization-execution-dev`, ImageUri는 검토한 standardization workload manifest의 불변 digest여야 한다. Task 스택 자체는 실행 역할을 생성하지 않는다.

이미지 게시 workflow의 `workload=standardization`은 기존 main 전용 OIDC 게시 역할과 ECR 하나를 사용한다. 허용한 패키지/빌드 target만 선택하며 태그 접두사와 manifest workload를 유지보수 이미지와 구분한다. AWS 자격증명을 받기 전에 실제 이미지의 비활성 명령 가드와 libzstd 수정 버전을 검사한다. 기본 `maintenance` 이미지와 기존 태그 규약은 유지한다. 두 이미지는 같은 저장소에 있지만 Task의 업무 역할과 실행 명령은 분리한다.

새 명령 `python -m foodtech_standardization_worker.runtime`은 명시적 opt-in·엄격 초기화 모드·Fargate 환경·같은 프로젝트/단계의 테이블·큐·작업 역할을 요구한다. 실제 container-role provider와 STS 역할을 확인하고 그 Session 및 regional/endpoint 보호 SDK 설정을 모든 중첩 bridge에 전달한다. 큐 메시지는 최대 10개를 한 번 처리하고 전체 실행은 최대 600초로 제한한다. 성공 로그는 처리 건수만 남기며 0건 성공을 실제 데이터 흐름 검증으로 간주하지 않는다. 실패·시간 초과는 원천/SDK 오류를 출력하지 않고 비정상 종료한다. 부분 처리·SQS 재노출 시 기존 멱등성 기록을 재조회해야 한다.

합성과 이미지 CI는 실제 Worker 역할·SCP/RCP·KMS/Endpoint 정책이나 전체 수집→공정판정 검증이 아니다. 실제 실행 전 이미지 digest·역할 정책·프라이빗 연결·큐/fixture/기존 작성자 상태·비용/관찰/중단/정리 범위를 검토한다. 앞선 identity 읽기 시험 승인을 이 Task 실행 승인으로 확대하지 않는다.


`provisionStandardizationExecutionAccess=true`는 별도 `FoodtechStandardizationExecutionAccess-dev`에 실행 역할 1개와 inline 정책만 합성한다. [ECS 실행 역할 문서](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html)의 agent 계약을 기준으로, ECR 인증 토큰·해당 저장소 이미지 읽기와 `worker/worker/*` 전용 로그 스트림의 생성/기록만 허용한다. SDK 업무 소스에서 작업 역할 정책을 새로 생성하지 않으며 기존 데이터 처리 역할은 유지한다. 업무 테이블·큐·KMS·Secrets·Cognito·IAM/PassRole·Task 실행 권한은 없다. ECS 신뢰는 같은 계정/ap-southeast-2 SourceArn으로 제한한다. [클러스터 단위 SourceArn 미지원](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html) 때문에 특정 클러스터라고 표시하지 않는다. 실제 IAM 생성/적용은 별도 검토 대상이다.

기존 Worker의 배포된 inline 정책을 읽기 전용으로 점검한 결과, 현재 main CDK가 요구하는 원천 데이터 테이블 Query와 인증 테이블 ConditionCheckItem이 아직 배포되지 않았다. `tests/live/worker_role_iam_check.py`는 두 권한 및 인증 데이터 읽기/쓰기 거부를 포함한 최신 기대 범위를 검사한다. 실제 시험 전 MinimalDataFlow 변경 세트를 검토하고, 시뮬레이터만으로 실제 역할·조직 정책 통과를 선언하지 않는다. 이 PR은 기존 Worker 정책을 직접 수정하거나 스택을 배포하지 않는다.


기존 수집자원 없이 최초 core-only로 배포한 개발 스택의 Worker 정책만 보완할 때는 `collectionInfrastructureMode=deferred`를 사용할 수 있다. 기본값 create의 기존 수집 자원 구성은 유지하며 deferred는 수집 S3/수집 역할/프로토콜 Task를 합성하지 않는다. 이미 수집 자원이 관리되는 스택에는 사용하지 않는다. 적용 전 원격 Original 템플릿과 후보를 `infra/scripts/review_worker_policy_update.py --deployed-template <GetTemplate JSON> --candidate-template <synth JSON> --account-id <verified-account>`로 비교한다. 이 도구는 리소스 추가/삭제·신뢰/역할 연결·출력 변경을 거부하고 기존 Worker 정책의 원천 Query와 USER#*·반환값 NONE로 제한한 인증 ConditionCheckItem 추가만 수락한다. 현재 개발 스택과 후보 비교에서 이 정책 1개만 바뀌고 리소스 추가/삭제는 0개임을 확인했다. 이 로컬 대조는 실제 CloudFormation 변경 세트·배포·권한 검증을 대신하지 않는다.


인증 상태 조건 검사는 `USER#*` LeadingKeys와 필수 키 존재 조건을 사용하고 `dynamodb:ReturnValues=NONE`(IfExists)을 요구한다. 세션 키의 조건 검사와 `ALL_OLD` 실패 응답으로 인증 항목을 읽는 경로를 허용하지 않는다. [AWS의 ConditionCheck 실패 반환 제한 예시](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis-iam.html)를 따르며 기존 배포에는 인증 ConditionCheck 권한 자체가 없어 이번 후보는 기존 인증 권한을 제거하는 변경이 아니다. 실제 역할 검증에서는 조건 통과·실패 및 반환 금지를 함께 확인해야 한다.

수집기 REST·MQTT·OPC UA JSON/Binary Task는 모두 검토한 ECR digest를 요구한다. 태그 입력 `collectionImageTag`는 거부한다. 합성 성공은 해당 digest의 존재·스캔·실제 역할·격리 경로 실행 증거가 아니며 게시/배포는 별도로 검토한다.
## 수집기 이미지 수동 게시 준비

`provisionCollectionPublishing=true`로 선택하는 `FoodtechCollectionPublishing-dev`는 수집 전용 ECR `foodtech/collection-dev`와 main 전용 게시 역할 `foodtech-collection-publish-dev`의 후보다. 기존 유지보수 저장소·역할을 확장하지 않고, 검토한 기존 GitHub OIDC provider ARN과 실제 inspect 결과의 정확한 subject를 매개변수로 받는다. 기본 앱 실행에서는 선택되지 않는다. 새 저장소는 AES256·불변 태그·게시 시 스캔·Retain이며, `collectionImageRepositoryMode=existing`은 보존된 저장소를 참조만 한다. 기존 모드는 import·설정 변경·정책 변경을 하지 않으므로 실제 저장소 설정을 별도로 대조해야 한다.

게시 역할은 ECR 인증 토큰과 이 저장소 하나의 layer/manifest 게시·읽기 및 저장소/digest 메타데이터 확인만 허용한다. 토큰 발급만 Resource=*가 필요하며 모든 statement는 선택 리전을 제한한다. IAM·PassRole·배포·Task·업무 데이터·저장소 생성/삭제·태그 변경·pull-through cache 권한은 부여하지 않는다. Bash/AWS CLI 경로는 IAM Policy Autopilot 0.4.0의 SDK 소스 지원 목록에 없어 공식 서비스 권한 참조와 [단일 저장소 push 정책](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-push-iam.html)을 대조했다. `BatchGetImage`의 추가 권한 중 [pull-through cache](https://docs.aws.amazon.com/AmazonECR/latest/userguide/pull-through-cache-iam.html)용 생성/import·태그 경로는 이 일반 저장소 게시 기능에 포함하지 않는다.

수동 workflow `.github/workflows/publish-collection-image.yml`의 기본 동작은 inspect다. 이 동작은 공개 OIDC claim만 기록하고 AWS 역할을 인수하거나 이미지를 게시하지 않는다. 코드 병합 뒤 main에서 inspect하여 저장소 ID·소유자 ID·main·workflow·subject를 대조한다. 디코딩한 claim은 인증을 대신하지 않으며 실제 역할 인수에서는 AWS STS가 서명된 토큰을 검증한다. 역할의 IAM 신뢰는 지정 저장소 main에 한정하며 같은 main의 다른 workflow를 IAM 수준에서 구분한다고 주장하지 않는다.

실제 새 스택 적용·역할 검증을 별도 승인받은 뒤 `FOODTECH_COLLECTION_PUBLISH_ROLE_ARN` 및 기존 `FOODTECH_AWS_ACCOUNT_ID` 변수를 연결하고, 검토한 main 커밋의 publish를 명시적으로 실행한다. 빌드와 네트워크 차단 네 프로토콜 `--help`·libzstd 보안 하한 검사가 AWS 자격증명 획득보다 먼저다. 이후 실제 저장소의 ARN·불변 태그·AES256·게시 시 스캔을 확인한 뒤 Docker 로그인/게시하며, 커밋·run·attempt를 포함한 태그와 ECR digest manifest를 남긴다. digest·source·플랫폼·ZIP 체크섬·실제 스캔을 대조하기 전에는 런타임 후보를 확정하지 않는다. 이미지 게시 성공이나 `--help` 통과는 실제 AWS 수집·권한·프로토콜 통신·취약점 비영향을 증명하지 않는다.

수집기 이미지는 Debian 13 trixie 기반 builder/runtime을 공유하고 stable libzstd 보안 하한을 검사한다. 이 하한이 모든 패키지 취약점 수정을 보장하지는 않는다. 실제 배포에서는 `collectionImageRepository=foodtech/collection-dev`처럼 저장소 이름을 전달한다. 레지스트리 호스트가 포함된 URI가 아니며 CDK가 같은 계정·리전의 URI를 구성한다. `collectionImageDigest`에는 검토한 digest를 전달한다. 게시 스택에는 Task·네트워크·스케줄이 없고 수집 실행 권한·기존 Guard 지적 및 이슈 #236의 실제 격리 시험은 별도다.


## C# Mock 이미지의 수동 게시

Mock은 테스트·시연용 별도 .NET 10 CLI이며 고객/관리자 서비스나 수집기 이미지에 포함하지 않는다. `provisionMockPublishing=true`로 선택하는 `FoodtechMockPublishing-dev`는 별도 ECR `foodtech/mock-dev`와 main 전용 게시 역할 `foodtech-mock-publish-dev`만 준비한다. 기존 GitHub OIDC provider와 실제 main inspect subject를 기본값 없는 매개변수로 받으며 다른 게시 역할을 확장하지 않는다. `mockImageRepositoryMode=create`는 AES256·불변 태그·게시 시 스캔·Retain 저장소를 생성한다. `existing`은 그 이름의 저장소를 참조만 하고 소유권·설정·정책을 변경하지 않는다. 기본 앱 실행에서는 선택되지 않는다.

`publish-mock-image.yml`은 수동 실행 전용이고 기본 operation은 `inspect`다. main의 공개 OIDC claim 확인은 AWS 자격증명을 획득하거나 이미지를 빌드/게시하지 않는다. 게시 전 검토한 `FOODTECH_AWS_ACCOUNT_ID`와 `FOODTECH_MOCK_PUBLISH_ROLE_ARN` 변수를 연결해야 한다. `publish`는 신뢰한 main source를 linux/amd64로 빌드한 뒤 네트워크·쓰기·capability를 제한한 컨테이너에서 실제 C# fixture 해시와 OS를 검사한다. 그 후 단기 AWS 자격증명을 얻어 실제 저장소 ARN·URI·암호화·태그 불변성·스캔 설정을 검증하고 source/run/attempt 태그로 게시한다. manifest의 source/digest를 실제 ECR과 대조하고 새 이미지 스캔을 확인해야 한다.

.NET 10의 [기본 Linux 이미지는 Ubuntu 24.04이며 Debian 이미지는 제공되지 않는다](https://learn.microsoft.com/en-us/dotnet/core/compatibility/containers/10.0/default-images-use-ubuntu). 기존 Mock 이미지도 이 기본값을 사용해 왔으며 `10.0.402-noble` SDK와 `10.0-noble` 런타임으로 명시해 암묵적 OS 선택을 없앤다. 프로젝트 업무 서버의 Debian 13 기준을 Mock 이미지가 충족한다고 표현하지 않는다. 실제 Mock 서버 시험은 이 OS·스캔 결과·네트워크·인증서/비밀 주입을 포함해 별도로 검토한다. Debian 13이 필요한 배포에 이 이미지를 그대로 사용하지 않는다.

게시 역할은 지정 저장소 한 곳의 ECR 게시·메타데이터 확인과 토큰 발급만 허용하며 Task 실행·PassRole·네트워크·장비/업무 데이터 권한을 갖지 않는다. Bash/AWS CLI 게시 흐름의 API를 공식 ECR 단일 저장소 push 정책과 Service Authorization Reference에 대조하며 pull-through cache의 선택적 저장소 생성·import·태그 권한은 포함하지 않는다. 새 저장소/권한 적용·변수 연결·실제 게시에는 구체적인 별도 승인이 필요하다. fixture 해시 통과는 모든 Mock 프로토콜의 서버 호환성이나 실제 AWS 실행 검증이 아니다.

## Private REST Secret preparation

`provisionPrivateRestSecrets=true`는 dev·검토한 계정·ap-southeast-2에서만 `FoodtechPrivateRestSecrets-dev`의 별도 고객 관리 KMS 키 2개와 빈 Secret 2개를 정의한다. 이름은 `foodtech/dev/private-rest-mock`와 `foodtech/dev/private-rest-collector`다. [CloudFormation의 빈 Secret 계약](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-secretsmanager-secret.html)에 따라 SecretString·GenerateSecretString을 모두 생략하며 임의 비밀번호나 개인키를 템플릿에 생성하지 않는다. 키와 Secret은 Retain이고 Task·역할·네트워크·복제·예약은 만들지 않는다. 적용에는 별도 승인이 필요하며 KMS/Secret 보관·요청 비용을 검토해야 한다.

키는 단일 리전·대칭 암호화·회전 활성화로 정의한다. 계정의 관리 권한 위임과 데이터 암호 연산을 분리한다. Decrypt/GenerateDataKey는 같은 계정·Secrets Manager ViaService·각 Secret 이름의 ARN namespace 조건으로 제한하며 두 namespace는 겹치지 않는다. 실제 ARN의 자동 접미사는 생성 후에 정해지므로 키 정책의 접미사는 `*`이고, PR265의 실행 역할은 생성 후 확인한 **정확한 Secret ARN·키 ARN·SecretARN 암호화 문맥**으로 다시 제한한다. ARN namespace만으로 정확한 객체 한 개에 제한됐다고 표현하지 않는다. 다른 이름/계정·직접 KMS 호출은 이 데이터 연산 조건을 충족하지 못하며 애플리케이션에는 키 관리 권한을 주지 않는다.

`infra/scripts/private_rest_material.py`는 합성 TLS/OAuth 자료를 메모리에서만 생성한다. 3시간 유효 CA·서버 인증서는 사설 시험 서브넷의 사용 가능한 27개 IPv4만 SAN에 넣으며 wildcard DNS나 공인 IP를 넣지 않는다. CA와 서버의 RSA 키는 분리하고 CA 개인키는 직렬화하지 않는다. Mock에는 공개 CA·서버 인증서/키·OAuth 쌍, 수집기에는 공개 CA·같은 OAuth 쌍만 제공한다. 공개 CA를 Mock 값에도 보관해 첫 업로드 뒤 중단되어도 CA 개인키를 직렬화하거나 서버 값을 다시 만들지 않고 수집기 값을 복원한다. fixture seed를 보안 키 생성에 쓰지 않으며 기본 repr과 metadata는 값 대신 만료·공개 인증서 지문만 포함한다. Python 메모리 복사본의 완전한 소거를 보장하지 않는다. 이는 2시간 한정 시험의 준비안이며 운영 인증서 수명·회전 정책을 확정한 것이 아니다.

생성기는 AWS 업로드나 파일 출력을 수행하지 않는다. 별도 운영자 명령 `infra/scripts/private_rest_initialize.py`의 기본 모드는 메타데이터 조회만 수행한다. 명시한 요청 UUID, 검토한 템플릿 해시와 실제 Secret/키 ARN을 받고 `foodtech` login 프로파일·지정 계정의 AccountFullAccessRole·스택 소유권/출력·키 상태/정책·폐기 버전까지 확인한다. 템플릿 해시는 원본 파일 바이트가 아니라 `canonical_hash()`의 키 정렬·compact JSON·ASCII 직렬화 SHA-256이다. 정책 파일 `infra/containers/private-rest-key-policy.json`은 CDK와 실제 정책 검사에서 공유한다.

`--apply`는 승인 후 운영자가 선택하는 쓰기 모드이며 자체적으로 배포 승인을 뜻하지 않는다. 두 빈 Secret에 메모리에서 생성한 값을 같은 UUID의 VersionId로 순차 기록하고 두 값을 다시 읽어 일치를 검사한다. 두 Secret의 쓰기는 원자적이지 않다. 첫 업로드만 성공했으면 같은 UUID로 재실행할 때 해당 Mock 값을 검증·복원해 수집기만 기록하며, 완료된 같은 요청은 다시 쓰지 않는다. 다른 요청의 버전·역순 부분 상태·만료/변조 자료는 덮어쓰거나 자동 회전하지 않고 실패한다. 오류 출력은 단계·예외 종류·시도/확인 건수만 포함하고 값·SDK 오류 본문은 포함하지 않는다. POSIX 호스트에서 전체 실행 상한은 10~120초이며 실패 후 진행 건수를 보고 실제 버전을 조정 확인해야 한다. 중복 운영자 실행을 잠그는 명령은 아니므로 한 명의 승인된 운영자가 순차 실행한다.

생성된 민감 값을 도구 인수나 파일로 전달하지 않도록 이 명령만 로컬 SDK 프로세스 안에서 생성·업로드·검증을 연결한다. 일반 AWS 검토/적용에는 공식 AWS MCP를 우선한다. 메모리 복사본 완전 소거를 보장하지 않는다. Task 템플릿의 기본값 없는 `MaterialVersionId`에 같은 요청 UUID를 지정하면 모든 필드가 그 버전에 고정되며 AWSCURRENT로 암묵적으로 전환하지 않는다.

이 명령의 SDK Stubber/실제 인증서 검증 결과는 실제 서비스 검증이 아니다. Secret 자동 회전·추가 CloudTrail trail·알람도 이 빈 저장소 정의에는 포함하지 않으며 production Secret 관리 완료로 표시하지 않는다. Guard의 자동 회전 지적과 한정 시험의 갱신·감사·보관/정리 조건을 실제 적용 전에 검토하고 별도 실행 승인을 받아야 한다.

## Private REST trial preparation

`provisionPrivateRestTrial=true`는 `FoodtechPrivateRestTrial-dev`에 서로 독립적인 Mock·수집기 Fargate Task 정의와 역할 4개(Task/실행 역할 각각), 7일 로그 그룹 2개, 전용 격리 버킷을 준비한다. Task·서비스·예약을 실행하거나 네트워크를 생성하지 않는다. 기존 `foodtech-data-flow-dev`와 `foodtech-ordered-dev.fifo`는 참조만 한다. legacy 수집 인프라와 중복 생성하지 않도록 `provisionDataFlow=true`를 함께 선택하려면 `collectionInfrastructureMode=deferred`가 필요하다. 기본 앱에서는 선택되지 않는다.

Mock Task 역할에는 업무 AWS 권한이 없다. 수집기 역할은 지정 개발 표의 STREAM/MOCK/INGESTION_DLQ 키에 필요한 Get/Put/Update, STREAM 키에만 Query/Delete, 지정 FIFO에 SendMessage, 격리 버킷 ingestion/*에 TLS·AES256 PutObject만 가진다. 목록 조회·원본 다운로드·다른 표·Secret 조회·KMS 복호화 권한을 주지 않는다. SDK 소스의 IAM Autopilot 초안에서 선택한 기능에 필요한 권한만 제한했으며 복제·ObjectLock·ACL·다른 저장 암호화 등 선택하지 않은 API 변형의 권한을 포함하지 않는다. 수집기 코드의 트랜잭션은 구성 Put/Update 권한으로 평가된다.

각 실행 역할은 자신의 ECR 저장소 이미지 읽기·자신의 로그 stream 쓰기·자신의 Secret 읽기와 해당 Secret의 KMS 복호화만 가진다. `MockSecretArn`과 `CollectorSecretArn`은 같은 계정/리전의 별도 이름으로 제한하고 각각 `MockSecretKeyArn`/`CollectorSecretKeyArn`의 정확한 고객 관리 키를 받는다. 복호화는 [Secrets Manager의 ViaService와 SecretARN 문맥](https://docs.aws.amazon.com/secretsmanager/latest/userguide/security-encryption.html)으로 제한한다. Secret 필드 선택이 같은 Secret 내 다른 필드의 IAM 접근을 막지는 않으므로 서버 개인키와 수집기 CA를 별도 Secret에 둔다. 이 스택은 Secret/키를 생성하거나 값을 받지 않는다. 별도로 검토한 생성·암호화·갱신·감사·삭제 절차 및 실 ARN이 필요하며 실제 값은 template/인자/로그에 넣지 않는다. ECS 역할 신뢰는 [공식 지원 범위](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html)에 맞춰 동일 계정·리전의 ECS SourceArn으로 제한한다.

`MockImageUri`·`CollectorImageUri`는 각각 지정 개발 저장소의 sha256 digest URI를 명시하며 기본값이 없다. 기존 게시 이미지는 새 주입 인증서/실행 명령을 포함하지 않으므로 그대로 시험하지 않는다. Mock은 잘못된 UTF-8 한 건을 제공하고 최대 600초 뒤 종료한다. 배포 Task와 실제 로컬 HTTPS 시험은 `infra/containers/private-rest-trial-command.json`을 공유한다. 명령은 오류 재현 옵션과 명시적 0.0.0.0:8443 listener를 포함하며 접근 범위는 사설 ENI/SG로 제한한다. 루프백 기본 listener로는 별도 수집기 Task에서 연결할 수 없다. 수집기는 명시적 trial UUID·시도 구분·사설 Mock origin을 RunTask override로 받아 최대 60초 실행한다. Task 정의에 가짜 기본 주소나 자동 시도를 넣지 않는다. 실제 IP SAN을 가진 짧은 수명 인증서와 동일 OAuth 정보를 두 Secret에 주입한 뒤 Fargate 플랫폼 1.4.0 이상으로 실행해야 한다.

`QuarantineWritesEnabled` 기본값 `false`는 수집기 역할의 격리 PutObject를 명시적으로 거부한다. 이 단계에서 실제 S3 AccessDenied와 커서/manifest 미생성을 확인한다. 별도 검토한 변경 세트로 `true`로 바꾸면 이 시험용 Deny 한 건만 제거되며 영구 TLS 거부·IAM 범위·공개 차단·AES256은 유지된다. 동일 trial UUID로 재시도한 뒤 원본 바이트·암호화·manifest·커서를 대조하고 소유한 합성 자료만 정리한다. 버킷은 Retain·ACL 비활성·비버전 관리이며 자동 삭제 custom resource는 없다. 이 오류 한 건 시험으로 정상 SQS 송신이나 모든 프로토콜의 실행 성공을 주장하지 않는다.

네트워크는 별도 `FoodtechRuntimeFoundation-dev`의 `runtimeCollectionMode=private-rest-trial` 옵션이다. 기존 유지보수 SG는 그대로 두고 수집기/Mock SG를 추가한다. 수집기→Mock TCP8443과 ECS/AWS API용 TCP443 송신을 허용하며 공용 인바운드는 없다. 사설 Task의 `assignPublicIp=DISABLED`를 실행 시 확인한다. S3 Gateway Endpoint에 정확한 수집기 역할·전용 버킷 ingestion/*·같은 ResourceAccount·AES256 PutObject만 추가한다. AWS 소유 ECR layer 다운로드 statement는 그대로 둔다. 옵션을 생략하면 기존 네트워크와 endpoint 정책을 유지한다.

두 Task는 UID 10001·읽기 전용 root filesystem·capability ALL 제거로 정의한다. 수집기 CA만 이미지 VOLUME과 일치하는 `/run/foodtech-collection` bind volume에 0600으로 생성한다. Mock 서버 개인키 파일은 만들지 않는다. root filesystem을 쓰기 가능하게 바꾸거나 TLS 검증을 끄는 방식으로 실행 실패를 우회하지 않는다. 실제 적용 전에는 템플릿·권한 분석·Guard 지적·새 이미지 스캔·Secret 준비·시간/비용/정리 범위를 검토하고 구체적인 실행 승인을 받아야 한다. 이 구성의 생성이나 로컬 검사를 실제 Task 검증 완료로 취급하지 않는다.


### Meal refresh IAM preparation

`-c provisionMealRefreshAccess=true` opts into `FoodtechMealRefreshAccess-dev`.
An explicit account and `ap-southeast-2` are required. This creates only three
role definitions and their policies: business refresh, image/log execution,
and EventBridge invocation. `ClusterArn` must be supplied at deployment review;
the disabled schedule and invoke trust share the fixed
`foodtech-meal-refresh-dev` rule name to avoid a first-deployment cycle; no schedule, Task, table, index or network is created.

The source-derived baseline in `bin/meal-refresh-baseline.json` was generated
by IAM Policy Autopilot 0.4 from `meal_refresh_runtime.py`,
`combi_refresh_worker.py` and `cooking_store.py` at main `ddec408` (same source
as PR267 head `a34000e`). The generated Policy is extracted, its metadata Id
removed and account replaced with `${ACCOUNT}`. The baseline is evidence, not
an attached policy: the construct selects only verified actions and scopes
resources/conditions separately. The shared-store baseline includes replication
and KMS actions unrelated to this command; these are not granted.

```bash
DISABLE_IAM_POLICY_AUTOPILOT_TELEMETRY=true uvx iam-policy-autopilot@latest generate-policies \
  "$PWD/apps/business-api/src/foodtech_business_api/meal_refresh_runtime.py" \
  "$PWD/apps/business-api/src/foodtech_business_api/combi_refresh_worker.py" \
  "$PWD/apps/business-api/src/foodtech_business_api/cooking_store.py" \
  --region ap-southeast-2 --account <reviewed-account> --service-hints dynamodb sts --pretty
```

Run that command from the repository root. The task reads/checks only `USER#`
authentication records. Cooking writes require `TransactWriteItems` and selected
meal/aggregate partitions; there is no auth mutation, credential access or
unrelated cooking-job write. IAM partition-key conditions do not enforce a
specific sort key inside an allowed meal partition. DIRTY-only cursor mutations
and version/epoch checks remain runtime responsibilities.

The execution role can pull only the existing maintenance repository and write
its own log streams. Invocation is limited to one fixed rule and supplied cluster, the
meal-refresh task family and passing the two named ECS roles. Do not enable
managed Task tags or add target overrides without reviewing any new permissions.
Actual IAM deployment, pending-index/backfill, coordinated strict-mode writer
rollout, deletion/restore safeguards, failure notifications and schedule
activation remain separate gates. Source checks and policy simulation alone
are not proof of actual role/service access.

Guard currently reports fixed resource names (required by the runtime role contract)
and wildcard-resource usage (STS identity observation and ECR authorization token).
The raw findings remain visible; no previous deployment exception is reused.
Review these findings and effective permissions before any actual application.

### Manual meal refresh task registration

`provisionMealRefreshRuntime=true` requires `principalProvisioningEnabled=true`,
the development stage and an explicit account. It registers one Fargate Task
definition and, by default, a retained seven-day `/foodtech/dev/meal-refresh` log
group. It creates no roles, network, service, schedule, table or index and does
not invoke `RunTask`. TaskRoleArn and ExecutionRoleArn accept only the separate
reviewed meal-refresh roles in that account. ImageUri accepts only a maintenance
repository digest; the caller must verify its manifest, source guards and scan.

The explicit command `python -m foodtech_business_api.meal_refresh_runtime`
uses strict principal checks, 25-item pages, at most 100 pages per shard and a
600-second pass deadline. External observation and stopping stalled Tasks remain
separate safeguards. Registration does not establish working service access,
index completeness, writer coordination or deletion safety.

The manual and disabled schedule stacks share the existing log path and Task
family. `mealRefreshLogGroupMode=create` preserves the schedule's existing
logical resource ID. After separately verifying an existing group's retention,
configuration and ownership, use `mealRefreshLogGroupMode=existing` to reference
it. Selecting both stacks in create mode is rejected. Existing mode performs no
existence check or CloudFormation import. Each stack owns its own Task revision;
when moving to scheduling, review and pin the intended revision explicitly.

### CDK가 소유하는 권고 브로커 역할

`FoodtechRecipeSourceAccess-dev`는 기본적으로 검토된 기존 `BrokerTaskRoleArn`을 요구합니다. 아직 전용 역할이 없다면 `-c provisionRecipeSourceAccess=true -c recipeBrokerRoleMode=create`로 `foodtech-recipe-broker-dev`를 함께 정의할 수 있습니다. 생성 모드에는 외부 BrokerTaskRoleArn 매개변수가 없으며 GatewayTaskRoleArn은 계속 명시해야 합니다. 잘못된 모드는 합성 단계에서 거부합니다.

생성한 역할은 같은 프로젝트·리전의 ECS Task만 신뢰하고 Gateway와 분리합니다. 읽기 역할과 정책이 브로커 역할보다 먼저 생성되지 않도록 의존성을 둡니다. 기존 원천 읽기 역할의 범위와 호출별 정확한 키의 session policy, Gateway의 AssumeRole 거부·TagSession 금지는 유지합니다. 새 역할에 직접 원천 조회·쓰기·KMS·Cognito·이미지·로그 권한을 추가하지 않습니다.

이 옵션은 브로커의 실행 신원을 준비합니다. 고객 인증/세션·일반 업무 API 저장 권한·이력 정책·작성자 잠금·Task 정의·실제 로그인과 권고 종단간 연결은 별도 검토 대상입니다. 이 역할만으로 업무 API 전체를 배포할 수 있다고 간주하지 않습니다. 기존/생성 모드 전환은 역할의 소유권과 삭제 영향을 확인한 뒤 변경 세트로 검토하며, 같은 이름의 외부 역할을 자동으로 인수하지 않습니다. 실제 적용은 명시적 배포 승인을 거칩니다.

### Combi 원본 읽기 위임 권한 후보

`provisionCombiRecipeReadAccess=true`는 개발용 `FoodtechCombiRecipeReadAccess-dev` 정의만 opt-in 한다. 기존 broker/Gateway ARN은 서로 다른 같은 계정의 경로 없는 역할로 별도 전달한다. 새 source read 역할과 하나의 관리형 정책을 만들고, 같은 정책을 양의 읽기 허용과 permissions boundary로 사용한다. 기존 broker에는 별도 AssumeRole·보호된 이력 조회 정책, Gateway에는 실패 반환 NONE인 조건 검사 정책을 추가하는 후보이다. IAM 자원은 RETAIN이다.

Autopilot의 원본/발급기·이력/조건 기준을 `combi-read-baseline.json`으로 분리하고 액션을 좁힌다. 원본 역할은 조리 표의 Combi UUID 배치/원본 파티션만 읽으며 auth·전체 목록·LIVE·전역 Jeyuk·다른 표·write·KMS/복제 권한은 없다. 역할의 기본 사용자 범위는 개별 고객 위임이 아니며 발급기는 매번 네 개의 정확한 키로 세션 정책을 전달해야 한다. broker의 이력 조회와 Gateway의 ConditionCheck는 source client로 합치지 않는다.

로컬 타입/합성·cfn-lint·정의/7개 안전조건 훼손 검사를 제공한다. Guard의 고정 역할명 지적은 검토 대상으로 남기며, 이 문서는 해당 예외를 승인하거나 실제 계정 정책을 증명하지 않는다. 기본 app 실행은 이 스택을 생성하지 않는다. 실제 적용 전 원격 동결·서비스/정책 검사·기존 역할의 모든 정책/신뢰/경계와 교집합을 검토하고 명시적 승인을 받는다. Task·네트워크·예약·보호된 coverage 발급·고객 실행/모델 호출은 활성화하지 않는다.

### Combi 실행 메타데이터 조회 권한 후보

`provisionCombiExecutionReadAccess=true`는 별도 개발용 `FoodtechCombiExecutionReadAccess-dev`에 기존 broker의 추가 inline 정책 하나만 정의한다. `BrokerTaskRoleArn`은 같은 계정의 `foodtech-recipe-broker-dev`만 허용하고 기본값이 없다. 역할·신뢰·기존 정책·테이블·키·Task·네트워크를 만들거나 변경하지 않는다. 이력/원본 역할이나 Gateway/BFF 권한과 합치지 않으며 기본 비활성이다. 실제 적용은 별도 검토/승인 대상이다.

`DynamoCombiExecutionReader`의 실제 SDK 코드를 Autopilot으로 분석한 기준에서 `GetItem`만 선택한다. 복제·KMS·Query/Scan/BatchGet/transaction/write 권한은 부여하지 않는다. 단일 환경의 AI 실행 표, UUID 모양 `EXEC#` 파티션 키, 명시적 메타데이터 16개 속성으로 제한한다. `ForAllValues`와 동일 키의 `Null=false`, `Select=SPECIFIC_ATTRIBUTES`로 projection 없는 전체 속성 조회를 허용하지 않는다. `EnclosingOperation`은 없어야 하므로 이 정책을 transaction 읽기로 재사용하지 않는다. 기준 해시와 변경 제약을 template metadata에 남기며 추가 정책은 RETAIN이다.

속성 allowlist는 Python 조회 코드와 검증기에서 대조한다. 식별자·상태·Combi 참조를 읽기 위한 범위이며 모델/자격증명/동의/결과/브라우저 세션 필드는 포함하지 않는다. IAM의 파티션 키 조건은 sort key나 소유자를 제한하지 않는다. 따라서 이 grant를 META 전용 또는 고객별 물리 격리로 표시하지 않는다. 애플리케이션은 실제 `META` 키·canonical UUID·owner/epoch/PENDING/버전/완전한 참조를 별도로 검증해야 한다. UUID 모양 IAM 패턴 자체가 UUID 검증은 아니다.

새 권한은 보호된 작성자/보존 이력 검증을 발급하지 않고 고객·broker·모델을 활성화하지 않는다. 실제 역할/리소스 정책·암호화 키·상위 정책과 서비스 호출은 별도로 확인하며 모의 정책 평가를 실제 실행 증거로 사용하지 않는다.

### Combi 실행 조회 probe 수동 Task 후보

`provisionCombiExecutionReadProbe=true`는 개발용 `FoodtechCombiExecutionReadProbe-dev`의 Task 정의만 opt-in 한다. 실행·서비스·예약·네트워크·IAM·새 로그 그룹을 만들지 않는다. 기존 broker task role과 maintenance image/log execution role, 검토한 불변 broker RoleId·새 UUIDv4·digest 고정 maintenance 이미지가 모두 필수 parameter이며 기본값이 없다. 이미지의 실제 main 소스·설치된 probe 명령·scan은 게시 후 따로 확인한다.

Task는 Fargate/awsvpc/x64 Linux, 0.25vCPU/512MiB, 비root·읽기 전용 파일 시스템·전체 capability 제거·privileged=false로 제한한다. 전용 probe 모듈을 entryPoint로 사용하고 포트·secret·volume·임의 command를 제공하지 않는다. 기존 execution role의 검토 대상인 `/foodtech/dev/meal-refresh` / `worker` 로그 경로만 사용한다. role permission은 이 정의로 생기지 않으며 실제 GetItem 후보 적용과 실서비스 접근 검증은 별도 단계다.

probe 키 부재 확인·최대 시도 횟수·Task 관찰/중지 한도·현재 이미지 finding·비용·네트워크/Task 정리를 구체화한 뒤 실제 실행 승인을 받아야 한다. CLI 자체는 180초 기한과 SDK 재시도 없는 허용2/거부6개의 유한 호출만 한다. 테스트 fixture나 모의 정책 결과를 실제 Task/서비스 접근 증거로 표시하지 않는다.

### 돈까스 권고의 원천 읽기 위임 권한 후보

`provisionTonkatsuRecipeAccess=true`로 개발용 `FoodtechTonkatsuRecipeAccess-dev`를 합성할 수 있다. 같은 계정에 있는 서로 다른 Gateway·업무 API 역할의 ARN을 전달한다. 스택은 브로커·위임 읽기 역할, 읽기 권한 경계, 각 역할에 부착하는 추가 정책을 정의한다. 리소스 적용에는 별도 검토가 필요하다.

위임 읽기는 합성 돈까스 원천 파티션과 UUID 모양의 조리작업 파티션만 허용한다. 브로커가 현재·직전 작업의 정확한 키를 세션 정책으로 좁히고, Gateway와 업무 API는 보호된 기록의 버전·계정 상태를 조건 검사한다. 이 추가 정책에 대한 합성 검사는 기존 정책과 결합한 실제 권한, 고객별 격리, 배포된 작업의 접근 성공 여부까지 확인하지 않는다.

### 돈까스 권고 브로커의 수동 Task 후보

`provisionTonkatsuBrokerTask=true`로 개발용 `FoodtechTonkatsuBrokerTask-dev`를 합성할 수 있다. 전용 브로커·실행 역할과 브로커 RoleId, ECR 이미지 digest, TLS 번들의 ARN과 불변 버전 및 사설 Gateway HTTPS 주소를 전달한다. 이미지와 TLS를 바꾸면 새 정의를 검토한다.

브로커는 비루트 사용자로 읽기 전용 루트에서 실행한다. 작업 종료 시 사라지는 별도 볼륨을 TLS 파일에 사용하며 전용 로그 그룹을 참조한다. 사설 서브넷과 보안 그룹의 인바운드·아웃바운드, 역할과 비밀 값, 이미지 게시, Task 실행과 시험 종료는 별도 준비가 필요하다.

`provisionTonkatsuBrokerExecution=true`로 실행 역할과 7일 보존 로그 그룹을 별도로 합성한다. 역할은 브로커 이미지 저장소와 자기 로그 스트림, 지정된 TLS 번들의 읽기 및 해당 번들에 대한 Secrets Manager 경유 복호화만 허용한다. 이 역할을 브로커의 업무 데이터 조회 역할과 분리한다.

`provisionTonkatsuBrokerTls=true`로 개발용 `FoodtechTonkatsuBrokerTls-dev`를 합성한다. 초기 값이 없는 브로커 전용 Secret과 자동 키 교체가 설정된 전용 KMS 키를 정의한다. 스택 종료 보호를 적용하고 두 리소스를 보존한다. KMS 암호 연산은 같은 계정과 리전의 Secrets Manager가 전용 Secret 이름의 암호화 문맥으로 호출할 때만 허용한다. 실행 역할은 실제 Secret ARN을 별도로 고정한다. 인증서와 키는 별도 검토 후 등록하고 Task에서 정확한 버전 ID를 지정한다.

`provisionTonkatsuPeerTls=true`로 개발용 `FoodtechTonkatsuPeerTls-dev`를 합성한다. BFF와 Gateway에 별도 빈 Secret과 전용 KMS 키를 하나씩 두며, 두 서비스의 Secret 이름과 암호화 문맥을 분리한다. 전체 ARN은 이후 각 서비스의 실행 역할을 검토할 때 사용한다.

시연용 TLS 번들은 `infra/scripts/tonkatsu_trial_tls_material.py`에서 만든다. 검토된 서로 다른 두 사설 DNS 이름에 서버 인증서를 묶고, BFF→브로커와 브로커→Gateway의 인증 기관 및 키를 분리한다. 2시간 시험과 준비 여유를 위해 3시간 동안 유효한 인증서를 만든다. 생성 함수는 값을 메모리로 반환한다. 실제 저장·버전 고정은 검토된 초기화 절차에서 진행한다.

`infra/scripts/tonkatsu_trial_tls_initialize.py --reviewed-plan <파일>`의 기본 모드는 메타데이터만 조회한다. 계획은 검토한 요청 UUID, 브로커와 Gateway 사설 DNS 이름, 보호된 두 스택의 canonical 템플릿 해시와 서비스별 Secret·KMS ARN으로 구성한다. 승인된 초기 등록에서 `--apply`를 지정하면 세 빈 저장소에 같은 요청 UUID로 기록하고, 세 버전을 정확히 다시 읽어 인증서 관계를 확인한 뒤 공개 pin과 버전 ID를 출력한다. 같은 요청의 세 버전이 이미 있으면 재검증하며, 일부만 있을 때는 등록을 중단한다. Task 정의는 확인된 버전 ID 세 개를 함께 고정한다.

### 돈까스 브로커 이미지 게시 후보

`provisionTonkatsuBrokerPublishing=true`로 개발용 `FoodtechTonkatsuBrokerPublishing-dev`를 합성한다. 기존 GitHub OIDC 공급자와 수동 inspect에서 관찰한 정확한 main 주체를 전달한다. 스택은 암호화된 불변 ECR 이미지 저장소 하나와 여기만 쓸 수 있는 게시 역할을 정의한다.

`Publish Tonkatsu broker image` 워크플로는 검토한 전체 커밋 SHA가 일치할 때만 게시 모드로 진행한다. AWS 자격증명을 얻기 전에 브로커 이미지와 TLS 볼륨을 확인하고, 실제 저장소 설정을 대조한 뒤 게시된 digest를 기록한다.

돈까스 전용 Gateway 이미지는 `provisionTonkatsuGatewayPublishing=true`로 합성하는 `FoodtechTonkatsuGatewayPublishing-dev`의 별도 ECR 및 게시 역할을 사용한다. `Publish Tonkatsu Gateway image` 워크플로의 기본 모드는 공개 OIDC 클레임 검사다. 실제 게시에는 검토한 전체 main SHA와 Gateway 역할, Gateway 저장소가 일치해야 한다.

`provisionTonkatsuGatewayExecution=true`는 Gateway의 전용 ECS 실행 역할과 7일 로그 그룹을 정의한다. Gateway ECR 이미지 가져오기, Gateway 컨테이너 로그 기록, Gateway TLS Secret의 조회와 해당 키의 문맥 제한 복호화만 허용한다. AI Gateway의 업무 역할과 이미지 게시 역할은 별도다.

`provisionTonkatsuBusinessExecution=true`는 고객 API의 전용 ECS 실행 역할과 7일 로그 그룹을 정의한다. 브로커 게시 이미지에서 고객 API 진입점을 함께 검증한다. 고객 API 실행 역할은 해당 이미지를 가져오고 고객 API 로그와 전용 TLS 번들에만 접근한다. 배포에서 각 컨테이너의 업무 역할은 구분한다.

`provisionTonkatsuGatewayTask=true`는 개발 환경의 Gateway 브로커 호출 리스너(8445)와 고객 기록 리스너(8447)의 Fargate Task 정의를 합성한다. 둘은 같은 Gateway 업무 역할과 고정 이미지 digest를 사용하고 TLS 파일 볼륨과 로그 스트림은 각각 갖는다. Gateway TLS의 고정 Secret 버전은 브로커·기록 TLS 필드와 별도 추론 클라이언트의 `inference_server_cert_pem`, `inference_client_cert_pem`, `inference_client_key_pem`을 포함해야 한다.

추론은 검토된 `*.internal:8448` 주소로만 연결한다. 추론 호스트는 Gateway 업무 역할과 분리한다. `tonkatsu-inference` 컨테이너 모드 또는 `foodtech_ai_gateway.serve_tonkatsu_inference` 모듈로 사설 mTLS 릴레이를 실행하고, 같은 호스트의 loopback에 승인된 `llama3.1:8b` Ollama를 실행한다. 릴레이는 전용 URI `urn:foodtech:service:tonkatsu-gateway-inference`의 정확한 클라이언트 인증서와 독립된 TLS 서버 인증서가 필요하다. AWS 모델 호스트·사설 네트워크·인증서 등록은 배포 시 별도로 검토한다.

`provisionTonkatsuInferenceTls=true`는 추론 호스트 전용 빈 TLS Secret과 KMS 키를 별도 스택에 정의한다. 초기화 계획에 `records_host`와 `inference_host`를 지정하면 기존 세 Secret과 추론 호스트 Secret의 동일 버전을 검증한다. Gateway에는 추론 클라이언트 키를, 추론 호스트에는 서버 키를 저장한다. 해당 계획은 네 개 Secret과 세 개 스택의 검토 SHA-256을 요구한다.

`provisionTonkatsuInferenceExecution=true`은 추론 태스크 전용 ECS 실행 역할과 7일 로그 그룹을 정의한다. 실행 역할은 지정한 릴레이·Ollama ECR 이미지, 두 전용 로그 스트림, 추론 TLS Secret과 해당 키에만 접근한다.

`provisionTonkatsuInferenceTask=true`는 4 vCPU·12 GiB의 개발용 CPU 시험 후보를 합성한다. 태스크 역할은 없으며 릴레이·Ollama 이미지는 각각 digest로 고정한다. 비루트 컨테이너별 전용 쓰기 볼륨을 사용하고, Ollama가 승인 모델을 인식하면 릴레이가 8448 포트를 연다. Ollama는 루프백에 바인딩한다. 실제 실행 전에 고정 이미지, 추론 TLS 버전, 사설 DNS와 Gateway 전용 인바운드 및 제한된 송신 경로를 검증한다.

`provisionTonkatsuOllamaPublishing=true`는 추론 태스크에서 참조할 모델 이미지 저장소와 해당 저장소에만 쓰는 게시 역할을 정의한다. 수동 워크플로의 기본값 `inspect`는 공개 OIDC 클레임만 검사한다. 검토 SHA를 고정한 `build`는 공개 레지스트리 모델을 크기와 SHA-256으로 검사해 amd64 이미지를 만들고 외부 네트워크를 끈 CPU 추론을 시험한다. `publish`는 같은 시험을 마친 뒤 대상 저장소 설정을 대조하고 이미지 digest를 기록한다.

`provisionTonkatsuPrivateNetwork=true`는 서비스 등록에 사용할 `tonkatsu.foodtech.internal` 사설 DNS와 개발용 네트워크를 합성한다. `AvailabilityZone`은 endpoint가 공통 지원하는 가용 영역을 지정하고 S3·DynamoDB prefix list ID는 Sydney의 AWS 관리 값을 전달한다. 기본 `isolated` 모드는 NAT 없이 지정한 AWS endpoint만 사용한다. `tonkatsuPrivateNetworkMode=managed-login`을 지정하면 Cognito OAuth를 사용하는 BFF 전용 NAT 서브넷과 세션 갱신용 KMS endpoint가 추가된다. BFF의 NAT 송신은 TCP 443이며 배포 시 별도 검토한다.

각 ECS 서비스에는 출력된 역할별 subnet, security group, Cloud Map registry를 연결하고 공인 IP 부여를 끈다. 승인된 사설 시험 클라이언트만 BFF 8080에 연결한다. mTLS 발급에 쓰는 브로커·Gateway 호출·고객 기록·추론 호스트는 이 스택의 DNS 출력으로 고정한다. 고객 기록용 `RecordsHost`는 같은 사설 호스팅 영역에서 Gateway의 Cloud Map 레코드를 가리킨다. Gateway 호출과 고객 기록 주소는 동일 태스크의 서로 다른 포트와 서버 인증서를 사용한다. Ollama 11434는 루프백에 유지된다. Cloud Map 등록과 ECS 태스크 실행은 후속 배포 단계다.

`provisionTonkatsuPrivateServices=true`는 검토한 네 태스크 리비전을 사설 네트워크의 역할별 서브넷·보안 그룹·Cloud Map 서비스에 연결한다. 스택의 기본 `DesiredCountPerService=0`에서 태스크는 정지 상태다. 승인된 시험에서 값 1로 전환하고 종료 시 0을 복원한다. 추론→Gateway→Broker→BFF 순서로 준비되며 서비스별 지정 서브넷 한 곳과 보안 그룹 한 개를 사용한다. 공인 IP와 ECS Exec을 비활성화한다.

리스너 준비 점검은 각 컨테이너 내부의 고정 포트를 확인한다. Ollama는 승인 모델 조회 점검을 먼저 수행한다. 포트 점검과 Cloud Map 등록은 mTLS 인증, 역할의 실제 서비스 접근, 고객 로그인과 종단간 응답에 대한 검증과 별개다.

사설 시연 스택과 서비스가 생성되어 있고 태스크 수가 0일 때 `infra/scripts/tonkatsu_trial_preflight.py --reviewed-plan <plan.json>`으로 배포 전 메타데이터를 읽어 대조한다. 계획에는 `network_mode`, 네 태스크·네트워크·서비스·세 TLS 스택의 canonical JSON SHA-256을 담은 `templates`, 브로커·Gateway·Ollama의 `image_digests`, 등록한 단일 `tls_version`을 넣는다. canonical 해시는 키를 정렬하고 공백을 제거한 ASCII JSON의 SHA-256이다. 도구는 `foodtech` 로그인 프로파일만 사용하고 결과에는 확인 단계와 수량을 기록한다.

사설 서브넷과 서비스별 인바운드, ECS 연결, 고정 이미지와 TLS 버전 메타데이터를 검증한다. 실제 실행 전에는 발급된 인증서의 검토 기록, 이미지 스캔 및 시험 비용·종료 계획을 별도로 확인한다.

`provisionTonkatsuPrivateProbe=true`는 BFF·Broker·Gateway의 사설 연결 확인용 단일 컨테이너 태스크 정의 세 개를 합성한다. 정의를 만든 뒤 실제 시험을 승인받으면 각 원래 송신자의 사설 서브넷과 보안 그룹을 지정해 공인 IP 없이 한 번씩 실행한다. 업무 IAM 역할과 인바운드 리스너는 없으며, 실행 역할은 기존 로그·이미지·TLS 저장소 경계를 사용한다. 각 태스크에는 발신 연결에 필요한 클라이언트 인증서·개인키와 상대 서버 공개 인증서만 주입한다.

프로브는 지정한 사설 호스트를 DNS로 해석하고 서버 핀과 mTLS를 확인한다. 업무 핸들러가 없는 경로에서 404를 받으면 해당 연결을 기록하고 바로 종료한다. 이 증거의 범위는 네 연결의 DNS/TLS이며 업무 권한·데이터 처리·모델 응답은 별도 종단간 시험에서 확인한다.
