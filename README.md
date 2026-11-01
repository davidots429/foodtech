# Foodtech AI Recipe Agent

고객·관리자 웹, Business API, AI Gateway, C# Mock 및 CDK의 실행·빌드 경계를 분리한 monorepo다. Cognito 고객·관리자 인증과 DynamoDB 공유 세션, 돈까스 합성 조리작업·안전·평가 API 및 고객 화면을 구현했다. 최소 데이터 흐름은 `mockprofile-100-a-001` 합성 fixture에 한정한다. 2026-11-01에 개발용 DynamoDB·SQS FIFO 스택에서 수집 순번 `1, 3, 2`의 복원, 정상 2건·격리 1건의 표준화 결과를 확인했다. AI 호출, 실제 장비 연계, 전체 업무 데이터 모델과 서비스 배포는 아직 없다.

## 폴더

| 경로 | 책임 | 현재 상태 |
| --- | --- | --- |
| `apps/customer-web` | 고객 웹(챗봇 포함 예정) | 가입·로그인·합성 조리작업·안전·평가 화면 |
| `apps/admin-web` | 수행자 관리자 웹 | 관리자 로그인·권한 경계 화면 |
| `apps/business-api` | 업무 API와 BFF | Cognito PKCE·세션·권한 검사, 합성 조리작업·평가 API |
| `apps/ai-gateway` | 모델 실행 경계 | 경로 없는 FastAPI 객체 |
| `apps/credential-registration` | 자격증명 등록 경계 | 책임 설명만 |
| `apps/collection-adapter` | 읽기 전용 수집 | 첫 HTTPS REST poller·DynamoDB 순번 복원·SQS FIFO 전송 |
| `apps/standardization-worker` | 표준화·품질 | 첫 프로파일 매핑·해시·격리·중복 판정 |
| `apps/mock-control` | 시연·시험용 C# Mock | 첫 합성 REST/JSONL·OAuth·연결 단절 구현 |
| `infra/cdk` | TypeScript IaC | 인증 세션 및 최소 데이터 흐름 스택; Identity 스택은 미배포 |
| `packages/contracts`, `fixtures` | 공통 계약과 합성 시험자료 | 첫 JSON Schema·정규화 패키지·대표 fixture |

## 로컬 확인

- Node.js 24에서 `npm ci && npm run check`를 실행한다. npm workspaces와 `package-lock.json`을 사용한다.
- Python 3.12와 uv 0.12.17에서 `uv sync --all-packages --locked`를 실행한다. 버전은 `uv.lock`에 고정되어 있다.
- `uv run --no-sync python -m unittest discover -s apps/business-api/tests -v`로 인증 경계 테스트를 실행한다.
- 고객으로 로그인해 합성 조리작업을 생성하면 `작업 ID`와 주소의 `#job=...`으로 다시 불러올 수 있다. 상태 진행 후 제품별 합성 심부온도 표본으로 안전 판정을 기록하고, 7항목 평가·이력·재조리를 확인한다. 작업 목록 API는 아직 없고, 심부온도 입력은 실제 장비 측정이 아니다.
- `uv run --no-sync python -m unittest discover -s apps/collection-adapter/tests -v`로 첫 정규화·순번·전송 장애 테스트를 실행한다.
- .NET SDK 10.0.402에서 `dotnet restore apps/mock-control/MockControl.csproj`와 `dotnet build apps/mock-control/MockControl.csproj --no-restore`를 실행한다. 현재 외부 NuGet 패키지는 없다.
- GitHub Actions의 CI는 Node·Python·Mock 빌드와 C#/Python 실제 HTTPS·해시 통합 검사를 분리한다. PR 검사에는 AWS 권한이 없다.

## 저장소와 배포

초기 환경을 `main`에 올린 뒤 기능별 브랜치에서 작업하고, `main` 병합은 PR로 진행한다. 문제 해결은 issue와 별도 브랜치로 추적한다. 배포 workflow는 아직 없으며 명시적 실행 전용으로 별도 작성한다. 인증 세션 테이블과 최소 데이터 흐름 스택을 CDK로 개발 환경에 배포했다. 시험용 Cognito 리소스는 CDK에서 아직 관리하지 않는다.

인증 기능의 로컬 설정·실행 방법과 아직 필요한 실제 Cognito 설정은 [Business API 안내](apps/business-api/README.md)에 적었다.

승인된 정책과 후속 계약은 로컬의 `AGENTS.md`, `implementation_guidelines.md`, `pending_decisions.md`에서 관리한다. 내부 관리 문서·원본 참고자료·`output/` 산출물과 `.env`·AWS 로컬 인증자료·빌드 결과·작업 임시 파일은 Git 추적 대상에서 제외한다.

