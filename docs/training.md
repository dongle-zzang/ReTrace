# 웹 기반 차량 모델 fine-tuning (`/training`)

ReTrace 웹에서 데이터셋 export를 골라 YOLO11m 차량 모델 fine-tuning을 시작·감시·중지하고, 결과 모델을
기존 모델과 같은 test set에서 비교한다. 학습은 **브라우저나 API 요청 안에서 돌지 않는다.** GPU 서버 host의
별도 worker 프로세스가 기존 `training/.venv`로 `train.py`/`evaluate.py`를 실행한다. 학습 스크립트·클래스
전략·GPU 측정값의 원문은 [training/README.md](../training/README.md)이다.

```text
브라우저 /training ──/api/training/*──> Preview :40225 proxy ──> Backend :8000 (FastAPI, PostgreSQL)
                                                                    ▲  job 상태·지표·로그·산출물 저장
host: training/.venv/bin/python worker.py ──loopback HTTP + worker token──┘
        └─ subprocess: train.py → evaluate.py (GPU), runs/web/<job>/
```

## 1. 안전 경계

- 동시 학습은 1개다. `training_jobs.active_slot`(진행 중 1, 종료 NULL)의 unique constraint가 DB 수준에서 막는다.
- 학습 시작·중지·모델/산출물 다운로드는 `TRAINING_ADMIN_TOKEN`(Bearer)이 필요하다. 토큰이 비어 있으면 503으로 꺼진다.
  조회 GET은 기존 `/api/*`와 같은 공개 범위다. 앱 전체 사용자 인증은 아직 없다(아래 §6).
- worker API(`/api/training/worker/*`)는 `TRAINING_WORKER_TOKEN`이 필요하고, Preview proxy는 이 경로를 404로 막는다.
  worker는 host loopback으로 Backend에 직접 붙는다. 두 토큰은 24자 이상이고 서로 달라야 한다(시작 시 검증).
- 사용자 문자열로 CLI를 만들지 않는다. worker가 job 설정을 정수·허용 값으로 다시 검증해 인자 list를 만든다.
- 업로드된 모델/코드를 실행하지 않는다. base는 host의 `training/weights/yolo11m.pt`(등록된 sha256과 비교) 또는
  이전 job의 `best.pt`(sha256 확인)만 쓴다.
- 데이터셋은 job 생성 시 export ZIP을 `dataset-storage/training/snapshots/<export_id>.zip`으로 고정(hardlink/복사 +
  sha256)한다. 이후 라벨링이나 export 삭제가 학습에 영향을 주지 않는다. worker는 sha256을 다시 확인하고
  export 구조 allowlist·symlink 금지·파일 수/크기 제한(Zip Slip 방지)으로 압축을 푼다.
- 운영 YOLO11m/DeepStream은 자동으로 바꾸지 않는다. 모델 registry의 `deployment`는 항상 `not_deployed`이며 UI의
  적용 버튼은 비활성이다. DeepStream 적용 절차는 training/README.md §8(운영자 수행).
- DeepStream 컨테이너를 멈추거나 재시작하지 않는다. 자원이 부족하면 학습 쪽이 기다리거나 멈춘다.

## 2. GPU·운영 보호

| 시점 | 동작 | 설정 |
| --- | --- | --- |
| 시작 전 (UI·API) | VRAM 추정 `150 + 800·batch·(imgsz/640)²` MiB, 필요 여유 `max(MIN_FREE, 추정 + RESERVE)`. worker·GPU·Preview 상태 확인 | `TRAINING_MIN_FREE_MIB`(6000), `TRAINING_GPU_RESERVE_MIB`(2048) |
| claim 후 | GPU 여유 메모리·util이 기준을 넘으면 대기, 시간 초과 시 `gpu_busy` 실패 | `TRAINING_MAX_GPU_UTIL`(85), `TRAINING_GPU_WAIT_SECONDS`(1800) |
| 학습 중 (heartbeat 5초) | claim 시 online 카메라별 FPS 기준선 대비 하락, Preview 응답 없음, GPU 여유 < reserve가 연속되면 안전 중지(`production_protection`) | `TRAINING_MAX_FPS_DROP`(0.15), `TRAINING_GUARD_SAMPLES`(6), `TRAINING_REQUIRE_PREVIEW`(1) |
| worker 소실 | heartbeat가 끊긴 job은 `worker_lost`, worker 재시작 시 진행 중 job은 `worker_restarted`로 실패 처리 | `TRAINING_WORKER_STALE_SECONDS`(60) |
| 저장 | Backend volume 여유가 부족하면 산출물 업로드 507 | `TRAINING_MIN_STORAGE_MIB`(2048) |

기본값은 training/README.md §2의 측정(640/b4 torch reserved 약 3.3 GiB, 960/b4 약 7.2 GiB)에 맞췄다. 카메라 수나
운영 부하가 바뀌면 다시 측정해 `.env`로 조정한다. CUDA OOM은 `gpu_oom`으로 기록되고 오류 메시지가 batch/imgsz를 낮추도록 안내한다. 설정 화면은 현재 GPU 여유로 가능한 batch를 보여 준다.

## 3. 상태와 실패 코드

상태: `queued → running → (stopping) → completed | stopped | failed`. phase는 `queued → preparing → validating → waiting_gpu → training → evaluating → saving`.
중지는 다음 batch 경계에서 `trainer.stop`으로 끝내고(검증·저장 후 종료), `--stop-grace` 안에 끝나지 않으면 SIGTERM → SIGKILL.

주요 `error_code`: `dataset_invalid`, `dataset_too_large`, `snapshot_changed`, `no_validation`, `invalid_config`,
`base_model_missing`, `base_model_changed`, `cuda_unavailable`, `gpu_busy`, `gpu_oom`, `storage_full`, `killed`,
`training_failed`, `download_failed`, `artifact_upload_failed`, `production_protection`, `worker_lost`, `worker_restarted`.

## 4. API (`/api/training`)

| 메서드·경로 | 권한 | 용도 |
| --- | --- | --- |
| GET `/options`, `/status?imgsz&batch` | 공개 | 설정 범위·기본값, worker/GPU/Preview/진행 job·VRAM 추정 |
| GET `/datasets`, `/datasets/{export_id}/check` | 공개 | 학습 가능한 export와 적합성(blocked/limited/ready, 세션·split·라벨 기준) |
| GET `/jobs`, `/jobs/{id}`, `/jobs/{id}/comparison` | 공개 | 목록, 진행·지표·로그, test 이미지별 GT/TP/FP/FN 비교 |
| GET `/models`, `/models/{id}`, `/models/compare?ids=` | 공개 | 모델 registry, 지표 비교 |
| POST `/auth` | admin | 토큰 확인(204) |
| POST `/jobs` | admin | 검증 후 job 생성. limited 데이터셋은 `acknowledge_limits: true` 필요 |
| POST `/jobs/{id}/stop` | admin | 안전 중지 요청 |
| GET `/jobs/{id}/artifacts/{name}`, `/models/{id}/files/{best\|last}` | admin | 산출물·가중치 다운로드 |
| `/worker/*` (heartbeat, claim, dataset, weights, artifacts, finish) | worker | host worker 전용, Preview에서 404 |

job별 산출물: `best.pt`, `last.pt`, `results.csv`, `results.png`, `args.yaml`, `run_info.json`, `train.log`,
`check_report.json`, `eval_metrics.json`, `eval_predictions.json`, `eval_report.md`. 저장 위치는 Backend volume의
`dataset-storage/training/jobs/<job_id>/`, worker 작업 폴더는 host `training/runs/web/<job_id>/`이다.
테이블은 `training_jobs`, `training_models`이며 기존 테이블처럼 `create_all`로 추가만 된다(삭제·초기화 없음).

평가는 baseline(COCO yolo11m)과 새 모델(재학습이면 base 모델 포함)을 같은 test split에서 비교한다. test 10장 이상·
test 세션 2개 이상·경고 없음일 때만 `conclusive`이며, 아니면 UI가 개선을 주장하지 않는다.

## 5. 운영 절차 (GPU 서버, 운영자 수행)

```bash
# 1) 비공개 .env에 토큰 추가 (각 24자 이상, 서로 다르게). 값은 문서·로그에 남기지 않는다.
#    TRAINING_ADMIN_TOKEN=...   TRAINING_WORKER_TOKEN=...
# 2) Backend만 재생성 (DeepStream retrace 서비스는 건드리지 않음)
sudo docker compose up -d --build --no-deps backend
# 3) Preview proxy 변경(preview_web.py) 반영: retrace 재시작은 영상이 잠시 끊기므로 운영 합의 후 수행
sudo docker compose restart retrace
# 4) 프론트 정적 산출물: frontend에서 npm run generate 후 .output/public/ 내용을 서버 frontend-dist/에 업로드
# 5) worker 시작 (host, 기존 venv 재사용, 별도 설치 없음)
cd training
RETRACE_TRAINING_WORKER_TOKEN='<TRAINING_WORKER_TOKEN 값>' nohup .venv/bin/python worker.py > runs/web-worker.log 2>&1 &
```

worker 옵션: `--backend`(기본 `http://127.0.0.1:8000`, `RETRACE_BACKEND_URL`), `--gpu 0`, `--poll 5`, `--work-dir runs/web`,
`--min-disk-mib 10000`, `--stop-grace 180`, `--dataloader-workers 4`. Backend 포트는 compose의
`BACKEND_BIND_IP`(기본 127.0.0.1):`BACKEND_PORT`(기본 8000)이므로 host worker가 loopback으로 닿는다.
상시 실행이 필요하면 systemd unit에서 `EnvironmentFile`을 root 전용 파일(권한 600)로 두고 `Restart=on-failure`로 실행한다.

첫 실행은 UI에서 smoke(epochs 1, 640/b4)로 GPU 여유와 Preview FPS를 확인한 뒤 본 학습을 한다. 실제 CCTV 데이터로
장시간 학습을 자동으로 시작하는 장치는 없다.

## 6. 남은 일·한계

- 사용자 계정 인증은 없다. admin 토큰은 공유 비밀번호 수준이며 브라우저 sessionStorage에만 둔다. 계정/권한이 도입되면
  `require_admin`을 교체한다.
- worker는 GPU 1장·job 1개를 가정한다. 여러 worker를 띄워도 DB 제약으로 job은 1개만 실행된다.
- 결과 모델의 DeepStream 적용(ONNX export·engine 생성·교체)은 범위 밖이며 training/README.md §8 절차를 따른다.
