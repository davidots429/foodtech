# Capsule 사용자 키와 항목 암호화

`UserKeyGenerator`는 서버가 정한 KMS pool·shard와 검증된 tenant 가명에 묶인 256비트 사용자 키를 생성하고 KMS 암호문만 반환한다. 평문 사용자 키는 제한된 내부 콜백에서만 사용하고 가변 버퍼를 종료 시 덮어쓴다. `UserKeyUnwrapper`는 별도 제한된 경계에서 동일 tenant 문맥과 허용된 키를 검사해 잠시 복호화한다. Python·SDK 내부의 불변 복사본까지 물리적으로 지우는 것은 보장하지 않는다.

`seal_entry`와 `open_entry`는 사용자 키를 AWS Encryption SDK Raw AES keyring의 wrapping key로 사용한다. SDK는 항목마다 새 메시지 키를 생성하고 서명·key commitment가 있는 암호문을 만든다. 항목에는 목적·기간·HMAC digest만 넣고, 복호화할 때 tenant 문맥·사용자 키 ID·pool·shard·키 ARN·목적·기간을 대조한다. KMS와 SDK 문맥에는 비밀이 아닌 tenant 가명·목적·pool·shard만 넣으며 SDK의 서명 공개키도 허용한다. 사용자 키 없이 KMS로 항목을 직접 암호화하는 이전 호환성 경로는 비활성이다.

Capsule META에는 암호화된 사용자 키와 키 ID·pool·shard·KMS ARN만 두고, 항목 쓰기는 META의 동일 키 ID·pool·shard·ARN과 revision을 조건 검사한다. 저장 모듈은 암호문의 진위나 소유자를 검증하지 않는다. 인증된 전용 암호화 서비스에서 생성한 결과만 저장하도록 서비스·IAM 경계를 연결하기 전에는 외부 입력을 해당 저장 경로로 전달하지 않는다.

2026-11-21 기존 개발용 자격증명 KMS 키와 합성 데이터로 사용자별 KMS 키 wrapping·SDK 암복호화, 다른 tenant 문맥과 변조된 키 거부, 가변 키 버퍼 정리를 확인했다. 별도 시험에서 기존 개발 DynamoDB에 암호화된 사용자 키와 항목을 저장·조회·복호화하고 시험 항목을 정리했다. 재실행은 `AWS_PROFILE=foodtech uv run --no-sync python tests/live/capsule_user_key_kms_check.py` 및 `tests/live/capsule_user_key_storage_check.py`다.

이 구성요소는 소유자 인증·사용자별 STS tag·JIT 승인·IAM·감사·월별 키 pool 선택을 제공하지 않는다. 등록·복호화 역할의 실제 분리, 사용자 키 생성·attach의 응답 유실 복구, 재암호화·키 교체, 삭제 Saga와 백업 재삭제는 구현·검증이 필요하다. 개발자 자격증명과 기존 테스트 CMK를 사용한 시험은 전용 역할·키 pool 또는 삭제 후 복구 불가능성 검증이 아니다.
