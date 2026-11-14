# 첫 표준화 Worker

SQS FIFO의 원본 참조를 받아 DynamoDB 원본을 강하게 읽고, 원천 스키마 검증 → 값 정규화 → RFC 8785 직렬화 → SHA-256 → 결정적 매핑 → 품질 판정을 수행한다. `python -m foodtech_standardization_worker.aws_flow`은 한 번에 최대 10건을 처리한다.

첫 품질 규칙은 절대영도보다 낮은 온도의 `HARD_LIMIT_EXCEEDED` 격리와 순번 결손·지연 도착 경고다. 정상 레코드만 표준 결과를 갖고, 격리 레코드도 원본 이벤트·불변 평가·Finding을 남긴다. 격리 참조는 별도 FIFO Standardization DLQ로 보낸다. 같은 업무 키의 다른 내용은 원본을 보존하고 `DUPLICATE_KEY_CONFLICT` Finding을 남긴다.

환경 변수는 `FOODTECH_DATA_TABLE`, `FOODTECH_ORDERED_QUEUE_URL`, `FOODTECH_STANDARDIZATION_DLQ_URL`이다. 이번 테이블은 최소 시험용 물리 배치이며 전체 승인 ERD의 테이블 정의를 대체하지 않는다. 데이터별 soft limit, 재처리 승인 절차, 집계 파생 버전과 운영 보존기간은 이후 계약이다.

`FOODTECH_COOKING_TABLE`을 추가하면 고객이 명시적으로 연결한 합성 수집 배치에 대해 첫 돈까스 온도 공정판정을 기록한다. Worker는 표준화 평가를 영속 저장한 뒤 조리작업 테이블의 바인딩을 강하게 읽고, 정상·경고 표본만 규칙에 전달한다. 5초보다 긴 간격이나 `DATA_GAP`은 연속시간을 끊으며 격리·중복·늦은 이벤트는 현재 판정에서 제외한다. 같은 수신 ID의 재시도는 사건 키와 조건부 트랜잭션으로 한 번만 반영한다. 공정 실패가 확정되면 작업을 `FAILED`로 변경하되 장비에는 어떤 명령도 전송하지 않는다. 개발용 AWS 테이블·큐에서 이 경로의 실저장 결과를 확인했다. 배포된 Worker 실행 역할의 권한·성능은 후속 검증 대상이다.

`LATE_AFTER_GAP` 평가가 명시적으로 연결된 조리작업에 도착하면 Worker는 작업이 종료된 뒤에도 조리작업 파티션에 `RESTATEMENT#REQUEST#<ingestion_event_id>`를 조건부로 한 번 기록한다. 요청에는 원본 평가 위치, 순번·시각, 발견 당시 작업 버전과 공정 상태를 담고 상태를 `PENDING`으로 둔다. 현재 작업 상태·판정과 과거 평가를 변경하지 않는다. 영향 범위 탐색, 과거 표본 재계산, 새 파생 버전 작성과 현재 포인터 반영은 후속 구현 범위다. 따라서 이 요청만으로 늦은 이벤트가 공정판정에 반영되었다고 간주하면 안 된다.

고객 업무 API가 기록한 두께·조리시간 상태는 공통 규칙으로 온도 상태와 합산한다. 온도 표본이 들어와도 기존 두께 경고를 지우지 않는다. 2026-11-14 기존 개발용 AWS 조리작업 테이블에서 정상 온도 표본의 `INSUFFICIENT_DATA`가 300초 완료 기록 후 종합 `NORMAL`로 바뀌고, 260초 조기 인양은 온도가 정상이더라도 `FAILED`가 되는 것을 확인했다. Worker 갱신이 완료 기록 직전에 들어오면 오래된 버전의 완료 요청은 409로 거부되고 재조회 후 재시도에서 결합 판정이 유지됐다. 완료 후 추가 표본은 현재 공정판정에 반영하지 않는다. 이 시험은 표준화 전단·SQS를 통과시키지 않고 Worker의 조리작업 반영 경계를 직접 사용했다.

온도·두께·실제 조리시간의 후속 결합과 늦은 이벤트 요청 저장 검증은 `AWS_PROFILE=foodtech FOODTECH_COOKING_TABLE=foodtech-cooking-jobs-dev .venv/bin/python tests/live/process_time_worker_dynamodb_check.py`로 재실행한다. 2026-11-14 개발용 DynamoDB에서 종료된 작업의 요청이 `PENDING`으로 한 번만 저장되고 재전달 시 기존 요청이 유지되며 현재 판정이 바뀌지 않는 것을 확인했다. 합성 작업·바인딩 파티션 4개를 생성하며 종료 시 모두 삭제하고 잔존 0건을 확인한다. 실제 AWS 요청 비용이 발생할 수 있다.

