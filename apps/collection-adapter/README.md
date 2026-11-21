# 첫 수집 어댑터

공통 DynamoDB 수집기는 원본·대기 항목과 같은 트랜잭션에서 스트림별 `SOURCE#REVISION`을 증가시킨다. 중복 수신도 별도의 원본이므로 버전이 증가한다. 모든 원본 작성자가 이 계약을 사용하도록 배포되었는지는 별도 검증해야 하며 구버전 작성자와 직접 데이터 수정은 감지 보장을 제공하지 않는다.

`mockprofile-100`을 커서 0부터 단일 실행에서 수집하고 서버가 종료를 명시하면, 한 스트림의 원본 바이트별 SHA-256과 건수·종료 커서를 불변 수집 기록으로 남긴다. 재시작된 수집, 여러 스트림, 원본 격리 또는 1,000건을 넘는 경우에는 기록을 생성하지 않는다. 이 기록은 수집기가 관찰한 종료 스냅샷일 뿐 품질 통과나 표준화 완료를 뜻하지 않는다. 2026-11-21 개발 DynamoDB에서 합성 기록·원본 대조와 정리를 확인했다. `AWS_PROFILE=foodtech .venv/bin/python tests/live/collection_receipt_dynamodb_check.py`로 재실행한다.

합성 Mock의 HTTPS REST/JSONL을 OAuth 2.0 Client Credentials로 한 번 또는 지정 횟수만큼 조회한다. 원본과 순번 대기 항목을 DynamoDB에 보존한 뒤 `Source + Device + Batch`별 5초 복원 결과를 SQS FIFO에 전송한다. 같은 전송 호출의 재시도는 같은 `ingestion_event_id`를 사용하고, 별도 수신은 새 ID를 갖는다.

필요한 환경 변수는 `FOODTECH_MOCK_BASE_URL`, `FOODTECH_MOCK_CLIENT_ID`, `FOODTECH_MOCK_CLIENT_SECRET`, `FOODTECH_MOCK_CA_FILE`, `FOODTECH_DATA_TABLE`, `FOODTECH_ORDERED_QUEUE_URL`이다. AWS 자격증명은 표준 프로필 또는 실행 역할로 전달한다. `python -m foodtech_collection_adapter --polls 1`로 실행한다.

Mock fixture를 바꿔 시험할 때는 `--cursor-key tonkatsu-process-high-v1`처럼 별도 커서 키를 지정한다. 기본 키 `default`는 기존 커서와 호환된다. 커서 항목에는 발견한 `Source + Device + Batch` 스트림을 함께 기록하여 다음 실행에서도 그 스트림의 순번 결손 만료를 확인한다. 수집 CLI는 특정 배치 하나로 고정된 drain 대신 실제 받은 레코드의 스트림을 사용한다.

첫 구현은 단일 합성 스트림의 최소 흐름이다. REST timeout 5초, 전송 오류 1회 재시도, 순번 결손 대기 5초, lease 30초를 초기 계약으로 둔다. 동시 다중 Task·장기 보류 상태·실패 복구·대규모 부하는 후속 적합성 검증 대상이다. REST 기본 프로파일의 비정상 JSON과 순서·업무 식별자 누락은 아래 수집 격리 경로로 보존한다.

2026-11-20 `AWS_PROFILE=foodtech FOODTECH_COOKING_TABLE=foodtech-cooking-jobs-dev .venv/bin/python tests/live/combi_mock_dynamodb_check.py`로 C# 합성 콤비 Mock의 인증된 HTTPS 응답을 수집기와 로컬 업무 API를 거쳐 개발용 DynamoDB에 저장했다. 세 단계 각 5초의 단축 fixture로 수집 중 15건과 최종 원본 해시를 확인했고 합성 파티션은 정리 후 잔존 여부를 검사했다. 고객 세션은 합성이며 실시간 대기 모드, 실제 Cognito, 배포된 서비스 및 전체 조리시간·장애·부하는 이 시험에 포함하지 않는다. 실행 전 .NET 10 Release Mock 빌드와 `dotnet`, `openssl` PATH가 필요하다.

`--profile mockprofile-110 --cursor-key <fixture-key>`는 C# 제육볶음 Mock의 HTTPS/OAuth 응답을 별도 계약으로 검증한다. 수집된 1초 원표본은 기존 데이터 테이블의 `JEYUK#<fixtureId>` 파티션에 불변 원본으로 저장하고 FIFO Ordered 큐에 참조를 보낸다. 같은 fixture·초의 원본 내용이 바뀌면 거부하며, 동일 원본의 재전송은 같은 SQS 중복 ID를 사용한다. 커서는 `mockprofile-100`과 분리한다. 제육볶음 경로는 순번 재배열을 하지 않으며 Worker가 연속 초 순서대로 판정한다. 2026-11-21 개발용 AWS에서 실시간 C# Mock의 HTTPS/OAuth 응답을 0.5초 간격으로 반복 조회해 DynamoDB·SQS FIFO·Worker까지 1초 합성 표본 30건을 전달했다. 걸림 5초 경고·30초 실패, 재전달 멱등성과 고유 합성 원본·커서 정리를 확인했다. 재실행은 Mock Release 빌드 후 공유 개발 큐가 비어 있을 때 `AWS_PROFILE=foodtech .venv/bin/python tests/live/jeyuk_dynamodb_check.py --mock-wire`로 수행한다. 이 시험은 로컬 Mock·수집기·Worker와 개발 AWS를 사용하며 배포된 Task 역할, 5초 운영 조회주기의 성능과 부하 검증은 포함하지 않는다.

실시간 제육볶음 Mock은 `--profile mockprofile-110 --cursor-key <fixture-key> --until-complete`로 수집한다. 기본 5초마다 새로 공개된 1초 원표본을 저장하고 Mock의 명시적 종료 표시까지 계속한다. 기본 제한시간은 1,800초이며 `--max-runtime`으로 조정한다. 시간 초과나 저장 실패 시 마지막으로 저장한 커서를 유지한다. Mock을 재시작해 재생 시점이 이전 커서보다 앞서면 충돌로 중단하며, 재시작 후 이력 자동 복구를 뜻하지 않는다. 0번 커서에서 단일 fixture의 연속 표본을 수집하고 명시적인 종료 표시를 받은 경우에만 원본 순서·내용의 SHA-256과 건수를 완료 기록으로 저장한다. 중간 커서에서 재개했거나 표본 누락·변경·fixture 혼합이 있으면 완료 기록을 만들지 않는다. 이 기록은 안전 판정이나 AI 실행 승인이 아니다.

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

MQTT 수집 중 TCP 연결이 끊어지면 동일 클라이언트 ID로 기한 내 재연결하고, 끊어진 동안에도 이미 수신한 스트림의 순번 대기 처리를 계속한다. 브로커가 이전 세션을 복원하지 못하면 누락이 없다고 판단하지 않고 오류로 중단한다. 같은 실행에서 이미 수신한 패킷 ID와 내용의 `DUP` 재전달은 원본으로 다시 저장하지만 수집 목표 건수에는 다시 세지 않는다. 재전달은 중복될 수 있으며 정확히 한 번 처리나 브로커 재시작 후 이력 복구를 보장하지 않는다. 로컬 C# Mock과 TLS 프록시의 실제 연결 단절·복구로 세 건 수신을 검증했다.

로컬 시험은 부분 저장 실패 뒤 재실행과 서버 프로세스 종료·재시작 후 세 합성 원표본을 확인한다. 이는 합성 Mock이 원본 fixture를 보존하고 동일 인증서로 복귀하는 조건에서의 복구이며, 일반 OPC UA 서버의 이력·UA Republish·장애 시 정확히 한 번 처리를 보장하지 않는다. 장기 장애, 서버 원본의 유실·변경, 실제 AWS 저장, 부하·나머지 프로파일과 인증서 발급·교체·폐기 절차는 후속 구현·검증이다.


### 합성 Session-less Browse/Read

`python -m foodtech_collection_adapter.opcua_json --count 3 --timeout 30`은 `FOODTECH_UA_JSON_URL`, `FOODTECH_MOCK_CLIENT_ID`, `FOODTECH_MOCK_CLIENT_SECRET`, `FOODTECH_MOCK_CA`, `FOODTECH_DATA_TABLE`, `FOODTECH_ORDERED_QUEUE_URL`을 사용한다. 인증된 프로젝트 전용 HTTPS Browse/Read로 합성 프로파일 `mockprofile-200-a-001`을 수집하고 `mockprofile-200-a-map-v1`으로 표준화한다. 원본 이력을 다시 읽어 저장 실패 후 재시도하며 기존 중복 판정에 의존한다. 서버 프로세스가 유지하는 합성 이력으로, 표준 OPC UA 이력 서비스나 실제 제조사 호환성을 뜻하지 않는다.

### 수집 전 격리 (REST 기본 프로파일)

`FOODTECH_INGESTION_DLQ_BUCKET`에 전용 버킷을 설정하면 JSON 구문·중복 키 오류와 필수 식별자 누락 원본을 S3에 암호화 저장하고 수집 테이블에 별도 `INGESTION_DLQ#.../MANIFEST`를 기록한다. 두 저장이 모두 성공한 뒤에만 커서를 갱신한다. 같은 fixture 커서 위치와 바이트의 재시도는 같은 manifest에 연결된다. 원본은 2 MB 이하이며 이를 초과하거나 버킷 설정·저장이 실패하면 커서를 전진시키지 않는다. 일반 이벤트나 품질 평가 레코드를 만들지 않는다. REST 경로는 `mockprofile-100`의 NDJSON 레코드를 대상으로 잘못된 UTF-8도 원본 바이트로 격리한다. 파일 전체·압축 오류와 recordId가 없는 fallback 처리, 실행 역할과 AWS 실환경 검증은 후속 범위다.

REST·MQTT JSON은 필수 식별자가 있더라도 각 vendor 원천 스키마 검증에 실패하면 원본을 격리한다. 격리 저장 후 REST는 커서를 갱신하고 MQTT는 QoS 1 메시지를 확인한다. 격리 저장 실패나 버킷 미설정 시 진행하지 않아 재수집·재전달을 허용한다. 정상 스키마에서 허용되지 않은 원천·인스턴스 연결은 별도 연결 오류로 중단한다.

같은 버킷이 설정된 합성 OPC UA Binary·HTTPS Browse/Read 수집기는 `AvailableSnapshots`의 잘못된 형식, 스키마·순서·소스 불일치 또는 기존 이력 변경을 발견하면 복구 값과 `INVALID_SOURCE_HISTORY` manifest를 보존한 뒤 수집을 중단한다. 같은 인스턴스·내용의 재시도는 같은 manifest로 연결하며 저장 실패 시에도 진행하지 않는다. Binary는 읽은 문자열의 UTF-8 바이트를 저장하고 HTTPS는 디코딩된 배열을 JSON으로 다시 직렬화하므로 HTTP 원본 바이트가 아니다. 전체 복구 값의 한도는 2 MB이며 초과분과 비문자열 Binary 값은 격리하지 못한 채 중단한다. 실제 AWS 저장·실행 역할 및 재처리는 후속 검증이다.

Binary Mock 재시작 후 복구 이력이 일시적으로 짧아지면 이미 본 접두 내용은 검증하되 기존 진행 상태를 유지하고 이후 자료를 기다린다. 전용 격리 버킷이 개발 스택에 배포되면 `AWS_PROFILE=foodtech .venv/bin/python tests/live/opcua_quarantine_check.py`로 무작위 합성 이력의 객체·manifest·동일 재시도와 정리를 확인할 수 있다. 이 스크립트는 해당 스택 출력의 전용 버킷이 없으면 쓰기 전에 중단한다. 2026-11-20 개발 스택에는 이 출력이 없어 실제 저장 시험을 수행하지 않았다.

합성 HTTPS Browse/Read도 재시작 후 짧아진 이력이 기존 접두 내용과 일치하면 중복 수집 없이 기존 진행 상태를 보존하고 이후 자료를 기다린다. 내용이 변경되거나 새로운 자료가 제한시간 안에 복구되지 않으면 각각 오류 또는 시간 초과로 중단한다. 이는 합성 복구 이력에 대한 동작이며 실제 장비의 이력 복구 보장은 아니다.

2026-11-21 로컬 C# HTTPS Mock을 두 원본 저장 직후 실제로 재시작하고 같은 인증서·fixture로 다시 연결했다. 인증 토큰을 다시 발급받고 1번부터 재생되는 이력이 따라온 뒤 순번 1·2·3을 각각 한 번씩 수집했다. AWS 저장과 장시간 장애는 이 검증에 포함되지 않는다.

`mockprofile-300-a-001` MQTT 구독에도 같은 버킷 설정을 사용할 수 있다. UTF-8 디코딩이나 JSON 파싱 실패, 필수 식별자 누락은 원본 바이트와 manifest를 저장한 뒤 QoS 1 ACK를 보낸다. 미설정·저장 실패·2 MB 초과 시 ACK하지 않는다. 재전달은 새로운 수신 ID로 보존될 수 있으며, 객체 저장 후 manifest 실패 시 미참조 객체가 남을 수 있다. MQTT 시험은 프로세스 메모리 기반 테스트 저장소를 사용하며 AWS 실저장은 별도 검증이다.

### vendor-b 기본 REST 인스턴스

`--instance mockprofile-100-b-001`로 별도 vendor-b JSON 원천을 수집한다. 해당 원천 JSON Schema를 먼저 검증하고 화씨·밀리초·정수 상태를 표준 단위와 상태로 결정적으로 변환한다. 출력 비율은 `OUTPUT_PERCENT`로 기록하고 RPM으로 변환하지 않는다. 수집 원본은 그대로 보존하며 vendor-a와 커서를 분리한다. 합성 제한상 화씨는 0.1°C로 정확히 변환 가능한 값만 처리하며 그 외는 중단한다. 로컬 HTTPS 시험 범위이며 AWS 실저장·나머지 vendor 및 프로파일은 후속이다.

### vendor-c 기본 REST 인스턴스

`--instance mockprofile-100-c-001`로 PascalCase·약어와 중첩 측정값을 쓰는 세 번째 합성 원천을 수집한다. 원천 Schema 검증 후 오프셋 시각을 UTC로 정규화하고 분 단위 경과값을 초로 변환한다. `0~10` 순서형 속도는 `SPEED_LEVEL`로 기록하며 출력 비율이나 RPM으로 환산하지 않는다. `Y/N` 알람과 빈 오류 문자열은 명시적으로 변환하고 원본 JSON은 유지한다. fixture의 분 단위 값은 표시 정밀도 때문에 변환 결과가 5초와 미세하게 다를 수 있으며 이를 보간하지 않는다. vendor별 커서는 분리된다. 실제 AWS 저장과 나머지 연계 방식·프로파일 검증은 후속이다.

### MQTT vendor-b·c 인스턴스

MQTT 수집 명령의 `--instance mockprofile-300-b-001` 또는 `--instance mockprofile-300-c-001`로 각 합성 원천을 선택한다. 선택된 인스턴스의 정확한 Topic만 구독하며 원천 Schema·출처·장비 ID를 확인한다. vendor-b의 출력 비율과 vendor-c의 속도 단계는 서로와 RPM으로 환산하지 않는다. QoS 1 확인은 원본 수집 또는 격리 저장 후에만 보낸다. 현재 로컬 TLS 시험 범위이며 AWS 실저장과 다른 프로파일은 별도로 남아 있다.

합성 Session-less Browse/Read 수집 명령의 `--instance mockprofile-200-b-001` 또는 `mockprofile-200-c-001`로 vendor 원천을 선택할 수 있다. 읽은 인스턴스 식별자와 원천 Schema를 검사하고 저장 실패 후 합성 이력에서 재조회한다. vendor-b의 출력 비율과 vendor-c의 속도 단계는 RPM으로 바꾸지 않는다. 이는 프로젝트 전용 HTTPS 계약이며 OPC UA 표준 JSON 서비스나 실제 제조사 호환 구현이 아니다. AWS 실저장은 후속이다.

볶음·교반 장비의 합성 Session-less Browse/Read 계열은 `--instance mockprofile-210-a-001`, `mockprofile-210-b-001`, `mockprofile-210-c-001`을 지원한다. 각 원천 스키마에서 합성 장비 유형을 확인하고, 온도는 `POT_TEMPERATURE`로 표준화한다. vendor-a의 RPM, vendor-b의 출력 비율, vendor-c의 속도 단계는 별도로 유지하며 서로 환산하지 않는다. 인증·순서 복구·원천 이력 위반 격리는 기존 프로젝트 전용 HTTPS 계약과 같다. 이 경로는 프로젝트 합성 계약이며 실제 제조사 호환성을 주장하지 않는다. 개발 AWS 실저장 검증 범위는 아래 기록을 따른다.

콤비오븐의 합성 Session-less Browse/Read 계열 `mockprofile-220-a-001`, `-b-001`, `-c-001`은 챔버 온도, 기후 설정 비율, 팬 단계와 습도 출처를 구분한다. 실제 습도만 `RELATIVE_HUMIDITY`로 표준화하며 제어기 추정값은 `HUMIDITY_ESTIMATE_PERCENT`로, 미지원은 수치 없이 기록한다. vendor-c의 중복 팬 단계가 서로 다르면 수집을 거부한다. 이 경로는 프로젝트 전용 합성 HTTPS 계약이다. 개발 AWS 실저장 검증 범위는 아래 기록을 따른다.

OPC UA Binary 수집 명령의 `--instance mockprofile-203-b-001` 또는 `mockprofile-203-c-001`은 각각의 합성 namespace·장비 ID와 원천 Schema를 확인한다. SDK 인증서 신뢰·SignAndEncrypt·읽기 전용 Subscription과 합성 이력 재조회는 기존 인스턴스와 동일하다. vendor-b의 출력 비율과 vendor-c의 순서형 속도 단계는 RPM으로 환산하지 않는다. 원천 이력과 합성 Node는 프로젝트 시험 계약이며 실제 제조사 사양이나 UA 표준 이력 서비스라는 주장은 아니다. AWS 실저장은 후속이다.

### 합성 REST XML

`--profile mockprofile-101 --instance mockprofile-101-a-001 --polls 4`는 인증된 HTTPS에서 요청마다 XML 레코드 한 건을 읽는다. 원천 XML은 XSD로 검증하고 DTD·외부 엔터티를 차단하며 UTF-8만 허용한다. 유효 원본은 XML 형태 그대로 수신 기록에 보존하고 표준화하며, 순번 복원과 품질 격리는 JSON 경로와 같다. 파싱·XSD 실패는 수집 격리 저장이 끝난 뒤 커서를 갱신한다. 기본 XML vendor-a·b·c를 `--instance`로 선택하며 커서는 서로 분리된다. vendor-b는 화씨·밀리초·정수 상태·출력 비율, vendor-c는 오프셋 시각·분·축약 상태·속도 단계와 중첩 확장을 적용한다. 출력 비율과 단계값을 RPM으로 환산하지 않는다. AWS 실저장은 후속이다.

### 합성 MQTT XML

`--instance mockprofile-301-a-001`, `mockprofile-301-b-001`, `mockprofile-301-c-001`은 인스턴스별 Topic으로 XML 메시지를 수집한다. 각 원천의 XSD와 UTF-8을 검사하고 DTD·외부 엔터티를 차단한다. 정상 원본은 XML 그대로 기록하며 스키마 오류는 격리 저장 후 QoS 1 확인 응답을 보낸다. 저장이 실패하면 확인하지 않아 재전달을 허용한다. 로컬 TLS 검증 범위이며 AWS 실저장과 나머지 프로파일은 후속이다.


콤비오븐 `combi_import`는 즉시 반환과 `serve --realtime`의 5초 단위 공개를 모두 처리한다. 완료 전 빈 응답은 재조회하며 전체 세 단계의 원표본이 검증된 후에만 고객 배치에 연결한다. 기본 제한시간은 5,100초이고 제한시간 초과 시 부분 자료를 제출하지 않는다. Mock 재시작 시 재생 시점이 초기화되므로 이전 cursor가 아직 공개되지 않았다면 충돌로 중단한다. 기본 경로는 완료 후 일괄 연결하며, 아래 `--live` 옵션은 수집 중 고객 화면 표시를 추가한다.

`combi_import --live`를 사용하면 각 원표본 묶음을 고객 배치의 수집 중 영역에도 기록한다. 예: `uv run --no-sync python -m foodtech_collection_adapter.combi_import --meal-id <배치 ID> --device-id <합성 장비 ID> --live`. 기존 환경변수로 Mock과 고객 세션을 설정하고 Mock은 `serve --realtime`으로 실행한다. 고객 화면은 5초마다 최근 합성 표본, 마지막 수신 시각과 완전한 5초 구간의 현재·최저·최고·평균 온도 및 목표 범위 이탈 초를 조회한다. 수집 중 표본은 안전·공정 판정이 아니며, 완료 연결 전에 저장된 모든 표본과 최종 원본의 내용·순서를 대조한다. 같은 자료 재전송은 중복으로 처리하고 다른 내용이나 건너뛴 순번은 거부한다. 작업 상태가 수집 중 바뀌어도 최종 연결 직전의 버전을 다시 확인한다. 장시간 실행 중 세션이 만료되면 수집이 중단된다. 재로그인 후 동일 Mock과 배치로 다시 실행하면 서버에 저장된 스냅샷과 Mock 원본을 처음부터 순서대로 대조한 뒤 나머지를 이어 수집한다. 원본이 다르거나 저장된 지점보다 일찍 끝나면 중단한다. AWS 실저장·장애 복구 실검증은 아직 수행하지 않았다.

콤비오븐 `--live` 수집은 HTTPS 대기 요청으로 합성 Mock의 단계·문·치명 알람·환기 변화를 일반 5초 묶음보다 먼저 수신한다. 순번 중간의 원표본도 함께 수신하여 재개와 중복 판정이 동일하게 유지된다. 일반 묶음 전에 전달되는 이탈 원표본으로 플랫폼의 기존 공정 감지도 갱신한다. 고객 화면 갱신 주기와 AWS 종단간 지연은 별도 검증 대상이다.

2026-11-21 개발 AWS에서 `mockprofile-210`·`220`의 각 vendor a/b/c에 대해 로컬 C# Mock의 HTTPS 응답부터 DynamoDB Resequencer·SQS FIFO·Worker 및 격리 큐까지 실검증했다. 각 조합은 고유 합성 표본 3건을 순서대로 처리했고 정상 2건·격리 1건을 확인한 뒤 합성 항목을 정리했다. 콤비오븐의 기후 설정·실측 습도·팬 단계와 볶음 장비의 솥 온도 구분도 확인했다. 재실행은 Mock Release 빌드 후 공유 개발 큐가 비어 있는 상태에서 `AWS_PROFILE=foodtech .venv/bin/python tests/live/opcua_json_aws_check.py`를 사용한다. 이 결과는 로컬 프로세스를 이용한 검증이며 배포된 Task 역할, 장기 복구와 부하를 검증하지 않는다.

제육볶음 원표본 최초 저장과 `SOURCE#REVISION` 증가는 DynamoDB 트랜잭션으로 함께 처리한다. 동일 원본 재전송은 버전을 올리지 않고 같은 초의 다른 내용은 거부한다. 원천 변경 검증은 모든 작성자가 이 계약을 사용하는 경우에만 유효하다. 수집 실행 역할에는 데이터 테이블의 `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:GetItem`과 큐 전송 권한이 필요하다.

OPC UA Binary 수집기는 `--instance mockprofile-213-{a,b,c}-001`과 `mockprofile-223-{a,b,c}-001`도 지원한다. 각 장비 계열의 namespace·인스턴스·원천 Schema를 확인하고 저장 실패 시 합성 이력을 다시 읽는다. 볶음 온도는 `POT_TEMPERATURE`로, 콤비오븐 기후 설정·습도 출처·팬은 각 의미를 유지해 표준화한다. 실제 제조사 호환성은 프로젝트 범위가 아니다.

2026-11-21 개발 DynamoDB·SQS에서 C# OPC UA Binary Mock의 `mockprofile-213`·`mockprofile-223` 각 vendor-a/b/c를 구독하고 수집·순서 유지·표준화·격리 및 합성 시험 데이터 정리를 확인했다. 재실행: `AWS_PROFILE=foodtech uv run --no-sync python tests/live/opcua_binary_equipment_aws_check.py`. 실제 제조사 호환성 검증은 프로젝트 범위가 아니다.

MQTT JSON 수집기는 `mockprofile-310-{a,b,c}-001` 볶음·교반과 `mockprofile-320-{a,b,c}-001` 콤비오븐도 지원한다. 인스턴스별 Topic·원천 스키마를 검사하고, 잘못된 입력은 해당 계열로 격리한 후 승인한다. 장비별 온도·기후·습도·팬의 의미를 유지하며 vendor별 상대 출력·속도 단계는 RPM으로 환산하지 않는다. 합성 계약이며 AWS 실저장은 별도 검증이다.

MQTT CSV 수집기는 `mockprofile-312-{a,b,c}-001` 볶음·교반과 `mockprofile-322-{a,b,c}-001` 콤비오븐을 지원한다. 장비별 고정 열과 값 유형을 검사하고 습도 미지원은 빈 CSV 셀에서 `null`로 표현한다. 공식 CSV 수식 위험 문자 차단을 유지하며 격리 저장이 성공한 후에만 MQTT 수신 확인을 보낸다. AWS 실저장은 별도 검증이다.
