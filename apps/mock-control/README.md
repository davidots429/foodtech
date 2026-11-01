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

