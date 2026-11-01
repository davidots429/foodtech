# 첫 수집 어댑터

합성 Mock의 HTTPS REST/JSONL을 OAuth 2.0 Client Credentials로 한 번 또는 지정 횟수만큼 조회한다. 원본과 순번 대기 항목을 DynamoDB에 보존한 뒤 `Source + Device + Batch`별 5초 복원 결과를 SQS FIFO에 전송한다. 같은 전송 호출의 재시도는 같은 `ingestion_event_id`를 사용하고, 별도 수신은 새 ID를 갖는다.

필요한 환경 변수는 `FOODTECH_MOCK_BASE_URL`, `FOODTECH_MOCK_CLIENT_ID`, `FOODTECH_MOCK_CLIENT_SECRET`, `FOODTECH_MOCK_CA_FILE`, `FOODTECH_DATA_TABLE`, `FOODTECH_ORDERED_QUEUE_URL`이다. AWS 자격증명은 표준 프로필 또는 실행 역할로 전달한다. `python -m foodtech_collection_adapter --polls 1`로 실행한다.

첫 구현은 단일 합성 스트림의 최소 흐름이다. REST timeout 5초, 전송 오류 1회 재시도, 순번 결손 대기 5초, lease 30초를 초기 계약으로 둔다. 동시 다중 Task·장기 보류 상태·실패 복구·대규모 부하는 후속 적합성 검증 대상이다. 비정상 JSON과 식별자 누락의 S3 Ingestion DLQ는 이 슬라이스에 포함하지 않는다.

