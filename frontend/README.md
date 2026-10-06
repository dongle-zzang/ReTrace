# ReTrace Frontend

Nuxt 4 기반 카메라 상태 및 MJPEG 미리보기 대시보드입니다. Backend와 Preview server는 별도로 실행됩니다.

## 로컬 실행

Node.js 22.12 이상과 npm이 필요합니다. 이 프로젝트는 Node 22.17.0에서 확인했습니다.

```powershell
Copy-Item .env.example .env
npm install
npm run dev
```

`.env`의 두 주소를 로컬 PC 브라우저에서 접근 가능한 실제 Backend 및 Preview server 주소로 바꾸세요. Backend 주소에는 `/api`를 붙이지 않고, Preview 주소에는 `/mjpeg`를 붙이지 않습니다. 예를 들어 `camera.preview_path`가 `/mjpeg/source6`이면 Preview 주소 뒤에 그대로 결합합니다. `.env`는 Git에서 제외됩니다.

```dotenv
NUXT_PUBLIC_BACKEND_BASE_URL=http://backend-host:8000
NUXT_PUBLIC_PREVIEW_BASE_URL=http://preview-host:8080
```

Backend는 로컬 개발 주소의 CORS 요청을 허용해야 합니다. 영상은 Backend를 거치지 않고 브라우저에서 Preview server로 직접 요청합니다. RTSP URL과 CCTV credential은 사용하지 않습니다.

## 확인

```powershell
npm run typecheck
npm run build
```

첫 화면은 `GET /api/cameras`의 `status`와 `preview_path`를 사용합니다. 목록은 10초마다 갱신하며, 화면에 보이는 카메라 카드만 MJPEG를 연결합니다.
