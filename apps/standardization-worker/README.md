# 첫 표준화 Worker

SQS FIFO의 원본 참조를 받아 DynamoDB 원본을 강하게 읽고, 원천 스키마 검증 → 값 정규화 → RFC 8785 직렬화 → SHA-256 → 결정적 매핑 → 품질 판정을 수행한다. `python -m foodtech_standardization_worker.aws_flow`은 한 번에 최대 10건을 처리한다.

첫 품질 규칙은 절대영도보다 낮은 온도의 `HARD_LIMIT_EXCEEDED` 격리와 순번 결손·지연 도착 경고다. 정상 레코드만 표준 결과를 갖고, 격리 레코드도 원본 이벤트·불변 평가·Finding을 남긴다. 격리 참조는 별도 FIFO Standardization DLQ로 보낸다. 같은 업무 키의 다른 내용은 원본을 보존하고 `DUPLICATE_KEY_CONFLICT` Finding을 남긴다.

환경 변수는 `FOODTECH_DATA_TABLE`, `FOODTECH_ORDERED_QUEUE_URL`, `FOODTECH_STANDARDIZATION_DLQ_URL`이다. 이번 테이블은 최소 시험용 물리 배치이며 전체 승인 ERD의 테이블 정의를 대체하지 않는다. 데이터별 soft limit, 재처리 승인 절차, 집계 파생 버전과 운영 보존기간은 이후 계약이다.

