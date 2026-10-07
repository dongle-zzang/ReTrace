# ReTrace Backend

FastAPI API와 PostgreSQL은 영상 처리와 별도 process/service로 실행한다.
Backend에는 GPU·GI·PyDS 또는 RTSP 비밀값이 필요하지 않다. Nuxt/Re-ID는 포함하지 않는다.

## 구성과 데이터 흐름

- 기존 루트: `preview.py`, `camera_config.py`, `camera_runtime.py`,
  `person_metadata.py`, `configs/`, `tests/`, `tools/`.
- `preview.py`는 HTTP 서버와 하나의 공유 batch pipeline을 실행한다.
  기본 영상은 WebRTC, metadata/signaling은 `/ws`이며 HTTP 40225 포트를 유지한다.
  `PREVIEW_MODE=mjpeg`에서만 기존 `/mjpeg/source{source_id}`와 OSD를 사용한다.
- `/streams.json`은 배열이다. 각 항목은 `camera_id`, `floor`, `name`, `id`
  (source_id), `format`, `status` (기존 UI 호환), `url` (WebRTC는 `/ws`, legacy는 상대 MJPEG 경로),
  `runtime`, `runtime_session`을 포함한다. Backend는 runtime의 state를 사용한다.
- `CameraRuntime`은 카메라별 상태를 메모리에 보관한다. `MetadataStore`는
  source별 최신 immutable FrameMetadata만 보관한다. PersonMetadata는
  camera_id/timestamp/track_id/class_id/class/confidence/tracker_confidence/bbox이다.
- 새 `/metadata.json`은 `{runtime_session, frames: [...]}`이다.
  frame은 source_id/camera_id/generation/timestamp/frame_number/좌표 크기/persons
  등을 포함한다. 실패·이전 generation의 frame은 제외한다. JSON snapshot만
  제공하며 worker에서 네트워크나 DB I/O를 수행하지 않는다.
- Backend 단일 background thread가 2초 간격으로 두 JSON endpoint를 순서대로
  polling한다. HTTP timeout은 1.5초, 응답 크기 제한은 8 MiB다.
  status와 metadata의 session/generation/source_id를 대조하여 재연결 경합을 처리한다.
- push는 inference 쪽 전송/실패 관리가 필요하므로 선택하지 않았다. polling은
  추론 callback이 Backend/PostgreSQL을 호출하거나 기다리지 않아 장애 격리가 단순하다.
  retrace에는 backend/postgres의 depends_on을 추가하지 않았다.
- Backend가 preview에 접근하지 못하면 offline/fps=0으로 최신 행을 갱신하고
  현재 bbox를 비운다. 통신 장애는 카메라 자체 고장과 구별하는
  `preview_unreachable` 코드다. 상태 관찰이 10초 이상 오래되어도 조회 시 offline이다.
  DB 장애는 API 503, polling은 자동 재시도하며 외부 예외 내용을 로그에 쓰지 않는다.

## YAML과 DB

`camera_config.py`의 공통 YAML validation은 비밀값을 읽지 않는다.
`load_public_cameras()`는 camera_id/floor/name/enabled/source_id만 반환한다.
Backend 시작 시 YAML -> DB로 동기화하며 비활성 카메라도 유지한다.
삭제된 카메라는 과거 track 참조 보존을 위해 DB에서 disabled 처리한다.
활성 카메라 순서로 source_id를 부여하며 비활성 카메라는 source_id가 없다.
설정 변경 후 retrace와 backend를 함께 재시작한다. API 설정 수정 기능은 없다.

초기 테이블 생성은 SQLAlchemy `create_all`을 사용한다. 지금은 migration이
필요한 기존 Backend schema가 없으므로 Alembic을 추가하지 않았다. 향후 schema
변경은 migration이 필요하며 `create_all`은 기존 테이블 구조를 수정하지 않는다.
PostgreSQL 데이터는 `postgres_data` named volume에 유지한다.
DB URL은 SQLAlchemy URL 객체로 구성하여 비밀번호의 특수문자를 지원한다.
DB statement/lock/connect timeout과 parameter masking을 적용한다.

## 저장 정책

- `cameras`: 공개 설정만 저장. RTSP URL/rtsp_env/계정은 컬럼 자체가 없다.
- `camera_status`: 카메라당 최신 상태 한 행을 UPDATE한다. state history는
  이번 단계에서 소비하는 기능이 없어 만들지 않았다. 동일 상태를 반복하거나
  state가 변경되어도 행 수는 증가하지 않는다.
- 실시간 bbox/confidence: Backend의 `LiveStore`에 camera별 최신 frame을 교체한다.
  빈 frame은 기존 사람을 제거하며 frame timestamp TTL은 10초다. DB에는 bbox가 없다.
- `person_tracks`: polling에서 관찰된 tracked person만
  `(camera_id, runtime_session, generation, track_id)` 단위로 INSERT/UPDATE한다.
  first_seen_at/last_seen_at/max_confidence만 저장한다. 같은 frame 재조회는 UPDATE도
  생략한다. 매 frame 저장이 아니며 polling에서 놓친 짧은 track은 수집되지 않는다.
- track_id는 uint64이므로 DB/API에서 10진 문자열로 표현한다. PostgreSQL signed
  BIGINT overflow와 JavaScript 정밀도 손실을 피한다.
- track 요약은 기본 최근 7일만 유지하며 60초마다 오래된 last_seen_at 행을 삭제한다.
  DB 장애 중에는 정리가 지연될 수 있다. 이 단계에서는 Re-ID 영구 이동 이력을
  보장하지 않는다. last_seen_at은 마지막 관찰 시각이며 track 종료를 뜻하지 않는다.
- timestamp는 기존 pipeline 처리 시각이다. 카메라 capture/NTP 시각은 아니다.

## API

| Method / path | 응답 |
| --- | --- |
| GET /api/health | DB readiness와 preview 연결 상태; DB 장애 503 |
| GET /api/cameras | 공개 카메라 목록과 최신 status, 상대 preview_path |
| GET /api/cameras/{camera_id} | 공개 카메라 상세; 알 수 없는 ID 404 |
| GET /api/cameras/{camera_id}/status | 최신 runtime 상태 및 observed_at/stale |
| GET /api/cameras/{camera_id}/tracks?limit=100 | current bbox와 recent DB 요약; limit 1~500 |
| GET /api/status | 전체 카메라 상태 목록과 state별 counts |

Pydantic 응답 schema는 `app/schemas.py`, OpenAPI는 `/openapi.json`에 있다.
Browser 영상 요청은 기존 retrace:40225로 직접 보낸다. Backend의 preview_path는
preview origin 기준 상대 경로다. `preview_format=webrtc`는 `/ws`에서 signaling하며
`signaling_path`, `metadata_path`도 함께 제공한다. `<img src>`는 `preview_format=mjpeg`에서만 사용한다.
Backend는 영상을 relay하지 않는다. WebRTC schema/네트워크는 [실시간 Preview](../docs/preview-realtime.md)를 따른다.
`PREVIEW_MODE`는 두 서비스에 동일하게 적용하며 host network 옵션의 poller origin은
`BACKEND_PREVIEW_URL`로 설정한다.
배포한 CSR 프론트는 40225에서 정적 제공하며 API도 같은 origin의 `/api/*`로 호출한다.
Preview HTTP request thread가 내부 Backend GET으로 전달하므로 브라우저에 8000 주소를
지정할 필요 없다. 업로드·적용 절차는 [프론트 배포 안내](../docs/agent/frontend-deployment.md)를 따른다.
로그인은 아직 없으므로 Backend host binding은 기본 loopback이다.

## 실행

기존 `.env`에 `.env.example`의 Backend 항목을 추가한다. POSTGRES_PASSWORD는
고유한 로컬 비밀번호를 설정한다. 빈 비밀번호면 postgres/backend 시작에 실패한다.
Compose interpolation은 retrace 단독 실행도 가능하도록 빈 기본값을 허용한다.
Backend/PostgreSQL에는 명시적인 DB 변수만 주입하며 `.env`를 mount하지 않는다.
실제 비밀번호와 기존 RTSP 설정은 Git에 포함하지 않는다.

```bash
docker compose build
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8000/api/health
curl -fsS http://127.0.0.1:8000/api/cameras
# Preview에 loopback binding이 있는 환경에서만 사용한다.
curl -fsS -o /dev/null http://127.0.0.1:40225/streams.json
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;"'
docker compose exec -T retrace python3 tools/check_backend.py
```

BACKEND_PORT를 변경했다면 curl의 포트를 맞춘다. preview host binding은 기존
ignored compose.override.yaml을 유지한다. `check_backend.py`는 실제 API와
preview JSON 전체를 읽고 로컬 RTSP URL/host/계정/비밀번호와 DB password의
노출을 검사하며 응답 내용이나 비밀값을 출력하지 않는다.

### 별도 개발 PC에서 접근

서버에는 retrace/backend/postgres만 유지한다. Frontend/Nuxt는 별도 PC에서
개발한다. Backend host binding은 `${BACKEND_BIND_IP:-127.0.0.1}`이며,
개발 PC에서 접근하려면 서버의 private `.env`에서 `BACKEND_BIND_IP`를 기존
Preview에 사용 중인 LAN 또는 Tailscale 인터페이스 주소 하나로 지정한다.
`0.0.0.0` 또는 `::`를 사용하지 않는다. 실제 주소는 공유 파일에 기록하지 않는다.
이 설정은 기존 loopback binding을 선택한 인터페이스로 대체한다.
컨테이너 내부 Uvicorn의 wildcard listener와 호스트 공개 범위는 별개다.
PostgreSQL은 호스트에 공개하지 않으며 Preview의 private override는 유지한다.

브라우저 API 호출에는 서버의 private `.env`에서 `BACKEND_CORS_ORIGINS`를
쉼표로 구분한 정확한 origin 목록으로 설정한다. 예:
`http://localhost:3000,http://127.0.0.1:3000`. 두 origin은 서로 다르며 실제
개발 서버 포트를 맞춘다. 기본값은 비어 있어 cross-origin 허용 응답이 없다.
허용 method는 GET이고 OPTIONS preflight는 middleware가 처리한다.
credential 허용은 꺼져 있으며 wildcard origin은 시작 시 거부한다.
Nuxt 서버 측 호출에는 브라우저 CORS가 적용되지 않지만 네트워크 접근은 필요하다.

설정 후 `docker compose up -d --build --no-deps backend`로 Backend만 재생성한다.
Frontend service, pipeline, 모델/tracker 또는 DB schema 변경은 필요 없다.
기본 WebRTC는 Preview origin의 `/ws`를 사용한다. legacy MJPEG에서만 `preview_path`를
`<img src>`로 표시할 수 있다. Preview에는 CORS 헤더가 없으므로 fetch나
canvas 픽셀 읽기가 필요해지면 별도로 검토한다.

## CPU 테스트

Python 3.11+ 환경에서 GPU 없이 실행한다.

```bash
python3.11 -m venv /tmp/retrace-backend-test
/tmp/retrace-backend-test/bin/pip install -r backend/requirements-test.txt
/tmp/retrace-backend-test/bin/python -m pytest -q backend/tests
/tmp/retrace-backend-test/bin/python -m unittest discover -s tests -p 'test_camera_*.py'
/tmp/retrace-backend-test/bin/python -m unittest discover -s tests -p 'test_rtsp_inputs.py'
/tmp/retrace-backend-test/bin/python -m unittest discover -s tests -p 'test_canonical_startup.py'
/tmp/retrace-backend-test/bin/python -m unittest discover -s tests -p 'test_preview_http.py'
/tmp/retrace-backend-test/bin/python -m unittest discover -s tests -p 'test_check_preview.py'
```

Backend 테스트는 SQLite 파일 DB와 실제 SQLAlchemy transaction을 사용하고 preview를
HTTP MockTransport로 대체한다. 실제 PostgreSQL 연결/GPU/RTSP/컨테이너 build 검증은
위 서버 명령으로 수행한다. 단일 Uvicorn worker만 사용한다. worker 증설 시 polling
중복과 메모리 snapshot 분리가 생기므로 별도 설계가 필요하다.
