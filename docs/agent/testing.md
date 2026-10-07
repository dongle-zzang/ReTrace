# 검증 선택

명령은 저장소 루트 기준이다. 필요한 dependency가 이미 준비된 Python 환경을 선택한다. 루트 requirements는 설정 로더용, backend test requirements는 API/DB 테스트용이다. 테스트하려고 GPU dependency를 CPU 환경에 설치하지 않는다.

## 작은 범위부터

| 변경 영역 | 존재하는 검증 명령 |
| --- | --- |
| 카메라 설정/metadata/runtime/reconnect | `python3 -m unittest discover -s tests -p 'test_camera_*.py'` |
| NvDCF OpenCV YAML 진단 파싱/traceback 비밀값 보호 | `python3 -m unittest discover -s tests -p 'test_camera_diagnostics.py'` |
| PGIE build identity, native engine 저장 경로, 프로세스 간 경로 재사용 | `python3 -m unittest discover -s tests -p 'test_pgie_cache.py'` |
| 첫 영상 startup timing / batch camera attribution / HTTP 저장·flush 경계 | `python3 -m unittest discover -s tests -p 'test_camera_startup_timing.py'` |
| 단일 RTSP 진단/오류 분류/native 출력 차단 | `python3 -m unittest discover -s tests -p 'test_camera_rtsp_diagnostics.py'` |
| nvurisrcbin 진단/caps 보호/NVMM gate 관측 | `python3 -m unittest discover -s tests -p 'test_camera_nvuri_probe.py'` |
| 실제 CLI parser 계약 (AST로 GPU import 우회) | `python3 -m unittest discover -s tests -p 'test_rtsp_inputs.py'` |
| canonical container 실행 설정 | `python3 -m unittest discover -s tests -p 'test_canonical_startup.py'` |
| Preview/CSR 파일·SPA routing·격리, 동일 origin API 프록시, MJPEG/JSON | `python3 -m unittest discover -s tests -p 'test_preview_http.py'` |
| Preview 웹 플레이어 연결/재생 복구/runtime 상태/페이지 종료 | `node --test tests/preview_web.test.cjs` |
| Preview API readiness checker | `python3 -m unittest discover -s tests -p 'test_check_preview.py'` |
| Backend API/polling/DB/preview 계약/보안 | `python -m pytest -q backend/tests` |
| DeepStream HTTP/element link 포함 전체 Python 회귀 | `docker compose exec -T retrace python3 -m unittest discover -s tests -p 'test_*.py'` |

더 작은 변경은 관련 unittest 모듈이나 pytest 파일/테스트만 선택한다. Backend CPU 환경 준비 방법은 [backend README의 CPU 테스트](../../backend/README.md#cpu-테스트)를 따른다. dependency 설치가 필요한지 먼저 확인한다.

## 검증 환경과 한계

- `tests/test_camera_shared_pipeline.py`는 실제 공유 pipeline 함수의 생성/연결을 fake Gst로 실행해 instance 수, batch 크기, mux/demux request pad, 상태/세대 독립성 및 HTTP schema를 CPU에서 검증한다. 실제 plugin 협상/복구 보장은 아니다.
- `tests/test_pgie_cache.py`는 CPU 파일 fixture로 config 생성, 빌드 조건/후처리 분리, 별도 프로세스의 경로 안정성, 모델 staging 및 config/GObject 일치를 검증한다. 실제 TensorRT serialization은 GPU 서버에서 최초 생성 후 동일 설정으로 재실행하여 canonical engine의 deserialization 로그와 rebuild 부재를 확인해야 한다. Warm nvinfer state transition의 8–9초 지연은 별도 최적화 과제다.
- `tests/test_preview.py`는 preview를 직접 import하므로 GI/GStreamer/PyDS와 NVIDIA plugin 환경이 필요하다. 전체 tests discovery를 일반 CPU 검증으로 간주하지 않는다.
- Backend 테스트는 임시 SQLite DB, FastAPI TestClient, httpx MockTransport와 transaction을 사용한다. 실제 PostgreSQL/GPU/RTSP 검증을 대체하지 않는다.
- `tests/test_preview_http.py`는 production HTTP handler를 AST로 로드하여 GPU import 없이 로컬 HTTP 서버에서 HTML/JS와 MJPEG bytes/JSON, CSR assets/SPA navigation/HEAD, 경로 및 symlink 격리, fake Backend의 query/status/502를 검증한다. `tests/preview_web.test.cjs`는 Node 내장 test runner와 가짜 DOM으로 확인용 플레이어를 검증한다. 실제 제품 프론트 빌드나 JPEG 디코딩/GPU 영상 수신을 보장하는 테스트는 아니다.
- 저장소에 lint/formatter/typecheck 설정, Makefile, CI workflow는 현재 없다. 별도 명령이나 자동 통과를 가정하지 않는다.

실행 중 서비스의 HTTP 진단은 `docker compose exec -T retrace python3 tools/check_preview.py`, backend 연결·비밀값 노출 검사는 `docker compose exec -T retrace python3 tools/check_backend.py`다. GPU 서버에서 영상 수신, 카메라 장애/복귀 및 다른 카메라 영향은 수동 확인한다. HTTP healthcheck만으로 영상 정상 여부를 판단하지 않는다.

PGIE 캐시의 실제 GPU 검증은 서비스 재시작이 가능한 시간에 아래 명령을 실행한다.
첫 실행에서 engine 생성과 정상 영상 출력을 기다린 뒤 같은 명령으로 한 번 더 재시작한다.
두 번째 실행에는 `.cache/pgie`의 `Use deserialized engine model`이 나오고 build/serialize 로그가 없어야 한다.
로그 조회의 `--since`는 해당 재시작 이후만 포함하도록 조절한다. 캐시를 임의 삭제할 필요는 없다.

```bash
sudo docker compose restart retrace
sudo docker compose logs --since=2m retrace 2>&1 | rg 'Use deserialized engine model|Trying to create engine|serialize cuda engine'
```

engine 경로와 파일 생성도 컨테이너에서 확인한다. 파일 존재만으로 재사용 성공을 판단하지 않는다.

```bash
sudo docker compose exec -T retrace python3 -c 'from pathlib import Path; print("\n".join(str(p) for p in Path(".cache/pgie").glob("*.engine")))'
```
