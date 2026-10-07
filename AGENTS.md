# 공통 에이전트 작업 지침

이 파일은 Codex, Claude Code 등 모든 코딩 에이전트의 공통 작업 규칙과 문서 라우터다. 모델별 진입 파일에는 이 파일의 참조만 두며 규칙을 복제하지 않는다.

## 기준과 변경 범위

- 현재 브랜치의 실제 코드가 문서보다 우선한다. 작업 트리의 미커밋 구현도 확인한다.
- 파일, symbol, API, 명령, 환경 변수, 구조를 추측하지 않는다. `rg` 등으로 저장소에서 검증하고 확인할 수 없으면 Unknown / Not verified로 표시한다.
- 사용자의 기존 변경을 보존한다. 요청과 무관한 수정, 정리, refactoring, 전체 formatter 적용으로 범위를 넓히지 않는다.
- 가까운 기존 구현과 테스트를 먼저 확인하고 재사용한다. 오래된 코드의 존재만으로 권장 패턴을 정하지 않는다. 필요하면 `git log` / `git blame`으로 확인한다.
- 변경 범위를 작게 유지한다. 새 dependency는 명확한 필요가 있을 때만 추가한다.
- secret, credential, 개인정보, 실제 RTSP 주소 및 민감한 운영 값을 문서·로그·공유 출력에 기록하지 않는다. 환경 변수는 이름과 역할만 기록한다.

## 작업 시작

1. 사용자의 요청과 범위를 파악한다.
2. `git status`로 기존 변경을 확인한다. dirty 상태에서 자동 pull/rebase/reset을 하지 않는다.
3. 관련 기존 구현을 검색한다.
4. [.ai/WORK.md](.ai/WORK.md)를 읽는다.
5. 요청과 관련된 진행 중 작업만 이어받는다. 관계없는 작업 상태를 임의로 지우지 않는다.
6. 아래 라우터에서 필요한 문서만 읽는다. 장기 제약이 관련되면 [.ai/MEMORY.md](.ai/MEMORY.md)도 확인한다.
7. 실제 코드를 확인한 뒤 작업한다. 관련 문서가 없으면 직접 조사한다.

## 항상 알아야 할 경계

- RTSP 사람 검출/추적 프로젝트다. 단일 저장소에 루트 Python DeepStream 실행부, `web/` 미리보기, `backend/` FastAPI 서비스가 있으며 JS workspace monorepo가 아니다.
- Python dependency는 pip와 루트/백엔드 `requirements*.txt`로 관리한다. `package.json`과 npm lock은 Codex 도구 dependency용이며 앱 build/test scripts는 없다.
- 모델·TensorRT engine·`.cache/`·영상/측정 출력은 로컬 산출물이다. 생성된 파일을 직접 고쳐 소스를 대신하지 않는다.
- `.env`, `compose.override.yaml`, `.codex/`, `.agents/` 등 로컬 설정은 Git 공유 문서와 분리한다. 모델/sandbox/permission/MCP/hook 설정을 이 문서 체계에 넣지 않는다.

## 필요한 문서만 읽기

| 작업 | 문서 |
| --- | --- |
| 시스템 이해, pipeline/runtime, 여러 layer 변경 | [architecture](docs/agent/architecture.md) |
| UI/component, 브라우저 영상/상태 표시 | [frontend](docs/agent/frontend.md) |
| 서버 로직, API 계약/연동, DB/schema | [backend](docs/agent/backend.md) + 경계를 바꾸면 architecture |
| 인증/권한 도입 | backend + frontend + deployment; 현재 인증 구현은 없음 |
| 테스트 작성/수정, 검증 선택 | [testing](docs/agent/testing.md) |
| Docker/운영/배포 | [deployment](docs/agent/deployment.md) + architecture |

## 구현과 검증

- 해당 영역 문서가 안내하는 실제 코드·테스트를 먼저 읽는다. naming/import/style은 인접 구현에 맞춘다.
- public JSON 필드와 소비자를 함께 확인하고 계약을 보존한다. 데이터 의미, lifecycle, 저장 정책은 영역 문서의 원문 참조를 따른다.
- 변경 범위에 맞는 가장 작은 검증부터 수행한다. 실제 명령과 환경 제약은 testing 문서를 따른다. 존재하지 않는 lint/typecheck/build 명령을 만들어내지 않는다.
- 기존 오류와 신규 오류를 구분한다. 관련 없는 기존 오류를 고쳐 범위를 확대하지 않는다.
- 문서만 바꿨다면 `git diff --check`, `git diff`, `git status`와 링크/사실 검토로 검증한다. 새 untracked 문서는 일반 `git diff`에 안 나오므로 내용도 별도로 확인한다. 전체 앱 build/test는 불필요하다.
- 완료 보고에는 변경, 검증 결과, 실행하지 못한 검증과 한계를 적는다.

## 인수인계와 문서 유지

- `.ai/WORK.md`는 현재 상태다. 의미 있는 새 작업 시작, 주요 단계 완료, 중요한 제약/문제 발견, 방향에 영향을 주는 검증 결과, 미완성 종료 및 에이전트 전환 시 갱신한다. 사소한 수정마다 쓰거나 작업 일지를 append하지 않는다.
- 인수인계에는 목표, 완료/진행/남은 작업, 검증 대기, blocker, 필요한 맥락과 구체적인 다음 행동을 둔다. 미커밋 파일과 검증 환경이 재개에 중요하면 함께 적는다.
- 완전히 끝난 작업은 WORK를 idle로 되돌리고 세부 완료 기록을 누적하지 않는다. 다른 기존 작업이 진행 중이면 그 상태를 보존한다.
- `.ai/MEMORY.md`는 반복적으로 중요한 비자명한 제약의 **이유**만 보관한다. 이유를 확인할 수 없거나 README/영역 문서에 충분히 설명돼 있으면 추가하지 않는다. 각 항목은 이유, 변경 시 확인사항, 잘못 바꿨을 때 영향을 담는다.
- 의미 있는 구조/패턴이 바뀌면 해당 영역 문서를 갱신한다: 시스템 → architecture, UI → frontend, 서버/API/DB → backend, 검증 체계 → testing, 배포 → deployment. 새 영역은 별도 문서가 유용할 때만 만들고 라우터를 갱신한다.
- “앞으로 다른 개발자나 AI가 실제로 필요한가?”를 기준으로 문서화한다. 현재 상태는 WORK, 장기 제약의 이유는 MEMORY, 구조적 기술 지식은 영역 문서로 분리하고 한 정보의 본문은 한 곳에만 둔다.
- Git clone/pull만으로 전달할 맥락은 추적 가능한 이 문서들에 둔다. 로컬 자동 memory나 대화 기록만을 유일한 인수인계 수단으로 사용하지 않는다. 장비/에이전트 전환 시 필요한 소스와 문서를 함께 전달해야 하며 commit/push는 사용자 요청 범위에 따른다.
