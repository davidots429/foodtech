# Capsule 사용자 키와 항목 암호화

`UserKeyGenerator`는 서버가 정한 KMS pool·shard와 검증된 tenant 가명에 묶인 256비트 사용자 키를 생성하고 KMS 암호문만 반환한다. 평문 사용자 키는 제한된 내부 콜백에서만 사용하고 가변 버퍼를 종료 시 덮어쓴다. `UserKeyUnwrapper`는 별도 제한된 경계에서 동일 tenant 문맥과 허용된 키를 검사해 잠시 복호화한다. Python·SDK 내부의 불변 복사본까지 물리적으로 지우는 것은 보장하지 않는다.

`seal_entry`와 `open_entry`는 사용자 키를 AWS Encryption SDK Raw AES keyring의 wrapping key로 사용한다. SDK는 항목마다 새 메시지 키를 생성하고 서명·key commitment가 있는 암호문을 만든다. 항목에는 목적·기간·HMAC digest만 넣고, 복호화할 때 tenant 문맥·사용자 키 ID·pool·shard·키 ARN·목적·기간을 대조한다. KMS와 SDK 문맥에는 비밀이 아닌 tenant 가명·목적·pool·shard만 넣으며 SDK의 서명 공개키도 허용한다. 사용자 키 없이 KMS로 항목을 직접 암호화하는 이전 호환성 경로는 비활성이다.

Capsule META에는 암호화된 사용자 키와 키 ID·pool·shard·KMS ARN만 두고, 항목 쓰기는 META의 동일 키 ID·pool·shard·ARN과 revision을 조건 검사한다. 저장 모듈은 암호문의 진위나 소유자를 검증하지 않는다. 인증된 전용 암호화 서비스에서 생성한 결과만 저장하도록 서비스·IAM 경계를 연결하기 전에는 외부 입력을 해당 저장 경로로 전달하지 않는다.

2026-11-21 기존 개발용 자격증명 KMS 키와 합성 데이터로 사용자별 KMS 키 wrapping·SDK 암복호화, 다른 tenant 문맥과 변조된 키 거부, 가변 키 버퍼 정리를 확인했다. 별도 시험에서 기존 개발 DynamoDB에 암호화된 사용자 키와 항목을 저장·조회·복호화하고 시험 항목을 정리했다. 재실행은 `AWS_PROFILE=foodtech uv run --no-sync python tests/live/capsule_user_key_kms_check.py` 및 `tests/live/capsule_user_key_storage_check.py`다.

이 구성요소는 소유자 인증·사용자별 STS tag·JIT 승인·IAM·감사·월별 키 pool 선택을 제공하지 않는다. 등록·복호화 역할의 실제 분리, 사용자 키 생성·attach의 응답 유실 복구, 재암호화·키 교체, 삭제 Saga와 백업 재삭제는 구현·검증이 필요하다. 개발자 자격증명과 기존 테스트 CMK를 사용한 시험은 전용 역할·키 pool 또는 삭제 후 복구 불가능성 검증이 아니다.

잠금된 Capsule의 완료된 인벤토리를 제한된 삭제 구성요소가 전달하면 `prepare_targets`는 암호화된 사용자 키를 열어 모든 항목의 사용자·목적·기간 문맥을 검증한 뒤 목적별 삭제 대상을 준비한다. 개수 불일치·중복·복호화 실패가 있으면 계획을 반환하지 않는다. 평문 결과는 별도 권한 경계에서만 일시적으로 사용하며 로그·외부 응답에 넣지 않는다. 인벤토리 완전성의 증명, 삭제 계획의 영속화, 저장소 삭제, 잔존 검증과 복원 후 재삭제는 이 함수에 포함되지 않으며 실제 서비스와 연결되지 않았다.

`prepare_from_inventory`는 신뢰된 내부 작업자가 전달한 Capsule 인벤토리의 모든 페이지와 잠금·revision 재확인이 성공한 뒤에만 사용자 키를 열어 메모리상의 삭제 대상을 구성한다. 이 연결은 호출자 인증, 계획의 영속화, 실제 삭제와 잔존 검증을 수행하지 않는다.

Capsule tenant 범위 복구기는 신뢰된 내부 사용자 ID와 목적·기간·HMAC 키 버전으로 길이 접두 UTF-8 규약의 가명을 재계산한다. 허용 목적과 버전별 키 ARN은 신뢰된 설정에서 제공한다. 개발 KMS 합성 시험에서 기존 credential 가명 인코더와 결과가 일치함을 확인했다. 현재 범위 descriptor의 Capsule 메타데이터 저장과 인증된 복구 작업자 연결은 아직 구현되지 않았으므로 외부에서 주입한 범위를 신뢰할 수 없다.

`prepare_from_resolved_inventory`는 안정적인 잠금 인벤토리의 목적·기간·키 버전과 신뢰된 내부 ID로 tenant를 재계산한 후 사용자 키와 항목을 열어 삭제 계획을 만든다. 잘못된 tenant는 키 복호화에서 거부된다. 개발 KMS·DynamoDB 합성 시험에서 등록·잠금 복구부터 대상 계획까지 확인했다. 실제 인증된 작업자·계획 영속화와 이전 형식 데이터의 범위 확인 및 이전은 아직 남았다.
