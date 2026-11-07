# 자격증명 등록 처리

전송 암호문 검증·KMS 전송 복호화·저장용 암호화를 담당할 독립 실행 경계다. 저장 자격증명 재조회·복호화 권한은 두지 않는다. `python -m foodtech_credential_registration.serve`는 loopback HTTPS에서 업무 API 전용 클라이언트 인증서를 요구하며 접근 로그를 끈다.

첫 구현의 `CredentialWriter.store`는 신뢰된 등록 처리기가 이미 전송 복호화한 1~400바이트 `bytearray`를 입력받는다. AWS Encryption SDK 4.0.7의 suite `0x0578`(AES-256-GCM, HKDF-SHA-512, ECDSA-P384, key commitment)과 `REQUIRE_ENCRYPT_REQUIRE_DECRYPT` 정책으로 저장용 대칭 KMS 키를 사용해 암호화한다. `tenant_pseudonym`, 자격증명 ID, provider, 환경, pool/shard 버전과 목적을 encryption context에 넣는다. SDK 메시지에는 암호화된 데이터 키가 포함된다. Vault의 `CREDENTIAL#<credential_id>/META` 항목은 조건부 최초 등록만 허용하며 입력 버퍼는 성공·실패 모두에서 덮어쓴다. Python·SDK 내부 불변 복사본의 물리적 소거는 보장하지 않는다.

`GET /internal/credentials/transfer-key`는 서버에서 정한 RSA-4096 공개키의 SPKI, 키 버전, `RSA-OAEP-SHA-256`, 10분 캐시 만료시각을 반환한다. `POST /internal/credentials`는 암호문·공급자·버전과 업무 API가 인증한 고객 ID만 받는다. KMS `RSAES_OAEP_SHA_256`으로 복호화하고 UTF-8 키 값 1~400바이트를 확인한다. 목적·월·버전을 길이 접두 형식으로 묶어 KMS HMAC-SHA-256 가명 ID를 생성한다. 저장 암호문과 Gateway용 `vault:` 메타데이터는 DynamoDB `TransactWriteItems` 한 건으로 기록한다. 응답은 자격증명 ID와 상태만 포함한다.

이 모듈에는 저장 자격증명 조회·복호화 API가 없다. CDK의 `CredentialRegistrationTaskRole`은 Vault·레지스트리의 `PutItem`, 저장 KMS 키 `GenerateDataKey`, 전송 키 `GetPublicKey`·`Decrypt`, 가명 키 `GenerateMac`만 갖는다. 저장 키 `Decrypt`·Vault `GetItem`은 없다. Gateway 역할은 저장 Vault의 `GetItem`·저장 키 `Decrypt`만 갖는다. 전송 키·가명 키의 갱신 자동화, 실제 서비스 ECS Task 연결과 STS tenant tag 조건은 후속이다. 실제 고객 API 키는 시험에 사용하지 않는다.

설정은 [환경변수 예시](.env.example)를 따른다. 전송용·저장용·HMAC KMS 키 ARN은 각각 서버 설정에서 선택하며 브라우저 요청값으로 받지 않는다. 클라이언트 인증서와 서버 인증서·키는 저장소 밖에 둔다. 브라우저·업무 API에는 저장용 KMS 복호화 권한을 부여하지 않는다.

개발용 실검증은 저장소 루트에서 `AWS_PROFILE=foodtech uv run --no-sync python tests/live/credential_vault_check.py`로 재실행한다. 더미 키만 저장·복호화하고 잘못된 context를 거부한 뒤 시험 항목을 삭제한다. AWS KMS·DynamoDB 요청 비용이 발생한다.

2026-11-07 두 역할을 각각 임시 Fargate Task에 연결해 실제 호출을 시험했다. 등록 역할은 Vault `PutItem`·KMS `GenerateDataKey`에 성공하고 `GetItem`·`Decrypt`는 거부됐다. Gateway 역할은 Vault `GetItem`·KMS `Decrypt`에 성공하고 `PutItem`·`GenerateDataKey`는 거부됐다. IAM 시뮬레이터는 실제 Vault 접근에 성공한 개발 역할까지 조직 정책 거부로 표시했으나, 실제 Task의 성공·거부 결과는 의도한 권한 경계와 일치했다. 시험 리소스는 정리했다. 실제 서비스 Task Definition 연결은 후속이다.

같은 날 새 전송·가명 키를 개발 스택에 추가했다. [합성 등록 검증](../../tests/live/credential_registration_check.py)은 공개키로 암호화한 더미 키의 KMS 전송 복호화, HMAC 가명화, Vault·레지스트리 동시 저장, Gateway 복호화를 통과했다. `AWS_PROFILE=foodtech uv run --no-sync python tests/live/credential_registration_check.py`로 재실행할 수 있다. 새 등록 역할을 임시 Fargate Task에 연결한 시험에서도 전송 키 공개키 조회·복호화, 가명 키 MAC 생성, 두 테이블의 원자적 기록이 성공했고 Vault 읽기는 거부됐다. 합성 시험 항목과 임시 Task 리소스는 정리했다. 고객 브라우저의 실제 클릭 흐름, 등록 지연, 실제 서비스 Task 배포는 아직 별도 검증 대상이다. 전송·HMAC·저장 KMS 키는 계속 유지되며 비용이 발생할 수 있다.

