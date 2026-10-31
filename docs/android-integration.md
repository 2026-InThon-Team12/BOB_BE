# Android 연동 계약

> 신규 구현은 [handoff 연동 계약](handoff-integration.md)을 우선합니다. 질문 원문은 기기 로컬 전용이며, 아래의 서버 답변 저장 API는 이전 `ADAPTIVE`/`LEGACY` 데이터용입니다. 로컬 모드 푸시는 `client_managed=true` 데이터 메시지를 받아 앱이 완료 기록을 확인한 뒤 표시합니다.

서버 API 문서는 실행 중인 `/docs`와 `/openapi.json`을 기준으로 합니다. Android에는 Lablup API 키와 Firebase 서비스 계정 JSON을 포함하지 않습니다.

## 1. 서버 연결과 게스트

- API 기본 경로는 `https://<server>/api/v1`입니다. 로컬 실기기에서는 같은 네트워크의 개발 PC 주소를 사용합니다. 에뮬레이터에서 PC 서버에 접근할 때는 일반적으로 `10.0.2.2:8000`을 사용합니다.
- 저장된 게스트 토큰이 없을 때만 `POST /guest-sessions`를 호출합니다. 본문은 없습니다.
- 응답의 `access_token`을 Android Keystore를 이용한 보안 저장소에 보관하고 모든 업무 요청에 `Authorization: Bearer <access_token>`을 추가합니다. `guest_id`와 FCM 토큰은 API 인증 수단이 아닙니다.
- 앱 데이터 삭제·기기 변경 후 계정 복구는 제공하지 않습니다. 재실행 때마다 게스트를 새로 만들면 기록이 분리됩니다.
- 금액은 원 단위 정수이며 날짜는 `YYYY-MM-DD`입니다. 단말 시간으로 응답 날짜를 계산하지 말고 `/today`가 반환한 날짜를 사용합니다.

## 2. FCM 초기 설정

1. 서버와 동일한 Firebase 프로젝트에 Android 패키지를 등록하고 `google-services.json` 및 Firebase Messaging SDK를 연결합니다.
2. 앱 시작 시 알림 채널 ID **`bob_daily`**를 생성합니다. Android 13 이상에서는 `POST_NOTIFICATIONS` 런타임 권한을 요청합니다.
3. FCM 등록 토큰을 받고 다음 요청으로 서버에 보냅니다. `onNewToken`과 앱 재시작 시 갱신합니다.

```http
PUT /api/v1/me/push-token
Authorization: Bearer <guest-access-token>
Content-Type: application/json

{"token":"<FCM registration token>","enabled":true}
```

알림을 끄면 `DELETE /me/push-token` 또는 `enabled:false`로 반영합니다. 다른 게스트에 등록된 토큰을 재사용하면 `409 TOKEN_ALREADY_REGISTERED`가 반환됩니다. 앱 데이터 초기화 뒤에는 새 게스트와 현재 유효한 FCM 등록 정보를 함께 관리해야 합니다.

## 3. 푸시 수신과 화면 이동

FCM은 notification과 data를 함께 보냅니다. data 값은 모두 문자열입니다.

```json
{"route":"today","digest_id":"<dispatch UUID>","test":"false"}
```

- 백그라운드에서는 OS가 알림을 표시합니다. 클릭으로 앱이 열리면 Intent extras를 읽고 `route=today`를 처리합니다.
- 전경에서는 `FirebaseMessagingService.onMessageReceived`에서 같은 채널로 로컬 알림을 표시합니다. 같은 메시지를 OS 알림과 중복 표시하지 않습니다.
- `digest_id`를 중복 처리 방지 키와 알림 tag로 사용합니다. 클릭할 때 `GET /today`를 새로 호출합니다. 푸시 본문에는 개별 상품 정보가 없으며 화면 데이터의 원본이 아닙니다.
- 정기 알림은 서울 시간 20시 이후 하루 한 건입니다. 수신 권한·네트워크·절전 상태에 따라 실제 표시는 지연되거나 누락될 수 있습니다. 미처리 작업은 `/today`에 남습니다.
- 서버 테스트 CLI가 보내는 메시지는 `test=true`이며 정기 알림과 별도입니다.

## 4. 질문 카드와 구매 후 회고

구매 전 새 흐름은 [handoff 연동 계약](handoff-integration.md)을 적용합니다. 신규 등록 기본값은 `question_mode:LOCAL`이며 `/today.pre_purchase_questions` → 로컬 질문 생성 POST → 기기 응답 저장 → 종료 시 요약 POST 순서입니다. 아래의 YES/NO/UNSURE 고정 질문은 `LEGACY` 시뮬레이션에만 사용합니다. 시뮬레이션 관찰 기간과 ‘구매 후 30일 예상 사용일수’를 구분해 표시합니다.

구매 후 새 질문 흐름은 [구매 후 AI 맞춤 질문 계약](purchase-questions.md)을 우선 적용합니다. `/today`의 `purchase_questions`에서 구매당 한 문항을 생성하고 `/purchase-questions/{id}/answer`에 답합니다. 아래 `reviews`는 기존 14일 회고 호환 계약이며 새 질문과 동시에 중복 응답을 요구하지 않습니다. 구매 전 시뮬레이션 질문은 별도로 유지합니다.

`GET /today`는 다음 구조를 반환합니다.

```json
{"date":"2026-10-31","questions":[],"decisions":[],"reviews":[],"answered":[],"summary":{"answered_count":0,"remaining_count":0,"counts":{"YES":0,"NO":0,"UNSURE":0}},"next_notification_at":"2026-11-01T20:00:00+09:00"}
```

`questions`의 각 카드에는 `item_id`, `simulation_id`, `answer_date`, `question`, `category`, `current_day`, `duration_days`, `calibration`, `answer_options`가 있습니다. **품목당 `question` 하나만 표시하고 답변도 하나만 보냅니다.** 기존 `measurement_question`·`supplemental_question`은 호환용이므로 새 화면에서 중복 표시하지 않습니다. `calibration`은 개인 이력이 부족하면 `null`입니다.

```http
PUT /api/v1/simulations/<simulation_id>/answers/<answer_date>
Content-Type: application/json
Authorization: Bearer <guest-access-token>

{"usage_answer":"YES"}
```

선택지 값은 `YES`, `NO`, `UNSURE`이며 화면 라벨은 서버의 `answer_options`를 사용합니다. 성공 후 `/today`를 다시 읽으면 응답한 카드는 제외됩니다. 같은 날짜에 같은 PUT을 보내면 수정됩니다. 지난 날짜는 수정할 수 없습니다.

완료 화면은 `answered`와 `summary`를 사용합니다. 처음부터 다시 보기는 `GET /today?include_answered=true`로 답변한 카드까지 받고 기존 `answer`를 표시합니다. 오늘 남은 카드가 0이어도 내일 카드가 실제로 존재한다는 뜻은 아닙니다.

`decisions`는 `item_id`로 구매·보류·포기를 처리합니다. `BUY`일 때만 `purchased_on`과 `actual_price`를 함께 보낼 수 있습니다. 반환된 품목의 `purchase.id`를 사후 회고에 사용합니다.

`HOLD`는 `Idempotency-Key`가 필수이며 다음 날부터 7일을 연장합니다. `ON_HOLD`는 연장 시작 전, `ACTIVE`는 연장 관찰 중, `COMPLETED`는 다시 결정할 수 있는 상태입니다. `GIVE_UP`은 가격을 목표에 적립하고 `BUY`는 실제 구매 가격을 지갑 지출에 기록합니다. 성공 후 `/home`, `/goals/current`, `/wallet`을 갱신합니다.

`reviews`의 `id`가 구매 ID이며, `review_start`와 `review_end`는 회고할 14일입니다. 화면과 비교 단위는 **횟수가 아닌 사용일수**입니다. `review_options`에 따라 `PUT /purchases/{id}/review`에 `{"frequency":"SOMETIMES"}`를 보냅니다. 선택지는 `ALMOST_DAILY`(10~14일), `OFTEN`(5~9일), `SOMETIMES`(1~4일), `NEVER`(0일)입니다. 늦게 응답해도 날짜 구간을 옮기지 않습니다. 이전 버전에서 이미 작성한 7일 회고는 원래 구간과 정확한 `used_days`를 유지합니다.

막대는 `expected_days_in_review_window`와 `actual_days_estimate`를 사용합니다. 실제 사용 막대에는 구간 및 ‘추정’을 표시합니다. `review.min_days/max_days`가 원래 선택 범위입니다. 구간 중간값으로 계산한 `overprediction_ratio_estimate`를 정확한 사용횟수라고 표시하지 않습니다.

`difference_percentage_points`가 `null`이면 비교 가능한 데이터가 부족하거나 회고 전입니다. 이를 0% 차이로 표시하지 않습니다. 결과의 모름·미응답·응답률도 함께 표시합니다.

## 5. AI 초안과 최종 등록

- 직접 입력은 `POST /item-drafts`로 이름·선택적 가격을 보냅니다. 이미지 등록은 `/cart-extractions`의 multipart 필드 `image`로 실제 이미지 파일을 보냅니다.
- 등록 방법 선택의 두 번째 버튼은 **‘상품 URL 입력’**입니다. URL 입력창에서 붙여넣고 ‘상품 정보 불러오기’를 누르면 `POST /product-url-drafts`에 `{"url":"https://…"}`로 보냅니다. 공유 Intent 연결은 새 흐름에 필요 없습니다. 서버는 공개 HTTPS 페이지의 OG/JSON-LD를 읽고 이름·가격·이미지를 추출하며 카테고리는 AI로 분류합니다. 로그인·JavaScript 렌더링이 필요한 쇼핑몰은 직접 입력으로 안내합니다.
- 세 초안 API는 `items`, `warnings`, `analysis_method`를 반환하며 DB에 품목을 저장하지 않습니다. 품목의 `simulation_days`로 기간을 미리 표시하고 가격 수정 시 서버 규칙(3/7/14일)과 맞춥니다.
- `price:null`, `category:OTHER`, `analysis_method:manual`도 정상적인 수정·직접 입력 경로입니다. 경고가 있을 때 사용자가 확인할 수 있게 합니다.
- 사용자가 확인한 품목만 `/items/batch`에 최대 20개 제출합니다. 이유는 `purchase_reasons` 배열로 1~8개 선택합니다. `supplemental_question`은 초안 값을 전달하거나 생략해 기본 질문을 사용합니다. 응답 전용 `simulation_days`, `category_source`, `image_crop`는 등록 본문에서 제외합니다. URL 초안은 `source:"URL"`, 필수 `source_url`, 선택적 `image_url`을 전달합니다. `NAME_REQUIRED`·`PRICE_REQUIRED`이면 해당 값을 먼저 입력하고, 불러오기 실패 시 URL은 로컬에 유지한 채 직접 입력으로 이어갑니다. 상세 오류·화면 계약은 [최신 handoff 명세](handoff-integration.md)를 따릅니다. `/share-drafts`와 `source:SHARE`는 이전 클라이언트 호환용입니다.
- S13 잔액 비교는 `GET /wallet/compare?price=189000`을 사용합니다. 아직 월 예산이 없으면 `budget_required:true`이며 금액을 0으로 단정하지 않습니다.
- 등록 버튼을 누를 때 UUID를 만들고 `Idempotency-Key`에 담습니다. 응답 유실로 재시도할 때는 본문과 키를 모두 유지합니다. 새 등록 동작에는 새 키를 사용합니다.

## 6. 목표와 오류 처리

월 예산은 `/budgets/{YYYY-MM}`에 `income`, `food`, `living`, `transport`, **`savings`**를 보냅니다. 지연 일수는 `savings / 월 일수`를 기준으로 반올림합니다. 활성 목표는 `/goals`에 `name`, `target_amount`로 생성합니다. `/impulses`에 `name`, `amount`와 중복 방지 키를 보내면 지연 일수와 `if_resisted_on`·`if_purchased_on`을 반환합니다. 비교 결과와 날짜는 생성 시점에 고정돼 재시도해도 변하지 않습니다.

즉석 소비의 결정은 `RESISTED`, `PURCHASED`, `CANCELLED`입니다. `RESISTED`는 가상 적립, `PURCHASED`는 지갑 지출, 이미 참은 기록의 `CANCELLED`는 적립 취소입니다. 별도 `/expenses` 요청으로 같은 구매를 다시 기록하지 않습니다. 목표가 없어도 적립은 보존되며 목표 생성 시 반영됩니다. `is_virtual=true`이므로 실제 저축이나 계좌 잔액으로 표시하지 않습니다.

목표 변경은 `POST /goals/current/replacement`이며 이동 ON/OFF와 `Idempotency-Key`를 보냅니다. 이동하지 않은 금액은 이전 목표에 보존합니다. 같은 목표의 정보만 수정할 때는 `PATCH /goals/current`를 사용합니다. 달성 후 `POST /goals/current/next`는 이전 달성 목표의 돈을 재사용하지 않습니다. 상세 계약은 [최신 handoff 명세](handoff-integration.md)를 따릅니다. 달성 화면에는 `resisted_count`, `elapsed_days`, `most_resisted_item`을 사용합니다.

홈은 `GET /home`, 지갑은 `GET /wallet`, 리포트는 `GET /reports?month=YYYY-MM`입니다. 지갑 수동 지출은 `POST /expenses`(중복 방지 키 필수)에 `{"name":"인형뽑기","amount":10000,"spent_on":"2026-10-31","memo":null}`를 보내고, 수정은 `PUT`, 삭제는 `DELETE /expenses/{id}`입니다. 새 월 예산이 없으면 `needs_monthly_confirmation:true`입니다.

할인은 사용자가 조건 입력 → 전체 품목 스위치 → `/discount-previews` → `/items/batch`의 `discount_review`에 `NEEDED` 또는 `ALL` 확정 순서입니다. `/discount-extractions`는 제거되어 410을 반환합니다. 상세 입력은 [handoff 연동 계약](handoff-integration.md)을 참고합니다.

업무 오류 형식:

```json
{"error":{"code":"ANSWER_DATE_INVALID","message":"오늘 날짜의 답변만 작성하거나 수정할 수 있습니다.","request_id":"..."}}
```

- `401`: 토큰 누락·무효. 보관한 토큰을 확인합니다.
- `404`: 소유한 항목이 없거나 아직 예산·목표를 만들지 않았습니다.
- `409`: 상태 충돌·중복 방지 키 재사용. 데이터 재조회 후 현재 상태를 반영합니다.
- `422`: 입력값 오류. 입력을 수정합니다.
- `413`: 이미지가 허용 용량·크기를 초과합니다.

실기기 완료 확인: 게스트 유지 → FCM 등록 → 전경·백그라운드 수신 → 알림 클릭 → 오늘 카드 응답 → 중복 재전송 → 이미지 초안 수정 → 목표 적립을 순서대로 실행합니다. 서버의 FCM 접수 메시지만으로 실제 수신을 완료 처리하지 않습니다.

공식 참고: [FCM Android 시작하기](https://firebase.google.com/docs/cloud-messaging/android/get-started), [Android 메시지 수신](https://firebase.google.com/docs/cloud-messaging/android/receive).
