# ReTrace Frontend

Nuxt 4 기반 카메라 상태 및 실시간 미리보기 대시보드입니다. UI는 shadcn-vue(Card, Badge, Button, Tabs, InputGroup, Alert, Empty, Tooltip 등)와 Tailwind CSS 4를 사용하며 다크 테마(`html.dark`)로 고정합니다. 헤더 배경은 Vue Bits Liquid Ether를 이식한 `LiquidEtherBackground.vue`(three.js WebGL 유체 시뮬레이션)이며, 화면 밖이거나 탭이 숨겨지면 렌더링을 멈추고 `prefers-reduced-motion` 또는 WebGL 미지원 시 정적 그라디언트로 대체합니다. 전역 폰트는 Pretendard Variable입니다.

## 로컬 실행

Node.js 22.12 이상과 npm이 필요합니다. `.env`(Git 제외)에 ReTrace 서버 origin을 둡니다.

```dotenv
NUXT_PUBLIC_BACKEND_BASE_URL=http://<서버 호스트>:40225
# 선택: 카메라당 최대 영상 전송률(1~30, 기본 10)
# NUXT_PUBLIC_PREVIEW_VIDEO_FPS=10
```

```powershell
npm install
npm run dev
```

코드는 상대 경로 `/api`, `/ws`만 사용합니다. 개발 중에는 dev server가 두 경로를 `NUXT_PUBLIC_BACKEND_BASE_URL`로 전달합니다.

- `/api/*`: Nitro `devProxy`
- `/ws`: `nuxt.config.ts`의 `listen` hook이 WebSocket upgrade를 직접 전달하고 `Origin`을 서버 origin으로 바꿉니다. 서버는 같은 origin의 WebSocket만 허용하므로 브라우저에서 서버로 직접 연결하면 403입니다. Nitro `devProxy`와 Vite middleware-mode proxy는 upgrade를 전달하지 않아 별도 처리합니다.

origin에는 `/api`나 `/ws`를 붙이지 않습니다. 실제 주소와 credential은 저장소에 기록하지 않습니다. `.env`를 바꾸면 dev server를 재시작합니다.

## 연결 구조

- `GET /api/cameras`는 10초마다 카메라 목록과 상태를 갱신합니다.
- 페이지당 WebSocket `/ws` 하나(`useRealtimePreview.ts` → `realtimePreviewClient.ts`)로 모든 카메라의 영상·상태를 받습니다. 카메라마다 `<img src=/mjpeg/...>`를 쓰면 브라우저의 origin당 HTTP/1.1 연결 6개 제한에 걸리므로 사용하지 않습니다.
- 화면에 보이는(IntersectionObserver, 120px 여유) 카드 또는 전체 화면 카드만 `subscribe`합니다. 필터·검색·스크롤로 카드가 바뀌면 구독 목록 전체를 다시 보냅니다: `{"version":1,"type":"subscribe","cameraIds":[...],"video":true,"videoFps":10}`.
- 영상은 binary 메시지(`u8 version`, `u8 id 길이`, cameraId, `u32 BE sequence`, JPEG)입니다. `CameraVideo.vue`는 `JpegFrameView`로 Blob → object URL → `<img>`를 한 장씩 decode하고, decode 중 도착한 프레임은 대기 슬롯 하나에 덮어씁니다. 이전 URL은 새 이미지 load 뒤, 나머지는 unmount·구독 해제·연결 단절 시 revoke합니다.
- `camera_status`가 있으면 카드 상태/FPS는 API 스냅샷 대신 실시간 값을 표시합니다. 연결이 끊기면 "서버 연결 중"을 표시하고 2초 뒤 재접속해 구독을 다시 보냅니다. unmount·`pagehide`에서는 소켓을 닫고 재접속하지 않습니다(bfcache 복원 시 다시 연결).
- 사람 bbox와 Track ID는 서버가 JPEG에 그려 보내므로 `detections`로 클라이언트 박스를 다시 그리지 않습니다. `CameraOverlay.vue`는 선·영역 표시용으로 남아 있습니다.
- 대역폭은 대략 `보이는 카메라 수 × videoFps × 약 127KB`입니다(21대 × 10fps ≈ 130Mbps). 필요하면 `NUXT_PUBLIC_PREVIEW_VIDEO_FPS`를 낮춥니다.

서버 계약은 서버 저장소의 `docs/preview-realtime.md`와 참조 구현 `web/preview.js`를 따릅니다.

## 주차 관리

- 모델: **주차 자리(space)** 는 실제 한 자리이며 고유 라벨(`A-01`)로 식별합니다. **영역(zone)** 은 그 자리를 한 카메라 화면에 그린 폴리곤입니다. 여러 카메라가 같은 라벨을 고르면 좌표는 각자 다르지만 같은 `parkingSpaceId`를 가리켜 하나의 자리로 묶입니다.
- `/parking`: 주차 카메라 카드(영역 overlay, 카메라별 수)와 오른쪽 `Spaces` 패널(라벨별 최종 상태 + 카메라별 상태·점수). 카메라 결과가 엇갈리면 `Check` 표시와 `n to check` 필터가 나옵니다. 상단 합계는 라벨(논리 자리) 기준입니다.
- 카드의 `P` 버튼 → `/cameras/<cameraId>/parking` 편집 화면. 영상 위 아이콘 도구(편집, 영역 추가, 저장, 취소) → 오른쪽에서 라벨 선택 → 영상 클릭으로 꼭짓점(첫 점 클릭 또는 Enter로 완료, Backspace로 마지막 점 취소, Esc로 그리기 취소) → 저장 / 취소(초안 폐기). 선택한 영역은 꼭짓점 드래그, 변 가운데 점 드래그(꼭짓점 추가), 꼭짓점 더블클릭·우클릭(삭제), 목록에서 라벨 변경·삭제를 지원합니다.
- 라벨 선택은 오른쪽 라벨 목록(`ParkingLabelList`) 한 곳에서만 합니다. 이 카메라에 그려진 라벨을 누르면 그 영역이 선택되고(보기 모드에서는 캘리브레이션·카메라별 상태가 열림), 영역을 선택한 채 다른 라벨을 누르면 라벨이 바뀌며, 아직 그려지지 않은 라벨을 누르면 그 라벨로 그리기를 시작합니다. 맨 아래 `Add label` 입력은 목록 검색을 겸하고, Enter나 +로 없는 라벨을 서버에 만든 뒤 바로 그리기를 시작합니다. 대소문자·공백만 다른 라벨은 같은 라벨로 보고 새로 만들지 않습니다(서버 409도 기존 라벨로 처리). 한 카메라에서 같은 라벨은 한 번만 쓸 수 있고, 라벨 없는 영역이 있으면 저장되지 않습니다.
- 상태 색: 주차 중 빨강, 빈자리 초록, 판정 불가 회색, 편집 중 선택 영역은 테마 강조색(`--primary`). 색과 함께 라벨 아래·목록에 상태 텍스트를 표시합니다.
- 통합 상태(`combineSpaceStatus`): 판정 불가 카메라는 무시하고, 나머지가 일치하면 그 값입니다. 엇갈리면 충돌로 표시하고 서버가 `spaces[].status`를 보내면 그 값을, 아니면 점수가 높은 쪽(동점·점수 없음은 주차 중)을 씁니다.
- 좌표는 영상 원본 프레임 기준 0~1 정규화 값입니다. 변환은 `app/lib/videoGeometry.ts`(object-fit별 콘텐츠 사각형, client ↔ 정규화)에 모여 있고, `useVideoStage.ts`가 stage 크기·전체 화면을 추적해 overlay를 실제 영상 사각형(letterbox 제외)에 맞춥니다.
- 데이터 계약은 `app/types/parking.ts`, 접근 로직은 `app/composables/parkingApi.ts`의 `ParkingApi`, 상태는 `useParking.ts`, 편집 초안은 `useParkingEditor.ts`입니다. 기본값은 서버 API(같은 origin `/api`)이고, `.env`에 `NUXT_PUBLIC_PARKING_API=mock`을 두면 이 브라우저의 `localStorage` mock(카메라 두 대가 A-04~A-06, B-01~B-02를 함께 보는 예시, 상태는 30초마다 바뀌는 가짜 값)을 씁니다(generate 시점 값이 들어감). mock 데이터는 서버로 옮겨지지 않습니다.

| 메서드 | 경로 | 내용 |
| --- | --- | --- |
| GET | `/api/parking/spaces` | `[{id, label}]` |
| POST | `/api/parking/spaces` | `{label}` → 201 `{id, label}`. 이미 있는 라벨은 409 |
| GET | `/api/parking/zones?cameraId={id}` | `[{id, cameraId, parkingSpaceId, polygon[{x,y}], enabled, threshold, hysteresis, confirmFrames, calibrated, revision}]` |
| POST | `/api/parking/zones` | `{cameraId, parkingSpaceId, polygon}` → 201 zone (`id`는 서버가 부여) |
| PATCH | `/api/parking/zones/{id}` | `{polygon}` → 200 zone. 라벨(공간)은 바꿀 수 없어 라벨 변경은 DELETE 후 POST |
| DELETE | `/api/parking/zones/{id}` | 204. 없으면 404(완료로 처리) |
| GET | `/api/parking/status` | `{spaces[{parkingSpaceId, label, status, conflict, zones[{zoneId, cameraId, status, score, updatedAt}]}]}` |
| POST | `/api/parking/zones/{id}/calibrate` | `{}` → zone(`calibrated: true`). 자리가 실제로 비어 있을 때만. 프레임 없음 409, ROI 작음 422 |
| DELETE | `/api/parking/zones/{id}/calibrate` | 기준 초기화 |

`status`는 `OCCUPIED`/`EMPTY`/`UNKNOWN`(대소문자 무관, 화면 표시 Occupied/Empty/Unknown), `score`는 0~1입니다. 편집 화면(편집 모드가 아닐 때)에서 영역 행을 누르면 캘리브레이션 패널(현재 상태, 빈자리 기준 등록, 기준 초기화, 안내)이 열립니다. 완료 여부는 zone의 `calibrated` 값입니다(polygon을 바꾸면 서버가 기준을 지웁니다). 목록 응답은 배열 또는 `{spaces: [...]}`/`{zones: [...]}`를 받습니다. 완료 시 편집 시작 시점의 영역과 비교해 DELETE → PATCH → POST를 하나씩 보내고 카메라를 다시 읽습니다(원자적이지 않음). 하나라도 실패하면 다시 읽은 서버 상태로 화면을 바꾼 뒤 오류를 보여 줍니다. 동시에 편집하면 나중 저장이 덮어씁니다.

실시간 상태는 영상과 같은 `/ws`의 `parking.status_updated`(`data`가 위 status 응답과 같은 모양)로 받습니다. REST `/api/parking/status`는 처음 진입과 `/ws` 재연결 때만 읽습니다(mock은 `/ws`가 없어 10초마다). 이전 polygon `revision`이나 더 오래된 관측 시각의 상태는 버립니다. REST 조회가 실패하면 모두 판정 불가로 표시합니다. 편집은 별도 초안에서 하므로 상태 갱신·다른 브라우저의 변경 반영이 편집 중인 폴리곤을 바꾸지 않습니다.

서버 계약 원문은 서버 저장소의 `docs/color-parking.md`입니다. `/ws` `parking.status_updated`는 카메라별로 그 카메라에 연결된 공간들의 전체 상태를 보내며 `parkingSpaceId`로 덮어씁니다. 영역 삭제 후 오는 `spaces: []`에는 해당 카메라 영역과 상태를 REST로 다시 읽습니다. 서버는 라벨 대소문자를 구분하지만 프론트는 `A-01`/`a-01`을 같은 라벨로 보고 새로 만들지 않습니다.

## 정적 배포

```powershell
npm run generate
```

`.output/public/`의 **내용물**을 서버 저장소의 `frontend-dist/` 바로 아래에 업로드합니다(`frontend-dist/index.html`, `frontend-dist/_nuxt/`). `.output/public` 폴더째 올려 한 단계 중첩되면 `/`가 404입니다. 업로드용으로 `.output/public/` 내용물을 zip으로 묶어 `frontend-dist/`에 바로 풀어도 됩니다. 서버의 `frontend/`는 이 소스 폴더(Git 추적)이므로 빌드를 넣지 않습니다. 빌드 파일은 요청마다 읽으므로 교체 후 재시작은 필요 없고, 배포본은 같은 origin에서 `/api`, `/ws`를 쓰므로 별도 주소 설정이 필요 없습니다. `NUXT_PUBLIC_PREVIEW_VIDEO_FPS`는 generate 시점 값이 들어갑니다.

## 확인

```powershell
npm run typecheck
npm test
npm run build
```

실제 서버와 브라우저에서 6대 이상 동시 재생, 필터·스크롤 시 구독 변경, 서버 단절 후 재접속, DevTools Memory/Network에서 object URL·JPEG 누적 없음 등을 확인합니다.
