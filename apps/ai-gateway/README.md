# AI Gateway 첫 실행 정책 경계

`foodtech_ai_gateway.policy.GatewayPolicy.authorize`는 서버가 보관한 실행 ID만 입력받아 실행 가능 여부를 판단한다. 모델 ID, 자격증명 ID, 동의 여부, 예상 비용과 tenant 범위는 신뢰된 Registry에서 다시 읽는다. 호출자는 인증된 업무 API 서비스와 tenant 범위여야 한다. HTTP 헤더 문자열만으로 `Caller`를 만들면 안 된다.

현재 검사는 다음을 포함한다.

- 모델의 회귀검증·명시적 승인·활성화, 커스텀 모델의 승인 주체
- 기능별 Tier와 고급 추론의 자동 다운그레이드 금지, 저등급 새 실행의 고지·동의 연결
- 외부 모델의 tenant 소유·공급자 일치·사용 가능한 자격증명 버전, 민감정보 로컬 전용 및 외부 전송 승인·동의
- 사용자·공급자·프로젝트의 예상 비용 한도와 호출 잔여량, 일반 작업의 가용한 로컬 기본 모델 fallback

`DynamoRegistry`는 실행·모델·자격증명 메타데이터·동의·월별 한도를 별도 DynamoDB 테이블에 저장한다. `EXEC#<실행 ID>`를 기본 키로 직접 `GetItem`하며 일관된 읽기를 사용한다. 자격증명 레코드에는 `vault:` 참조만 저장하고 키 원문·암호문은 저장하지 않는다. 한도는 `YYYY-MM` 기간의 tenant, provider, project 세 범위로 관리한다. 관리 주체가 각 기간의 레코드를 먼저 생성해야 하며 모든 수정자는 `version`을 증가시켜야 한다.

`GatewayService.prepare(인증된 Caller, 실행 ID)`는 서버 저장 기록을 다시 읽고 정책 판정 후, `PENDING -> RESERVED` 상태와 세 범위의 예상 비용·호출 잔여량을 DynamoDB 트랜잭션 하나로 갱신한다. 모델·자격증명·동의 기록의 읽은 버전도 같은 트랜잭션에서 확인한다. 선택된 로컬 fallback은 원 실행에 임의 적용하지 않고 새 실행 생성을 요구한다. `finish`는 `RESERVED -> SUCCEEDED/FAILED`와 실제 비용 차액 정산을 하나의 트랜잭션으로 처리한다. 예약된 호출 수는 시도 후 복구하지 않는다.

직접 내부 HTTP의 첫 구현에는 mTLS를 사용한다. `python -m foodtech_ai_gateway.serve`는 loopback HTTPS에서 서버 인증서·키와 **업무 API 전용 CA**를 요구하고, 클라이언트 인증서가 없으면 TLS 단계에서 연결을 거절한다. 앱의 내부 경로도 전용 mTLS 실행기와 HTTPS가 아니면 거절한다. 임의의 신원 헤더를 인증 근거로 사용하지 않는다. 서버 인증서의 SAN에는 `127.0.0.1`을 넣고 업무 API의 클라이언트 인증서만 전용 CA로 서명한다. 개발용 인증서·키는 저장소 밖에서 관리한다. 향후 배포 진입 계층에서 IAM 인증이 가능하면 이 경계를 재검토한다.

`POST /internal/ai/executions`는 인증된 업무 API가 전달한 tenant 범위에 `GENERAL_GUIDE`·`llama3.1:8b` 로컬 실행을 생성한다. `POST /internal/ai/executions/{execution_id}/invoke`는 요청의 실행 ID로 기록을 다시 읽고 정책·한도 예약 후 로컬 실행은 loopback Ollama `/api/generate`에 `stream=false`, `num_ctx=4096`으로 요청한다. 승인된 외부 실행 기록은 아래 저장 자격증명·LiteLLM 경계를 거친다. 결과 또는 정규화된 실패 코드를 `RESULT` 항목과 종료 상태·실제 비용 정산에 함께 기록한다. 프롬프트 원문은 레지스트리에 넣지 않는다. 결과 원문에는 90일 TTL을 설정했으며 정해진 시점의 Hard Delete 보장은 별도 삭제 작업에서 구현해야 한다. 고객이 외부 실행을 생성하는 화면·API는 후속 작업이다.

필수 환경변수는 Gateway의 `FOODTECH_AI_REGISTRY_TABLE`, `FOODTECH_GATEWAY_SERVER_CERT`, `FOODTECH_GATEWAY_SERVER_KEY`, `FOODTECH_GATEWAY_BUSINESS_CA`와 업무 API의 `FOODTECH_GATEWAY_URL`, `FOODTECH_GATEWAY_CA`, `FOODTECH_BUSINESS_CLIENT_CERT`, `FOODTECH_BUSINESS_CLIENT_KEY`다. Gateway의 `FOODTECH_OLLAMA_URL` 기본값은 `http://127.0.0.1:11434`이며 loopback URL만 허용한다. 현재 월의 tenant·ollama provider·project 한도와 승인된 `llama3.1:8b` 모델 레코드를 먼저 준비해야 실행된다.

서버 배포 시 Ollama는 Gateway가 접근하는 같은 호스트의 loopback에서 실행하는 첫 계약이다. 서버의 GPU 드라이버·VRAM·실제 사용 가능 메모리, 4K context의 모델 적재 여부, CPU offload, 동시 요청·대기열·OOM·p95 지연을 서버에서 재검증한다. 승인된 최소 목표인 전용 GPU VRAM 8GB 이상은 서버 선정 기준이며, 개발 PC의 성공 여부를 서버 성능 증거로 대체하지 않는다. 서버가 별도 AI 호스트를 써야 한다면 loopback 제한을 해제하기 전에 내부 전송 인증·네트워크 경계를 다시 설계한다.

2026-11-06 개발 PC에서 Ollama 0.40.15와 `llama3.1:8b`를 실제 실행했다. GTX 1650 Ti 4GB의 4K context 적재는 Ollama 표시상 GPU 83%·CPU 17%였고 첫 합성 한국어 요청은 모델 적재를 포함해 96.2초, 출력 78토큰이었다. 이는 단일 로컬 기능 확인이며 서버의 지연·처리량·품질 기준이 아니다. 첫 흐름의 출력 상한은 128토큰, Ollama 호출 timeout은 180초, 업무 API의 Gateway timeout은 195초로 두었다. 서버 GPU에서 실측 후 다시 조정한다.

같은 날 합성 고객 요청을 업무 API HTTP 경로 → 실제 mTLS Gateway → 실제 Ollama → 실행 결과 저장까지 통과시켜 `SUCCEEDED`와 저장 응답 일치를 확인했다. 없는 모델의 실제 Ollama HTTP 오류는 `FAILED / OLLAMA_HTTP_ERROR`로 저장됐다. 이 시험에서는 고객 세션·CSRF를 대역으로, 레지스트리는 메모리 시험 구현으로 사용했다.

2026-11-07에는 [실저장 검증 스크립트](../../tests/live/ai_gateway_dynamodb_check.py)로 같은 업무 API → 실제 mTLS Gateway → 실제 Ollama → 개발용 DynamoDB 레지스트리 경로를 결합했다. 합성 요청의 `SUCCEEDED`와 응답 본문 저장 일치, Ollama 종료 후 `FAILED / OLLAMA_UNAVAILABLE`, 두 실행의 tenant 호출 잔여량 `2→1→0`을 확인했다. 시험 레코드는 삭제했고 테이블 항목 수 0을 확인했다. 재실행 명령은 저장소 루트에서 `AWS_PROFILE=foodtech uv run --no-sync python tests/live/ai_gateway_dynamodb_check.py`다. 스크립트는 로컬 Ollama와 Gateway를 임시 실행하며 AWS 요청이 발생한다. 고객 세션·CSRF는 합성 대역이고, 실제 Cognito 세션·배포된 Gateway 실행 역할·서버 GPU 성능은 아직 검증하지 않았다.

로컬 검사는 `uv run --no-sync python -m unittest discover -s apps/ai-gateway/tests -v`로 실행한다. 자격증명 값·암호문·프롬프트 원문을 시험 입력에 넣지 않는다.

## 저장 자격증명과 외부 공급자 첫 실행 경계

이미 승인된 외부 실행 기록의 ID로 들어온 요청은 모델·credential·동의·tenant 소유권·Tier·한도를 레지스트리에서 다시 확인한다. `FOODTECH_CREDENTIAL_VAULT_TABLE`과 `FOODTECH_CREDENTIAL_KMS_KEY_ARNS`가 설정된 Gateway에서만 Vault를 일관되게 읽는다. Vault ref는 `vault:<credential_id>`로 고정하고 credential의 tenant 가명값·provider·환경·허용 KMS 키 ARN과 비교한다. 저장 암호문은 AWS Encryption SDK 4.0.7의 committing signed suite `0x0578`과 `REQUIRE_ENCRYPT_REQUIRE_DECRYPT`로 복호화한다. KMS keyring은 서버가 허용한 키만 사용하고 encryption context의 소유자·목적·환경·버전이 맞아야 평문을 반환한다.

공급자 호출은 LiteLLM Core 1.108.1의 비동기 `acompletion`을 사용하며 `openai/`, `gemini/`, `anthropic/`의 서버 레지스트리 모델만 허용한다. API 키는 호출별 인자로만 전달하고 응답·오류·레지스트리에 기록하지 않는다. 오류는 정해진 코드로만 반환한다. 호출 뒤 `bytearray`는 모든 경로에서 덮어쓰지만 Python 문자열·라이브러리 내부 복사본의 완전 소거는 보장하지 않는다. LiteLLM 비용 목록은 패키지에 포함된 버전으로 고정하여 시작 시 별도 네트워크 조회를 막는다. 공급자 오류 시 실제 청구액을 즉시 알 수 없으므로 예약 비용을 보존한다.

2026-11-07 개발용 KMS·Vault 테이블에서 더미 키의 암호화·복호화, 잘못된 context 거부, 실제 DynamoDB 실행 레지스트리의 동의·예약·성공 결과와 한도 차감을 검증했다. 외부 공급자는 네트워크 대역으로 처리했으며 실제 키·유료 API 요청은 없었다. 시험 후 두 테이블 항목 수는 0이다. [재실행 스크립트](../../tests/live/credential_vault_check.py)를 사용한다. 외부 실행 생성·고지·실동의 UI, 전송 RSA 암호화·등록 API, 가격 상한의 보수적 산정과 청구 대사, 실제 공급자 네트워크·egress, tenant STS tag 조건, ECS 역할로 실행한 검증은 아직 후속이다. 현재 Business API의 고객 AI 경로는 로컬 모델만 생성한다.
