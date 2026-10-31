# 기간 중 Bye

대상: `PUT /api/v1/items/{item_id}/decision`, `{ "decision": "GIVE_UP" }`.
시작 전·진행 중·HOLD 연장 중에도 허용한다. 기간 종료 전 제한은 HOLD에만 적용한다.
기존 소유권/게스트 row lock/확정 결정 보호/구매 정보 검증을 유지한다.
동일 GIVE_UP 재시도는 저장된 결과를 반환한다. 확정 뒤 과거 HOLD 키 재전송도 409다.

## 저장 및 응답

- 조기 포기는 `Item.decision=GIVE_UP`과 변경 불가한 `decided_on`으로 계산한다. `Simulation.ended_early_on`은 조기 구매 전용으로 유지한다. **DB 마이그레이션 없음**.
- 결정일이 계획 종료일보다 빠르면 `simulation.status` 및 결과 `comparison_reason`은 `EARLY_GIVE_UP`, 비교 자격은 false다. 조기 구매는 기존 `EARLY_PURCHASED` / `EARLY_PURCHASE`를 유지한다.
- `ItemOutput.decided_on`과 `simulation/result.observation_end_date_exclusive`, `termination_reason`을 제공한다. 실제 관찰 종료(미포함)는 조기 종료일의 다음 날이다. `end_date_exclusive`와 `duration_days`는 원래 계획을 보존한다.
- 반환되는 `observation_dates`는 조기 종료일까지 제한하고 `current_day`, 결과 `observed_days`도 그 시점에 고정한다. 이후 미래 날짜가 미응답으로 누적되지 않는다. 조기 종료 `pending_days`는 0이다.
- LOCAL 조기 포기에 미전송 응답이 있더라도 서버는 알 수 없다. 서버 집계가 없으면 `answer_state=CLIENT_MANAGED`, `answered_days`, `unsure_days`, `unanswered_days`, `definite_answer_days`, `answer_coverage`는 **null**이다. 프론트는 0일 미응답으로 표시하지 말고 기기 기록을 사용하거나 미확인으로 표시한다. 기존 기기 질문·답변을 지우거나 요약을 임의 생성하지 않는다.
- 집계를 이미 받은 LOCAL 기록은 `AGGREGATED`, 서버 저장 모드는 `SERVER_RECORDED`다.
- 기존 SavingEvent 경로로 1회 적립·목표 달성·포기 통계를 처리한다. Purchase/Expense/후속 점검은 생성하지 않는다.
- `/today`, `/home` 진행 목록, 질문 생성 및 알림 대상에서 제외한다. `/items` 전체 기록은 유지한다. 시작 전 확정된 포기 물품도 PATCH를 금지한다.

## 검증

`tests/test_early_give_up.py`: 3가지 질문 모드 × 시작 전/첫날/중간날/연장 중/종료 후, 재시도/다른 결정/이전 HOLD 키/소유권, 보존된 질문·답변, 날짜 이동, 목표·포기율, 알림 워커 제외, PostgreSQL 동시 요청을 검증한다.
기존 전체 테스트로 조기 BUY와 정상 구매·HOLD 기간/마감/멱등성을 함께 확인한다.
PostgreSQL 동시성 테스트는 `TEST_DATABASE_URL` 설정이 필요하며, 각 테스트는 임시 스키마를 생성·제거한다.

배포 시 DB 마이그레이션 없이 API를 재배포한다. 운영 검증은 별도 테스트 게스트로 등록 당일 GIVE_UP, 재시도 시 단일 적립, HOLD 차단, 홈 제외 및 전체 기록 유지를 확인한다.
Expo 실기기 확인은 배포된 백엔드 주소에 연결한 뒤 기간 중 Bye → 확정 → 홈 제외 → 전체 기록 유지 → 앱 재실행 순서로 별도 확인한다.
