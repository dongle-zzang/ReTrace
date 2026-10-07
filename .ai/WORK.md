# Current work

## MJPEG over one WebSocket (WebRTC removed) — deployed

- Goal: user abandoned WebRTC (2026-10-07; NVENC worked after fixes, but browser ICE stayed `connecting` and ended in a 30 s server timeout; UDP/firewall cause unverified). Keep the OSD/JPEG branch and show all cameras: the old 6-camera ceiling is attributed (inferred, not measured) to the browser's 6 HTTP/1.1 connections per origin with one long-lived `/mjpeg/sourceN` response each.
- Done: deleted `preview_webrtc.py`, `web/webrtc.js`, `compose.webrtc.example.yaml`, `tools/check_webrtc.py`, `tests/webrtc_web.test.cjs`. Reverted `preview.py`/`startup_timing.py`/`tools/check_preview.py`/Dockerfile to the pre-WebRTC MJPEG pipeline plus `/ws` (no PREVIEW_MODE, single 0.0.0.0 listener, no `WEB_BIND_IP`, no `NVIDIA_DRIVER_CAPABILITIES` override). `preview_socket.py`: `subscribe` accepts `video`/`videoFps` (1–30, default 10) and sends each subscribed camera's latest `FrameStore` JPEG as a binary message (u8 version, u8 id length, id, u32 BE sequence, JPEG); no resend of the same sequence, rate-limited, replies before frames, 5 s write timeout. `/diagnostics` (`web/preview.js`) shows every camera over one socket. Backend CameraOut drops `preview_format`/`signaling_path`; `preview_path` is `/mjpeg/sourceN` again, `metadata_path` `/ws` stays. `.env` restored from the agent's backup (the two appended lines removed); `wsproto` stays in requirements.
- Verified (CPU): realtime 7, HTTP 4, CLI 8, startup 3, camera 64, checker 4, PGIE cache 10, Backend 39, node diagnostics 1; compose YAML parses; links and `git diff --check` OK. Deployed 2026-10-07 with the normal bridge Compose: live `/ws` probe received all 18 online cameras (1035 valid JPEGs in 8 s, ~7.2 fps/camera at videoFps 10, ~127 KB/frame, ~134 Mbps measured on the server itself); user confirmed every camera plays in the browser `/diagnostics`.
- Backend image rebuilt 2026-10-07: `/api/cameras` returns `preview_path=/mjpeg/sourceN` and `metadata_path=/ws`, health 200. Committed and pushed as `0840ae0`.
- Next: Retrace image rebuild is optional (only drops the unused WebRTC apt packages).
- Unrelated: 1f_lobby/3f_terrace_4k/5f_server_room/7f_rooftop had RTSP timeout/error during the WebRTC runs. Preserve the earlier `.env.example` deletion / camera YAML edits and other tasks below.

## PGIE TensorRT engine cache — GPU verification pending

- Goal: restrict the cache identity to engine build inputs and keep load/save paths in `.cache/pgie`; preserve detection threshold and the shared RTSP/NvDCF/MJPEG pipeline.
- Completed: `pgie_cache.py` hashes explicit build options, model/build-file contents and GPU/TRT/DeepStream compatibility identity. Native model hardlink/copy staging makes builder output and config/GObject paths agree; matching precision fallback is discovered within the same key. Both app/preview use the shared config setter. Selected config precision is honored (bundled default FP16); threshold remains 0.2. Unproven legacy engines are preserved but not imported. README/architecture/testing describe the policy.
- Verified: cache tests 10, camera tests 64 (including shared construction 13), CLI tests 8 passed in `/tmp/retrace-shared-venv`; syntax (5 files), git diff --check and scoped before/after review passed. No source/mux/tracker/demux/JPEG lifecycle changes. CPU fixtures simulate engine bytes and do not prove TensorRT serialization/deserialization.
- Verification limit: Docker daemon access is denied even outside sandbox; host V100 is visible outside sandbox, but installed source is DeepStream 6.1 rather than deployed 7.0. No service was restarted. Actual cold/warm GPU validation remains pending; see testing document. Warm nvinfer state-transition delay (8–9 seconds) is outside scope.
- Next: on the GPU server with Docker permission, restart retrace once and wait for engine creation/normal video, then restart again and confirm canonical `.cache/pgie` deserialization without rebuild. Existing unrelated dirty files and tasks below must be preserved; no commit/push performed.

## Preview browser playback — confirmation pending

- Live `/` and `/preview.js` returned HTTP 200 and matched workspace files; `/mjpeg/source0` returned multipart JPEG bytes with valid start/end markers. Full short MJPEG check delivered frames from 21 cameras; previous three missing cameras plus `b1_parking_overview` had no frames. Browser and direct URL behavior requested from user; not yet received.
- Found restored page opened all 25 long-lived MJPEG requests against HTTP/1.0 server. Browser connection exhaustion is a plausible cause of queued images/status polling, not yet reproduced in a real browser (no browser automation installed). Fixed page to play four cameras at a time with previous/next controls; old streams close on navigation and page exit. HTML/JS are read per request, so this asset-only fix needs refresh rather than service restart.
- Verification: Node test covers 25-camera bounded playback, recovery, navigation and cleanup. Original local HTTP tests and pipeline tests passed earlier; GPU pipeline unchanged. Next action: refresh browser page and confirm playback/status updates; diagnose remaining camera failures separately. Preserve other tasks below.

## Current RTSP/output investigation

- Current source: all 25 effective inputs use profile2; TCP/native nvurisrcbin reconnect retained; drop-on-latency defaults true; child traversal only observes diagnostics. NVMM gate retained, source connects directly to mux (1920×1080), with no input queue/converter/caps. Person pre-cluster-threshold is 0.2 even when an installed sample is selected; NvDCF perf tracker is unchanged.
- User reported restart complete. Post-restart live checks on 2026-10-06: a new batch-25 generated config contains person threshold 0.2 and excluded classes 1;2. Initial connecting/no-frame period resolved during observation; 22 cameras became online. All 22 metadata frame numbers advanced, inference_done was true, and a snapshot had 11 persons, all class 0 with track IDs. All 25 MJPEG endpoints checked again: 22 delivered frames; three did not within the five-second read timeout. This is a short functional check, not a long stability/reconnect test.
- Remaining issue: `3f_terrace_4k`, `5f_server_room`, `7f_rooftop` still have no output, matching the pre-restart baseline. The previous direct RTSP probe failed for terrace and timed out for the latter two; exact native cause remains unverified.
- Access: Preview HTTP works through configured private binding; Docker socket access remains denied outside sandbox. Host nvidia-smi now works (V100, active Python GPU process). No native log/caps inspection obtained; requested safe filtered startup logs from user. Do not claim CUDA/caps logs are error-free or that exact initialization delay is measured.
- Next action: with Docker access or safe operator log output, confirm startup TCP/drop/PGIE/NvDCF settings and diagnose the three failing cameras. User's failing browser/frontend display path remains unknown. Preserve other tasks below.

## Startup timing — server verification pending

- Goal: measure the exact first-frame delay without changing shared architecture, model/tracker, buffering defaults or Backend.
- Completed: `startup_timing.py` plus `preview.py`/`preview_mjpeg.py` diagnostics record source creation, native RTSP progress/request/SDP/RTP, decoder input/output, per-camera mux/PGIE/tracker/demux/JPEG/appsink/store/HTTP and runtime online. Native state messages use a synchronous observer; GPU memory sampling runs separately. Added diagnostics-only `--startup-source-count {1,2,4,8}` and safe JSON summaries.
- Confirmed in code: all source branches built in NULL before one shared PLAYING call; no per-camera connection/state wait; one PGIE and one tracker. Online has a default 2-second continuous-frame condition distinct from first JPEG.
- Verified: `/tmp/retrace-shared-venv/bin/python` camera tests 64 passed; CLI tests 7 passed; HTTP tests 2 passed outside sandbox for local sockets; syntax and diff whitespace passed. Final weak-reference observer change additionally passed 6 startup tests.
- Blocker: NVIDIA driver unavailable; Docker socket access denied even outside sandbox. RTSP/DeepStream timing, bottleneck, native callback compatibility and GPU memory are not measured here.
- Next action: on the GPU server follow `docs/agent/startup-timing.md` to run fresh 1/2/4/8-source containers while the normal retrace pipeline is stopped for maintenance; compare cold/warm engines and HTTP request/flush. Use `.cache/startup/sources-N.jsonl` and missing-camera/error status to distinguish A–F. Do not claim a cause or lower latency until these measurements exist.
- Preserve the pre-existing nvurisrcbin probe and server-network work below, plus all unrelated dirty/untracked files. No commit/push or service changes performed.

- Active task: observe H264/JPEG caps through the actual nvurisrcbin source helper and mux input path, without changing negotiation.
- Status: implementing a single-camera probe; existing shared pipeline and unrelated dirty work preserved.
- Remaining work: safe caps/decoder observations, CPU regression tests and server commands.
- Verification pending: actual DeepStream 7 caps/memory require the user's GPU server.
- Blockers: no GPU verification in this environment.
- Next action: instrument optional observer callbacks before the source-bin NVMM gate; verify the probe matches the newly authorized direct source-to-mux path.

## Server network verification

- Goal: verify existing retrace/backend/postgres and prepare restricted Backend access for a separate development PC; no frontend implementation.
- Prepared: Preview private bindings preserved; Compose supports loopback-default `BACKEND_BIND_IP`; Backend supports opt-in exact-origin `BACKEND_CORS_ORIGINS` (GET, no credentials, wildcard origins rejected). Configuration instructions are in backend/README.md.
- Verified: host listeners are restricted Preview interfaces (one LAN, one Tailscale) on 40225 and loopback only on 8000; Preview HTTP 200 on both; multiple MJPEG streams returned data, but a later stream timed out on both so all-camera health is not established. Backend health returned status/database ok and preview reachable.
- Verification: Backend tests 39 passed outside sandbox (sandbox TestClient hung); existing Starlette/httpx deprecation warning. Compose JSON parsed with exactly retrace/backend/postgres, no PostgreSQL publication, no wildcard publication. Python syntax and git diff --check passed.
- Blocker: Docker daemon denies this account; noninteractive sudo requires authentication. Compose container state/health and direct PostgreSQL query remain unverified. No service recreated; private .env/override unchanged.
- Next action: in private .env choose one existing Preview LAN/Tailscale interface for `BACKEND_BIND_IP` and the actual local development origins for `BACKEND_CORS_ORIGINS`; an operator with Docker permission runs `docker compose up -d --build --no-deps backend`, service/DB check commands in backend/README.md, then confirms API/CORS and MJPEG from the development PC. Never record actual private addresses or credentials here.
- Existing nvurisrcbin probe task above remains unchanged.
