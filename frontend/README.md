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

## 정적 배포

```powershell
npx nuxt generate
```

`.output/public/`의 **내용물**을 서버 `frontend/` 바로 아래에 업로드합니다(`frontend/index.html`, `frontend/_nuxt/`). `.output/public` 폴더째 한 단계 중첩하지 않습니다. 배포본은 같은 origin에서 `/api`, `/ws`를 쓰므로 별도 주소 설정이 필요 없습니다. `NUXT_PUBLIC_PREVIEW_VIDEO_FPS`는 generate 시점 값이 들어갑니다.

## 확인

```powershell
npm run typecheck
npm test
npm run build
```

실제 서버와 브라우저에서 6대 이상 동시 재생, 필터·스크롤 시 구독 변경, 서버 단절 후 재접속, DevTools Memory/Network에서 object URL·JPEG 누적 없음 등을 확인합니다.
