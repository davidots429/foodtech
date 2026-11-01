# Business API 인증과 첫 조리 업무 흐름

고객 공개 가입과 로그인은 Cognito 관리형 화면에서 시작한다. Business API가 Authorization Code + PKCE의 state·verifier를 관리하고 코드 교환, ID·Access 토큰의 서명·발급자·client·용도를 확인한다. 브라우저에는 HttpOnly 세션 쿠키만 전달한다. 관리형 로그인 토큰에 `acr`·`amr`가 제공되지 않는 시험 Pool에서는 관리자 화면이 별도 App Client의 Cognito `USER_PASSWORD_AUTH` → `SOFTWARE_TOKEN_MFA` 챌린지를 사용한다. 관리자 세션은 Cognito가 실제 TOTP 응답을 승인하고 `foodtech-admin` 그룹과 현재 계정 상태를 확인한 경우에만 발급한다.

## 실행

1. 하나의 Cognito User Pool에 공개 가입·이메일 확인을 설정한다. 고객·관리자 App Client를 분리하고 `code` OAuth flow와 `openid email profile` scope를 허용한다. 고객 Client는 TOTP 직접 등록을 위해 `aws.cognito.signin.user.admin` scope도 허용한다. 관리자 Client에는 `ALLOW_USER_PASSWORD_AUTH`, `ALLOW_USER_SRP_AUTH`, `ALLOW_REFRESH_TOKEN_AUTH`를 설정한다. 서비스의 고객 화면만 공개 가입으로 연결한다. 공개 가입 사용자에게 관리자 그룹을 부여하지 않으며, 관리자 계정과 `foodtech-admin` 그룹은 별도로 부여한다.
2. 각 Client의 callback URL을 아래 설정과 정확히 일치시키고, sign-out URL에 해당 웹 URL을 등록한다. 관리형 로그인 도메인을 설정한다. 관리형 로그인 v2라면 Client별 브랜딩을 생성한다.
3. `apps/business-api/.env.example`을 로컬 `.env`로 복사해 값을 채우고 환경변수로 로드한다. 키·토큰은 넣지 않는다. 로컬 실행에서만 `FOODTECH_SECURE_COOKIES=false`를 사용한다. `FOODTECH_AUTH_TABLE`을 지정하면 DynamoDB 공유 저장소를 사용한다. 배포 환경에서는 이 값이 필수다. AWS 자격증명은 환경 변수나 IAM 실행 역할로 전달한다.
4. `uv sync --all-packages --locked` 후 `uv run --no-sync uvicorn foodtech_business_api.main:app --host 127.0.0.1 --port 8000 --no-access-log`을 실행한다. 콜백 URL의 일회용 인증 코드가 접근 로그에 남지 않도록 한다. 다른 터미널에서 `npm run dev -w @foodtech/customer-web`과 `npm run dev -w @foodtech/admin-web`을 실행한다.
5. 고객 `http://127.0.0.1:5173`, 관리자 `http://127.0.0.1:5174`에서 확인한다. 두 Vite 개발 서버는 `/api`를 로컬 Business API로 전달한다.

## 구현 경계

- 로그인 흐름과 세션은 DynamoDB 테이블이 지정되면 공유된다. 비어 있으면 로컬 개발용 메모리 저장소를 사용하며 재시작 시 로그아웃된다. DynamoDB TTL 삭제는 비동기이므로 API는 모든 읽기에서 만료 시각을 직접 검사한다.
- 세션은 최장 1시간이고 Access 토큰 만료를 넘지 않는다. 고객 TOTP 등록용 Access 토큰과 관리자 인증 챌린지 세션은 브라우저에 노출하지 않고 공유 저장소에 만료 시각과 함께 저장한다. 등록 키는 등록 요청의 응답으로만 전달하고 저장하지 않는다. 관리자 화면의 비밀번호와 TOTP 값은 Cognito 인증 요청에만 사용하고 로그나 저장소에 기록하지 않는다. 공개 배포에는 HTTPS가 필수다. 프로젝트 관리자의 `POST /api/admin/users/{subject}/revoke-admin`과 `POST /api/admin/users/{subject}/suspend`는 CSRF를 확인한 뒤 공유 저장소의 차단 상태와 권한 버전을 변경한다. 기존 세션은 다음 API 요청부터 거부되며 새 관리자 로그인 또는 이용 중단 계정의 로그인도 거부된다. 이 조치는 애플리케이션 접근을 차단하며 Cognito 그룹·계정 상태 자체를 변경하지 않는다.
- 보호 API 요청마다 Cognito의 현재 계정 활성 상태를 확인하고 관리자 요청에는 현재 그룹도 확인한다. Cognito 호출에 실패하면 접근을 허용하지 않는다. 이 방식은 요청마다 Cognito API 호출과 지연을 추가하므로 부하 시험에서 조정해야 한다. 토큰 갱신은 아직 구현되지 않았고, 실제 공개 서비스용 인증 완료로 취급하지 않는다.
- 무료 예시 API는 공용 합성 자료만 읽기 전용으로 반환한다. 가입 계정의 영속 저장, 매장별 작업 데이터, 유료 전환 신청·사업자 정보 입력, 무료 AI 모델 호출은 후속 기능이다.
- 2026-11-01 AWS Free Plan의 시험용 Cognito User Pool에서 고객 가입·이메일 확인·Authorization Code/PKCE 토큰 교환·고객 세션·관리자 그룹 거부·로그아웃 후 API 거부를 확인했다. Cognito 관리형 로그인 v2는 시험 계정에서 동작했다. 시험 리소스는 아직 CDK 관리 대상이 아니다.
- 같은 시험 계정으로 DynamoDB 공유 저장소를 연결해 다시 로그인했다. 세션 생성, API 프로세스 재시작 후 로그인 유지, 로그아웃 후 세션 제거를 실제 AWS 테이블에서 확인했다.
- 시험 Pool의 관리형 로그인은 TOTP를 완료해도 토큰에 `acr`·`amr`를 넣지 않았다. 관리자 App Client의 직접 인증 경로에서 Cognito `SOFTWARE_TOKEN_MFA` 챌린지 성공 후 관리자 화면 진입을 확인했다. 시험용 관리자 그룹을 즉시 회수하고 다음 화면 요청에서 접근이 거부되는 것도 확인했다. Pool MFA는 `OPTIONAL`을 유지한다.

## 돈까스 합성 조리작업 첫 흐름

`/api/customer/cooking-jobs`는 고객 세션의 `subject`에서 저장 범위를 만들고 상태 변경 요청마다 CSRF와 기대 `version`을 검사한다. 첫 범위는 합성 튀김기 `synthetic-fryer`의 돈까스 작업이다. 생성, 승인된 수동 상태 전이, 심부온도 안전 확인, 안전 이상·해제, 완료 품질평가, 평가 이력과 재조리 연결을 제공한다. 공정 실패 자동판정은 수집 데이터와 규칙 엔진을 연결한 뒤 추가한다. 고객이 `PROCESS_FAILURE_DETECTED`를 임의로 제출하는 API는 제공하지 않는다.

안전 확인은 계획 인분마다 독립 탐침의 0~60초 1Hz 합성 시계열 61개를 요구한다. 모든 유효 표본이 `75°C` 이상이면 `PASSED`, 유효 표본 하나라도 미달하면 `FAILED`, 시간축·필수값·교정 유효성이 부족하면 `INCOMPLETE`다. 이상 의심·확인 중·실패에서는 평가를 완료할 수 없다. 의심 해제나 새 안전 확인만으로 기존 합격 평가를 다시 사용 가능하게 만들지 않으며, 새 완료 평가가 필요하다. 작업 상태·안전 상태의 변경과 근거는 `EVENT#...` 이력에 현재 상태 변경과 함께 원자적으로 기록한다.

품질평가는 초기 공통 척도 `tonkatsu-common-v1`의 7개 항목 모두에 1~5점과 관찰 근거를 요구한다. `해당 없음`은 이 첫 척도에 없다. 종합점수는 합계/35×100이며 모든 항목이 3점 이상일 때 합격, 4점 이상일 때 목표 달성이다. 수정·재평가는 사유와 이전 버전을 기록하고 원본을 보존한다. 새 버전과 `ASSESSMENT#CURRENT` 포인터는 같은 DynamoDB 트랜잭션으로 기록하며 작업 `version` 충돌을 거부한다.

첫 합성 적재 fixture는 튀김기 기준 배치 `0.8 kg`이다. `0 < 적재율 ≤ 50%`는 `PARTIAL`, `50% < 적재율 ≤ 100%`는 `REFERENCE`다. 적재율은 실제 투입 중량/0.8 kg으로 서버가 검증하며 이는 실제 장비 사양이 아니다. 인분은 이 fixture에서 최대 4인분이다. [fixture](../../fixtures/tonkatsu-business-v1.json)에 경계 예시를 둔다.

`FOODTECH_COOKING_TABLE`이 비어 있으면 로컬 메모리 저장소를 사용한다. 공개 배포에서는 별도 DynamoDB 테이블이 필수다. CDK의 `-c provisionCookingJobs=true`로 개발 테이블을 정의하고 `foodtech-cooking-jobs-dev`를 `ap-southeast-2`에 배포했다. 로컬 API를 이 테이블에 연결하여 합성 작업의 소유자 분리, 안전 차단, 버전 이력, 재조리 연결과 동시 평가 제출의 단일 승자를 검증했다. DynamoDB 숫자형을 API의 정수형으로 정규화한다. 실사용 규모·성능과 배포된 API 실행 역할은 검증하지 않았다. 현재 고객 화면에는 이 API의 UI가 없다.

실저장 검증을 재실행하려면 `AWS_PROFILE=foodtech FOODTECH_COOKING_TABLE=foodtech-cooking-jobs-dev .venv/bin/python tests/live/cooking_dynamodb_check.py`를 저장소 루트에서 실행한다. 스크립트는 계정·테이블을 확인하고 합성 레코드만 생성·삭제한다. 실제 AWS 요청이 발생하므로 실행 전 비용과 계정 상태를 확인한다.
