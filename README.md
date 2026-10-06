# ReTrace

ReTrace는 RTSP 영상에서 사람을 검출하고 추적하는 개인 토이 프로젝트입니다.
Python과 NVIDIA DeepStream을 사용하며, GPU 실행은 Linux 서버에서 수행합니다.
MacBook에서는 코드를 작성하고 GitHub의 `main` 브랜치로 작업을 이어갑니다.

## 현재 기능

- 여러 RTSP 입력을 하나의 DeepStream batch로 처리
- NVIDIA TAO PeopleNet으로 사람 검출
- `preview.py`: NvDCF로 카메라별 사람 추적, Bounding Box와 Track ID 표시
- source별 MJPEG 브라우저 미리보기 및 프레임/FPS/검출 통계
- 선택적 pipeline 단계별 타이밍 진단
- `app.py`: 미리보기와 tracker 없이 검출 metadata만 확인하는 실행 경로

카메라 간 동일 인물 매칭(Re-ID), DB 저장, 자동 RTSP 재연결은 아직 구현하지 않았습니다.
Track ID는 카메라별로 해석합니다. 향후 Re-ID와 카메라 간 이동 분석을 추가할 계획입니다.

## Pipeline

```text
CCTV / RTSP
  → uridecodebin / NVIDIA NVMM → queue → nvvideoconvert
  → nvstreammux → PeopleNet nvinfer → NvDCF nvtracker → nvstreamdemux
  → source별 nvvideoconvert → nvdsosd → nvjpegenc → appsink
  → 최신 JPEG 한 장 → HTTP multipart → 브라우저 <img>
```

MJPEG는 source별 최신 프레임만 보관합니다. 느린 클라이언트는 지난 프레임을 건너뜁니다.
`/streams.json`은 source 번호, 상태, MJPEG 경로를 제공하며 RTSP 입력 주소를 제공하지 않습니다.
정적 파일은 `web/index.html`, `web/preview.js`만 제공합니다.
기존 HLS 출력물을 사용하거나 새로운 영상 파일을 저장하지 않습니다.

## 실행 환경

- Linux, NVIDIA GPU 및 호환 드라이버
- Docker Compose와 NVIDIA Container Toolkit
- DeepStream 7.0, Python 3.10, PyDS 1.1.11, PyGObject/GStreamer
- PeopleNet ONNX와 labels; TensorRT engine은 실행 환경에서 생성

이미지는 `nvcr.io/nvidia/deepstream:7.0-triton-multiarch`를 기반으로 합니다.
모델, engine, 캐시, 영상 및 로컬 설정은 Git에 포함하지 않습니다.

## 로컬 환경 설정

프로젝트 루트에서 예제 파일을 복사합니다.

```bash
cp .env.example .env
chmod 600 .env
id -u
id -g
```

`.env`의 `RTSP_URL`, `RTSP_URL_2`에 사용자가 실제 RTSP 주소를 직접 입력합니다.
한 입력만 사용하면 두 번째 값은 비워 둡니다. 인증정보의 특수문자는 URL 인코딩합니다.
`LOCAL_UID`, `LOCAL_GID`는 위 명령으로 확인한 로컬 사용자 ID로 설정합니다.
이 두 값이 비어 있으면 Compose의 기본 UID/GID를 사용합니다.
`.env`와 내부 접속 정보는 공유하거나 commit하지 않습니다.

```dotenv
RTSP_URL=
RTSP_URL_2=
LOCAL_UID=
LOCAL_GID=
```

Compose는 루트 `.env`의 RTSP 값을 컨테이너 환경변수로 전달합니다.
Python 프로그램 자체는 `.env`를 읽지 않으므로 직접 실행할 때는 환경변수를 별도로 설정해야 합니다.
기존 `--input`을 반복하는 방식도 지원하며, 하나라도 지정하면 CLI 입력만 사용합니다.
CLI 인자에 실제 주소를 넣으면 셸 history와 프로세스 인자에 남을 수 있으므로 환경변수 방식을 권장합니다.
환경변수도 Docker inspect 등 로컬 관리 도구에서 확인될 수 있습니다.
Compose 설정을 확인한 출력에는 실제 값이 포함될 수 있으므로 공유하지 않습니다.

## Docker Compose 실행

```bash
docker compose build
docker compose up -d
docker compose exec retrace python3 preview.py --diagnostics
```

브라우저에서 `http://<DEVELOPMENT_SERVER>:8080/`에 접속합니다.
프로젝트는 상대 bind mount로 `/workspace/ReTrace`에 연결됩니다.
GPU reservation, DeepStream 이미지와 기존 파이프라인 구조를 사용합니다.
`.env`나 Compose 설정을 변경했다면 컨테이너를 재생성해야 적용됩니다.
컨테이너 재생성 전 실행 중인 Python 프로그램을 정상 종료합니다.

검출 metadata만 확인하려면 다음을 실행합니다.

```bash
docker compose exec retrace python3 app.py
```

`--input`은 카메라 수만큼 반복할 수 있습니다. 환경변수 fallback은 두 입력을 지원합니다.
입력 순서대로 source 번호가 부여됩니다. 입력이 없거나 잘못된 URI이면 값 없이 오류로 종료합니다.

## PeopleNet 모델 준비

컨테이너에 완전한 PeopleNet sample config와 모델/labels가 있으면 우선 재사용합니다.
없으면 `configs/pgie_peoplenet.txt`와 로컬 `models/peoplenet/` 파일을 사용합니다.
모델 파일은 자동 다운로드하지 않습니다. 필요한 경우 공식 NVIDIA NGC에서 다운로드합니다.

```bash
docker compose exec -T retrace sh -lc '
  cd /workspace/ReTrace || exit 1
  mkdir -p models/peoplenet || exit 1
  ngc_base=https://api.ngc.nvidia.com/v2/models/nvidia/tao/peoplenet/versions/deployable_quantized_onnx_v2.6.2/files
  for file in resnet34_peoplenet.onnx labels.txt nvinfer_config.txt; do
    curl --fail --location --retry 3 "$ngc_base/$file" -o "models/peoplenet/$file.part" || exit 1
    mv "models/peoplenet/$file.part" "models/peoplenet/$file" || exit 1
  done
'
```

NGC 파일: [PeopleNet deployable_quantized_onnx_v2.6.2](https://catalog.ngc.nvidia.com/orgs/nvidia/tao/models/peoplenet/deployable_quantized_onnx_v2.6.2/file-browser).
FP16 설정으로 실행하며 `person` class ID는 labels에서 찾습니다.
실행용 config와 TensorRT engine은 `.cache/pgie/`에 생성합니다.
첫 engine 생성에는 시간이 걸리며, GPU/TensorRT 환경이 다른 장비의 engine을 그대로 복사하지 않습니다.

## 미리보기와 검증

기본 미리보기는 legacy nvstreammux, 입력 수와 같은 batch-size, 1920×1080 mux,
RTSP TCP/latency 1000ms, NvDCF 640×384, JPEG 1280×720/quality 80을 사용합니다.
`--port`, `--jpeg-quality`, `--rtsp-latency`, `--rtsp-drop-on-latency`,
`--mux-live-source`, `--diagnostics`로 필요한 설정을 조정합니다.

통계의 `persons`는 마지막 프레임의 검출/추적 사람 수입니다.
고유 방문자 수나 카메라 간 동일 인물 수가 아닙니다.
GStreamer 오류는 pipeline 전체를 종료합니다. Ctrl+C/SIGTERM으로 정상 종료할 수 있습니다.
진단 로그에 입력 주소가 포함될 수 있으므로 실제 실행 로그와 디버그 결과는 공개하지 않습니다.

```bash
# GPU/DeepStream 없이 입력 설정 동작 검증
python3 -m unittest discover -s tests -p 'test_rtsp_inputs.py'
# DeepStream 환경에서 기존 회귀 검사
docker compose exec -T retrace python3 -m unittest discover -s tests -p 'test_*.py'
node tests/preview_web.test.cjs
# 실행 중인 MJPEG를 측정 (영상 저장 없음)
docker compose exec -T retrace python3 tools/measure_preview.py \
  --seconds 60 --report outputs/mjpeg_test.json
```

## GitHub main workflow

GitHub 연결 후 각 장비에서 저장소를 복제합니다.

```bash
git clone <GITHUB_REPOSITORY_URL> ReTrace
cd ReTrace
# 작업 시작
git pull --rebase
# 코드 수정 후 변경 확인
git status
git diff
# 수정한 공개 파일을 명시적으로 추가
git add <CHANGED_SOURCE_FILES>
git commit -m "<COMMIT_MESSAGE>"
git push origin main
```

다른 장비에서도 작업 시작 전에 `git pull --rebase`를 실행합니다.
장비를 옮기기 전에 commit/push하고, pull 전에는 작업 트리를 정리합니다.
충돌이 발생하면 내용을 검토하여 해결한 뒤 `git rebase --continue`를 실행합니다.
모델과 `.env`는 각 실행 장비에서 별도로 준비합니다.
로컬 테스트 자료 `hy_test/`는 보존하되 공개 저장소에서 전체 제외합니다.
