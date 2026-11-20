# 첫 수집 어댑터

합성 Mock의 HTTPS REST/JSONL을 OAuth 2.0 Client Credentials로 한 번 또는 지정 횟수만큼 조회한다. 원본과 순번 대기 항목을 DynamoDB에 보존한 뒤 `Source + Device + Batch`별 5초 복원 결과를 SQS FIFO에 전송한다. 같은 전송 호출의 재시도는 같은 `ingestion_event_id`를 사용하고, 별도 수신은 새 ID를 갖는다.

필요한 환경 변수는 `FOODTECH_MOCK_BASE_URL`, `FOODTECH_MOCK_CLIENT_ID`, `FOODTECH_MOCK_CLIENT_SECRET`, `FOODTECH_MOCK_CA_FILE`, `FOODTECH_DATA_TABLE`, `FOODTECH_ORDERED_QUEUE_URL`이다. AWS 자격증명은 표준 프로필 또는 실행 역할로 전달한다. `python -m foodtech_collection_adapter --polls 1`로 실행한다.

Mock fixture를 바꿔 시험할 때는 `--cursor-key tonkatsu-process-high-v1`처럼 별도 커서 키를 지정한다. 기본 키 `default`는 기존 커서와 호환된다. 커서 항목에는 발견한 `Source + Device + Batch` 스트림을 함께 기록하여 다음 실행에서도 그 스트림의 순번 결손 만료를 확인한다. 수집 CLI는 특정 배치 하나로 고정된 drain 대신 실제 받은 레코드의 스트림을 사용한다.

첫 구현은 단일 합성 스트림의 최소 흐름이다. REST timeout 5초, 전송 오류 1회 재시도, 순번 결손 대기 5초, lease 30초를 초기 계약으로 둔다. 동시 다중 Task·장기 보류 상태·실패 복구·대규모 부하는 후속 적합성 검증 대상이다. 비정상 JSON과 식별자 누락의 S3 Ingestion DLQ는 이 슬라이스에 포함하지 않는다.

`--profile mockprofile-110 --cursor-key <fixture-key>`는 C# 제육볶음 Mock의 HTTPS/OAuth 응답을 별도 계약으로 검증한다. 수집된 1초 원표본은 기존 데이터 테이블의 `JEYUK#<fixtureId>` 파티션에 불변 원본으로 저장하고 FIFO Ordered 큐에 참조를 보낸다. 같은 fixture·초의 원본 내용이 바뀌면 거부하며, 동일 원본의 재전송은 같은 SQS 중복 ID를 사용한다. 커서는 `mockprofile-100`과 분리한다. 제육볶음 경로는 순번 재배열을 하지 않으며 Worker가 연속 초 순서대로 판정한다. 2026-11-15 개발용 AWS에서 C# 생성 표본 30건의 DynamoDB 저장·SQS FIFO 전달과 합성 항목 정리를 확인했다. HTTPS 수집과 AWS 저장을 한 번의 실행으로 결합한 검증은 후속 범위다.

기존 개발용 AWS 테이블·큐에서 실제 C# Mock의 HTTPS/OAuth 응답을 수집하려면 .NET 10 Mock을 Release로 빌드하고 `AWS_PROFILE=foodtech .venv/bin/python tests/live/process_flow_dynamodb_check.py --mock-wire`를 실행한다. 이 검증은 임시 인증서와 고유 합성 배치를 만들고 Mock의 첫 연결 단절 후 재시도, DynamoDB·SQS FIFO 저장, Worker 공정판정, 조리작업 실패 전이 및 재전달 멱등성을 확인한 뒤 합성 항목을 정리한다. 공유 개발용 FIFO 큐가 비어 있어야 시작한다. 2026-11-15 개발용 AWS에서 5건이 처리되어 `HIGH_PROCESS_FAILURE`와 작업 `FAILED`가 기록됐고 합성 항목 정리가 완료됐다. 배포된 Worker Task 역할·부하·장기 복구 검증은 별도다.

## 콤비오븐 합성 배치 연결

개발·시연용으로 고객 식사 배치를 생성하고 그 배치의 세 작업 ID와 장비 ID를 넣은 `mockprofile-120` fixture로 별도 C# Mock의 HTTPS 서버를 실행한다. `FOODTECH_BUSINESS_API_URL`, `FOODTECH_CUSTOMER_SESSION`과 위의 네 `FOODTECH_MOCK_*` 연결 변수를 설정하고 `python -m foodtech_collection_adapter.combi_import --meal-id <배치 ID> --device-id <장비 ID>`를 실행한다. 고객 세션은 인증된 시험 세션의 `ft_session` 값이며 비밀번호와 마찬가지로 취급한다. 공유하거나 저장소·로그에 기록하지 않고 시험 후 환경 변수에서 제거한다. 업무 API 주소는 HTTPS 또는 로컬 루프백 HTTP만 허용한다.

어댑터는 고객 권한으로 배치와 CSRF 값을 확인하고 전체 합성 스트림의 배치·작업·장비 ID, 순서와 크기를 검증한 뒤 기존 등록 API에 배치 버전을 포함해 한 번 제출한다. 중간에 배치가 변경되면 API가 거절한다. 이 경로는 즉시 생성된 합성 원표본을 일괄 연결하며 실시간 5초 수집, SQS/Worker 경로 또는 조리 안전·품질의 자동 승인을 의미하지 않는다. 실제 장비 호환성도 주장하지 않는다.

## 합성 MQTT 구독

`python -m foodtech_collection_adapter.mqtt --count 3 --timeout 30`은 별도 C# MQTT Mock의 TLS 인증서를 검증하고 MQTT 5·QoS 1로 고정 합성 Topic을 구독한다. `FOODTECH_MQTT_HOST`, `FOODTECH_MQTT_PORT`, `FOODTECH_MQTT_CA_FILE`, `FOODTECH_MQTT_USER`, `FOODTECH_MQTT_PASSWORD`, `FOODTECH_MQTT_CLIENT_ID`, `FOODTECH_DATA_TABLE`, `FOODTECH_ORDERED_QUEUE_URL`이 필요하다. Client ID는 `foodtech-collector-` 접두어로 고정하고 재연결 시 동일 ID를 사용한다. 원표본을 기존 DynamoDB Resequencer에 기록하고 SQS FIFO에 연결하며 지속 중 1초마다 결손 만료를 확인한다. 저장 성공 후에만 MQTT 확인 응답을 보낸다. 재전달은 새 수신으로 보존하고 업무 중복 판정은 기존 Worker가 수행한다.

로컬 통합 시험은 별도 C# Mock·TLS·인증 오류·Topic ACL·외부 발행 차단과 저장 실패 후 동일 세션 재전달, 합성 표본 세 건의 표준화 규칙을 확인한다. 표준화 Worker의 vendor-a MQTT 매핑 버전은 `mockprofile-300-a-map-v1`이다. MQTT 메시지의 AWS 실저장과 배포된 수집기 실행 역할, 프로세스 재시작·장기 단절과 63개 인스턴스 시험은 후속 검증이다.

## 합성 OPC UA Binary 구독

`python -m foodtech_collection_adapter.opcua --count 3 --timeout 30`은 명시적으로 신뢰한 클라이언트 인증서·개인키와 고정된 서버 인증서를 사용해 UA-TCP `Basic256Sha256/SignAndEncrypt`로 연결한다. `FOODTECH_OPCUA_ENDPOINT`, `FOODTECH_OPCUA_CLIENT_CERT`(PEM), `FOODTECH_OPCUA_CLIENT_KEY`(PEM), `FOODTECH_OPCUA_SERVER_CERT`(DER), `FOODTECH_DATA_TABLE`, `FOODTECH_ORDERED_QUEUE_URL`을 설정한다. 서버 인증서의 유효기간·애플리케이션 URI·호스트와 고정 인증서의 일치를 확인한다. Browse·Read로 합성 장비를 확인한 뒤 `RecordSnapshot`을 Subscription으로 받아 원표본을 기존 Resequencer·FIFO 흐름으로 전달한다. 표준화 매핑 버전은 `mockprofile-203-a-map-v1`이다.

현재는 vendor-a 단일 합성 인스턴스의 연결 경로다. UA 클라이언트가 Publish 응답을 자동 확인하므로 수집기는 이를 저장 완료 증거로 사용하지 않는다. 알림과 주기적 `AvailableSnapshots` 조회를 이용해 이미 발생한 전체 합성 원표본을 순번·배치·내용 해시로 대조하고 아직 저장하지 않은 항목을 처리한다. 저장 실패 후 새 수집 실행은 처음부터 다시 읽으며 기존 항목의 중복 여부는 기존 품질 파이프라인에서 판정한다. 실행 중 서버 재시작 시 SDK 재연결 후 같은 fixture를 재생해 누락된 항목을 복구한다. 같은 실행에서 이미 본 순번의 내용이 달라지거나 중간 순번이 없으면 오류로 중단한다.

로컬 시험은 부분 저장 실패 뒤 재실행과 서버 프로세스 종료·재시작 후 세 합성 원표본을 확인한다. 이는 합성 Mock이 원본 fixture를 보존하고 동일 인증서로 복귀하는 조건에서의 복구이며, 일반 OPC UA 서버의 이력·UA Republish·장애 시 정확히 한 번 처리를 보장하지 않는다. 장기 장애, 서버 원본의 유실·변경, 실제 AWS 저장, 부하·나머지 프로파일과 인증서 발급·교체·폐기 절차는 후속 구현·검증이다.

