# Business API 인증 첫 구현

고객 공개 가입과 고객·관리자 로그인을 Cognito 관리형 화면에서 시작한다. Business API가 Authorization Code + PKCE의 state·verifier를 관리하고 코드 교환, ID·Access 토큰의 서명·발급자·client·용도를 확인한다. 브라우저에는 HttpOnly 세션 쿠키만 전달한다. 관리자 콜백에는 별도 App Client, `foodtech-admin` Cognito 그룹, TOTP 기반 `urn:cognito:loa:4` 인증 수준이 모두 필요하다.

## 실행

1. 하나의 Cognito User Pool에 공개 가입·이메일 확인을 설정한다. 고객·관리자 App Client를 분리하고 `code` OAuth flow와 `openid email profile` scope를 허용한다. 서비스의 고객 화면만 공개 가입으로 연결한다. Cognito 관리형 로그인 화면의 가입 링크는 Pool의 공개 가입 설정을 공유하므로 관리자 Client 화면에도 나타날 수 있다. 공개 가입 사용자에게 관리자 그룹을 부여하지 않으며, 관리자 계정과 `foodtech-admin` 그룹은 별도로 부여한다.
2. 각 Client의 callback URL을 아래 설정과 정확히 일치시키고, sign-out URL에 해당 웹 URL을 등록한다. 관리형 로그인 도메인을 설정한다. 관리형 로그인 v2라면 Client별 브랜딩을 생성한다.
3. `apps/business-api/.env.example`을 로컬 `.env`로 복사해 값을 채우고 환경변수로 로드한다. 키·토큰은 넣지 않는다. 로컬 실행에서만 `FOODTECH_SECURE_COOKIES=false`를 사용한다. `FOODTECH_AUTH_TABLE`을 지정하면 DynamoDB 공유 저장소를 사용한다. 배포 환경에서는 이 값이 필수다. AWS 자격증명은 환경 변수나 IAM 실행 역할로 전달한다.
4. `uv sync --all-packages --locked` 후 `uv run --no-sync uvicorn foodtech_business_api.main:app --host 127.0.0.1 --port 8000 --no-access-log`을 실행한다. 콜백 URL의 일회용 인증 코드가 접근 로그에 남지 않도록 한다. 다른 터미널에서 `npm run dev -w @foodtech/customer-web`과 `npm run dev -w @foodtech/admin-web`을 실행한다.
5. 고객 `http://127.0.0.1:5173`, 관리자 `http://127.0.0.1:5174`에서 확인한다. 두 Vite 개발 서버는 `/api`를 로컬 Business API로 전달한다.

## 구현 경계

- 로그인 흐름과 세션은 DynamoDB 테이블이 지정되면 공유된다. 비어 있으면 로컬 개발용 메모리 저장소를 사용하며 재시작 시 로그아웃된다. DynamoDB TTL 삭제는 비동기이므로 API는 모든 읽기에서 만료 시각을 직접 검사한다.
- 세션은 최장 1시간이고 Access 토큰 만료를 넘지 않는다. 프로젝트 관리자의 `POST /api/admin/users/{subject}/revoke-admin`과 `POST /api/admin/users/{subject}/suspend`는 CSRF를 확인한 뒤 공유 저장소의 차단 상태와 권한 버전을 변경한다. 기존 세션은 다음 API 요청부터 거부되며 새 관리자 로그인 또는 이용 중단 계정의 로그인도 거부된다. 이 조치는 애플리케이션 접근을 차단하며 Cognito 그룹·계정 상태 자체를 변경하지 않는다.
- 보호 API 요청마다 Cognito의 현재 계정 활성 상태를 확인하고 관리자 요청에는 현재 그룹도 확인한다. Cognito 호출에 실패하면 접근을 허용하지 않는다. 이 방식은 요청마다 Cognito API 호출과 지연을 추가하므로 부하 시험에서 조정해야 한다. 토큰 갱신은 아직 구현되지 않았고, 관리자 인증 수준 검사는 구현했지만 실제 관리자 TOTP 가입·로그인 시험은 남아 있다. 실제 공개 서비스용 인증 완료로 취급하지 않는다.
- 무료 예시 API는 공용 합성 자료만 읽기 전용으로 반환한다. 가입 계정의 영속 저장, 매장별 작업 데이터, 유료 전환 신청·사업자 정보 입력, 무료 AI 모델 호출은 후속 기능이다.
- 2026-11-01 AWS Free Plan의 시험용 Cognito User Pool에서 고객 가입·이메일 확인·Authorization Code/PKCE 토큰 교환·고객 세션·관리자 그룹 거부·로그아웃 후 API 거부를 확인했다. Cognito 관리형 로그인 v2는 시험 계정에서 동작했다. 시험 리소스는 아직 CDK 관리 대상이 아니다.
- 같은 시험 계정으로 DynamoDB 공유 저장소를 연결해 다시 로그인했다. 세션 생성, API 프로세스 재시작 후 로그인 유지, 로그아웃 후 세션 제거를 실제 AWS 테이블에서 확인했다.
