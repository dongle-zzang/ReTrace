# Backend / API / DB

이 영역은 작고 하나의 서비스로 연결되어 있어 API와 DB 지식을 함께 라우팅한다. 구성·endpoint 표·저장 정책·운영 절차는 [backend README](../../backend/README.md)가 원문이며 여기에는 코드 탐색과 변경 판단에 필요한 구현 경계를 둔다.

## 활성 구현 패턴

- `backend/app/main.py`: `create_app()` factory와 FastAPI lifespan. `app.state`에 config/session factory/poller/live store를 두고 background thread를 시작·종료한다. router/controller/service/repository 디렉터리 계층을 따로 사용하지 않는다.
- route 함수는 SQLAlchemy session으로 조회하고 `schemas.py`의 Pydantic response model을 반환한다. DB 예외는 안전한 503, 입력 validation은 원문을 반사하지 않는 422, 없는 카메라는 404로 처리한다.
- `ingest.py`: `Poller`가 httpx로 preview JSON을 읽고 Pydantic `FrameIn`으로 검증한다. 상태는 허용 값으로 정규화하고 session/generation/source 매칭으로 잘못된 snapshot을 걸러낸다. `LiveStore`는 lock과 TTL을 사용하는 메모리 저장소다.
- DB 쓰기는 `sessions.begin()` transaction 안에서 수행한다. 초기 YAML 동기화와 polling/retention 책임은 poller에 있다. 실패 시 live bbox를 비우고 재시도하며 외부 예외 원문을 로그로 남기지 않는다.
- `db.py`: SQLAlchemy 2 declarative model과 session factory. 운영 DB는 psycopg/PostgreSQL, CPU 테스트는 SQLite다. `create_all()`은 초기 생성이며 기존 schema migration 수단이 아니다. Alembic/migration 체계는 현재 없다.
- `settings.py`: Compose가 주입한 DB 변수, 공개 카메라 경로와 `BACKEND_CORS_ORIGINS`를 읽는다. Backend가 루트 `.env`나 RTSP credential을 읽도록 바꾸지 않는다. preview URL 등 dataclass 기본값이 모두 환경 변수로 설정 가능한 것은 아니다; `from_env()`를 확인한다.

## API와 데이터 변경

preview JSON과 backend `/api/*`는 서로 다른 계약이다. preview producer는 `preview.py`, consumer는 `ingest.py`, 외부 응답 schema는 `schemas.py`에 있다. 배포한 Browser는 40225의 `/api/*` GET 프록시로 Backend에 접근한다. `preview_path`도 같은 origin의 상대 경로이며 backend가 영상을 relay하지 않는다.

CameraOut의 `preview_path`는 `/mjpeg/sourceN`, `metadata_path`는 `/ws`다. `/ws`(여러 카메라 JPEG·metadata)는
Preview가 소유하고 Backend는 기존 JSON snapshot을 polling한다. browser 계약은 [실시간 Preview](../preview-realtime.md)다.

API/DB 수정 전 backend README의 “저장 정책”을 확인한다. track 식별 범위와 문자열 표현, bbox 보관 위치, polling 관찰의 한계, timestamp 의미를 단순화하지 않는다. 삭제된 YAML 카메라와 기존 track 참조 처리도 “YAML과 DB”의 정책을 따른다. Schema 변경에는 기존 데이터용 migration 계획이 필요하다.

인증/인가, session/token, route protection은 아직 없다. 공개 운영을 위한 인증 체계가 있다고 가정하지 않는다. 현재 네트워크 경계와 worker 수 제약은 [deployment](deployment.md)를 확인한다.

계약 검증은 `backend/tests/`의 API/polling/보안 테스트와 preview AST 계약 테스트를 먼저 찾는다. 명령은 [testing](testing.md)에 있다.
