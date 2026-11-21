# Capsule 항목 암호화 경계

`CapsuleEntryWriter`는 승인된 purpose·epoch·HMAC digest만 포함한 항목을 AWS Encryption SDK 4.0.7의 AES-256-GCM/HKDF/ECDSA/key commitment suite로 암호화한다. SDK는 항목마다 새 데이터 키를 생성하고 암호화된 데이터 키를 SDK 메시지에 포함한다. 서버가 선택한 pool version·shard ID·KMS 키 ARN은 메시지와 함께 보존하며, KMS encryption context는 비밀이 아닌 tenant 가명·목적·pool·shard로 제한한다. 서명 suite가 추가하는 `aws-crypto-public-key`도 허용한다.

`CapsuleEntryReader`는 별도 제한된 복호화 경계에서 키 allowlist, 암호화 문맥, 알고리즘, 평문의 purpose·epoch 및 필드 구성을 확인한다. 이 모듈 자체는 소유자 인증·사용자별 STS tag·JIT 승인·IAM·감사·키 풀 선택 정책을 제공하지 않으며, 클라이언트가 제공한 tenant 가명이나 key map을 신뢰하면 안 된다. 등록 경로에 복호화 메서드를 제공하지 않아도 실제 권한 분리는 배포 역할·키 정책의 검증이 필요하다. Python과 SDK의 불변 복사본까지 물리적으로 메모리에서 지우는 것은 보장하지 않는다.

Capsule 저장 모듈은 암호문과 키 메타데이터만 저장하며 그 진위나 소유자를 검증하지 않는다. 인증된 전용 암호화 서비스에서 생성한 결과만 저장하도록 서비스·IAM 경계를 연결하기 전에는 외부 입력을 해당 저장 경로로 전달하지 않는다. 별도의 기존 형식 데이터가 있다면 메타데이터 이관과 검증 전에는 이 형식으로 읽을 수 없다.

2026-11-21 기존 개발용 자격증명 KMS 키와 합성 데이터로 SDK 암복호화, 문맥 불일치·변조 거부를 확인했다. 별도 시험에서 기존 개발 DynamoDB에 항목 암호문과 key ARN·pool·shard를 저장·조회·복호화하고 시험 항목을 정리했다. 재실행은 `AWS_PROFILE=foodtech uv run --no-sync python tests/live/capsule_crypto_kms_check.py` 및 `tests/live/capsule_crypto_storage_check.py`다. 이는 임시 호환성 시험이며 Capsule 전용 CMK pool, 사용자별 JIT/STS 권한, 실제 개인정보 또는 삭제 후 복구 불가능성 검증이 아니다. 사용자별 키 연결·폐기와 과거 pool의 운영·복구 절차는 별도로 검증해야 한다.
