# 첫 표준화 Worker

SQS FIFO의 원본 참조를 받아 DynamoDB 원본을 강하게 읽고, 원천 스키마 검증 → 값 정규화 → RFC 8785 직렬화 → SHA-256 → 결정적 매핑 → 품질 판정을 수행한다. `python -m foodtech_standardization_worker.aws_flow`은 한 번에 최대 10건을 처리한다.

첫 품질 규칙은 절대영도보다 낮은 온도의 `HARD_LIMIT_EXCEEDED` 격리와 순번 결손·지연 도착 경고다. 정상 레코드만 표준 결과를 갖고, 격리 레코드도 원본 이벤트·불변 평가·Finding을 남긴다. 격리 참조는 별도 FIFO Standardization DLQ로 보낸다. 같은 업무 키의 다른 내용은 원본을 보존하고 `DUPLICATE_KEY_CONFLICT` Finding을 남긴다.

환경 변수는 `FOODTECH_DATA_TABLE`, `FOODTECH_ORDERED_QUEUE_URL`, `FOODTECH_STANDARDIZATION_DLQ_URL`이다. 이번 테이블은 최소 시험용 물리 배치이며 전체 승인 ERD의 테이블 정의를 대체하지 않는다. 데이터별 soft limit, 재처리 승인 절차, 집계 파생 버전과 운영 보존기간은 이후 계약이다.

`FOODTECH_COOKING_TABLE`을 추가하면 고객이 명시적으로 연결한 합성 수집 배치에 대해 첫 돈까스 온도 공정판정을 기록한다. Worker는 표준화 평가를 영속 저장한 뒤 조리작업 테이블의 바인딩을 강하게 읽고, 정상·경고 표본만 규칙에 전달한다. 5초보다 긴 간격이나 `DATA_GAP`은 연속시간을 끊으며 격리·중복·늦은 이벤트는 현재 판정에서 제외한다. 같은 수신 ID의 재시도는 사건 키와 조건부 트랜잭션으로 한 번만 반영한다. 공정 실패가 확정되면 작업을 `FAILED`로 변경하되 장비에는 어떤 명령도 전송하지 않는다. 조리작업 테이블 접근 권한과 실제 AWS 연동은 후속 검증 대상이다.

