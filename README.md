# Foodtech AI Recipe Agent

고객·관리자 웹, Business API, AI Gateway, C# Mock 및 CDK의 실행·빌드 경계를 분리한 monorepo다. 첫 기능 브랜치에서 Cognito 인증 코드 + PKCE의 Business API 경계와 고객·관리자 로그인 화면, 고객의 읽기 전용 합성 예시 화면을 구현했다. Cognito 리소스 생성·실연동, AI 호출, 장비 연계, 데이터 저장과 배포는 아직 없다.

## 폴더

| 경로 | 책임 | 현재 상태 |
| --- | --- | --- |
| `apps/customer-web` | 고객 웹(챗봇 포함 예정) | 가입·로그인·합성 예시 화면 |
| `apps/admin-web` | 수행자 관리자 웹 | 관리자 로그인·권한 경계 화면 |
| `apps/business-api` | 업무 API와 BFF | Cognito PKCE·세션·권한 검사, 읽기 전용 예시 API |
| `apps/ai-gateway` | 모델 실행 경계 | 경로 없는 FastAPI 객체 |
| `apps/credential-registration` | 자격증명 등록 경계 | 책임 설명만 |
| `apps/collection-adapter` | 읽기 전용 수집 | 책임 설명만 |
| `apps/standardization-worker` | 표준화·품질 | 책임 설명만 |
| `apps/mock-control` | 시연·시험용 C# Mock | .NET 10 콘솔 시작점 |
| `infra/cdk` | TypeScript IaC | 빈 CDK App, 스택 없음 |
| `packages/contracts`, `fixtures` | 공통 계약과 합성 시험자료 | 자리만 마련 |

## 로컬 확인

- Node.js 24에서 `npm ci && npm run check`를 실행한다. npm workspaces와 `package-lock.json`을 사용한다.
- Python 3.12와 uv 0.12.17에서 `uv sync --all-packages --locked`를 실행한다. 버전은 `uv.lock`에 고정되어 있다.
- `uv run --no-sync python -m unittest discover -s apps/business-api/tests -v`로 인증 경계 테스트를 실행한다.
- .NET SDK 10.0.402에서 `dotnet restore apps/mock-control/MockControl.csproj`와 `dotnet build apps/mock-control/MockControl.csproj --no-restore`를 실행한다. 현재 외부 NuGet 패키지는 없다.
- GitHub Actions의 CI는 위 세 빌드를 분리한다. PR 검사에는 AWS 권한이 없다.

## 저장소와 배포

초기 환경을 `main`에 올린 뒤 기능별 브랜치에서 작업하고, `main` 병합은 PR로 진행한다. 문제 해결은 issue와 별도 브랜치로 추적한다. 실제 저장소 식별자·AWS Role·OIDC 신뢰 조건이 정해지면 **명시적 실행 전용** 배포 workflow를 별도로 작성한다. 지금은 CDK 스택·배포 workflow·AWS 자원을 만들거나 실행하지 않는다.

인증 기능의 로컬 설정·실행 방법과 아직 필요한 실제 Cognito 설정은 [Business API 안내](apps/business-api/README.md)에 적었다.

승인된 정책과 후속 계약은 로컬의 `AGENTS.md`, `implementation_guidelines.md`, `pending_decisions.md`에서 관리한다. 내부 관리 문서·원본 참고자료·`output/` 산출물과 `.env`·AWS 로컬 인증자료·빌드 결과·작업 임시 파일은 Git 추적 대상에서 제외한다.

