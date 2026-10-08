# 색상 기반 폴리곤 주차 MVP

색상 감지는 딥러닝 모델/차량 metadata를 사용하지 않는다. 기존 사람·차량 detector와
카메라별 `parking_spaces` API는 유지하며, 신규 논리 공간 API와 독립적으로 동작한다.
기존 영역을 자동 변환하거나 동일 label로 병합하지 않는다.

## 저장과 처리 경계

- `color_parking_spaces`: `id`(UUID), trim된 `label`(대소문자 구분, UNIQUE).
  코드 모델은 기존 `ParkingSpace`와 구분하기 위해 `ColorParkingSpace`다.
- `parking_zones`: `id`, camera FK, logical space FK, 정규화 polygon, enabled,
  threshold, hysteresis, confirm_frames, revision, 빈자리 HSV histogram(JSON).
- 두 테이블은 기존 DB에 `create_all()`로 추가된다. 기존 테이블 변경/migration은 없다.
  DB 백업/적용은 [Backend 운영 절차](../backend/README.md#운영-주차-기능-적용-순서)를 따른다.
- 판정/연속 관측 수는 단일 Backend worker의 메모리에 저장한다. 재시작 시 UNKNOWN이며
  설정/캘리브레이션은 보존된다. 변경 이력의 영구 저장은 이 MVP에 포함하지 않는다.
- 기존 RTSP → DeepStream → OSD/JPEG → FrameStore를 그대로 사용한다.
  Preview의 `GET /snapshots/{cameraId}.jpg`가 최신 JPEG와 age/session/generation/sequence를
  반환한다. 새 RTSP 연결이나 영상 branch는 없다. 카메라 offline/프레임 없음은 503이다.
  이 endpoint는 기존 Preview 네트워크 경계에 속하며 인증은 기존과 같이 없다.
- Backend worker는 enabled zone이 있는 카메라만 한 번씩 HTTP 조회·CPU JPEG decode한다.
  여러 zone은 그 이미지를 공유한다. GPU callback에서 네트워크/DB/색상 처리를 하지 않는다.
- 기본 분석 주기는 1초, `COLOR_PARKING_INTERVAL`로 0.5~60초 설정 가능하다.
  `COLOR_PARKING_STALE_AFTER` 기본 5초, 최소 두 분석 주기~최대 300초다.
  Compose backend environment로 전달하며 변경 시 backend 재생성이 필요하다.
  카메라를 순차 처리하므로 지연/많은 영역 때문에 실제 주기는 늘어날 수 있다.

## 알고리즘과 판정

1. JPEG를 BGR로 decode하고 폭이 640보다 크면 종횡비를 유지해 축소한다.
2. 정규화 polygon을 이미지 좌표로 변환하고 `fillPoly` 마스크를 생성한다.
   분석 해상도에서 ROI가 64픽셀 미만이면 UNKNOWN이다.
3. HSV로 변환하고 H/S/V 12×4×4 bins의 정규화 히스토그램을 구한다.
   저채도(S<32)/암부(V<32)의 불안정한 hue는 0으로 묶는다.
4. 기준이 없으면 normalized Shannon entropy를 색상 다양도 score(0~1)로 사용한다.
   기준이 있으면 빈자리 히스토그램과 Hellinger distance(0~1)를 score로 사용한다.
   score는 확률/정확도나 차량 종류를 의미하지 않는다.
5. score ≥ threshold+hysteresis → OCCUPIED 후보,
   score ≤ threshold−hysteresis → EMPTY 후보. 그 사이는 이전 확정 상태를 유지한다.
   초기 중간 구간은 UNKNOWN. 기본 threshold=.3, hysteresis=.05, confirmFrames=3.
   후보가 서로 다른 새 JPEG에서 연속 confirmFrames번 관측되어야 확정한다.
6. 반복 sequence는 확인 횟수/TTL을 갱신하지 않는다. 프레임 실패·너무 작은 ROI는 즉시
   UNKNOWN, TTL 만료는 조회 시에도 UNKNOWN이다. session/generation 변경이나
   TTL 초과 뒤의 복구는 확인 횟수를 초기화한다. 설정/캘리브레이션 변경은 revision을
   올려 기존 판정을 무효화하고 polygon 변경은 빈자리 기준도 지운다.

통합 시 enabled zone만 연결된 근거로 본다. 비활성 zone은 응답에는 UNKNOWN으로 남지만
집계에서 제외한다. enabled zone의 카메라가 disabled/offline이면 UNKNOWN 근거가 된다.
하나라도 OCCUPIED이면 최종 OCCUPIED, 하나 이상 연결되어 모두 EMPTY이면 EMPTY,
나머지는 UNKNOWN이다. OCCUPIED와 EMPTY가 동시에 있으면 conflict=true다.
서로 다른 카메라의 ROI별 연속 확인과 hysteresis를 거친 상태를 통합한다.

## API

같은 Preview origin의 `/api/*` 프록시 또는 Backend에 요청한다.

| 요청 | 본문 / 응답 |
| --- | --- |
| GET /api/parking/spaces | `[{"id":"UUID","label":"A-01"}]` |
| POST /api/parking/spaces | `{"label":"A-01"}` → 201, `{id,label}`; 중복 409 |
| GET /api/parking/zones?cameraId=first | zone 배열; cameraId 생략 시 전체 |
| POST /api/parking/zones | 아래 생성 예제 → 201, zone |
| PATCH /api/parking/zones/{id} | polygon/enabled/threshold/hysteresis/confirmFrames 중 일부 → zone |
| DELETE /api/parking/zones/{id} | 204 |
| POST /api/parking/zones/{id}/calibrate | 빈자리일 때 `{}` 전송 → zone (`calibrated:true`) |
| DELETE /api/parking/zones/{id}/calibrate | 기준 제거 → 204, 기본 다양도 분석 복귀 |
| GET /api/parking/status | 아래 통합 상태 |

공간/카메라 연결은 생성 시 결정한다. 변경하려면 zone 삭제 후 생성한다.
설정 수정 시 null/빈 PATCH/알 수 없는 필드/잘못된 단순 polygon은 422,
없는 공간·카메라·zone은 404다. threshold±hysteresis는 0~1에 들어와야 한다.
캘리브레이션은 enabled zone/카메라의 최신 프레임을 새로 조회한다. 프레임이 없으면 409,
ROI가 작으면 422, 조회 중 영역이 바뀌면 409다. 빈자리인지 자동으로 검증하지 않는다.
이미지는 저장하지 않고 histogram만 저장한다.

```json
{
  "cameraId": "first",
  "parkingSpaceId": "<space UUID>",
  "polygon": [{"x":0.1,"y":0.2},{"x":0.8,"y":0.2},{"x":0.8,"y":0.9},{"x":0.1,"y":0.9}],
  "enabled": true,
  "threshold": 0.3,
  "hysteresis": 0.05,
  "confirmFrames": 3
}
```

zone 응답은 생성 필드에 `id`, `calibrated`, `revision`을 추가한다.
polygon은 닫힘 점을 중복하지 않는 3~64개 normalized point이며 오목한 단순 polygon도 허용한다.

```json
{
  "spaces": [{
    "parkingSpaceId": "<space UUID>",
    "label": "A-01",
    "status": "OCCUPIED",
    "conflict": true,
    "zones": [
      {"zoneId":"<zone UUID>","cameraId":"first","enabled":true,"status":"OCCUPIED","score":0.72,"updatedAt":"2026-10-08T00:00:00+00:00"},
      {"zoneId":"<zone UUID>","cameraId":"second","enabled":true,"status":"EMPTY","score":0.02,"updatedAt":"2026-10-08T00:00:00+00:00"}
    ]
  }]
}
```

updatedAt은 최신 분석 프레임의 Preview 저장 시각 추정값(UTC)이며 CCTV capture 시각이 아니다.
미확보 시 null이고 만료 시 마지막 관측 시각을 유지할 수 있다. score는 유효 프레임이 없으면 null이다.

## 프론트엔드 연결

초기 로딩에 spaces/zones/status를 조회한다. 기존 `/ws`에
`{"type":"subscribe","cameraIds":["first","second"]}`를 전송한다.
JPEG를 함께 받으려면 기존 `video:true`/`videoFps` 필드를 사용한다.

상태/충돌/영역 구성 변경 시 `type:"parking.status_updated"`를 받는다.
기존 `version:1, cameraId, sourceId, runtimeSession, generation, timestamp, data` envelope를 유지한다.
`data`는 `{spaces:[...]}`이며 해당 cameraId와 연결된 공간들의 **전체 통합 결과**다.
동일 공간이 여러 cameraId 메시지에 포함될 수 있으므로 parkingSpaceId로 upsert한다.
영역 삭제 시 해당 카메라에 `spaces:[]`도 전송된다. 다른 카메라의 동일 공간을 함께 지우지 말고
REST를 다시 조회해 화면 목록을 동기화한다.

변경 감지는 기존 Preview relay가 기본 1초 간격으로 수행한다. Backend 장애는 UNKNOWN으로
전달하며 연결/재구독 때 최근 변경 snapshot을 재전송한다. score/updatedAt만의 변화는 이벤트를
발행하지 않는다. 최초/재접속 시 REST status로 최신 점수를 조회한다. WS는 durable event log가
아니며 relay 조회 사이에 여러 번 변한 중간 상태는 생략될 수 있다. 기존 `parking_status`와
차량 기반 `event.kind=parking_occupancy_changed` 계약은 별개로 유지된다.

## 실행 확인과 한계

Backend 의존성에 CPU `opencv-python-headless`와 NumPy를 추가했다.
Backend image rebuild 및 Preview restart가 필요하다. 운영 백업·재시작은 기존 운영 절차를 따른다.
색상 기능 적용 후 `/api/parking/spaces`, `/api/parking/zones`, `/api/parking/status`가
200인지 확인하고 DB에는 `color_parking_spaces`, `parking_zones` 두 테이블을 확인한다.
기존 절차의 차량 모델 비활성→unknown 설명은 기존 차량 기반 API에만 해당한다.
모델 파일/engine, 사설 주소/포트, 기존 차량 detector 활성화 설정을 바꾸지 않는다.

검증 명령:

```bash
python -m pytest -q backend/tests/test_color_parking.py
python -m unittest discover -s tests -p 'test_color_parking_transport.py'
python -m unittest discover -s tests -p 'test_preview_realtime.py'
python -m unittest discover -s tests -p 'test_preview_api_writes.py'
```

실제 CCTV에서는 빈자리에서 캘리브레이션한 후 차량 진입/출차 때 score와 상태를 확인하고
영역별 threshold를 조정한다. 합성 영상 테스트가 CCTV 정확도를 보장하지 않는다.
기준 없는 다양도는 단색 차량을 EMPTY로, 다양한 노면을 OCCUPIED로 오판할 수 있다.
캘리브레이션도 그림자·조명·비·눈·카메라 이동·차량과 노면의 비슷한 색상에 취약하다.
JPEG는 기존 OSD를 포함하므로 bbox/문자가 점수에 영향을 줄 수 있다. 폴리곤 내부 색상
분포만 비교하므로 색상의 공간적 배치/차량 형상/물체 종류는 구분하지 않는다.
CPU 비용/운영 정확도/실제 PostgreSQL 동시 접근은 별도 현장 검증이 필요하다.
