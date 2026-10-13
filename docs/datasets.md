# AI 차량 데이터셋 수집과 라벨링

현재 구현은 수동 라벨링부터 검수·YOLO ZIP 생성까지 제공한다. 모델 학습, ONNX 변환,
운영 모델/추론 설정 변경은 포함하지 않는다. 운영 GPU/실제 CCTV/PostgreSQL 확인과 배포는
아직 수행하지 않았다. CPU 테스트와 합성 영상 브라우저 검증은 이를 대체하지 않는다.

## 화면과 사용 흐름

- 내비게이션 `AI Datasets` → `/datasets`: 카메라 선택, 기존 실시간 영상 미리보기,
  `Capture current frame`, 저장 이미지 썸네일·촬영 시각·상태, 카메라/상태 필터와 페이지네이션,
  이미지 삭제, 내보내기 상태·분할별 이미지 수·경고·ZIP 다운로드.
- `/datasets/images/:imageId`: 드래그로 박스 생성, 선택 후 이동/네 꼭짓점 크기 조절,
  클래스 변경, 삭제 버튼 또는 Delete/Backspace, 100~300% 확대, 기존 라벨 로드,
  이전/다음 이미지(같은 카메라), `Save draft`, `Mark reviewed`.
- 빈 이미지도 검수할 수 있지만 `I reviewed this image. There are no vehicles.`를 명시적으로
  확인해야 한다. 미저장 변경은 페이지 이동/탭 종료 전에 확인한다.
- 상태는 `pending`(Pending), `in_progress`(In progress), `reviewed`(Reviewed).
  검수한 이미지를 다시 초안 저장하면 `in_progress`로 돌아가 이후 내보내기에서 제외된다.
- 박스는 저장 이미지의 원본 픽셀 좌표 `{id, class_id, x, y, width, height, source: "manual"}`다.
  `(x,y)`는 좌상단이다. `DatasetBoxEditor.vue`는 기존 `useVideoStage`/`videoGeometry`를
  재사용하여 letterbox·리사이즈·확대 후에도 SVG와 원본 좌표가 일치하도록 한다.
- 클래스는 **데이터셋 전용 `0: car`, `1: truck`, `2: bus`**다. COCO 모델의 class ID와 다르다.

## 깨끗한 프레임의 경계

기존 `/snapshots/:cameraId.jpg`, MJPEG와 WebSocket JPEG에는 서버 OSD가 들어 있다.
데이터셋 캡처는 이 이미지를 재사용하거나 브라우저 화면을 저장하지 않는다.

`DATASET_CAPTURE_ENABLED=1`인 Preview만 각 카메라의 기존 demux 출력에 분기를 추가한다.
`tee` 위치는 **NV12 → 기존 OSD용 RGBA 변환 전**이다. OSD가 깨끗한 분기와 동일한
RGBA surface를 직접 수정하지 않도록 변환보다 앞에서 나눈다.

```text
기존 RTSP → decode → mux / inference / tracker → demux → queue → tee
    ├─ leaky queue → 기존 RGBA / OSD / preview JPEG → MJPEG, /ws
    └─ valve(평소 닫힘) → leaky queue → I420 변환 → JPEG quality 95 → appsink
```

새 RTSP 연결/decoder/inference는 없다. 요청 시 한 프레임만 valve를 통과하고 PTS가
일치하는 JPEG만 요청에 전달한다. 요청 간 프레임을 저장하지 않으며 timeout은 5초다.
각 queue는 1프레임 leaky, appsink는 async/sync false이므로 캡처가 없는 카메라가
preroll을 붙잡지 않도록 구성했다. Python handoff 오류는 공유 flow에 실패를 반환하지 않는다.
실제 plugin 협상·색상·OSD surface 독립성·FPS/VRAM 영향은 GPU에서 검증해야 한다.

Backend는 `GET /training-snapshots/:cameraId.jpg`를 요청하고
`X-Capture-Clean: pre-osd-v1`과 timezone이 있는 `X-Captured-At`를 확인한다.
Preview는 응답 전후 카메라 runtime session/generation과 online/degraded 상태를 검사한다.
오래된 세대나 unavailable/disabled 캡처는 실패하며 **OSD JPEG로 fallback하지 않는다**.
캡처 UTC 시각은 Preview에서 JPEG가 전달된 시각이며 센서 노출 시각은 아니다.

여기서 원본은 **기존 mux 출력 해상도의 OSD 없는 프레임**이다. 현재 공유 mux는
1920×1080이며 Preview용 1280×720 축소를 적용하지 않는다. RTSP 센서 원래 해상도를
복원하거나 새로운 고해상도 디코딩 경로를 만드는 기능은 아니다.

기본 플래그는 `0`이므로 비활성일 때 기존 출력 chain을 유지한다. 자동 라벨은 제공하지 않는다.
현재 최신 metadata와 캡처 JPEG의 동일 프레임 association이 없기 때문에 최신 박스를
초기 라벨로 붙이지 않는다. `/classes`의 `auto_labels_available`은 false이고 source는 manual뿐이다.

## API

브라우저는 기존 상대 경로 `/api`, `/ws`를 사용한다. 기존 Nuxt 개발 프록시 및 Preview
동일 origin API 프록시를 통과한다. 별도 주소/서버 서비스나 dependency는 추가하지 않았다.

| 메서드 | `/api/datasets` 아래 경로 | 계약 |
| --- | --- | --- |
| GET | `/classes` | 데이터셋 클래스 맵, auto_labels_available |
| POST | `/capture` | `{camera_id}` → 201 이미지 메타데이터 |
| GET | `/images` | camera_id/status/page/page_size 필터 → items/total/page/page_size |
| GET | `/images/{uuid}` | 이미지 메타데이터, boxes, reviewed_empty, previous_id/next_id |
| GET | `/images/{uuid}/file`, `/thumbnail` | JPEG, no-store |
| PUT | `/images/{uuid}/annotations` | `{revision, boxes, status, reviewed_empty}` → 저장 결과·새 revision |
| DELETE | `/images/{uuid}` | 라벨과 원본/썸네일 삭제 → 204 |
| GET | `/exports` | 최신 20건, building/ready/failed, 개수/경고/다운로드 URL |
| POST | `/exports` | `{seed: 42}` → 202 백그라운드 작업 |
| GET | `/exports/{uuid}/download` | ready인 ZIP 다운로드 |

없는 이미지/파일은 404, 동시 캡처/내보내기·동일 JPEG 중복·revision 충돌은 409,
유효하지 않은 박스/검수 조건은 422, 카메라 캡처·저장소·DB 장애는 503이다.
박스 ID 중복, 유한하지 않은 좌표, 이미지 경계 밖 박스, 양수가 아닌 크기, 클래스 범위를 거부한다.
저장 시 PostgreSQL row lock과 revision을 함께 확인하므로 다른 편집자의 변경을 덮어쓰지 않는다.

제한: 이미지 최대 20MiB/32MP, 이미지당 박스 최대 300개, 페이지 최대 100개(화면 24개),
내보내기 1~5000개 검수 이미지. Preview의 기존 JSON 쓰기 제한 64KiB가 함께 적용된다.
ZIP/JPEG 응답은 64KiB 단위로 스트리밍하여 기존 JSON 4MiB 응답 제한에 걸리지 않는다.
내보내기는 현재 Backend 단일 worker의 thread/lock을 사용하며 multi-worker로 늘리지 않는다.

인증/session/token은 기존 앱에 구현되어 있지 않다. 기존 LAN/Tailscale 제한 binding과
동일 origin 접근 경계를 유지하며, 캡처·조회·다운로드도 같은 접근 권한이다.
기존 Authorization/Cookie 프록시 전달은 유지한다. 외부 이미지 업로드 경로는 없다.

## DB와 파일 정합성

기존 shared SQLAlchemy Base에 두 테이블만 추가한다. Alembic은 없으며 기존 startup
`create_all()`이 없는 테이블을 생성한다. 기존 테이블 ALTER/삭제와 기존 데이터 이동은 없다.

- `dataset_images`: UUID, camera FK, 촬영 UTC, capture_session, 상대 파일 경로,
  width/height, SHA-256, status, boxes(JSON), reviewed_empty, revision, updated_at.
  `(camera_id, sha256)` unique로 같은 JPEG 중복 저장을 막는다.
- `dataset_exports`: UUID, created_at, seed, status, manifest(JSON), 안전한 error 메시지.

Backend `DATASET_STORAGE_DIR` 기본은 `/app/dataset-storage`이며 Compose named volume
`dataset_data`로 유지한다. Dockerfile이 UID 10001 Backend 소유 디렉터리를 준비한다.
직접 생성한 bind mount를 사용한다면 동일 UID 쓰기 권한을 운영자가 준비해야 한다.

```text
DATASET_STORAGE_DIR/
  images/{image_uuid}.jpg          # 원본 JPEG
  images/{image_uuid}.thumb.jpg    # 최대 폭 400px 썸네일
  exports/{export_uuid}.zip        # 완성 ZIP
```

라벨/상태는 PostgreSQL JSON에 저장하고 이미지 bytes는 DB에 넣지 않는다.
캡처 transaction이 실패하면 파일을 정리한다. 삭제는 row lock 안에서 파일을 `.deleting`으로
옮긴 후 DB commit하며 실패 시 복원한다. 시작 시 DB 존재 여부로 삭제 tombstone을 복구/정리한다.
파일시스템과 DB는 분산 transaction이 아니므로 강제 종료 중 생성된 미참조 원본 파일은 남을 수 있다.
DB와 이미지 볼륨을 함께 백업해야 한다. 용량 quota/자동 이미지·ZIP 보존 기간은 이번에 추가하지 않았다.

완성 ZIP은 생성 시점의 immutable snapshot이다. 원본 라벨 수정/이미지 삭제로 이미 만든 ZIP은
바뀌지 않으며 다시 생성해야 한다. 삭제 시 과거 ZIP의 복사본까지 삭제하는 기능은 없다.
Backend 재시작으로 중단된 building 작업은 failed로 표시하고 새 작업을 만들도록 한다.

루트 `/dataset-storage/`, `/datasets/`, `.cache/`는 Git 제외다. `/datasets/` 규칙은 루트에만
적용해야 한다. 그렇지 않으면 `frontend/app/pages/datasets/`와 Tailwind 소스 탐지가 함께 빠진다.
실제 이미지/ZIP/DB fixture는 추적하지 않는다.

## YOLO ZIP 생성

PostgreSQL REPEATABLE READ snapshot에서 검수한 이미지·라벨 revision을 읽는다.
파일 존재/해시, 박스 유효성과 명시적인 빈 이미지 검수를 다시 확인하고 실패 시 ZIP을 제공하지 않는다.
각 이미지 UUID가 같은 이름의 TXT와 1:1 대응한다. 빈 검수 라벨은 길이 0인 TXT다.

```text
data.yaml
manifest.json
images/{train,val,test}/{image_uuid}.jpg
labels/{train,val,test}/{image_uuid}.txt
```

TXT 한 줄은 `class_id (x+w/2)/W (y+h/2)/H w/W h/H`이며 8자리 소수로 쓴다.
`data.yaml`은 train/val/test 상대 경로와 0 car/1 truck/2 bus를 담는다.
이동 가능한 ZIP이므로 고정 서버 경로나 `path: .`를 넣지 않고 YAML 부모를 기준으로 한다
([Ultralytics 경로 해석 원문](https://github.com/ultralytics/ultralytics/blob/main/ultralytics/data/utils.py)).
학습은 실행하지 않는다.

같은 카메라의 직전 캡처와 간격이 **30분 미만**이면 같은 capture_session을 이어 쓴다.
고정 시각 bucket이 아니므로 30분 경계 앞뒤의 연속 사진이 다른 그룹이 되지 않는다.
그룹 전체를 seed 기반 deterministic hash 순서로 약 80/10/10 분할한다. 비율은 이미지 수가
아닌 세션 수 기준이다. 세션 하나는 train만, 둘은 train/val, 셋 이상은 test도 확보한다.
이미지를 복제해서 평가 세트를 채우지 않는다. val/test가 각각 10장 미만이면 API/화면에 경고한다.
장소·시점의 독립성과 실제 평가 충분성은 수집자가 확인해야 하며, 같은 장면을 다른 세션으로
수집한 경우까지 자동으로 탐지하지 않는다. 이미지가 없는 평가 split은 학습 평가용으로 충분하지 않다.

`manifest.json`에는 seed, 클래스, session→split, 이미지 ID/revision/카메라/촬영 시각/해시,
박스 수, 분할별 개수와 경고를 넣어 재현과 검사를 지원한다.

## 실행과 아직 필요한 검증

개발 UI는 `frontend/README.md`의 기존 환경 변수/프록시 설정 후 `npm run dev`로 열고
`/datasets`로 이동한다. 서버에 새 API가 적용되지 않았다면 오류 안내가 정상이다.
Mock 모드로 자동 대체하지 않는다.

운영 적용은 Docker 권한이 있는 운영자가 수행해야 한다. 이번 작업에서는 실행하지 않았다.

1. [Backend 백업 절차](../backend/README.md#운영-주차-기능-적용-순서)로 DB 백업과
   `pg_restore --list`를 확인한다. 기존 이미지가 있다면 dataset_data도 함께 백업한다.
   현재 동작 중 서비스의 DB 대상, private binding, 모델/config와 작업 트리의 미적용 변경을 비교한다.
   특히 이전 작업의 대기 중 모델 전환을 이 기능 때문에 함께 활성화하지 않는다.
2. 기존 PostgreSQL volume을 유지하고 Backend만 build/recreate한다.
   예: `docker compose build backend` → `docker compose up -d --no-deps --no-build backend`.
   시작 시 두 새 테이블과 dataset_data 쓰기 권한을 확인한다. `down -v`는 사용하지 않는다.
3. GPU 서버의 private 환경에 `DATASET_CAPTURE_ENABLED=1`을 지정한다. Compose environment
   변경은 Preview 컨테이너 재생성이 필요하다. 실행 중 모델/카메라/config를 보존한 상태에서
   운영자가 기존 적용 절차에 따라 retrace만 재생성한다. 이때 영상은 잠시 중단된다.
4. Frontend에서 `npm run generate` 후 `.output/public/`의 내용물을 `frontend-dist/`에 올린다.
   또는 개발 PC에서 기존 동일 origin 프록시로 접속한다.
5. 실제 카메라에서 캡처 JPEG에 사람/차량 박스·ID·문구·주차 폴리곤이 없는지 확인하고,
   저장→라벨→검수→ZIP→재조회 및 Backend 재시작 후 파일/라벨 보존을 확인한다.
   기존 다중 카메라 MJPEG/WS·사람/차량 추론·주차 판정의 FPS/VRAM/장애 복구를 전후 비교한다.
   PostgreSQL 동시 저장 revision 충돌, 캡처 중 재연결, export 중 이미지 삭제도 확인한다.

CPU 명령(저장소 루트, backend test requirements 환경):

```bash
python -m pytest -q backend/tests
python -m unittest discover -s tests -p 'test_preview_capture.py'
python -m unittest discover -s tests -p 'test_preview_api_writes.py'
python -m unittest discover -s tests -p 'test_camera_shared_pipeline.py'
python -m unittest discover -s tests -p 'test_preview_http.py'
```

프론트는 `frontend/`에서 `npm test`, `npm run typecheck`, `npm run build`.
이번 Windows 검증은 임시 SQLite/합성 JPEG·실제 로컬 API/파일 저장소·Chrome 브라우저를 사용했다.
실제 DeepStream/GStreamer plugin과 PostgreSQL을 대신한 검증은 아니며 운영 배포는 미완료다.

검증 기록(2026-10-12): Backend 138개 통과(데이터셋 13개 포함), clean capture 5개,
쓰기/파일 프록시 12개, 공유 pipeline 14개, WebSocket 11개, parking relay 9개 통과.
프론트 27개·typecheck·production build 통과. Chrome에서 실제 로컬 Backend/SQLite/파일 저장과
합성 영상으로 생성·확대 이동/리사이즈·클래스·검수·재조회·미저장 이탈 취소·ZIP 다운로드를 확인했다.
1440×900/1920×1080/390×844/390×667에서 가로 넘침과 편집 페이지 전체 세로 넘침이 없었다.
기존 `test_preview_http.py`는 2개 통과/2개 Windows 환경 오류다(심볼릭 링크 권한 WinError 1314,
연결 거절 처리보다 짧은 클라이언트 timeout). 변경 전 HEAD 프록시로도 같은 두 오류를 재현했다.
이 테스트를 통과로 기록하지 않으며 native Linux에서 재검증해야 한다.
