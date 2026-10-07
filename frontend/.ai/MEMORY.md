# Frontend memory

비자명한 장기 제약과 그 이유만 둔다.

## `/ws` dev 프록시는 `nuxt.config.ts`의 `listen` hook이 직접 처리한다

- 이유: 서버는 같은 origin의 WebSocket만 허용한다(Origin 검사). Nitro `devProxy`는 HTTP만 전달하고 upgrade는 Nitro worker로 보내며, Vite `server.proxy`는 Nuxt의 middleware mode에서 upgrade를 받지 못한다. 그래서 `listen` hook에서 `/ws` upgrade만 가로채 Node `http`로 전달하고 `Origin`을 서버 origin으로 바꾼다. 서버의 `PREVIEW_WS_ORIGINS`는 compose에 연결돼 있지 않아 쓰지 않는다.
- 변경 시 확인: dev server에서 `ws://localhost:<port>/ws`로 연결해 `hello` 수신과 binary 프레임 수신을 확인한다. Nuxt/Nitro 업그레이드 후 기존 upgrade listener(HMR 등) 위임이 유지되는지 본다.
- 잘못 바꾸면: dev에서 영상이 403이거나 소켓이 Nitro worker로 가서 끊긴다(운영 빌드에는 영향 없음).

## 코드는 상대 경로 `/api`, `/ws`만 사용한다

- 이유: 운영 빌드는 서버 `frontend/`에 올라가 같은 origin에서 제공된다. `runtimeConfig.public`에 base URL 키를 두면 `.env`의 `NUXT_PUBLIC_*` 값이 `nuxt generate` 산출물에 절대 주소로 들어가, 다른 인터페이스(LAN/Tailscale)로 접속할 때 cross-origin이 되고 서버 주소가 산출물에 노출된다.
- 변경 시 확인: generate 산출물에서 서버 주소가 검색되지 않는지 확인한다.

## 영상은 화면당 소켓 1개, 카메라별 단일 대기 슬롯으로 표시한다

- 이유: 카메라마다 MJPEG `<img>`를 열면 브라우저의 origin당 HTTP/1.1 연결 6개 제한으로 6대에서 멈춘다. 느린 클라이언트에서 프레임을 큐에 쌓으면 지연·메모리가 늘어나므로 decode 중 도착한 프레임은 하나의 슬롯에 덮어쓴다. 화면의 `<img>.src`를 바로 바꾸면 빈 프레임이 보여 깜빡이므로 offscreen `Image.decode()` 후 교체한다.
- 변경 시 확인: `tests/realtimePreviewClient.test.cjs`, DevTools에서 blob URL 누적 여부.

## `CameraVideo.vue`는 카메라 객체가 아니라 원시 값만 watch한다

- 이유: `/api/cameras`가 10초마다 새 객체 배열로 교체된다. 객체 참조를 watch하면 10초마다 구독 해제/화면 비움/재구독이 일어나 영상이 깜빡인다.
- 변경 시 확인: watch source가 `camera_id`, 표시 여부, client 같은 원시 값인지 확인한다.

## 사람 박스는 클라이언트에서 그리지 않는다

- 이유: 서버가 JPEG에 bbox와 Track ID를 이미 그려 보낸다. `detections`로 SVG를 다시 그리면 박스가 이중으로, 시간차를 두고 보인다. `CameraOverlay.vue`는 선/영역 표시용으로 남겨 두었다.

## 루트 `.gitignore`가 `frontend/`를 무시한다

- 이유: 서버 저장소에서 `frontend/`는 정적 빌드 업로드 폴더이고 서버 쪽에서는 프론트 작업을 하지 않는다. 프론트 소스는 강제로 추적한다.
- 변경 시 확인: 새 파일은 `git add -f`. 커밋 전 누락 여부를 확인한다(누락 시 저장소에서 빌드 실패).
