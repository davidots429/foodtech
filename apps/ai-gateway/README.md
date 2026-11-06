# AI Gateway 첫 실행 정책 경계

`foodtech_ai_gateway.policy.GatewayPolicy.authorize`는 서버가 보관한 실행 ID만 입력받아 실행 가능 여부를 판단한다. 모델 ID, 자격증명 ID, 동의 여부, 예상 비용과 tenant 범위는 신뢰된 Registry에서 다시 읽는다. 호출자는 인증된 업무 API 서비스와 tenant 범위여야 한다. HTTP 헤더 문자열만으로 `Caller`를 만들면 안 된다.

현재 검사는 다음을 포함한다.

- 모델의 회귀검증·명시적 승인·활성화, 커스텀 모델의 승인 주체
- 기능별 Tier와 고급 추론의 자동 다운그레이드 금지, 저등급 새 실행의 고지·동의 연결
- 외부 모델의 tenant 소유·공급자 일치·사용 가능한 자격증명 버전, 민감정보 로컬 전용 및 외부 전송 승인·동의
- 사용자·공급자·프로젝트의 예상 비용 한도와 호출 잔여량, 일반 작업의 가용한 로컬 기본 모델 fallback

`DynamoRegistry`는 실행·모델·자격증명 메타데이터·동의·월별 한도를 별도 DynamoDB 테이블에 저장한다. `EXEC#<실행 ID>`를 기본 키로 직접 `GetItem`하며 일관된 읽기를 사용한다. 자격증명 레코드에는 `vault:` 참조만 저장하고 키 원문·암호문은 저장하지 않는다. 한도는 `YYYY-MM` 기간의 tenant, provider, project 세 범위로 관리한다. 관리 주체가 각 기간의 레코드를 먼저 생성해야 하며 모든 수정자는 `version`을 증가시켜야 한다.

`GatewayService.prepare(인증된 Caller, 실행 ID)`는 서버 저장 기록을 다시 읽고 정책 판정 후, `PENDING -> RESERVED` 상태와 세 범위의 예상 비용·호출 잔여량을 DynamoDB 트랜잭션 하나로 갱신한다. 모델·자격증명·동의 기록의 읽은 버전도 같은 트랜잭션에서 확인한다. 선택된 로컬 fallback은 원 실행에 임의 적용하지 않고 새 실행 생성을 요구한다. `finish`는 `RESERVED -> SUCCEEDED/FAILED`와 실제 비용 차액 정산을 하나의 트랜잭션으로 처리한다. 예약된 호출 수는 시도 후 복구하지 않는다.

내부 IAM/mTLS 호출자 인증, 암호화 Vault, 실제 모델 호출과 HTTP 실행 endpoint는 아직 없다. FastAPI에는 실행 endpoint를 열지 않았고 실제 모델 호출은 일어나지 않는다. 레지스트리 테이블은 CDK의 `provisionAiExecutionRegistry=true`로 선택적으로 합성하며 아직 배포하지 않았다.

로컬 검사는 `uv run --no-sync python -m unittest discover -s apps/ai-gateway/tests -v`로 실행한다. 자격증명 값·암호문·프롬프트 원문을 시험 입력에 넣지 않는다.
