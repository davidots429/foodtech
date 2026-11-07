# 자격증명 등록 처리

전송 암호문 검증·KMS 전송 복호화·저장용 암호화를 담당할 독립 실행 경계다. 저장 자격증명 재조회·복호화 권한은 두지 않는다.

첫 구현의 `CredentialWriter.store`는 신뢰된 등록 처리기가 이미 전송 복호화한 1~400바이트 `bytearray`를 입력받는다. AWS Encryption SDK 4.0.7의 suite `0x0578`(AES-256-GCM, HKDF-SHA-512, ECDSA-P384, key commitment)과 `REQUIRE_ENCRYPT_REQUIRE_DECRYPT` 정책으로 저장용 대칭 KMS 키를 사용해 암호화한다. `tenant_pseudonym`, 자격증명 ID, provider, 환경, pool/shard 버전과 목적을 encryption context에 넣는다. SDK 메시지에는 암호화된 데이터 키가 포함된다. Vault의 `CREDENTIAL#<credential_id>/META` 항목은 조건부 최초 등록만 허용하며 입력 버퍼는 성공·실패 모두에서 덮어쓴다. Python·SDK 내부 불변 복사본의 물리적 소거는 보장하지 않는다.

이 모듈에는 저장 자격증명 조회·복호화 API가 없다. CDK의 `CredentialRegistrationTaskRole`은 Vault `PutItem`과 저장 KMS 키 `GenerateDataKey`만 갖고 `GetItem`·저장 키 `Decrypt`는 갖지 않는다. Gateway 역할은 반대 방향의 Vault `GetItem`·KMS `Decrypt`를 갖는다. 현재 전송 RSA-OAEP 복호화·등록 HTTP UI/API, HMAC tenant 가명화·월별 CMK pool 자동화와 실제 ECS Task 연결은 후속이다. 실제 고객 API 키는 시험에 사용하지 않는다.

개발용 실검증은 저장소 루트에서 `AWS_PROFILE=foodtech uv run --no-sync python tests/live/credential_vault_check.py`로 재실행한다. 더미 키만 저장·복호화하고 잘못된 context를 거부한 뒤 시험 항목을 삭제한다. AWS KMS·DynamoDB 요청 비용이 발생한다.

2026-11-07 두 역할을 각각 임시 Fargate Task에 연결해 실제 호출을 시험했다. 등록 역할은 Vault `PutItem`·KMS `GenerateDataKey`에 성공하고 `GetItem`·`Decrypt`는 거부됐다. Gateway 역할은 Vault `GetItem`·KMS `Decrypt`에 성공하고 `PutItem`·`GenerateDataKey`는 거부됐다. IAM 시뮬레이터는 실제 Vault 접근에 성공한 개발 역할까지 조직 정책 거부로 표시했으나, 실제 Task의 성공·거부 결과는 의도한 권한 경계와 일치했다. 시험 리소스는 정리했다. 실제 서비스 Task Definition 연결은 후속이다.

