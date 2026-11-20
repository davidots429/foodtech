# C# 합성 Mock: 첫 REST/JSON 프로파일

`mockprofile-100-a-001`은 실제 제조사 호환 구현이 아닌 `SYNTHETIC` 시험 프로파일이다. .NET 10의 ASP.NET Core 공유 프레임워크로 HTTPS GET과 OAuth 2.0 Client Credentials의 필요한 부분만 제공한다. 장비 제어·쓰기 기능과 GUI는 없다.

```bash
dotnet run --project apps/mock-control/MockControl.csproj -- emit \
  --fixture fixtures/mockprofile-100-a-001.json --output /tmp/foodtech-mock.jsonl
dotnet run --project apps/mock-control/MockControl.csproj -- hash \
  --fixture fixtures/mockprofile-100-a-001.json
```

서버는 `FOODTECH_MOCK_CLIENT_ID`, `FOODTECH_MOCK_CLIENT_SECRET`, `FOODTECH_MOCK_CERT_PASSWORD`를 환경에서 읽는다. 개발용 인증서의 공개 CA 파일을 수집기의 `FOODTECH_MOCK_CA_FILE`에 지정한다. TLS 검증을 우회하지 않는다.

```bash
dotnet run --project apps/mock-control/MockControl.csproj -- serve \
  --fixture fixtures/mockprofile-100-a-001.json --cert /path/to/dev-cert.pfx --port 8443 --disconnect-once
```

`POST /oauth2/token`은 Basic Client 인증과 `client_credentials` grant를 확인한다. `GET /v1/records?cursor=0`은 읽기 전용 JSONL을 도착 순서 `1, 3, 2`로 반환하고 `X-Next-Cursor`를 제공한다. `--disconnect-once`는 첫 데이터 GET의 실제 HTTP 연결을 종료하고 다음 요청부터 복구한다. `GET /health`는 상태 확인용이다.

첫 구현의 생성기 버전·seed·논리 시작 시각은 fixture에 고정되어 있다. `hash`는 이 프로파일의 제한된 필드 집합에 대한 C# 정규화·SHA-256 교차 검증 명령이다. 일반 JSON 정규화기는 아니다.

CI의 `tests/integration/test_mock_protocol.py`는 빌드한 C# Mock을 임시 HTTPS 인증서로 실행하고, 잘못된 OAuth 자격증명·인증 없는 조회·쓰기 요청의 거부를 확인한다. 이어 실제 HTTPS 응답을 Python 수집 클라이언트로 읽어 연결 단절 후 재시도, 순번 `1, 3, 2`의 `1, 2, 3` 복원, Worker의 정상·격리 판정과 기대 오류 코드를 대조한다. 이 검사는 로컬 프로세스 간 계약 검증이며 DynamoDB·SQS를 거치는 실저장 검증은 별도다.

## 제육볶음 합성 시뮬레이션

C# Mock의 `simulate-jeyuk` 명령은 승인된 소형 볶음·교반 합성 fixture의 구간을 1초 원표본 JSONL로 전개한다. 출력에는 `mockprofile-110-a-001`, 조리량, 표면온도, 실제·목표 RPM, 모터 부하율과 연속 초 번호가 들어간다. 공정판정은 Mock이 수행하지 않고 플랫폼의 규칙이 수행한다.

```bash
dotnet run --project apps/mock-control/MockControl.csproj -- simulate-jeyuk \
  --fixture fixtures/jeyuk-small-process-v1.json --output /tmp/foodtech-jeyuk.jsonl
```

`tests/integration/test_jeyuk_mock.py`는 C# 파일 출력을 플랫폼 공정 규칙에 넣어 5초 걸림 경고와 30초 실패를 확인한다. 같은 fixture를 `serve --fixture fixtures/jeyuk-small-process-v1.json`에 주면 기존 HTTPS/OAuth 읽기 전용 `/v1/records`에서 `X-Profile-ID: mockprofile-110`과 함께 30개 원표본을 조회할 수 있다. 초 번호는 논리 시각이며 현재 조회는 실시간 1Hz 송신이 아니라 fixture 전체의 일괄 반환이다. `tests/integration/test_jeyuk_https.py`가 실제 TLS·인증·프로파일 검증·수집·공정판정을 확인한다. C# 생성 표본의 DynamoDB·SQS 실저장과 고객 조리작업 연결은 개발용 AWS에서 검증했다.

`jeyuk-small-safety-v1.json`은 C# Mock이 혼합물 온도·교정 상태·최저온도 센서 위치·마지막 재료 투입 완료 사건을 원표본에 담는 별도 합성 시나리오다. 기존 걸림 fixture의 필드 계약은 유지한다. 자동 안전판정은 Mock이 아닌 플랫폼에서 수행하며, Mock 값은 실제 식품 안전을 보증하지 않는다.

## 콤비오븐 합성 시뮬레이션

`simulate-combi`는 하나의 합성 식사 배치에 서로 다른 밥·전환·고등어 작업 ID를 지정하고, 각 단계의 온도·기후 설정·습도 출처·팬·문을 1초 원표본과 5초 온도 요약으로 출력한다. 원표본에는 고정 논리 시작시각, 사건 ID, 발생·수신시각, 전체 순번, 단계별 초, 스키마·프로그램 버전과 `SYNTHETIC` 출처가 들어간다. 요약에는 현재·최소·최대·평균 온도와 목표 구간 이탈 초가 들어간다. 생성기 버전과 seed는 실행 결과에 기록하며 현재 정상 fixture는 난수를 사용하지 않는다.

`synthetic-combi-v2`는 각 구간에 치명 알람·환기 플랩 지원 여부와 선택적 열림·닫힘 사건을 명시한다. 사건이 있는 구간은 1초여야 하며 지원하지 않는 플랩의 사건은 거부한다. v1 원표본은 플랫폼이 계속 읽지만 전환 원천 판정에 필요한 이 필드가 없으므로 자동 준비 판정에 사용하지 않는다.

```bash
dotnet run --project apps/mock-control/MockControl.csproj -- simulate-combi \
  --fixture fixtures/combi-normal-medium-v1.json --output /tmp/combi-raw.jsonl \
  --aggregates /tmp/combi-5s.jsonl
```

정상 중형 fixture는 밥 1,800초, 습도 미지원 건열 전환 180초, 고등어 720초를 순서대로 재현한다. 동일 fixture로 기존 `serve` 명령을 실행하면 인증된 HTTPS GET으로 `mockprofile-120` 원표본을 제공한다. 수집 클라이언트는 해당 프로파일 헤더를 확인하고 별도 검증기는 명시된 식사 배치·장비·세 작업 ID와 순번·단계·1초 시각을 확인한다. 현재 요청은 원표본 전체를 반환하며 5초 주기 실시간 송신, AWS 수집·고객 작업 연결 및 다른 크기·장애 fixture는 후속 작업이다. 판정은 플랫폼에서 수행하며 실제 제조사 규격이나 식품 안전을 주장하지 않는다.

## 합성 MQTT Mock

`serve-mqtt --fixture fixtures/mockprofile-300-a-001.json --cert <pfx> --port <포트>`는 MQTTnet 5.2.0.1603 기반 MQTT 5 서버를 로컬 루프백의 TLS로만 연다. `FOODTECH_MQTT_USER`, `FOODTECH_MQTT_PASSWORD`, `FOODTECH_MOCK_CERT_PASSWORD`를 환경 변수로 제공한다. 인증된 `foodtech-collector-` 접두 클라이언트만 연결하고 `foodtech/synthetic/mockprofile-300-a-001/observations`를 QoS 1로 구독할 수 있다. 와일드카드·다른 Topic·외부 발행은 거부한다. 첫 구독 후 합성 fixture 세 건을 5초 간격으로 한 번 발행하며, 메시지 두 번째 연결 시 처음부터 다시 재생하지 않는다.

이는 `mockprofile-300`의 vendor-a JSON 단일 논리 인스턴스이며 실제 장비 호환성을 주장하지 않는다. 세션은 Mock 프로세스 메모리에서 유지되고 수집기의 세션 만료 초기값은 300초다. 프로세스 재시작 이후 복구·장기 단절·다중 인스턴스, 다른 vendor·XML·CSV, AWS에서의 실제 연결과 부하 검증은 아직 남아 있다.

## 합성 OPC UA Binary Mock

`serve-opcua --fixture fixtures/mockprofile-203-a-001.json --pki <절대 경로> --port <포트>`는 OPC Foundation .NET SDK 2.0.2 기반 UA-TCP 서버를 연다. `SignAndEncrypt` 정책만 제공하고 미신뢰 클라이언트 인증서를 자동 수락하지 않는다. 시험 전에 수집기의 공개 인증서 DER을 PKI 경로의 `trusted/certs`에 넣고, 수집기에도 서버의 RSA 공개 인증서 DER을 별도로 신뢰시킨다. 개인키가 포함된 PKI 디렉터리는 저장소에 넣지 않는다.

합성 namespace `urn:foodtech:synthetic:opcua:203:a:v1`의 `SyntheticDevice` 아래 읽기 전용 온도·회전수·순번·식별자 등의 UA Node를 Browse·Read할 수 있다. 일관된 한 건의 수집을 위해 `RecordSnapshot` 문자열 Node에 같은 합성 레코드를 담으며 UA Binary Subscription으로 전달한다. 최초 구독부터 5초 간격으로 세 합성 표본이 바뀐다. 이는 표준 30200 정보모델이나 실제 제조사 사양을 구현했다는 주장이 아니다. 일반 UA Republish·장기 장애, 다른 203 fixture와 UA JSON·다른 프로파일은 별도 구현·검증 대상이다.

`AvailableSnapshots` 읽기 전용 문자열 Node는 현재까지 발생한 합성 원표본만 JSON 문자열 배열로 반환한다. 전체 배열의 UTF-8 크기는 최대 2,000,000바이트다. 서버 재시작 시 동일 fixture를 처음부터 재생하므로 수집기는 이미 처리한 순번을 대조하고 누락된 자료를 다시 읽을 수 있다. 이 Node는 프로젝트 합성 복구 계약이며 OPC UA 표준 HistoryRead 서비스나 실제 장비의 이력 제공을 뜻하지 않는다.


### 합성 Session-less Browse/Read (프로파일 200)

`serve-ua-json --fixture fixtures/mockprofile-200-a-001.json --cert <PFX> --port <포트>`는 프로젝트 전용 HTTPS JSON 계약을 제공한다. `FOODTECH_MOCK_CERT_PASSWORD`, `FOODTECH_MOCK_CLIENT_ID`, `FOODTECH_MOCK_CLIENT_SECRET`을 환경변수로 설정한다. OAuth client credentials 후 `/ua-json/v1/browse`와 `/ua-json/v1/read?node=DeviceId` 또는 `AvailableSnapshots`를 읽는다. 쓰기와 제어는 제공하지 않는다. 첫 이력 조회 이후 합성 표본을 5초 간격으로 공개한다. 이는 UA 세션이 없는 프로젝트 시험 계약이며 OPC UA 표준 JSON 서비스나 제조사 호환 서버라는 의미가 아니다. 이력은 프로세스 메모리에만 존재한다.

MQTT `serve-mqtt`에 `--fault invalid-utf8` 또는 `--fault invalid-json`을 더하면 첫 번째 전송에서 합성 인코딩·JSON 오류를 재현한다. 정상 fixture와 별도 오류 시나리오이며 장비 제어를 제공하지 않는다.

REST 기본 프로파일의 `serve`에 `--fault invalid-utf8`을 지정하면 첫 NDJSON 레코드에 합성 UTF-8 오류를 재현한다.

### 두 번째 합성 REST 표현

`serve --fixture fixtures/mockprofile-100-b-001.json --cert <PFX> --port <포트>`는 vendor-b 평면 snake_case, 화씨, Unix 밀리초, 정수 상태, 출력 비율 및 밀리초 경과값을 제공한다. 이는 실제 제조사 규격이 아니다. 화씨 입력은 섭씨 0.1°C 단위로 정확히 되돌릴 수 있는 값만 허용하는 합성 초기 계약이다. 출력 비율을 RPM으로 환산하지 않는다.

`serve --fixture fixtures/mockprofile-100-c-001.json --cert <PFX> --port <포트>`는 PascalCase/약어, 중첩 측정값, 오프셋 시각, 분 단위 경과값, `Y/N`과 순서형 `0~10` 속도 단계를 사용하는 합성 vendor-c 원천을 제공한다. 실제 제조사 호환 사양이 아니다.

MQTT `serve-mqtt`는 `fixtures/mockprofile-300-b-001.json`과 `fixtures/mockprofile-300-c-001.json`도 지원한다. 인스턴스별 Topic에만 구독을 허용하며 각 vendor의 원천 표현을 그대로 전송한다. 실제 장비 호환 사양은 아니다.

`serve-ua-json`은 `fixtures/mockprofile-200-b-001.json`과 `fixtures/mockprofile-200-c-001.json`도 지원한다. 각 합성 원천의 이력을 읽기 전용으로 제공하며 표준 OPC UA JSON 서비스라는 의미는 아니다.

`serve-opcua`는 `fixtures/mockprofile-203-b-001.json`과 `fixtures/mockprofile-203-c-001.json`도 지원한다. vendor별 합성 namespace와 읽기 전용 Node를 제공하며 온도는 섭씨로 표현하고 출력 비율·속도 단계·RPM Node는 서로 구분한다. 이 Node는 프로젝트 합성 정보모델이다. 실수 Node는 IEEE 754 값이며 정확한 원천 값과 변환 검증은 원본 이력을 기준으로 한다.
