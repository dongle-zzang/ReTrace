# ReTrace Frontend

Nuxt 4 기반 카메라 상태 및 실시간 미리보기 대시보드입니다. UI는 shadcn-vue(Card, Badge, Button, Tabs, InputGroup, Alert, Empty, Tooltip 등)와 Tailwind CSS 4를 사용하며 다크 테마(`html.dark`)로 고정합니다. 헤더 배경은 Vue Bits Liquid Ether를 이식한 `LiquidEtherBackground.vue`(three.js WebGL 유체 시뮬레이션)이며, 화면 밖이거나 탭이 숨겨지면 렌더링을 멈추고 `prefers-reduced-motion` 또는 WebGL 미지원 시 정적 그라디언트로 대체합니다. 전역 폰트는 Pretendard Variable입니다.

## 로컬 실행

Node.js 22.12 이상과 npm이 필요합니다. `.env`(Git 제외)에 ReTrace 서버 origin을 둡니다.

```dotenv
NUXT_PUBLIC_BACKEND_BASE_URL=http://<서버 호스트>:40225
# 선택: 카메라당 최대 영상 전송률(1~30, 기본 10)
# NUXT_PUBLIC_PREVIEW_VIDEO_FPS=10
# 선택: 주차 판정 기본값 detection(차량 탐지) | color(색상), 기본 detection
# NUXT_PUBLIC_PARKING_OCCUPANCY=detection
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

- 첫 화면은 `/parking`입니다. `/`는 `/parking`으로 이동하고, CCTV 모니터링은 `/monitoring`입니다.
- `GET /api/cameras`는 10초마다 카메라 목록과 상태를 갱신합니다.
- 페이지당 WebSocket `/ws` 하나(`useRealtimePreview.ts` → `realtimePreviewClient.ts`)로 모든 카메라의 영상·상태를 받습니다. 카메라마다 `<img src=/mjpeg/...>`를 쓰면 브라우저의 origin당 HTTP/1.1 연결 6개 제한에 걸리므로 사용하지 않습니다.
- 화면에 보이는(IntersectionObserver, 120px 여유) 카드 또는 전체 화면 카드만 `subscribe`합니다. 필터·검색·스크롤로 카드가 바뀌면 구독 목록 전체를 다시 보냅니다: `{"version":1,"type":"subscribe","cameraIds":[...],"video":true,"videoFps":10}`.
- 영상은 binary 메시지(`u8 version`, `u8 id 길이`, cameraId, `u32 BE sequence`, JPEG)입니다. `CameraVideo.vue`는 `JpegFrameView`로 Blob → object URL → `<img>`를 한 장씩 decode하고, decode 중 도착한 프레임은 대기 슬롯 하나에 덮어씁니다. 이전 URL은 새 이미지 load 뒤, 나머지는 unmount·구독 해제·연결 단절 시 revoke합니다.
- `camera_status`가 있으면 카드 상태/FPS는 API 스냅샷 대신 실시간 값을 표시합니다. 연결이 끊기면 "서버 연결 중"을 표시하고 2초 뒤 재접속해 구독을 다시 보냅니다. unmount·`pagehide`에서는 소켓을 닫고 재접속하지 않습니다(bfcache 복원 시 다시 연결).
- 차량 bbox는 서버가 주차장 카메라 JPEG에 그려 보내므로(사람 검출은 서버에서 제거) `detections`로 클라이언트 박스를 다시 그리지 않습니다. `CameraOverlay.vue`는 선·영역 표시용으로 남아 있습니다.
- 대역폭은 대략 `보이는 카메라 수 × videoFps × 약 127KB`입니다(21대 × 10fps ≈ 130Mbps). 필요하면 `NUXT_PUBLIC_PREVIEW_VIDEO_FPS`를 낮춥니다.

서버 계약은 서버 저장소의 `docs/preview-realtime.md`와 참조 구현 `web/preview.js`를 따릅니다.

## 주차 관리

- 모델: **주차 자리(space)** 는 실제 한 자리이며 고유 라벨(`A-01`)로 식별합니다. **영역(zone)** 은 그 자리를 한 카메라 화면에 그린 폴리곤입니다. 여러 카메라가 같은 라벨을 고르면 좌표는 각자 다르지만 같은 `parkingSpaceId`를 가리켜 하나의 자리로 묶입니다.
- `/parking`: 주차 카메라 카드(영역 overlay, 카메라별 수)와 오른쪽 `Spaces` 패널(라벨별 최종 상태 + 카메라별 상태·점수). 카메라 결과가 엇갈리면 아이콘 없이 `Check` 표시와 `n to check` 필터가 나옵니다. 상단 합계와 Spaces 목록은 정규화된 라벨명 기준으로 중복을 합치며, 영역이 없는 등록 라벨도 Unknown으로 포함합니다. 카드·확대 화면·편집 화면의 폴리곤은 영상 프레임이 실제 표시될 때만 보이고, 연결 중·오프라인·stale 상태에서는 숨깁니다.
- 카드의 `P` 버튼 → `/cameras/<cameraId>/parking` 편집 화면. 제목은 투명 배경으로 공통 Liquid Ether 배경에 이어지며, 영상은 고정 사이드바 없이 화면 너비와 가용 높이를 사용합니다. 편집 페이지의 `viewportWorkspace` 메타를 보고 공통 레이아웃이 `100dvh` 안에서 내비게이션·푸터의 실제 높이를 먼저 확보하고 편집 영역이 나머지를 채웁니다. 제목·도구 줄이 줄바꿈되어도 페이지 전체에 세로 스크롤을 만들지 않으며, 긴 구역 목록은 패널 안에서 스크롤합니다. 영상 아래 카드는 없고 작업 도구·선택 구역의 라벨 수정/삭제는 영상 위 도구 바에 있습니다. `Zones` 버튼으로 오른쪽 패널을 열면 현재 카메라의 구역 목록·상태·선택 구역 상세를 볼 수 있습니다. 기본은 닫힘이며 좁은 화면에서는 영상 대신 패널을 표시하고, 닫거나 그리기를 시작하면 영상으로 돌아갑니다. 모달·패널 모두 전체 화면에서도 사용할 수 있습니다.
- 구역 추가: `Add zone` → 영상 클릭으로 꼭짓점 배치 → 첫 점 클릭 또는 Enter/`Finish shape`로 완성 → 자동으로 열리고 입력에 포커스되는 라벨 모달에서 기존 라벨 선택 또는 새 라벨 입력 → `Apply label`/`Create & apply`. `Add another`로 이어서 그리고 `Save changes`로 초안을 저장합니다. 라벨 없는 구역은 지정 또는 삭제 후 다음 구역을 추가할 수 있습니다. Backspace는 마지막 점 취소, Esc는 그리기 취소입니다.
- 그림이나 `Zones` 버튼으로 연 패널의 행을 클릭하면 해당 구역을 선택합니다. 목록은 현재 카메라의 구역만 표시하며, 선택만으로 라벨이 바뀌지 않습니다. 편집 중 선택 구역의 `Edit label`/`Set label` 버튼으로 모달을 열어 라벨을 적용합니다. 적용하면 모달이 닫히고 구역 초안에 반영됩니다. 취소·Esc·바깥 클릭으로 닫으면 라벨 변경 없이 도형을 유지하며, 입력 실패 시 모달과 입력값을 유지합니다. 구역 삭제는 도구 바나 패널에서 할 수 있습니다. 보기 모드의 상태·캘리브레이션·다른 카메라 결과도 패널에서 확인합니다. 꼭짓점 드래그, 변 가운데 점 드래그(꼭짓점 추가), 꼭짓점 더블클릭·우클릭(삭제)을 지원합니다. 모달과 배경은 열기 180ms/닫기 140ms 페이드, 패널은 180ms 이동/페이드로 전환하며 시스템의 동작 줄이기 설정을 따릅니다.
- 라벨 입력은 모달에서 등록된 공간 라벨을 목록으로 보여주며 입력한 글자로 검색합니다. 현재 카메라의 다른 구역이 사용한 라벨도 목록에 표시하되 이미 사용 중이라고 안내하고 선택을 막으며, 직접 입력해도 중복 지정을 차단합니다. 다른 카메라와 같은 라벨은 같은 물리적 자리로 연결됩니다. 대소문자·공백만 다른 라벨은 기존처럼 같은 것으로 처리하고, 최대 40자입니다. 새 라벨 생성은 즉시 서버에 반영되며 구역 변경은 저장할 때 반영됩니다. 초안을 취소해도 이미 생성된 라벨은 남습니다. 라벨 생성 실패 시 그림과 입력을 보존하고, 선택 변경·취소 후 늦게 도착한 응답은 다른 구역에 적용하지 않습니다.
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

`status`는 `OCCUPIED`/`EMPTY`/`UNKNOWN`(대소문자 무관, 화면 표시 Occupied/Empty/Unknown), `score`는 0~1입니다. 편집 화면(편집 모드가 아닐 때)에서 그림이나 구역 행을 선택하고 `Zones` 패널을 열면 캘리브레이션 영역(현재 상태, 빈자리 기준 등록, 기준 초기화, 안내)이 열립니다. 완료 여부는 zone의 `calibrated` 값입니다(polygon을 바꾸면 서버가 기준을 지웁니다). 목록 응답은 배열 또는 `{spaces: [...]}`/`{zones: [...]}`를 받습니다. 완료 시 편집 시작 시점의 영역과 비교해 DELETE → PATCH → POST를 하나씩 보내고 카메라를 다시 읽습니다(원자적이지 않음). 하나라도 실패하면 다시 읽은 서버 상태로 화면을 바꾼 뒤 오류를 보여 줍니다. 동시에 편집하면 나중 저장이 덮어씁니다.

실시간 상태는 영상과 같은 `/ws`의 `parking.status_updated`(`data`가 위 status 응답과 같은 모양)로 받습니다. 이벤트는 상태/연결 구조 변화에만 발행되므로 REST `/api/parking/status`는 처음 진입·재연결과 연결 중 10초마다 확인합니다. 서버 연결이 끊기거나 조회가 실패하면 모두 판정 불가로 표시하고, 개별 영역의 새 관측을 15초 동안 확인하지 못하면 해당 판정을 만료시킵니다. 일부 영역만 만료된 자리는 Empty로 표시하지 않습니다. 이전 polygon `revision`이나 더 오래된 관측 시각의 상태는 버립니다. 편집은 별도 초안에서 하므로 상태 갱신·다른 브라우저의 변경 반영이 편집 중인 폴리곤을 바꾸지 않습니다.

주차 운영 도구: 상단 합계 클릭/Spaces 검색·상태·미사용·기준 미등록 필터, Unknown 사유, 라벨 클릭 시 관련 카메라 비교(동일 라벨 영역 강조)를 제공합니다. Empty baselines에서 카메라별 기준 상태를 확인하고 비어 있는 것을 직접 확인한 영역만 개별 등록합니다. Manage labels는 연결된 카메라를 보여 주고 이름을 전체 카메라에 함께 적용하며, 미사용 라벨만 확인 모달 후 삭제합니다. 라벨 변경/삭제는 새 `PATCH|DELETE /api/parking/spaces/{id}`와 Preview 프록시 배포가 필요합니다. 카메라 비교·관리 모달은 기존 WebSocket 클라이언트를 공유합니다.

### 주차 판정 방식 (차량 탐지 / 색상)

`/parking` 상단 `Occupancy method` 카드에서 고릅니다. 기본은 **차량 탐지**이며 빌드 기본값은 `NUXT_PUBLIC_PARKING_OCCUPANCY`(`detection` | `color`, generate 시점 값), 화면에서 고른 값은 이 브라우저의 `localStorage` `retrace.parking.occupancy`에 남고 편집 화면에도 적용됩니다. 라벨·영역(`/api/parking/spaces`, `/api/parking/zones`)은 두 방식이 같은 것을 씁니다.

- **차량 탐지** (`app/lib/vehicleOccupancy.ts`): 영역이 있는 카메라마다 `GET /api/cameras/{id}/vehicles`(서버 차량 detector의 최신 bbox, 서버 poller 주기 2초)를 2초마다 읽어 **브라우저에서** 판정합니다. 차량 bbox 아래쪽 25%(좌우 10% 제외)를 지면 footprint로 보고, footprint의 55% 이상이 영역 안(점유 유지는 35%)이고 footprint 중앙점이 영역 안이며(박스 하단 모서리는 비스듬한 카메라에서 주차선을 넘거나 화면 끝에서 잘리므로 쓰지 않음) 영역 면적의 10% 이상을 덮으면 주차 후보입니다. 차량 하나는 가장 많이 겹치는 영역 하나에만 셉니다. 새 프레임 2번 연속 주차 후보면 Occupied, 3번 연속 비어 있으면 Empty입니다. 신뢰도 0.4 미만이거나 신뢰도 없는(tracker-only) 차량이 겹치면 Empty로 바꾸지 않고 현재 상태를 유지하며, 그런 차량만으로는 Occupied가 되지 않습니다. detector 꺼짐·추론 미완료·프레임 없음은 Unknown, polygon `revision`이 바뀌면 다시 확인합니다. 점수는 footprint 겹침 비율입니다. 기준값은 서버 차량 점유 policy(서버 저장소 `docs/parking-occupancy.md` "좌표와 판정")의 기본값을 따르며 `DEFAULT_OCCUPANCY_POLICY`에서 바꿉니다. 이 방식은 `/ws`가 끊겨도 동작하고, 색상 `/api/parking/status`는 다른 곳에서 바뀐 영역을 알아채는 데만 씁니다. 빈자리 기준(캘리브레이션) UI와 `No baseline` 필터는 숨깁니다.
- 자리별 기준: 편집 화면에서 구역을 선택하면 `주차 판정 기준` 슬라이더(10~95%, 기본 55%)로 그 구역만 기준을 바꿀 수 있고 현재 겹침 비율이 함께 보입니다. 주차 유지 기준은 설정값보다 20%p 낮습니다. 값은 이 브라우저의 `localStorage` `retrace.parking.thresholds`에 저장되며 다른 브라우저와 공유되지 않습니다(서버 저장은 백엔드 변경 필요).
- 편집 화면 상단 오른쪽 이미지 버튼으로 이 카메라의 AI 데이터셋 이미지(라벨된 차량 없는 이미지 우선)를 골라 실시간 영상 대신 그 위에 구역을 그릴 수 있습니다. 좌표는 같은 0~1 정규화라 실시간 영상과 그대로 맞습니다. 고른 이미지는 카메라별로 `localStorage` `retrace.parking.backdrop.<cameraId>`에 남고, 영상 버튼으로 실시간 영상에 돌아갑니다.
- 한계: 판정은 화면을 연 브라우저마다 따로 계산되며 저장·이벤트 기록이 없습니다. 서버 정차 시간·출차 추적 규칙(`stationary_seconds`, `validated_cameras` 등)은 적용하지 않습니다. 실영상 기준값 검증은 아직 하지 않았습니다.
- **색상**: 기존 서버 색상/빈자리 기준 판정(`/api/parking/status`, `/ws` `parking.status_updated`) 그대로입니다.

서버 계약 원문은 서버 저장소의 `docs/color-parking.md`입니다. `/ws` `parking.status_updated`는 카메라별로 그 카메라에 연결된 공간들의 전체 상태를 보내며 `parkingSpaceId`로 덮어씁니다. 영역 삭제 후 오는 `spaces: []`에는 해당 카메라 영역과 상태를 REST로 다시 읽습니다. 서버는 라벨 대소문자를 구분하지만 프론트는 `A-01`/`a-01`을 같은 라벨로 보고 새로 만들지 않습니다.

## AI 데이터셋

내비게이션 `AI Datasets` → `/datasets`에서 카메라 캡처·이미지 필터/삭제·YOLO ZIP 생성을,
`/datasets/images/:imageId`에서 차량 박스 생성/이동/크기 수정·클래스 변경·검수를 제공합니다.
확대/리사이즈는 기존 `useVideoStage`/`videoGeometry`를 재사용하고 박스는 원본 픽셀 좌표로 저장합니다.
캡처 목록은 화면당 최대 두 줄(1280px 이상 8장, 640px 이상 4장, 그 미만 2장)로 페이지를 나눕니다.
화면 크기가 바뀌면 기존 첫 이미지 순번을 포함하는 페이지로 전환하며, 라벨링 후 돌아오면 필터·페이지 위치를 복원합니다.
라벨링의 이전·다음 이동은 현재 필터의 전체 이미지 순서를 따라 페이지 경계를 넘습니다.
검수 없이 내보내지 않으며, 빈 이미지 검수는 별도 확인이 필요합니다. 자동 라벨은 아직 제공하지 않습니다.
화면은 실제 `/api/datasets`만 사용하며 서버에 API가 없거나 clean capture가 꺼져 있으면 오류를 표시합니다.
새 설정 주소나 프론트 Mock 데이터는 없습니다. API·DB·볼륨·캡처·분할·운영 적용 원문은
[AI 데이터셋 계약](../docs/datasets.md)을 따릅니다. 핵심 구현은 `DatasetBoxEditor.vue`,
`pages/datasets/`, `composables/datasetApi.ts`, `types/dataset.ts`입니다.

내비게이션 `AI Training` → `/training`에서 데이터셋 export 선택·적합성 확인, 학습 설정·시작·중지, 진행(epoch·loss·mAP·GPU·로그),
기존 모델과의 같은 test 이미지 비교, 모델 registry를 제공합니다. 시작·중지·다운로드는 서버 `TRAINING_ADMIN_TOKEN`을 입력해야 하며
토큰은 sessionStorage에만 둡니다. Preview proxy는 body 없는 POST를 415로 거부하므로 `/auth`, `/stop`도 `Content-Type: application/json`과 `{}`를 보냅니다. 계약은 [웹 fine-tuning](../docs/training.md), 구현은 `pages/training.vue`,
`components/Training*.vue`, `composables/trainingApi.ts`, `lib/training.ts`입니다.

## 화면 언어 (한국어 / English)

상단 바 오른쪽(휴대폰은 바 아래)의 `한 | EN` 토글로 UI 문구를 바꿉니다. 선택은 `localStorage`의 `retrace.locale`에 저장되고,
저장된 값이 없으면 브라우저 언어가 한국어일 때 한국어, 그 밖에는 영어로 시작합니다. 새 dependency 없이 `composables/useI18n.ts`의
`t('English text', { name })`를 씁니다. 영어 원문이 key이고 한국어는 `lib/i18n.ko.ts`에 있으며, 사전에 없는 문구(서버가 보낸 오류·경고,
카메라 이름 등)는 받은 그대로 보입니다. 화면 문구를 추가하면 `lib/i18n.ko.ts`에도 넣습니다. `tests/i18n.test.cjs`가 문자열 리터럴로 쓴
`t('…')` key의 번역 누락과 `{placeholder}` 불일치를 잡습니다(변수로 넘기는 lib/API 메시지는 직접 확인).

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

Dataset controls share `DatasetSelect.vue` (Reka UI, body scroll lock disabled). Camera options match names containing `주차장`. The box editor stays in Draw mode between boxes, shows solid image-aligned crosshairs, and uses Select / resize or a box-list row to edit an existing box. Capture/review feedback reserves layout space and refresh keeps existing cards visible.

데이터셋 ZIP 구조·학습 흐름·다른 모델에 전달할 프롬프트: [학습 안내](docs/dataset-training.md).

라벨 도구 바는 기본 그리기이며 Select / resize 버튼으로 편집을 토글합니다. 새 박스는 car로 생성하고 기존 클래스는 보존합니다. 목록의 각 행·도구 바·Delete 키에서 박스 삭제 모달을 열며 저장 전에는 초안에만 반영됩니다. 이미지 삭제는 영구 삭제 확인 모달을 사용합니다.

편집 도구 바는 `.editor-tools` 아이콘 그룹 스타일을 공유합니다. 모달은 root scrollbar gutter와 ConfigProvider의 padding/margin 0 조합으로 스크롤 잠금 시 중복 여백을 방지하고, 공통 fade/scale 애니메이션 및 reduced-motion을 지원합니다.

브라우저 native confirm은 `useConfirmDialog`와 레이아웃의 `AppConfirmDialog`로 통일합니다. 내부 화면 이탈·편집 취소·캘리브레이션은 비동기 확인 모달을 기다리고, 닫을 때 표시 내용은 유지합니다. 탭 종료/새로고침의 `beforeunload`는 브라우저 제어이므로 유지합니다. Parking의 Zones는 우측 끝에 독립하며 편집 중 폴리곤 선택 시 라벨 모달을 엽니다.

최신 라벨 편집 UI는 임시저장 없이 Save labels로 저장·검수 완료를 함께 처리합니다. 확대 도구는 영상 중앙, 저장은 영상 우측, 선택 박스 삭제는 Boxes 목록 상단에 둡니다.

데이터셋 목록 진입 시 필터와 최신순 ID 순서를 보관해 편집기의 이전/다음에 재사용합니다. 페이지 경계를 지원하고 저장 후 상태가 바뀌어도 순서를 유지합니다. 브라우저 세션 저장소를 사용할 수 없으면 메모리로만 유지합니다. 영상 위 빈 메시지 줄은 없고 하단 설명과 완료 메시지가 같은 높이를 사용합니다. 내보내기 삭제는 원본/라벨을 유지하며 서버의 새 DELETE API가 필요합니다. 새 ZIP은 car-only, 과거 다중 클래스 기록은 Legacy로 표시합니다.
