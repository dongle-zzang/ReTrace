# 배포와 로컬 환경

## 저장소에서 확인되는 구성

- `compose.yml`은 GPU `retrace`, CPU `backend`, PostgreSQL 서비스와 DB named volume을 정의한다. retrace는 backend/postgres의 기동에 의존하지 않는다.
- 루트 Dockerfile은 DeepStream 7.0 기반에 PyDS 1.1.11을 설치하고 Compose와 같은 preview 실행 경로를 사용한다. 소스는 Compose bind mount로 제공하며 루트 이미지에 앱 소스를 COPY하는 방식이 아니다.
- Backend Dockerfile은 Python 3.12 slim에 앱/공통 카메라 로더를 COPY하고 비-root 사용자, Uvicorn worker 하나로 실행한다. YAML은 read-only mount다.
- retrace는 컨테이너 내부 40225, backend는 8000을 사용한다. 40225에서 CSR 빌드와 metadata/signaling(legacy는 MJPEG)을 제공하고 `/api/*` GET을 내부 Backend로 전달한다. Backend 호스트 공개는 기본 loopback이며 retrace 호스트 binding은 ignored override에서 정한다. 별도 reverse proxy 서비스와 CI/CD workflow는 없다.
- 별도 개발 PC 접근은 private `.env`의 `BACKEND_BIND_IP`로 기존 제한된 인터페이스 하나를 선택한다. 브라우저 API CORS는 `BACKEND_CORS_ORIGINS`로 opt-in한다. 구체적인 설정/재생성 절차는 [backend README](../../backend/README.md#별도-개발-pc에서-접근)를 따른다. 서버 Compose에는 frontend service가 없다.

- CSR 산출물은 기본 `frontend/`에 업로드하며 `FRONTEND_DIST_DIR`은 컨테이너 내부 경로다. `PREVIEW_BACKEND_URL`은 내부 Backend origin이다. 빌드가 없으면 `web/`의 mode별 영상 확인 페이지를 제공한다. 업로드·최초 컨테이너 재생성·HTTP 확인 절차는 [frontend deployment](frontend-deployment.md)를 따른다. retrace healthcheck와 `tools/check_preview.py`는 `/streams.json`을 사용한다.

## 실행 원문과 변경 판단

기본 Preview 영상은 WebRTC이며 `/ws`에서 metadata/signaling을 처리한다. Dockerfile에 nice/DTLS/GI
구성요소를 추가하고 NVIDIA driver capability에 `video`를 포함한다. 기존 bridge HTTP port만으로
media 경로를 보장하지 않는다. `compose.webrtc.example.yaml`은 명시적으로 선택하는 host-network
옵션이며 기존 ignored binding override를 자동 병합하지 않는다. private `WEB_BIND_IP`,
`BACKEND_PREVIEW_URL`, `PREVIEW_BACKEND_URL` 설정과 검증은 [실시간 Preview](../preview-realtime.md)를 따른다.
기본 Compose와 private override 자체의 기존 port/GPU 예약/DB volume은 보존한다.

환경 준비, `.env` 복사, 모델/engine 준비, Compose build/up, 컨테이너 교체·복구 주의사항은 [루트 README](../../README.md)의 실행 섹션을 따른다. Backend/PostgreSQL 실행, DB 확인, 단일 worker를 유지하는 이유는 [backend README](../../backend/README.md)의 “실행” 및 “CPU 테스트”에 있다. 현재 호스트의 실제 배포/영상 수신 상태는 문서 작성에서 검증하지 않았다.

`docker compose build`, `docker compose up -d`, `docker compose ps`는 존재하는 기본 운영 명령이다. 이미지/command/mount/ports 변경은 컨테이너 재생성이 필요한지 확인한다. 기존 동작 중 서비스 교체는 이번 코드 변경의 범위와 운영 영향을 확인한 뒤 수행한다.

모델·labels·TensorRT cache와 GPU/NVIDIA runtime은 장비마다 준비해야 한다. Git clone/pull만으로 실행 환경이 완성되지 않는다. 모델/engine 생성과 재사용 조건을 변경할 때 README의 모델 준비 절차를 확인한다.

## 공유 문서와 private 설정

Git 공유 대상은 `AGENTS.md`, `CLAUDE.md`, `.ai/`, `docs/agent/`다. `.gitignore`에서 이 경로들을 제외하지 않는다. 실제 `.env`, host binding override, 모델·engine·캐시·출력 및 도구별 로컬 설정은 공유 대상과 구분한다.

환경 변수 목록과 역할은 `.env.example`, Compose, `settings.py` / `camera_config.py`에서 확인한다. Backend/PostgreSQL에는 명시된 DB 변수만 전달한다. `docker compose config` / inspect / logs에는 private 값이 포함될 수 있으므로 원문을 공유 문서나 인수인계에 복사하지 않는다. 실행 경로 확인이 필요하면 README의 `tools/check_container_startup.py` 요약 절차를 사용한다.
