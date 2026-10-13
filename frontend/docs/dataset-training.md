# ReTrace 데이터셋으로 차량 검출 모델 학습하기

ReTrace는 이미지 수집·박스 라벨링·검수·YOLO ZIP 내보내기를 제공합니다. 학습은 별도 Python/GPU 환경에서 수행합니다. 웹의 Create dataset은 학습 실행 버튼이 아닙니다.

## ZIP 구조

```text
dataset/
  data.yaml
  manifest.json
  images/train/<uuid>.jpg
  images/val/<uuid>.jpg
  images/test/<uuid>.jpg
  labels/train/<uuid>.txt
  labels/val/<uuid>.txt
  labels/test/<uuid>.txt
```

Mark reviewed한 이미지만 포함됩니다. JPG와 같은 UUID의 TXT가 짝입니다. TXT의 각 줄은 `class_id center_x center_y width height`이며 좌표는 이미지 크기로 나눈 0~1 값입니다. 현재 UI와 새 ZIP은 **0 car 한 클래스**를 사용합니다. 기존 truck/bus 저장 라벨은 자동 변환하지 않으며 그런 검수 라벨이 포함되면 새 내보내기는 실패하므로 먼저 확인해야 합니다. 과거 ZIP은 세 클래스일 수 있으니 해당 ZIP의 data.yaml을 기준으로 확인하세요. 차량이 없다고 명시적으로 검수한 이미지의 TXT는 비어 있습니다.

`data.yaml`에는 `train: images/train`, `val: images/val`, `test: images/test`와 car 클래스 이름이 들어 있습니다. `manifest.json`은 클래스, seed, 세션별 split, 이미지 ID/revision/촬영 시각/해시/박스 수, split별 개수와 경고를 기록합니다. 학습 도구가 manifest를 직접 소비하지는 않습니다.

동일 카메라의 연속 캡처 간격이 30분 미만이면 같은 세션으로 묶고 세션 단위로 약 80/10/10 분할합니다. seed는 UI에서 42입니다. 비율은 이미지 수가 아닌 세션 수 기준입니다. 한 세션이면 train만, 두 세션이면 train/val, 세 세션 이상이면 test도 확보합니다. val/test가 없으면 먼저 독립 세션 데이터를 더 모으세요. 다른 세션의 유사한 장면까지 자동으로 분리하지는 않습니다.

## 학습 흐름

1. ZIP을 풀고 실제 이미지/TXT, 클래스별 수, 누락 라벨, train/val/test 분포를 점검합니다. 화면에 보이는 차량을 일관되게 모두 라벨링하고 다른 날·조명·날씨·혼잡도 자료를 확보합니다.
2. 별도 가상환경에 GPU에 맞는 PyTorch와 Ultralytics를 설치합니다. 버전을 기록하고 운영 DeepStream 환경과 분리합니다.
3. 사전학습 YOLO11 검출 모델을 시작점으로 fine-tuning합니다. 아래는 명령 형식 예시이며 장비에 맞게 모델 크기, 해상도, batch를 정해야 합니다.

```bash
python -m pip install ultralytics
yolo detect train model=yolo11m.pt data=/absolute/path/dataset/data.yaml epochs=100 imgsz=640 batch=4 device=0 name=retrace_vehicles
```

`yolo11m.pt`는 예시 사전학습 가중치입니다. 설치 전 PyTorch/CUDA 지원을 확인하세요. 작은 차량이 많은 CCTV는 640 해상도로 충분한지 검증해야 합니다. data.yaml 상대경로를 도구 버전이 다르게 해석하면 작업 복사본 YAML에 `path: /absolute/path/dataset`을 추가합니다.

4. 학습 로그가 알려주는 실행 디렉터리의 `weights/best.pt`를 검증합니다. 검증 데이터로 설정을 조정하고 test는 최종 평가에 사용합니다. 차량 종류별 precision/recall/mAP와 실제 CCTV의 오검출·미검출을 확인합니다.
5. 운영 적용은 별도 작업입니다. ONNX 출력 및 parser 호환, 클래스 수/labels 순서, TensorRT·DeepStream 버전과 입력 크기를 확인하고 대상 GPU에서 엔진을 준비해야 합니다. `.pt` 파일이나 다른 GPU의 engine을 바로 기존 설정에 덮어쓰지 않습니다. 현재 저장소의 vehicle_detection.py는 label 이름과 클래스 수를 확인하므로 새 3클래스 모델에 맞는 설정을 준비해야 합니다. 사람 모델은 이번 데이터셋 학습 범위가 아닙니다.

공식 문서: [YOLO 형식](https://docs.ultralytics.com/datasets/detect/), [학습](https://docs.ultralytics.com/modes/train/), [YOLO11](https://docs.ultralytics.com/models/yolo11/).

## 다른 모델에 전달할 프롬프트

```text
ReTrace에서 내보낸 YOLO ZIP으로 CCTV 차량 검출 모델을 fine-tuning하려고 합니다.
ZIP은 data.yaml, manifest.json, images/{train,val,test}/*.jpg, labels/{train,val,test}/*.txt 구조입니다.
새 ZIP은 0 car 한 클래스입니다. 과거 ZIP은 0 car, 1 truck, 2 bus일 수 있으므로 첨부한 data.yaml과 실제 라벨 분포를 먼저 확인해 주세요. TXT는 class_id center_x center_y width height의 정규화 좌표이며 빈 검수 이미지의 TXT는 비어 있습니다.
검수 완료 이미지만 포함되고 같은 카메라의 30분 미만 간격 연속 캡처는 같은 세션입니다. 세션 단위로 seed 42 기반 약 80/10/10 분할합니다. 적은 세션에서는 val/test가 비어 있을 수 있습니다.
원본은 기존 mux 해상도의 OSD 없는 JPEG이며 라벨은 수동 작성입니다. manifest는 이미지 해시/revision과 split 추적용입니다.
학습 머신의 OS, GPU/VRAM, CUDA/PyTorch 환경과 데이터 수를 먼저 확인해 주세요.
데이터·라벨·누수 검사, YOLO11 사전학습 모델 선택, 설치·학습·평가 명령, 작은 CCTV 차량에 맞는 imgsz/batch/augmentation 선택, best.pt 평가 방법을 안내해 주세요.
ReTrace 운영은 DeepStream/TensorRT이며 학습과 배포를 분리합니다. 배포까지 요청하면 저장소 parser·labels·클래스 수·ONNX·TensorRT 호환을 확인해 주세요. 운영 모델은 자동 교체하지 마세요.
```
