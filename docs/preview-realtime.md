# CCTV 실시간 Preview

## 전송 구조

`preview.py`의 기본 모드는 `webrtc`다. 기존 입력·mux(1920×1080)·PeopleNet·NvDCF와
native RTSP reconnect는 유지한다. tracker probe는 PyDS 값을 복사하고, 네트워크/DB I/O는 하지 않는다.

```text
RTSP → nvurisrcbin(NVMM) → nvstreammux → PeopleNet → NvDCF
  ├─ tracker probe → MetadataStore → /ws JSON → 브라우저 SVG/Canvas
  └─ nvstreamdemux → 카메라별 아래 encoding branch
      queue(leaky, 1 raw buffer) → nvvideoconvert
      → NVMM NV12 (기본 1280×720) → nvv4l2h264enc (Baseline, CBR, 기본 2 Mbps)
      → h264parse → byte-stream/AU, constrained-baseline → appsink
      → bounded EncodedStore (압축 데이터만 Python으로 복사)
      → 시청자별 appsrc → h264parse → rtph264pay → webrtcbin
      → ICE/DTLS/SRTP → 브라우저 <video>
```

카메라당 encoder 하나를 모든 시청자가 공유한다. 시청자·카메라 조합마다 WebRTC peer 하나가
필요하고 같은 WebSocket에서 모두 협상한다. RTP packetization/암호화/네트워크 비용은 peer마다
발생한다. SFU/MCU, 재디코딩, CPU JPEG/H.264 인코딩, raw CPU frame 복사는 기본 경로에 없다.
GPU 변환/스케일링에 필요한 NVMM surface 이동까지 zero-copy라고 보장하지는 않는다.
V100은 NVIDIA [지원표](https://developer.nvidia.com/video-encode-decode-support-matrix)상 H.264 NVENC를 지원한다.

raw queue는 오래된 프레임을 encoder **앞에서** 버린다. compressed appsink는 프레임을 임의로
버리지 않으며 callback은 복사/메모리 저장만 한다. EncodedStore는 카메라당 최대 8 AU,
250 ms 이내 데이터만 제공한다. 시청자가 AU를 놓치거나 appsrc backlog가 1 MiB에 도달하면
delta frame을 보내지 않고 다음 keyframe부터 재개한다. DeepStream 7/GStreamer 1.20에서는
appsrc도 최대 2개 pending buffer를 기준으로 전송을 제한한다. 기본 GOP는 30 frame이다.
원본 FPS가 낮으면 keyframe을 기다리는 시간이 길어지므로 `PREVIEW_VIDEO_GOP`를 조절한다.
누락/네트워크 손실 후 화질 회복은 주기적 IDR에 의존한다. peer의 RTCP PLI를 공유 encoder에
직접 전달하는 기능과 시청자별 adaptive bitrate는 아직 없다. 느린 회선에는 bitrate/해상도를 낮춘다.
기존 RTSP latency 기본 1000 ms는 유지하며 전체 지연이 수십 ms라고 가정하지 않는다.

기본 server cap은 WebSocket 16개, 전체 peer 32개, connection당 peer 최대 32개다.
연결 종료·ICE 실패·30초 협상 timeout 시 peer pipeline과 request pad를 반환한다.
카메라 encoder는 연결된 카메라 수만큼 항상 실행하므로 미시청 카메라도 encoding 비용이 든다.
NVENC 처리량/GPU memory, 여러 시청자 CPU/대역폭은 실제 camera 수와 FPS로 측정해야 한다.

## WebSocket 계약

기존 HTTP listener의 `/ws`를 upgrade한다. 기본 같은 origin만 허용한다.
Nuxt dev origin을 직접 연결할 때 `PREVIEW_WS_ORIGINS`에 정확한 origin을 쉼표로 설정하거나
dev proxy에서 `/ws` upgrade를 전달한다. Backend의 CORS 설정과 별개다.
`/ws`에는 JPEG/H.264 frame을 전송하지 않는다.

metadata/event envelope는 `version`, `type`, `cameraId`, `timestamp`(UTC 처리 시각),
`runtimeSession`, `generation`을 사용한다. camera 상태와 detections에는 `sourceId`도 있다.
track identity는 `(cameraId, runtimeSession, generation, trackId)`다. uint64 trackId와
나노초 PTS는 JavaScript 정밀도 보존을 위해 **10진 문자열**로 전달한다.

```json
{
  "version": 1,
  "type": "detections",
  "cameraId": "camera-01",
  "sourceId": 0,
  "runtimeSession": "process-session",
  "generation": 1,
  "timestamp": "2026-10-07T00:00:00.123+00:00",
  "frameNumber": 100,
  "ptsNs": "1234567890",
  "inferenceDone": true,
  "persons": [{
    "trackId": "42",
    "bbox": {"x": 0.12, "y": 0.24, "width": 0.18, "height": 0.46},
    "confidence": 0.93,
    "trackerConfidence": 0.85
  }]
}
```

bbox는 mux 크기로 나누고 화면 경계에 clip한 0~1 값이다. 축소된 영상과 같은 화면 비율을
사용한다. `object-fit: contain`의 letterbox 영역이 있다면 실제 영상 사각형에 overlay를 맞춘다.
`timestamp`는 camera capture/NTP 시간이 아니다. PTS는 원본 metadata 기준이고 WebRTC
appsrc는 peer running-time을 사용한다. 기본 overlay는 최신 metadata를 약 10 Hz로 반영하며,
프레임 단위의 영상/metadata 동기화를 보장하지 않는다. 정확한 동기화는 RTP timestamp 매핑이 필요하다.

| type | payload/역할 |
| --- | --- |
| `detections` | persons; 빈 배열은 overlay 삭제. 무효화 시 `stale:true` clear도 전송 |
| `camera_status` | `status`에 기존 runtime 필드 포함 |
| `track_update` | 확장 producer의 `data` (예: personId/Re-ID 결과) |
| `parking_status` | 확장 producer의 `data` (예: spaces 배열) |
| `event` | 확장 producer의 `data` (예: kind=line_crossing/zone, eventId) |
| `subscribe` | client command: `cameraIds` 배열. 빈 배열은 metadata 구독 해제 |
| `watch` / `unwatch` | client command: `cameraId`, client가 정한 고유 `peerId` |
| `offer` / `answer` | 서버 offer, 브라우저 answer: `cameraId`, `peerId`, `sdp` |
| `ice` | 양방향: `cameraId`, `peerId`, `candidate`, `sdpMLineIndex` |
| `hello` / `subscribed` / `error` | connection/control 응답. error는 안전한 code만 제공 |

연결 직후 모든 카메라 metadata를 구독한다. `subscribe`로 목록을 바꾸며 영상 watch와 독립적이다.
같은 connection에 여러 `watch`를 보낼 수 있다. SDP/ICE만 주고받는 signaling은
[GStreamer webrtcbin](https://gstreamer.freedesktop.org/documentation/webrtc/)으로 처리한다.
브라우저는 offer 수신 시 `setRemoteDescription`, `createAnswer`, `setLocalDescription` 후 answer를
보내고 ICE를 trickle 교환한다. SDP 준비 전에 도착한 ICE는 양측에서 잠시 보관한다.
peerId는 connection 안에서만 유효하며 재시도 시 새 ID를 사용한다.

Re-ID/parking/line/zone 계산은 구현하지 않았다. 향후 producer는
`server.realtime_hub.publish_message(camera_id, type, detached_json_data)`로 확장 메시지를
발행할 수 있다. 구독별 bounded outbox로 전달하며 네트워크 I/O는 WebSocket thread가 맡는다.
모든 이벤트를 영구 보관하거나 재생하는 전달 보장은 없다. outbox가 넘치거나 socket write가
1초 이상 막히면 connection을 종료한다. durable event 저장은 별도 Backend 정책이 필요하다.

`/metadata.json`의 기존 pixel bbox/snake_case schema와 Backend polling/DB 저장 정책은 유지한다.
`/streams.json`은 기존 id/runtime/session을 유지하고 `format=webrtc`, `url=/ws`,
`signaling_path=/ws`, `metadata_path=/ws`를 제공한다. Backend `/api/cameras`에는
`preview_format`, `signaling_path`, `metadata_path`를 추가했고 기본 `preview_path`는 `/ws`다.
`preview_format=webrtc`의 경로를 `<img src>`에 넣으면 안 된다.

## Vue/Nuxt와 진단 페이지

업로드된 제품 build는 자동 변환되지 않는다. mount 이후 `/webrtc.js`를 client script로 로드해
`new window.ReTraceRealtime('/ws', onMessage)`를 화면당 하나 생성하고,
`realtime.watch(cameraId, videoElement, onState)`로 영상 세션을 만든다.
반환된 stop 함수를 unmount/화면 이동 시 호출하고 마지막에 `realtime.close()`한다.
Vue에서는 `persons`를 cameraId별 reactive state에 넣고 SVG/Canvas로 그린다.
Nuxt SSR 시 WebSocket/RTCPeerConnection 생성은 `onMounted` 이후에만 수행한다.

`/diagnostics`는 제품 build가 있어도 기본 진단 페이지를 표시한다. 4개 카메라씩 WebRTC를
재생하고 페이지 변경 시 peer를 해제한다. 하나의 WebSocket으로 metadata를 받고 SVG overlay를
그린다. metadata 단절/카메라 offline 및 1초 TTL에서 overlay를 비운다. 서버 재시작으로 WebSocket이
닫히면 재접속하여 현재 카메라들의 WebRTC peer도 다시 만든다.

## Docker / LAN / Tailscale

DeepStream 7.0 image에 GStreamer bad/good/nice, GstWebRTC/GstSdp GI bindings와 wsproto를
추가한다. `NVIDIA_DRIVER_CAPABILITIES` 기본값은 `compute,utility,video`이며 운영 환경에서
이미 설정한 값이 있다면 `video` 포함 여부를 확인한다. GPU 예약·RTSP·PGIE/NvDCF·DB port는 유지한다.

**HTTP 40225 port publication만으로는 WebRTC media가 통과하지 않는다.** 기본 bridge의
container ICE candidate는 외부 browser에서 직접 도달할 수 없을 수 있다. STUN만 추가해도
Docker UDP publication/방화벽 문제를 해결한다고 가정하지 않는다.

LAN/Tailscale에서는 양쪽 host candidate에 직접 접근할 수 있으면 STUN/TURN이 필요 없다.
가장 단순한 제공 옵션은 retrace의 host network다. 기존 ignored override를 수정하지 않고
`compose.webrtc.example.yaml`을 **명시적으로** 사용한다. 이 옵션은 기존 TCP port publication을
host listener binding으로 대체하므로 별도 적용이 필요하다. private `.env`에서:

- `WEB_BIND_IP`: 기존 LAN/Tailscale의 특정 IPv4 interface 하나. wildcard를 사용하지 않는다.
- `BACKEND_PREVIEW_URL`: 해당 listener의 HTTP origin. Backend bridge에서 이 interface로 접근해야 한다.
- `PREVIEW_BACKEND_URL`: 기본 `http://127.0.0.1:8000`; Backend의 기존 loopback port가 다르면 조절한다.
- `PREVIEW_MODE`: 기본 `webrtc`. 두 서비스에 동일하게 적용한다.

실제 주소는 공유 문서/코드에 쓰지 않는다. host mode의 healthcheck도 지정한 interface를 사용한다.
아래 명령은 `-f`를 명시하여 기존 자동 `compose.override.yaml` port mapping을 병합하지 않는다.
Backend/PostgreSQL은 기존 bridge와 DB volume을 유지한다. 방화벽은 사용하려는 LAN/Tailscale에서
ICE가 고른 UDP 경로에 접근할 수 있어야 한다. 이 구현은 고정 UDP port range/NAT candidate rewrite를
제공하지 않으므로 bridge를 유지해야 하는 환경은 별도 ICE/UDP 배포 설계가 필요하다.

```bash
docker compose -f compose.yml -f compose.webrtc.example.yaml up -d --build
docker compose -f compose.yml -f compose.webrtc.example.yaml exec -T retrace python3 tools/check_preview.py
docker compose -f compose.yml -f compose.webrtc.example.yaml exec -T retrace python3 tools/check_webrtc.py --loopback
```

host network를 도입하지 않는 기존 Compose에서도 `PREVIEW_MODE=mjpeg`로 debug/rollback할 수 있다.
`PREVIEW_MODE` 변경 시 retrace와 backend를 함께 재생성한다. MJPEG mode는 기존 OSD/JPEG branch만
실행하며 H.264와 동시 인코딩하지 않는다. WebSocket metadata는 legacy mode에서도 제공한다.
기존 소규모 구현을 유지하면 RTSP/inference/tracker 문제와 WebRTC/ICE 문제를 분리할 수 있어 남겨 두었다.

| 설정 | 기본값 |
| --- | --- |
| `PREVIEW_MODE` / `--preview-mode` | `webrtc` (`mjpeg`는 legacy OSD 진단) |
| `PREVIEW_VIDEO_WIDTH`, `PREVIEW_VIDEO_HEIGHT` | 1280, 720 |
| `PREVIEW_VIDEO_BITRATE` | 2000000 bits/s, 카메라당 |
| `PREVIEW_VIDEO_GOP` | 30 frames, 주기적 IDR |
| `PREVIEW_WS_ORIGINS` | 빈 값, 같은 origin |
| `WEBRTC_STUN_SERVER`, `WEBRTC_TURN_SERVER` | 빈 값, 추가 인프라 없음 |

외부 NAT/UDP 차단 환경에서는 STUN/TURN이 필요할 수 있다. server는 위 변수로 webrtcbin의
STUN/TURN URI를 설정할 수 있고 browser helper의 세 번째 인자로 `RTCPeerConnection` config를
전달할 수 있다. TURN credential은 배포 설정/일시 credential로 관리한다. 공개 인터넷 배포 시
HTTPS/WSS·인증 경계도 별도로 준비해야 한다. 이번에는 TURN/SFU 서비스를 추가하지 않았다.

## 검증과 한계

CPU 회귀/실제 loopback WebSocket은 `tests/test_preview_realtime.py`로 확인한다.
`tools/check_webrtc.py`는 native plugin inventory와 synthetic H.264 SDP/ICE loopback을 확인한다.
`--software-test-source`는 checker 전용 CPU fixture이며 운영 경로에 CPU 인코더를 넣지 않는다.
loopback의 `received_units`는 H.264 depay/parser 수신 수이며 브라우저 렌더링 증거는 아니다.

GPU 서버 적용 후 `/diagnostics`에서 1→2→4개 카메라를 동시에 재생하고 browser Network의 `/ws`,
WebRTC internals의 selected candidate pair·bytesReceived·framesDecoded·packetsLost를 확인한다.
`/metadata.json`과 WebSocket의 cameraId/track ID·bbox를 대조하고 실제 사람이 이동할 때 overlay를 확인한다.
느린 네트워크에서 오래된 영상 backlog가 누적되지 않는지, 페이지 이동 후 peer 수가 회수되는지,
RTSP 장애/복귀와 컨테이너 재시작 후 영상/metadata가 함께 복귀하는지도 확인한다.
RTSP 연결·PeopleNet·NvDCF·다중 GPU encoding·실제 browser·컨테이너 재시작은 이 checker로 대체하지 않는다.
