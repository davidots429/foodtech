# 첫 수집 어댑터

합성 Mock의 HTTPS REST/JSONL을 OAuth 2.0 Client Credentials로 한 번 또는 지정 횟수만큼 조회한다. 원본과 순번 대기 항목을 DynamoDB에 보존한 뒤 `Source + Device + Batch`별 5초 복원 결과를 SQS FIFO에 전송한다. 같은 전송 호출의 재시도는 같은 `ingestion_event_id`를 사용하고, 별도 수신은 새 ID를 갖는다.

필요한 환경 변수는 `FOODTECH_MOCK_BASE_URL`, `FOODTECH_MOCK_CLIENT_ID`, `FOODTECH_MOCK_CLIENT_SECRET`, `FOODTECH_MOCK_CA_FILE`, `FOODTECH_DATA_TABLE`, `FOODTECH_ORDERED_QUEUE_URL`이다. AWS 자격증명은 표준 프로필 또는 실행 역할로 전달한다. `python -m foodtech_collection_adapter --polls 1`로 실행한다.

Mock fixture를 바꿔 시험할 때는 `--cursor-key tonkatsu-process-high-v1`처럼 별도 커서 키를 지정한다. 기본 키 `default`는 기존 커서와 호환된다. 커서 항목에는 발견한 `Source + Device + Batch` 스트림을 함께 기록하여 다음 실행에서도 그 스트림의 순번 결손 만료를 확인한다. 수집 CLI는 특정 배치 하나로 고정된 drain 대신 실제 받은 레코드의 스트림을 사용한다.

첫 구현은 단일 합성 스트림의 최소 흐름이다. REST timeout 5초, 전송 오류 1회 재시도, 순번 결손 대기 5초, lease 30초를 초기 계약으로 둔다. 동시 다중 Task·장기 보류 상태·실패 복구·대규모 부하는 후속 적합성 검증 대상이다. 비정상 JSON과 식별자 누락의 S3 Ingestion DLQ는 이 슬라이스에 포함하지 않는다.

`--profile mockprofile-110 --cursor-key <fixture-key>`는 C# 제육볶음 Mock의 HTTPS/OAuth 응답을 별도 계약으로 검증한다. 수집된 1초 원표본은 기존 데이터 테이블의 `JEYUK#<fixtureId>` 파티션에 불변 원본으로 저장하고 FIFO Ordered 큐에 참조를 보낸다. 같은 fixture·초의 원본 내용이 바뀌면 거부하며, 동일 원본의 재전송은 같은 SQS 중복 ID를 사용한다. 커서는 `mockprofile-100`과 분리한다. 제육볶음 경로는 순번 재배열을 하지 않으며 Worker가 연속 초 순서대로 판정한다. 2026-11-15 개발용 AWS에서 C# 생성 표본 30건의 DynamoDB 저장·SQS FIFO 전달과 합성 항목 정리를 확인했다. HTTPS 수집과 AWS 저장을 한 번의 실행으로 결합한 검증은 후속 범위다.

기존 개발용 AWS 테이블·큐에서 실제 C# Mock의 HTTPS/OAuth 응답을 수집하려면 .NET 10 Mock을 Release로 빌드하고 `AWS_PROFILE=foodtech .venv/bin/python tests/live/process_flow_dynamodb_check.py --mock-wire`를 실행한다. 이 검증은 임시 인증서와 고유 합성 배치를 만들고 Mock의 첫 연결 단절 후 재시도, DynamoDB·SQS FIFO 저장, Worker 공정판정, 조리작업 실패 전이 및 재전달 멱등성을 확인한 뒤 합성 항목을 정리한다. 공유 개발용 FIFO 큐가 비어 있어야 시작한다. 2026-11-15 개발용 AWS에서 5건이 처리되어 `HIGH_PROCESS_FAILURE`와 작업 `FAILED`가 기록됐고 합성 항목 정리가 완료됐다. 배포된 Worker Task 역할·부하·장기 복구 검증은 별도다.

