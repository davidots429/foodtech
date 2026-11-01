# Business API 인증 첫 구현

고객 공개 가입과 고객·관리자 로그인을 Cognito 관리형 화면에서 시작한다. Business API가 Authorization Code + PKCE의 state·verifier를 관리하고 코드 교환, ID·Access 토큰의 서명·발급자·client·용도를 확인한다. 브라우저에는 HttpOnly 세션 쿠키만 전달한다. 관리자 콜백에는 별도 App Client와 `foodtech-admin` Cognito 그룹이 모두 필요하다.

## 실행

1. 하나의 Cognito User Pool에 공개 가입·이메일 확인을 설정한다. 고객·관리자 App Client를 분리하고 `code` OAuth flow와 `openid email profile` scope를 허용한다. 고객 Client만 공개 가입 화면으로 연결한다. 관리자 계정과 `foodtech-admin` 그룹은 별도로 부여한다.
2. 각 Client의 callback URL을 아래 설정과 정확히 일치시키고, sign-out URL에 해당 웹 URL을 등록한다. 관리형 로그인 도메인을 설정한다. 관리형 로그인 v2라면 Client별 브랜딩을 생성한다.
3. `apps/business-api/.env.example`을 로컬 `.env`로 복사해 값을 채우고 환경변수로 로드한다. 키·토큰은 넣지 않는다. 로컬 실행에서만 `FOODTECH_SECURE_COOKIES=false`를 사용한다.
4. `uv sync --all-packages --locked` 후 `uv run --no-sync uvicorn foodtech_business_api.main:app --host 127.0.0.1 --port 8000`을 실행한다. 다른 터미널에서 `npm run dev -w @foodtech/customer-web`과 `npm run dev -w @foodtech/admin-web`을 실행한다.
5. 고객 `http://127.0.0.1:5173`, 관리자 `http://127.0.0.1:5174`에서 확인한다. 두 Vite 개발 서버는 `/api`를 로컬 Business API로 전달한다.

## 구현 경계

- 현 세션·로그인 흐름 저장소는 **단일 프로세스 메모리**다. 재시작 시 로그아웃되고 여러 API 인스턴스 사이에 공유되지 않는다. 실제 배포 전 만료·회수 가능한 공유 저장소로 교체해야 한다.
- 세션은 최장 1시간이고 Access 토큰 만료를 넘지 않는다. 토큰 갱신, 계정 중지·그룹 회수의 즉시 반영, 관리자 추가 인증 확인은 아직 구현되지 않았다. 실제 공개 서비스용 인증 완료로 취급하지 않는다.
- 무료 예시 API는 공용 합성 자료만 읽기 전용으로 반환한다. 가입 계정의 영속 저장, 매장별 작업 데이터, 유료 전환 신청·사업자 정보 입력, 무료 AI 모델 호출은 후속 기능이다.
- Cognito 자원과 AWS 비용 발생 작업은 이 브랜치에서 실행하지 않았다. 실연동·관리형 화면·무료 계정 적합성은 설정 후 검증한다.
