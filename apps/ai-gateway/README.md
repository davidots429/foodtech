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

`POST /internal/ai/executions`는 인증된 업무 API가 전달한 tenant 범위에 `GENERAL_GUIDE`·`llama3.1:8b` 로컬 실행을 생성한다. `POST /internal/ai/executions/{execution_id}/invoke`는 요청의 실행 ID로 기록을 다시 읽고 정책·한도 예약 후 loopback Ollama `/api/generate`에 `stream=false`, `num_ctx=4096`으로 요청한다. 결과 또는 정규화된 실패 코드를 `RESULT` 항목과 종료 상태·실제 비용 정산에 함께 기록한다. 프롬프트 원문은 레지스트리에 넣지 않는다. 결과 원문에는 90일 TTL을 설정했으며 정해진 시점의 Hard Delete 보장은 별도 삭제 작업에서 구현해야 한다. 이 첫 경로는 로컬 기본 모델만 지원하며 외부 공급자 키 복호화·호출, LangChain/LiteLLM 통합은 후속이다. 실제 Ollama와 DynamoDB 테이블을 사용하는 검증은 별도 수행한다.

필수 환경변수는 Gateway의 `FOODTECH_AI_REGISTRY_TABLE`, `FOODTECH_GATEWAY_SERVER_CERT`, `FOODTECH_GATEWAY_SERVER_KEY`, `FOODTECH_GATEWAY_BUSINESS_CA`와 업무 API의 `FOODTECH_GATEWAY_URL`, `FOODTECH_GATEWAY_CA`, `FOODTECH_BUSINESS_CLIENT_CERT`, `FOODTECH_BUSINESS_CLIENT_KEY`다. Gateway의 `FOODTECH_OLLAMA_URL` 기본값은 `http://127.0.0.1:11434`이며 loopback URL만 허용한다. 현재 월의 tenant·ollama provider·project 한도와 승인된 `llama3.1:8b` 모델 레코드를 먼저 준비해야 실행된다.

로컬 검사는 `uv run --no-sync python -m unittest discover -s apps/ai-gateway/tests -v`로 실행한다. 자격증명 값·암호문·프롬프트 원문을 시험 입력에 넣지 않는다.
