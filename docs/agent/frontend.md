# Frontend 개발 경계

제품 Frontend는 별도 로컬 PC에서 CSR로 개발·빌드하고 정적 산출물을 GPU 서버에 업로드한다.
`preview.py`의 기존 40225 listener가 `preview_web.py`를 통해 빌드와 같은 origin의 API·영상을 제공한다.
서버에 Node runtime이나 별도 frontend 서비스는 필요 없다. 업로드·Nuxt 설정·적용 절차의 원문은
[frontend deployment](frontend-deployment.md)다.

빌드 디렉터리는 `FRONTEND_DIST_DIR`로 지정하며 기본값은 저장소의 `frontend/`다.
`index.html`이 있으면 `/`와 JS/CSS/이미지 등 빌드 파일을 제공한다. 확장자 없는 HTML navigation은
SPA index로 fallback하며 누락된 asset, 숨김 경로, 상위 경로 및 외부 symlink는 404다.
정적 제공 범위는 전용 빌드 디렉터리이며 저장소 전체가 아니다.
빌드가 없으면 기존 `web/index.html`과 `web/preview.js`의 영상 확인 페이지를 제공한다.

확인 페이지는 `/streams.json`의 `format`에 따라 WebRTC video 또는 legacy MJPEG img를 사용한다.
4개씩 재생하며 페이지 변경/unmount 시 peer를 닫는다. `web/webrtc.js`는 화면당 WebSocket 하나로
SDP/ICE와 다중 camera metadata를 처리한다. 기본 영상에는 OSD가 없고 `web/preview.js`의 SVG가
normalized bbox/track ID를 표시하며 metadata 단절/TTL에서 제거한다. `/diagnostics`는 build가 있어도
확인 페이지를 제공한다. 제품 build는 별도로 새 계약에 맞춰 갱신해야 한다.
연동 API와 동기화 한계는 [실시간 Preview](../preview-realtime.md)를 따른다.
Backend는 기존 Preview JSON을 polling하며 영상을 relay하지 않는다.

배포한 Frontend는 `/api/*`를 같은 40225 origin으로 호출한다. HTTP request thread에서
`PREVIEW_BACKEND_URL`의 Backend로 GET을 전달하며 현재 API 계약·query·status를 유지한다.
Backend 장애/timeout은 502이며 frontend 정적 파일 및 MJPEG 제공과 별도로 처리한다.
Backend API의 `preview_format`을 확인하고 `signaling_path`, `metadata_path`를 같은 origin에서 사용한다.
상대 `preview_path`를 img로 사용하는 것은 MJPEG mode에만 해당한다.
API/metadata 계약은 [backend](backend.md), 제한된 인터페이스 바인딩과
브라우저 CORS 설정은 [deployment](deployment.md) 및 해당 문서가 연결한
backend README를 따른다. 실제 주소/credential은 공유 문서에 기록하지 않는다.

Preview HTTP와 API readiness의 CPU 검증 명령은 [testing](testing.md)에 있다.
