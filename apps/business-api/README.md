# Business API 인증과 첫 조리 업무 흐름

`POST /api/customer/assessment-aggregates`는 고객이 명시한 서로 다른 조리작업 1~20개와 평가 완료 기간·레시피 버전·조리량 구간을 입력받아 선택 표본의 기술 통계를 만든다. 현재 유효한 돈까스 평가만 평균 종합 조리품질 점수·7항목 평균·품질 합격률·목표 달성률에 포함한다. 작업 미완료, 안전 실패·평가 차단, 척도·기간·구간 불일치 등은 0점으로 처리하지 않고 작업별 제외 사유를 남긴다. 분모가 0이면 수치는 `null/미산출`이다. 결과에는 사용한 작업·안전·평가 상태와 버전, 기간·차원·표본·산식 버전을 기록하며 같은 선택에서 입력 상태가 바뀌면 새 불변 결과 ID를 만든다. `GET /api/customer/assessment-aggregates/{group_id}/results/{result_id}`로 과거 결과를 다시 읽고, 뒤에 `/status`를 붙이면 조회 시점의 작업·안전·현재 평가 입력과 비교한 `CURRENT/STALE` 및 변경 작업 ID를 확인한다. `POST .../{result_id}/refresh`는 고객 세션·CSRF를 확인한 뒤 저장된 선택 조건으로 현재 평가를 다시 계산한다. 입력이 같으면 기존 결과를 재사용하고 바뀌었으면 이전 결과를 보존한 채 새 결과 ID를 기록한다. 이 경로는 고객 선택 표본의 설명 통계이며 대표성이나 레시피 개선 확정을 판정하지 않는다. 최신성은 조회 시점의 스냅샷이고 자동 재계산은 아직 제공하지 않는다. 늦은 이벤트·재조리에 따른 파생 결과 자동 갱신과 실제 서비스 배포·부하 검증은 후속이다.

비교·집계의 실제 저장소 검증은 `AWS_PROFILE=foodtech FOODTECH_COOKING_TABLE=foodtech-cooking-jobs-dev .venv/bin/python tests/live/derived_results_dynamodb_check.py`로 실행한다. 기존 개발용 테이블에서 임의의 합성 고객 범위에 작업·평가·파생 결과를 만들고, 평가 변경 후 최신성·재계산·이력 보존·고객 분리와 트랜잭션 직전의 경쟁 쓰기 거부를 확인한다. 종료 시 해당 시험 범위의 항목을 삭제한다. 실제 AWS 요청 비용이 발생할 수 있다. 2026-11-14 이 시험에서 두 파생 결과의 `STALE` 판정과 새 버전, 과거 결과 보존, 다른 고객의 404, 두 경쟁 쓰기의 409 및 합성 파티션 3개의 정리 후 잔존 0건을 확인했다. 이는 기존 개발용 테이블의 합성 자료 검증이며 실제 서비스 배포·부하·자동 파생 결과 갱신의 증거는 아니다.

`POST /api/customer/cooking-jobs/{candidate_job_id}/comparisons`는 같은 고객의 이전 작업 ID를 받아 두 작업의 현재 유효한 완료 평가를 비교한다. 메뉴·평가척도·장비가 같고 조리량 구간이 같거나 인접한 현재 합성 프로파일에서만 항목별 점수 변화와 합계 변화, 품질 합격 상태를 계산한다. 합계가 2점 이상 오르면 개별 조리의 `개선 관찰`로 표시하지만 레시피 버전 전체의 개선 확정은 `미평가`로 유지한다. 비교에 사용한 작업·안전·평가 버전과 산식 버전을 포함한 불변 파생 기록은 후속 비교에서 덮어쓰지 않으며, `GET /api/customer/cooking-jobs/{candidate_job_id}/comparisons/{comparison_id}`로 다시 읽을 수 있다. 뒤에 `/status`를 붙이면 두 작업의 현재 유효성·버전과 비교해 `CURRENT/STALE` 및 변경 작업 ID를 반환한다. `POST .../{comparison_id}/refresh`는 고객 세션·CSRF를 확인하고 저장된 이전 작업 ID로 현재 평가를 다시 비교한다. 입력이 같으면 기존 기록을 재사용하고 바뀌었으면 이전 기록을 보존한 채 새 비교 ID를 만든다. 안전 차단 등으로 두 평가가 현재 유효하지 않으면 재비교를 거부한다. 최신성은 조회 시점의 스냅샷이고 중복 비교는 같은 기록을 반환하며, 저장 중 현재 평가·안전 상태가 변경되면 거부한다. 실제 서비스 배포·부하 검증은 아직 진행하지 않았다.

시연용 유료 티어 신청은 `POST /api/customer/tier-requests`와 `GET /api/customer/tier-requests/current`로 제공한다. 고객 ID는 세션에서만 가져오고, 신청에는 사업자명과 시험용 사업자등록번호를 받는다. 번호 전체는 저장하지 않고 끝 네 자리만 신청·검토 기록에 남긴다. 관리자는 별도 관리자 세션에서 `GET /api/admin/tier-requests/pending`으로 대기 목록을 보고 `POST /api/admin/tier-requests/{subject}/{application_id}/decision`으로 승인 또는 거절한다. 승인 상태만 고객 `/me`의 `DEMO_PAID` 티어로 반영한다. 쓰기는 CSRF와 조건부 트랜잭션을 사용하며, 실제 과금·결제 수단 수집·사업자 진위 조회는 없다. 유료 표시에는 `00원`과 시연 고지를 함께 둔다. 로컬은 메모리 저장소를 쓰고, 공유 저장소는 `FOODTECH_TIER_TABLE`로 지정한다. 현재 이 기능은 코드·CDK 합성까지만 검증했으며 AWS 테이블은 아직 배포하지 않았다.

고객의 `POST /api/customer/ai/chat`은 세션·CSRF를 검사하고 최대 최근 5개 메시지의 `user/assistant` 순서를 검증한다. 서버는 고객 ID를 세션에서 가져와 mTLS Gateway의 기존 로컬 `GENERAL_GUIDE` 실행을 호출한다. 대화 내용은 브라우저가 다음 질문의 맥락으로 보낼 때만 사용하고 업무 API는 대화 저장소를 만들지 않는다. Gateway는 실행 상태·결과와 월별 한도만 관리하며 프롬프트 원문을 레지스트리에 기록하지 않는다. Ollama와 모델·한도 레코드가 준비되지 않으면 답변을 생성할 수 없다.

고객의 자격증명 등록은 `GET /api/customer/credentials/transfer-key`와 `POST /api/customer/credentials`로 제공한다. 고객 세션과 현재 Cognito 접근을 확인하고 등록 시 CSRF를 검사한다. 브라우저는 Web Crypto RSA-OAEP-SHA-256으로 API 키 값만 암호화하고, 업무 API는 공급자·버전·암호문과 서버 세션의 고객 ID를 전용 mTLS 등록 서비스에 전달한다. 키 원문이나 저장용 복호화 권한은 업무 API에 없다. `FOODTECH_REGISTRATION_URL`·`FOODTECH_REGISTRATION_CA`와 업무 API 클라이언트 인증서가 필요하며, 등록 서비스가 없으면 실패를 반환한다. 외부 공급자 호출·동의·비용 예약은 이 등록 화면에서 실행하지 않는다.

고객의 합성 외부 AI 시험은 `/api/customer/ai/external/preview`에서 모델·자격증명 ID를 보내 견적을 받고, 표시된 외부 전송과 비용 가능성에 명시 동의한 경우에만 `/api/customer/ai/external/quotes/{id}/confirm`으로 실행한다. 두 요청 모두 고객 세션과 CSRF를 검사하고 tenant ID는 서버 세션에서만 가져온다. `/api/customer/ai/executions/{id}/status`는 확인 요청이 끊긴 경우 결과를 다시 읽는다. 업무 API는 Gateway에 고정된 합성 질문의 내용이나 저장된 API 키를 받지 않는다. Gateway의 외부 실행 설정이 꺼져 있으면 견적을 만들 수 없다.

`POST /api/customer/ai/guidance`는 고객 세션·CSRF를 확인한 뒤 업무 API의 클라이언트 인증서로 Gateway에 mTLS 연결한다. Gateway에서 실행 ID를 만들고 그 ID로 로컬 AI 실행을 요청한다. 브라우저는 Gateway URL·인증서·모델 ID·자격증명을 받지 않는다. 이 첫 흐름은 `GENERAL_GUIDE`와 로컬 `llama3.1:8b`에 한정되며 실제 과금은 없다. Gateway mTLS 설정이 없으면 503으로 거절한다.

고객 공개 가입과 로그인은 Cognito 관리형 화면에서 시작한다. Business API가 Authorization Code + PKCE의 state·verifier를 관리하고 코드 교환, ID·Access 토큰의 서명·발급자·client·용도를 확인한다. 브라우저에는 HttpOnly 세션 쿠키만 전달한다. 관리형 로그인 토큰에 `acr`·`amr`가 제공되지 않는 시험 Pool에서는 관리자 화면이 별도 App Client의 Cognito `USER_PASSWORD_AUTH` → `SOFTWARE_TOKEN_MFA` 챌린지를 사용한다. 관리자 세션은 Cognito가 실제 TOTP 응답을 승인하고 `foodtech-admin` 그룹과 현재 계정 상태를 확인한 경우에만 발급한다.

## 실행

1. 하나의 Cognito User Pool에 공개 가입·이메일 확인을 설정한다. 고객·관리자 App Client를 분리하고 `code` OAuth flow와 `openid email profile` scope를 허용한다. 고객 Client는 TOTP 직접 등록을 위해 `aws.cognito.signin.user.admin` scope도 허용한다. 관리자 Client에는 `ALLOW_USER_PASSWORD_AUTH`, `ALLOW_USER_SRP_AUTH`, `ALLOW_REFRESH_TOKEN_AUTH`를 설정한다. 서비스의 고객 화면만 공개 가입으로 연결한다. 공개 가입 사용자에게 관리자 그룹을 부여하지 않으며, 관리자 계정과 `foodtech-admin` 그룹은 별도로 부여한다.
2. 각 Client의 callback URL을 아래 설정과 정확히 일치시키고, sign-out URL에 해당 웹 URL을 등록한다. 관리형 로그인 도메인을 설정한다. 관리형 로그인 v2라면 Client별 브랜딩을 생성한다.
3. `apps/business-api/.env.example`을 로컬 `.env`로 복사해 값을 채우고 환경변수로 로드한다. 키·토큰은 넣지 않는다. 로컬 실행에서만 `FOODTECH_SECURE_COOKIES=false`를 사용한다. `FOODTECH_AUTH_TABLE`을 지정하면 DynamoDB 공유 저장소를 사용한다. 배포 환경에서는 이 값이 필수다. AWS 자격증명은 환경 변수나 IAM 실행 역할로 전달한다.
4. `uv sync --all-packages --locked` 후 `uv run --no-sync uvicorn foodtech_business_api.main:app --host 127.0.0.1 --port 8000 --no-access-log`을 실행한다. 콜백 URL의 일회용 인증 코드가 접근 로그에 남지 않도록 한다. 다른 터미널에서 `npm run dev -w @foodtech/customer-web`과 `npm run dev -w @foodtech/admin-web`을 실행한다.
5. 고객 `http://127.0.0.1:5173`, 관리자 `http://127.0.0.1:5174`에서 확인한다. 두 Vite 개발 서버는 `/api`를 로컬 Business API로 전달한다.

### 고객 외부 AI 흐름의 로컬 브라우저 검증

저장소 루트에서 `uv run --no-sync python tests/live/customer_external_browser_harness.py`를 실행하면 고객 웹, 업무 API, 자격증명 등록 서비스와 AI Gateway가 로컬 HTTP/mTLS 경로로 시작한다. 출력된 주소에서 합성 키 `synthetic-browser-key`를 등록하면 생성된 자격증명 ID가 외부 AI 시험 화면에 자동 입력된다. 이어서 견적 확인, 외부 전송 고지 동의, 실행 및 결과·정산을 확인한다. 종료는 `Ctrl+C`다. 브라우저 없이 같은 경로를 검사하려면 `--smoke-only`를 붙인다.

2026-11-08에 위 합성 경로의 HTTP 검사와 고객 화면 클릭 검증을 마쳤다. 등록·견적·동의·실행·정산이 완료됐고 mTLS 서비스 연결을 확인했다. 이 하네스의 고객 세션, 레지스트리, KMS·Vault와 외부 공급자는 명시적인 메모리 시험 대역이다. AWS API나 유료 공급자 API를 호출하지 않으며 실제 자격증명을 입력하지 않는다. 실제 Cognito 로그인과 AWS 저장소를 결합한 브라우저 검증, 서비스 Task 배포와 실제 공급자 응답은 별도 검증 대상이다.

같은 하네스에 `--aws --smoke-only`를 붙이면 기존 `foodtech` 개발용 DynamoDB·KMS에서 더미 키의 전송 복호화·저장 암호화·재조회와 견적·동의·예약·정산을 HTTP/mTLS 경로로 검증한다. 실행 전 두 개발용 테이블이 비어 있어야 하며, 종료 시 생성한 항목을 삭제한다. 2026-11-08 이 경로도 통과했고 종료 후 레지스트리와 Vault가 각각 0건임을 확인했다. 이 모드에서도 고객 인증과 외부 공급자 응답은 합성 대역이며 유료 공급자 API를 호출하지 않는다. AWS KMS·DynamoDB 요청 비용은 발생할 수 있다.

실제 Cognito 고객 세션까지 연결하려면 로컬 `.env`의 기존 개발용 Cognito callback·공유 인증 테이블 설정을 준비하고 `AWS_PROFILE=foodtech uv run --no-sync python tests/live/customer_external_cognito_harness.py`를 실행한다. 개발용 레지스트리·Vault가 비어 있어야 시작한다. 계정 소유자가 Cognito 화면에서 비밀번호와 필요한 TOTP를 직접 입력한 뒤 고객 화면에 합성 시험 키를 등록하고, 견적·동의·실행·상태를 확인한다. 종료 시 하네스가 생성한 레지스트리·Vault 항목을 삭제한다. 공급자 응답은 합성 대역이며 실제 유료 API 요청은 없다. Cognito·KMS·DynamoDB 사용에 따른 AWS 요청 비용은 발생할 수 있다.

2026-11-08 개발용 시험 계정의 실제 Cognito 비밀번호·TOTP 로그인, 공유 세션 저장소의 고객 화면 진입, 합성 키 등록, 고객 ID 자동 연결, 견적·동의·합성 실행을 브라우저에서 확인했다. 동일한 로그인 세션의 상태 재조회는 `SUCCEEDED / SETTLED`와 합성 응답·추정 정산액을 반환했다. 로그아웃 후 같은 상태 조회는 로그인 필요 오류로 거부됐다. 공급자 네트워크 호출은 합성 대역으로 유지했고 유료 API 요청은 없었다. 종료 후 개발용 AI 레지스트리·Vault·공유 인증 테이블은 각각 0건이었다. 실제 서비스 Task 배포·실제 공급자 호출과 서버 성능은 이 시험 범위에 포함하지 않는다.

## 구현 경계

- 로그인 흐름과 세션은 DynamoDB 테이블이 지정되면 공유된다. 비어 있으면 로컬 개발용 메모리 저장소를 사용하며 재시작 시 로그아웃된다. DynamoDB TTL 삭제는 비동기이므로 API는 모든 읽기에서 만료 시각을 직접 검사한다.
- 세션은 최장 1시간이고 Access 토큰 만료를 넘지 않는다. 고객 TOTP 등록용 Access 토큰과 관리자 인증 챌린지 세션은 브라우저에 노출하지 않고 공유 저장소에 만료 시각과 함께 저장한다. 등록 키는 등록 요청의 응답으로만 전달하고 저장하지 않는다. 관리자 화면의 비밀번호와 TOTP 값은 Cognito 인증 요청에만 사용하고 로그나 저장소에 기록하지 않는다. 공개 배포에는 HTTPS가 필수다. 프로젝트 관리자의 `POST /api/admin/users/{subject}/revoke-admin`과 `POST /api/admin/users/{subject}/suspend`는 CSRF를 확인한 뒤 공유 저장소의 차단 상태와 권한 버전을 변경한다. 기존 세션은 다음 API 요청부터 거부되며 새 관리자 로그인 또는 이용 중단 계정의 로그인도 거부된다. 이 조치는 애플리케이션 접근을 차단하며 Cognito 그룹·계정 상태 자체를 변경하지 않는다.
- 보호 API 요청마다 Cognito의 현재 계정 활성 상태를 확인하고 관리자 요청에는 현재 그룹도 확인한다. Cognito 호출에 실패하면 접근을 허용하지 않는다. 이 방식은 요청마다 Cognito API 호출과 지연을 추가하므로 부하 시험에서 조정해야 한다. 토큰 갱신은 아직 구현되지 않았고, 실제 공개 서비스용 인증 완료로 취급하지 않는다.
- 무료 예시 API는 공용 합성 자료만 읽기 전용으로 반환한다. 합성 조리작업·평가와 시연용 티어 전환 신청은 별도 고객 범위에 저장한다. 매장별 범위·권한 모델과 무료 AI 모델의 자동 제공은 후속 기능이다.
- 2026-11-01 AWS Free Plan의 시험용 Cognito User Pool에서 고객 가입·이메일 확인·Authorization Code/PKCE 토큰 교환·고객 세션·관리자 그룹 거부·로그아웃 후 API 거부를 확인했다. Cognito 관리형 로그인 v2는 시험 계정에서 동작했다. 시험 리소스는 아직 CDK 관리 대상이 아니다.
- 같은 시험 계정으로 DynamoDB 공유 저장소를 연결해 다시 로그인했다. 세션 생성, API 프로세스 재시작 후 로그인 유지, 로그아웃 후 세션 제거를 실제 AWS 테이블에서 확인했다.
- 시험 Pool의 관리형 로그인은 TOTP를 완료해도 토큰에 `acr`·`amr`를 넣지 않았다. 관리자 App Client의 직접 인증 경로에서 Cognito `SOFTWARE_TOKEN_MFA` 챌린지 성공 후 관리자 화면 진입을 확인했다. 시험용 관리자 그룹을 즉시 회수하고 다음 화면 요청에서 접근이 거부되는 것도 확인했다. Pool MFA는 `OPTIONAL`을 유지한다.

## 돈까스 합성 조리작업 첫 흐름

`/api/customer/cooking-jobs`는 고객 세션의 `subject`에서 저장 범위를 만들고 상태 변경 요청마다 CSRF와 기대 `version`을 검사한다. 첫 범위는 합성 튀김기 `synthetic-fryer`의 돈까스 작업이다. 생성, 승인된 수동 상태 전이, 심부온도 안전 확인, 안전 이상·해제, 완료 품질평가, 평가 이력과 재조리 연결을 제공한다. 연결된 합성 배치의 표준화된 온도값에 대해서는 자동 공정판정을 수행한다. 고객이 `PROCESS_FAILURE_DETECTED`를 임의로 제출하는 API는 제공하지 않는다.

안전 확인은 계획 인분마다 독립 탐침의 0~60초 1Hz 합성 시계열 61개를 요구한다. 모든 유효 표본이 `75°C` 이상이면 `PASSED`, 유효 표본 하나라도 미달하면 `FAILED`, 시간축·필수값·교정 유효성이 부족하면 `INCOMPLETE`다. 이상 의심·확인 중·실패에서는 평가를 완료할 수 없다. 의심 해제나 새 안전 확인만으로 기존 합격 평가를 다시 사용 가능하게 만들지 않으며, 새 완료 평가가 필요하다. 작업 상태·안전 상태의 변경과 근거는 `EVENT#...` 이력에 현재 상태 변경과 함께 원자적으로 기록한다.

품질평가는 초기 공통 척도 `tonkatsu-common-v1`의 7개 항목 모두에 1~5점과 관찰 근거를 요구한다. `해당 없음`은 이 첫 척도에 없다. 종합점수는 합계/35×100이며 모든 항목이 3점 이상일 때 합격, 4점 이상일 때 목표 달성이다. 수정·재평가는 사유와 이전 버전을 기록하고 원본을 보존한다. 새 버전과 `ASSESSMENT#CURRENT` 포인터는 같은 DynamoDB 트랜잭션으로 기록하며 작업 `version` 충돌을 거부한다.

첫 합성 적재 fixture는 튀김기 기준 배치 `0.8 kg`이다. `0 < 적재율 ≤ 50%`는 `PARTIAL`, `50% < 적재율 ≤ 100%`는 `REFERENCE`다. 적재율은 실제 투입 중량/0.8 kg으로 서버가 검증하며 이는 실제 장비 사양이 아니다. 인분은 이 fixture에서 최대 4인분이다. [fixture](../../fixtures/tonkatsu-business-v1.json)에 경계 예시를 둔다.

`FOODTECH_COOKING_TABLE`이 비어 있으면 로컬 메모리 저장소를 사용한다. 공개 배포에서는 별도 DynamoDB 테이블이 필수다. CDK의 `-c provisionCookingJobs=true`로 개발 테이블을 정의하고 `foodtech-cooking-jobs-dev`를 `ap-southeast-2`에 배포했다. 로컬 API를 이 테이블에 연결하여 합성 작업의 소유자 분리, 안전 차단, 버전 이력, 재조리 연결과 동시 평가 제출의 단일 승자를 검증했다. DynamoDB 숫자형을 API의 정수형으로 정규화한다. 실사용 규모·성능과 배포된 API 실행 역할은 검증하지 않았다. 고객 화면은 생성·상태·안전·평가·재조리를 연결하며, 목록 API가 없어 작업 ID 또는 저장한 주소로 다시 불러온다.

실저장 검증을 재실행하려면 `AWS_PROFILE=foodtech FOODTECH_COOKING_TABLE=foodtech-cooking-jobs-dev .venv/bin/python tests/live/cooking_dynamodb_check.py`를 저장소 루트에서 실행한다. 스크립트는 계정·테이블을 확인하고 합성 레코드만 생성·삭제한다. 실제 AWS 요청이 발생하므로 실행 전 비용과 계정 상태를 확인한다.

## 합성 수집 배치와 자동 온도 공정판정

조리작업의 `POST /api/customer/cooking-jobs/{job_id}/source-binding`은 고객 세션·CSRF·작업 버전을 확인한 뒤 조리 시작 전 `synthetic-rest / mockprofile-100-a-001 / batch-a-...` 배치를 한 작업에만 연결한다. 같은 배치를 다른 작업에 다시 연결하지 않는다. 연결 정보는 작업 메타와 역방향 바인딩을 같은 DynamoDB 트랜잭션으로 기록한다. 고객 화면에서는 배치 연결 뒤 조리를 시작하고 새로고침으로 자동판정을 확인한다.

표준화 Worker가 같은 `FOODTECH_COOKING_TABLE`을 사용하면 중복이 아닌 유효한 정규화 표본을 바인딩된 `RUNNING` 또는 `PAUSED` 작업에 반영한다. 저온·고온 지속 및 저온 복원시간의 첫 합성 온도 규칙을 적용한다. 실패가 확정되면 공정판정과 작업 상태 `FAILED`, 평가 차단, 원본 수신 ID·규칙 버전이 있는 사건을 원자적으로 기록한다. 이미 격리된 값과 늦은 이벤트는 현재 판정에 사용하지 않고, 결손은 연속시간을 끊는다. 장비 제어·안전판정·품질평가는 별개다.

새 합성 작업은 최대 두께(cm)를 입력받아 Thin `0.5~1.0` 미만, Standard `1.0~1.5` 미만, Thick `1.5~2.0` 이하로 분류한다. `0.5cm` 미만은 `BELOW_RANGE` 경고이며 시간 기준을 적용하지 않는다. `2.0cm` 초과는 `PROFILE_REJECTED`로 작업 생성이 거부된다. 조리 시작에는 운영자가 기록한 투입시각, 완료에는 인양시각이 필요하다. 실제 조리시간은 `인양시각 - 투입시각`으로 계산하며 서버의 완료 기록시각과 인양시각 차이도 저장한다. 두께별 승인 시간 경계에서 조기 인양·최대시간 초과는 공정 실패와 작업 `FAILED`, 그 밖의 단축·지연·권장시간 초과는 경고로 처리한다. 수집 온도 판정과 두께·시간 판정은 같은 공정 종합 상태에 합친다. 시간 기준은 조리 완료 시 판정하며 진행 중 시간 초과 알림은 아직 제공하지 않는다. 실제 장비 호환성과 현장 성능은 검증 범위에 없다.

개발용 AWS 테이블·FIFO 큐에서 [합성 고온 fixture](../../fixtures/tonkatsu-process-high-v1.json)를 수집 어댑터에 직접 넣어 5건 처리, 마지막 `HIGH_PROCESS_FAILURE`, 재처리 `REPLAYED` 및 시험 항목 정리를 확인했다. 재실행 명령은 `AWS_PROFILE=foodtech uv run --no-sync python tests/live/process_flow_dynamodb_check.py`다. C# Mock HTTPS/OAuth·실제 Cognito 경로와 배포 실행 역할은 포함하지 않는다.

위 수집·자동 온도판정 AWS 결과는 두께·조리시간 판정 추가 전의 검증이다.

새 판정의 로컬 경계값 시험과 Standard `1.20cm`·`300초` 개발용 DynamoDB 저장 확인을 완료했다. Worker와 조리시간 판정의 동시 실행은 아직 검증하지 않았다.
