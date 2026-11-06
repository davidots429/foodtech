# AWS CDK 개발 인프라

`-c provisionAiExecutionRegistry=true`는 별도 `FoodtechAiExecutionRegistry-dev` 스택을 합성한다. DynamoDB On-demand 테이블 `foodtech-ai-execution-registry-dev`는 실행·모델·자격증명 메타데이터·동의·월별 한도를 저장한다. `pk=EXEC#<실행 ID>, sk=META`로 Gateway가 실행 ID를 직접 조회한다. AWS 관리형 저장 암호화, 시점 복구와 `RETAIN` 제거 정책을 적용한다. 키 원문·암호문은 이 테이블에 저장하지 않는다. 이 스택과 실행 역할은 아직 배포되지 않았다.

`FoodtechAuthSessions-dev`는 만료 시각(`expires`)이 있는 인증 흐름·세션과 관리자 권한 회수 상태를 저장하는 DynamoDB 테이블을 만든다. 앱은 `FOODTECH_AUTH_TABLE=foodtech-auth-sessions-dev`로 연결한다. TTL은 물리 삭제용이며 애플리케이션이 만료를 직접 검사한다.

2026-11-01에 `foodtech` 프로파일의 `ap-southeast-2`에 `CDKToolkit`과 `FoodtechAuthSessions-dev`를 배포했다. 시험용 합성 식별자로 두 DynamoDB 저장소 인스턴스의 흐름 단일 소비·세션 공유·관리자 차단 상태 전파를 확인하고 시험 항목을 삭제했다. 이 스택은 Cognito User Pool을 만들지 않았다.

`-c provisionIdentity=true`를 주면 Cognito Essentials User Pool, 고객·관리자 App Client, 관리형 로그인 v2, 선택적 TOTP MFA, 관리자 그룹을 정의하는 `FoodtechAuthIdentity-dev`도 합성한다. 두 callback URL·두 웹 URL·고유 도메인 접두어를 context로 전달해야 한다. 현재 CLI로 만든 `foodtech-auth-poc-20261101` Pool은 이 스택의 소유 대상이 아니므로 `provisionIdentity` 없이 세션 스택만 배포한다. PoC Pool을 이 스택으로 자동 편입하거나 같은 이름으로 다시 만들지 않는다. 향후 별도 환경에 새 Pool을 배포할 때 이 정의를 사용하고, 기존 PoC는 사용자·연동 전환을 확인한 후 별도 정리한다. 시험용 Pool의 TOTP MFA도 `OPTIONAL`로 설정했다.

스택은 `ap-southeast-2`에 한정한다. 배포 전 `cdk synth --strict`와 `cdk diff`를 확인한다. AuthSessions 테이블과 User Pool의 제거 정책은 `RETAIN`이다. 현재 배포된 API 실행 역할은 없으며, 실제 API 배포 시 테이블의 `GetItem`·`PutItem`·`DeleteItem`·`UpdateItem`과 Pool의 `AdminGetUser`·`AdminListGroupsForUser`만 실행 역할에 부여한다. CI 검사에는 AWS 권한을 부여하지 않는다. 기본 CDK bootstrap의 CloudFormation 실행 정책은 `AdministratorAccess`로 생성되었으므로, 향후 GitHub OIDC 배포 역할을 연결하기 전에 신뢰 조건과 실행 권한을 별도로 제한한다.

`-c provisionDataFlow=true`로 `FoodtechMinimalDataFlow-dev`를 합성한다. 2026-11-01에 `foodtech` 프로파일의 `ap-southeast-2`에 배포했다. `foodtech-data-flow-dev` DynamoDB On-demand 테이블, `foodtech-ordered-dev.fifo`, `foodtech-processingdlq-dev.fifo`, `foodtech-standardizationdlq-dev.fifo`를 생성한다. 테이블은 AWS 관리형 저장 암호화와 `RETAIN` 제거 정책을 사용한다. 큐에는 TLS만 허용하고 content-based deduplication을 끈다. Ordered 큐의 처리 오류는 Processing DLQ로, 품질 격리는 Worker가 Standardization DLQ로 보낸다.

첫 합성 fixture 3건을 실제 테이블·큐에 넣어 순번 복원 `1, 2, 3`, 품질 `VALID` 2건·`QUARANTINED` 1건, `HARD_LIMIT_EXCEEDED` Finding 1건과 격리 큐 1건을 확인했다. 이는 단일 스트림의 최소 기능 증거이며 다중 Task 동시성·부하 기준 달성 증거가 아니다. 스택에는 상시 실행 컴퓨트가 없지만 테이블·큐는 계속 남아 저장·요청 사용량이 발생할 수 있다.

`-c provisionCookingJobs=true`는 조리작업·안전·평가 컬렉션용 별도 `FoodtechCookingJobs-dev` 스택과 `foodtech-cooking-jobs-dev` DynamoDB On-demand 테이블을 합성한다. `PK=SCOPE#USER#<서버 세션 subject>#JOB#<UUID>`에 `META`, `ASSESSMENT#REV#<12자리 버전>`, `ASSESSMENT#CURRENT`, `EVENT#JOB#...`, `EVENT#SAFETY#...`를 둔다. 작업 메타와 현재 포인터의 변경, 새 완료 평가 버전 및 상태 변경 이력은 조건부 트랜잭션으로 묶는다. 2026-11-02에 승인된 개발 스택을 `ap-southeast-2`에 생성했고 테이블이 `ACTIVE`임을 확인했다. 합성 레코드로 동시 평가 제출의 단일 성공·단일 충돌, 평가 버전과 포인터의 원자적 일치, 안전 차단·재평가·재조리 연결을 확인한 뒤 시험 레코드를 삭제했다. 테이블은 향후 개발을 위해 유지하며 저장·요청 사용량이 발생할 수 있다.

`FoodtechMinimalDataFlow-dev`에는 표준화 Worker용 ECS Task Role도 정의한다. 이 역할은 수집 테이블과 조리작업 테이블에 `GetItem`·`PutItem`·`ConditionCheckItem`, Ordered 큐에 `ReceiveMessage`·`DeleteMessage`, Standardization DLQ에 `SendMessage`만 허용한다. 신뢰 정책은 같은 계정·리전의 ECS Task로 제한한다. 역할 ARN은 `StandardizationWorkerTaskRoleArn` 출력으로 제공한다. 역할 생성만으로 Worker가 배포되거나 실행되지는 않으며, 후속 Task Definition의 `taskRole`에 연결해야 한다. 이 역할은 컨테이너 이미지 다운로드와 로그 전송에 쓰는 ECS Task Execution Role과 별개다. 현재 개발 스택에는 이 역할 변경이 배포되지 않았으므로 실제 역할 자격증명으로 실행한 검증은 아직 없다.

