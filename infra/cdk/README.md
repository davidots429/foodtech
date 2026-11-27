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

필수 CDK context: `credentialKeyPeriod`, `credentialHmacVersion`, `registrationImageRepository`, `registrationImageTag`, `gatewayImageRepository`, `gatewayImageTag`, `registrationTlsSecretArn`, `gatewayTlsSecretArn`. 이미지 태그는 `latest`를 허용하지 않는다. TLS Secret 두 개는 각각 JSON 키 `server_cert_pem`, `server_key_pem`, `business_ca_pem`을 가져야 한다. 인증서 개인키와 CA는 저장소와 Task Definition의 일반 환경변수에 넣지 않는다. ECS 실행 역할이 Secret 값을 주입하고 컨테이너 시작기가 권한 `0600`의 임시 파일로 작성한다. 이미지 빌드는 `infra/containers/ai-service.Dockerfile`에 `FOODTECH_PACKAGE=foodtech-credential-registration` 또는 `foodtech-ai-gateway`를 전달한다. CI는 두 이미지를 빌드만 하며 게시하지 않는다.

두 Task의 리스너는 컨테이너 안에서 `0.0.0.0`에 결합한다. 실제 실행 전에 업무 API가 두 서비스에만 접근할 수 있는 **사설 서브넷·보안 그룹**, 서비스 DNS와 일치하는 서버 인증서 SAN, 업무 API 전용 클라이언트 인증서를 마련해야 한다. 현재 Task Definition은 ECS Service, Cluster, 네트워크, 이미지 게시, TLS Secret 생성, 업무 API 주소 설정을 만들지 않는다. 따라서 합성 성공만으로 종단간 배포가 완료된 것은 아니다. Gateway의 Ollama 기본 주소도 같은 Task의 loopback이다. 서버 GPU·Ollama 배치가 결정되기 전에는 로컬 모델 호출을 배포 검증 결과로 간주하지 않는다.

현재 개발 계정에는 서비스용 ECR 저장소와 TLS Secret이 아직 없다. 실행 전에 이미지와 Secret을 준비하고 실제 ARN·태그로 합성해야 한다. Fargate 상시 실행은 요금이 발생하므로 Task Definition 합성과 서비스 배포를 분리한다.

`-c provisionCredentialVault=true -c credentialKeyPeriod=2026-11`은 `FoodtechCredentialVault-dev` 스택을 합성한다. 2026-11-07 개발 계정 `ap-southeast-2`에 Vault DynamoDB On-demand 테이블, PITR 7일, 저장용 대칭 KMS 키와 별도 등록·Gateway ECS Task 역할을 배포했다. KMS 키·테이블은 `RETAIN`이다. 등록 역할은 Vault `PutItem`·저장 키 `GenerateDataKey`, Gateway 역할은 Vault `GetItem`·저장 키 `Decrypt` 및 AI 실행 레지스트리 접근만 허용한다. KMS 역할 정책은 `purpose=EXTERNAL_AI_CREDENTIAL`, `environment=dev` context를 요구한다. KMS 키와 PITR은 리소스를 유지하는 동안 비용이 발생할 수 있다. 실제 서비스 Task Definition 연결은 후속이다.

같은 날 두 역할을 각각 임시 ECS Fargate Task의 `taskRoleArn`에 연결해 실제 AWS API를 시험했다. 등록 역할은 Vault `PutItem`·KMS `GenerateDataKey`가 성공하고 `GetItem`·`Decrypt`가 `AccessDeniedException`으로 거부됐다. Gateway 역할은 Vault `GetItem`·KMS `Decrypt`가 성공하고 `PutItem`·`GenerateDataKey`가 거부됐다. 합성 Vault 항목과 임시 클러스터·Task 정의·실행 역할·보안 그룹·로그 그룹은 시험 후 정리했다. IAM 시뮬레이터는 실제 Vault 읽기에 성공한 개발 역할까지 조직 정책 거부로 표시했으므로, 이 계정의 해당 시뮬레이션 결과를 실제 Task 요청 결과보다 우선하지 않는다. 이 시험은 역할의 AWS API 권한을 검증한 것이며 실제 서비스 배포·외부 공급자 호출 증거는 아니다.

자격증명 등록 경로를 위해 같은 개발 스택에 RSA-4096 전송 KMS 키와 HMAC-256 가명 KMS 키를 추가했다. 등록 역할에는 전송 키 `GetPublicKey`·`Decrypt(RSAES_OAEP_SHA_256)`, 가명 키 `GenerateMac`, AI 레지스트리 `PutItem`을 추가했다. 기존 Vault·저장 KMS 키·Gateway 역할은 교체하지 않았다. 두 새 키의 ARN은 스택 출력에 있으며 저장 키와 마찬가지로 `RETAIN`이다. 임시 Fargate Task에 등록 역할을 부여한 실제 요청에서 새 KMS 권한과 Vault·AI 레지스트리 원자적 기록이 성공했고 Vault `GetItem`은 거부됐다. 시험 데이터와 Task 리소스는 정리했다. 실제 서비스 Task 배포는 후속이다.

`-c provisionAiExecutionRegistry=true`는 별도 `FoodtechAiExecutionRegistry-dev` 스택을 합성한다. DynamoDB On-demand 테이블 `foodtech-ai-execution-registry-dev`는 실행·모델·자격증명 메타데이터·동의·월별 한도를 저장한다. `pk=EXEC#<실행 ID>, sk=META`로 Gateway가 실행 ID를 직접 조회한다. AWS 관리형 저장 암호화, 7일 시점 복구, 결과 항목의 `expires` TTL과 `RETAIN` 제거 정책을 적용한다. TTL은 비동기 물리 삭제이므로 90일 Hard Delete의 최종 보장은 별도 삭제 작업이 필요하다. 키 원문·암호문은 이 테이블에 저장하지 않는다. 2026-11-07에 개발 계정 `ap-southeast-2`에 이 스택을 배포했다. Gateway 실행 역할은 아직 배포되지 않았으며, 로컬 검증은 개발자 AWS 세션으로 수행했다. 2026-11-22 읽기 전용 점검에서 실제 PITR 기간이 기본값 35일인 것을 확인하여 코드에 7일을 명시했다. 준비한 CloudFormation 변경 세트는 이 속성만 수정하며 테이블 교체는 없다. 사용자 승인 후 해당 변경 세트 실행이 UPDATE_COMPLETE로 종료되었고 DescribeContinuousBackups에서 실제 RecoveryPeriodInDays=7을 확인했다. 이 확인은 해당 개발 테이블의 PITR 설정에 한정되며 전체 삭제·복원 후 재삭제 검증을 대신하지 않는다.

`FoodtechAuthSessions-dev`는 만료 시각(`expires`)이 있는 인증 흐름·세션과 관리자 권한 회수 상태를 저장하는 DynamoDB 테이블을 만든다. 앱은 `FOODTECH_AUTH_TABLE=foodtech-auth-sessions-dev`로 연결한다. TTL은 물리 삭제용이며 애플리케이션이 만료를 직접 검사한다.

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

DynamoDB Endpoint는 같은 계정의 명시된 프로젝트 테이블/인덱스와 항목 작업만 통과시킨다. 서비스별 역할·사용자 키 범위는 별도로 제한해야 한다. S3 Endpoint는 지역 ECR 이미지 계층 버킷의 GetObject만 허용하므로 애플리케이션 격리 버킷 읽기/쓰기를 제공하지 않는다. 수집기를 이 서브넷에 연결하려면 실제 격리 버킷과 권한을 검토해 Endpoint 정책을 확장해야 한다. Endpoint 정책은 역할 권한을 부여하지 않는다.

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

수집기 이미지는 Debian 13 trixie 기반 builder/runtime을 공유하고 stable libzstd 보안 하한을 검사한다. 이 하한이 모든 패키지 취약점 수정을 보장하지는 않는다. 실제 배포에서는 `collectionImageRepository`에 위 저장소 URI, `collectionImageDigest`에 검토한 digest를 전달한다. 게시 스택에는 Task·네트워크·스케줄이 없고 수집 실행 권한·기존 Guard 지적 및 이슈 #236의 실제 격리 시험은 별도다.


## C# Mock 이미지의 수동 게시

Mock은 테스트·시연용 별도 .NET 10 CLI이며 고객/관리자 서비스나 수집기 이미지에 포함하지 않는다. `provisionMockPublishing=true`로 선택하는 `FoodtechMockPublishing-dev`는 별도 ECR `foodtech/mock-dev`와 main 전용 게시 역할 `foodtech-mock-publish-dev`만 준비한다. 기존 GitHub OIDC provider와 실제 main inspect subject를 기본값 없는 매개변수로 받으며 다른 게시 역할을 확장하지 않는다. `mockImageRepositoryMode=create`는 AES256·불변 태그·게시 시 스캔·Retain 저장소를 생성한다. `existing`은 그 이름의 저장소를 참조만 하고 소유권·설정·정책을 변경하지 않는다. 기본 앱 실행에서는 선택되지 않는다.

`publish-mock-image.yml`은 수동 실행 전용이고 기본 operation은 `inspect`다. main의 공개 OIDC claim 확인은 AWS 자격증명을 획득하거나 이미지를 빌드/게시하지 않는다. 게시 전 검토한 `FOODTECH_AWS_ACCOUNT_ID`와 `FOODTECH_MOCK_PUBLISH_ROLE_ARN` 변수를 연결해야 한다. `publish`는 신뢰한 main source를 linux/amd64로 빌드한 뒤 네트워크·쓰기·capability를 제한한 컨테이너에서 실제 C# fixture 해시와 OS를 검사한다. 그 후 단기 AWS 자격증명을 얻어 실제 저장소 ARN·URI·암호화·태그 불변성·스캔 설정을 검증하고 source/run/attempt 태그로 게시한다. manifest의 source/digest를 실제 ECR과 대조하고 새 이미지 스캔을 확인해야 한다.

.NET 10의 [기본 Linux 이미지는 Ubuntu 24.04이며 Debian 이미지는 제공되지 않는다](https://learn.microsoft.com/en-us/dotnet/core/compatibility/containers/10.0/default-images-use-ubuntu). 기존 Mock 이미지도 이 기본값을 사용해 왔으며 `10.0.402-noble` SDK와 `10.0-noble` 런타임으로 명시해 암묵적 OS 선택을 없앤다. 프로젝트 업무 서버의 Debian 13 기준을 Mock 이미지가 충족한다고 표현하지 않는다. 실제 Mock 서버 시험은 이 OS·스캔 결과·네트워크·인증서/비밀 주입을 포함해 별도로 검토한다. Debian 13이 필요한 배포에 이 이미지를 그대로 사용하지 않는다.

게시 역할은 지정 저장소 한 곳의 ECR 게시·메타데이터 확인과 토큰 발급만 허용하며 Task 실행·PassRole·네트워크·장비/업무 데이터 권한을 갖지 않는다. Bash/AWS CLI 게시 흐름의 API를 공식 ECR 단일 저장소 push 정책과 Service Authorization Reference에 대조하며 pull-through cache의 선택적 저장소 생성·import·태그 권한은 포함하지 않는다. 새 저장소/권한 적용·변수 연결·실제 게시에는 구체적인 별도 승인이 필요하다. fixture 해시 통과는 모든 Mock 프로토콜의 서버 호환성이나 실제 AWS 실행 검증이 아니다.
