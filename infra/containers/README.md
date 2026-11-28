# 돈까스 추론용 Ollama 이미지 준비

로컬 모델 저장소의 `llama3.1:8b`로 Docker 빌드 컨텍스트를 준비한다. 검토된 manifest와 설정·모델·템플릿·라이선스 등 manifest가 가리키는 모든 blob을 각각 SHA-256과 크기로 검사한다. 모델 등록에 필요한 파일만 복사한다.

```bash
python3 infra/containers/prepare_tonkatsu_ollama_context.py \
  --model-store models/ollama-models \
  --output /tmp/foodtech-ollama-context \
  --base-image "ollama/ollama@sha256:<검토한-base-digest>"
```

출력 경로는 존재하지 않아야 하며 약 5 GB의 공간이 필요하다. 로컬 검증과 같은 릴리스인 Ollama 0.40.15의 `linux/amd64` 후보 digest는 `sha256:a717bfc1c56acfc2e85d3109bd40a7646258ec9062c40cea83b9ed0927eb4223`이다. 빌드 전에 베이스의 태그·digest 관계와 `linux/amd64` 지원을 확인하고 결과 이미지에도 해당 플랫폼을 명시한다. 컨텍스트 생성은 로컬 작업이다. 검토를 마친 이미지를 배포할 때는 빌드 결과 digest를 고정한다.

컨테이너는 권한 없는 사용자로 실행되고 Ollama는 태스크의 루프백에만 바인딩한다. 같은 태스크의 mTLS 릴레이가 여기에 연결한다. Ollama 클라우드 기능은 비활성화하고 로컬 모델을 환경에 포함한다. 오프라인 검증 때는 컨테이너에 쓰기 가능한 전용 볼륨 `/run/foodtech-ollama`를 마련하고 AWS 자격증명 없이 기동과 모델 목록·추론을 확인한다.
