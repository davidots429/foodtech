# AI Gateway 첫 실행 정책 경계

`foodtech_ai_gateway.policy.GatewayPolicy.authorize`는 서버가 보관한 실행 ID만 입력받아 실행 가능 여부를 판단한다. 모델 ID, 자격증명 ID, 동의 여부, 예상 비용과 tenant 범위는 신뢰된 Registry에서 다시 읽는다. 호출자는 인증된 업무 API 서비스와 tenant 범위여야 한다. HTTP 헤더 문자열만으로 `Caller`를 만들면 안 된다.

현재 검사는 다음을 포함한다.

- 모델의 회귀검증·명시적 승인·활성화, 커스텀 모델의 승인 주체
- 기능별 Tier와 고급 추론의 자동 다운그레이드 금지, 저등급 새 실행의 고지·동의 연결
- 외부 모델의 tenant 소유·공급자 일치·사용 가능한 자격증명 버전, 민감정보 로컬 전용 및 외부 전송 승인·동의
- 사용자·공급자·프로젝트의 예상 비용 한도와 호출 잔여량, 일반 작업의 가용한 로컬 기본 모델 fallback

`Decision`은 정책 판정이며 모델 응답이나 비용 예약이 아니다. 현 단계에는 Registry의 영속 구현, 내부 IAM/mTLS 인증, 한도 원자적 예약, KMS 복호화, LangChain/LiteLLM 공급자 호출, 실행 이력 저장이 없다. 따라서 FastAPI에는 실행 endpoint를 열지 않았다. 이 경계가 구현되기 전에는 실제 모델 호출이 일어나지 않는다.

로컬 검사는 `uv run --no-sync python -m unittest discover -s apps/ai-gateway/tests -v`로 실행한다. 자격증명 값·암호문·프롬프트 원문을 시험 입력에 넣지 않는다.
