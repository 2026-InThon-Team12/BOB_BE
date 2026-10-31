> 2026-11-01 변경: 목표·적립·근로 시간에 관한 아래 초기 명세는 [달력 적립 명세](calendar-funding.md)로 대체되었습니다. 포기/참기 이벤트를 게이지에 합산하지 않습니다.

# 2026-10-31 handoff 연동 계약

배포 API: `https://bob-be.vercel.app/api/v1` · [Swagger](https://bob-be.vercel.app/docs).
Vercel 배포에서는 이미지 파일을 **4,000,000바이트 이하**로 압축해서 전송한다.
플랫폼 한도 초과 시 JSON이 아닌 413 응답도 처리해야 한다.
무료 cron 푸시는 한국 시간 20:00~20:59에 실행되며, Firebase 연결 후 사용할 수 있다.
운영 설정은 [Vercel 배포 문서](vercel-deployment.md)를 참고한다.

기준 자료는 `BoB_handoff/FE_SPEC.md`와 `prototype/*.html`이다. 사용자가 이후 확정한 사항을 우선한다: 질문·답변 원문은 **로컬에만 저장**, 종료 때 전송해 요약, 구매 후 AI 질문 유지, 마감일은 기본 기간보다 빠를 때 단축하고 오늘 마감이면 즉시 결정.

추가 확정: **목표 달성 즉시 진열**, 구매 확인 없음. 목표 설정에서 받은 상품 URL을 ‘사러 가기’로 연다. 할인 화면의 무료배송 토글 OFF는 배송비 입력, ON은 무료배송 기준 금액 입력이다. 이 계약이 앞선 ‘구매한 목표만 진열’ 설명을 대체한다.

최신 `GoalSetup.html`·`GoalSetupFirst.html`을 반영했다. 목표 변경의 적립금 이동 OFF 및 초과분은 **이전 목표 기록에 보존**한다. HTML은 사용자가 수정하며, 이 문서는 상품 URL 입력 및 진열장의 구매 후 점검 영역 제거에 대한 연동 계약을 포함한다.

이 저장소는 백엔드다. 첨부 HTML은 화면 참고용이며 Android 프로젝트는 포함되어 있지 않다. `clients/local-question-store.mjs`는 실제 동작과 테스트가 있는 웹 저장 어댑터다. Android는 동일한 규칙을 앱 전용 로컬 DB로 구현한다. 서버 키와 게스트 토큰은 질문 저장소에 넣지 않는다.

## AI 사용과 저장 경계

| 위치 | API | 서버 보관 |
|---|---|---|
| 구매 전 품목별 고정 질문 변환 | `POST /simulations/{id}/local-question` | 질문·응답 미보관 |
| 종료 시 응답 요약 | `POST /simulations/{id}/result-summary` | 응답 일수·모름 일수·명시적 예상 사용일수 집계만 |
| 스크린샷 품목명·가격·카테고리·이미지 | `POST /cart-extractions` | 이미지 원본/크롭 미보관 |
| 구매 후 고정 질문 변환 | `POST /purchases/{id}/local-question` | 질문·응답 미보관 |
| 할인·지갑·목표 계산 | 아래 일반 API | 일반 업무 기록; AI 호출 없음 |

모든 경로의 공통 접두사는 `/api/v1`, 인증은 게스트 Bearer 토큰이다. AI 오류·타임아웃·형식 오류 시 기본 질문/계산 기반 요약을 반환한다. 형식 보정은 1회다. 모델에 전송되는 원문은 처리 중에만 사용하며 애플리케이션 DB·로그에는 저장하지 않는다. AI 공급자의 보관 정책과는 별개다.

## 1. 등록 → 할인 확인 → 시작

### 상품 URL로 등록

등록 방법은 **장바구니 스크린샷 / 상품 URL 입력 / 직접 입력**이다. ‘쇼핑앱에서 공유하기’와 Android 공유 Intent 수신을 새 등록 흐름에서 사용하지 않는다. 화면 버튼은 ‘상품 URL 입력’, 설명은 ‘상품 페이지 URL을 붙여넣으면 이름·이미지·가격을 불러와요. 확인할 수 없는 정보는 직접 적어 주세요.’로 변경한다. HTML 수정은 사용자가 진행한다.

버튼을 누르면 URL 입력창과 ‘상품 정보 불러오기’ 버튼을 표시한다. 비어 있을 때는 버튼을 비활성화하고, 사용자가 붙여넣은 공개 HTTPS 상품 URL 한 개를 전송한다. 클립보드를 자동으로 읽거나 링크만 입력했는데 품목을 등록하지 않는다.

```http
POST /api/v1/product-url-drafts
Authorization: Bearer <guest token>
Content-Type: application/json

{"url":"https://shop.example.com/products/keyboard"}
```

- 응답은 기존 초안과 같은 `items, warnings, analysis_method` 형식이며 품목은 한 개다. 품목명·가격·이미지는 페이지 OG/JSON-LD/제목에서 읽고, 카테고리는 기존 AI 분류를 사용한다. AI가 가격을 임의로 보완하지 않는다.
- 품목의 `source="URL"`, `source_url`은 리다이렉트 후 실제 상품 URL, `image_url`은 확인된 공개 HTTPS 이미지 주소다. 이 단계는 DB에 품목을 저장하지 않는다.
- 로딩 중 중복 요청을 막고, 완료하면 상품 정보 확인 화면으로 이동한다. 이름·가격·카테고리 확인/수정 → 구매 이유 선택 → 등록 미리보기/할인 확인 → `/items/batch` 순서다. URL 한 개가 장바구니 전체 등록을 의미하지 않는다.
- `CONFIRM_PRODUCT_INFORMATION`: 불러온 상품 정보를 확인한다. `NAME_REQUIRED`: 이름을 직접 입력한다. `PRICE_REQUIRED`: 가격이 `null`이므로 실제 원화 금액을 입력한다. `PRODUCT_IMAGE_UNAVAILABLE`: 기본 카테고리 이미지 또는 로컬 사진을 사용한다. 외화·범위·옵션별로 불명확한 가격을 원화 숫자로 추정하지 않는다.
- `422 UNSAFE_PRODUCT_URL`: URL을 수정하도록 안내한다. 공개 HTTPS만 가능하며 내부 주소·인증 정보·비표준 포트를 거절하고 리다이렉트마다 확인한다. `422 PRODUCT_URL_UNAVAILABLE`: 접근 제한/시간 초과/메타데이터 페이지를 불러올 수 없음이므로 URL 재확인·직접 입력·스크린샷을 안내한다. 로그인하거나 스크립트를 실행해서 상품을 가져오지 않는다.
- 불러오기 실패 후 직접 입력으로 이어가도 입력한 URL은 로컬 초안에 유지한다. 최종 등록에 `source:"URL"`, `source_url`, 확인한 `name, price, category`, `purchase_reasons`를 보낸다. 응답 전용 `warnings, category_source, simulation_days, image_crop`를 등록 JSON에 그대로 복사하지 않는다.
- URL 등록에는 `source_url`이 필수다. 가격이 `null`인 초안은 최종 등록할 수 없다. 초안 생성에는 중복 방지 키가 필요 없고 최종 `/items/batch`에는 기존대로 필요하다.
- 이전 `/share-drafts`는 deprecated 호환 API로 남고 `source:"SHARE"`를 반환한다. 새 화면은 `/product-url-drafts`만 사용한다. 기존 SHARE 기록을 바꾸거나 삭제하지 않으며, `source`는 문자열 열이므로 이 변경만을 위한 DB 마이그레이션은 없다.

### 초안 확인과 구매 이유

인식 또는 직접 입력 결과는 로컬 초안이다. 등록 전에 이름·금액·카테고리를 확인하고 품목별 이유를 선택한다. 이유 코드는 다음과 같다. FE 타입 초안의 별칭을 그대로 보내지 않는다.

| FE 초안 | API 코드 | 표시 |
|---|---|---|
| SNS_AD | ADVERTISEMENT | SNS·광고를 보고 |
| SALE_LIMITED | DISCOUNT_LIMITED | 할인·한정 판매 때문에 |
| THRESHOLD_DISCOUNT | EXTRA_DISCOUNT | 일정 금액 이상 담으면 추가 할인을 받을 수 있어서 |
| STRESS | STRESS | 스트레스·기분 전환을 위해 |
| PEER | SOCIAL | 주변 사람이 가지고 있어서 |
| NEED | NECESSITY | 실제로 필요해서 |
| LONG_WANTED | LONG_WANTED | 오래전부터 원했던 물건이라서 |
| OTHER | OTHER | 기타 |

`purchase_reasons`는 중복 없는 1~8개 배열이다. `OTHER` 선택 시 `reason_other_text` 1~300자, `DISCOUNT_LIMITED` 선택 시 서울 날짜 `sale_ends_on: YYYY-MM-DD`가 필수다. 다른 이유에서는 해당 상세 필드를 생략한다. 이유를 해제하는 PATCH에서는 상세 필드도 `null`로 지운다.

```json
{
  "items": [{
    "name": "코트", "price": 189000, "category": "CLOTHING",
    "purchase_reasons": ["DISCOUNT_LIMITED", "OTHER"],
    "reason_other_text": "출근할 때 입으려고",
    "sale_ends_on": "2026-11-03", "question_mode": "LOCAL"
  }]
}
```

먼저 `POST /registration-previews`로 기간과 다음 화면을 확인한다. 같은 요청으로 `POST /items/batch`를 호출하면 등록된다(필수 `Idempotency-Key`, 8~128자). 하나라도 `EXTRA_DISCOUNT`이면 확인 없는 등록은 `409 DISCOUNT_CHECK_REQUIRED`다.

할인 화면에서는 **등록 묶음 전체 품목**을 보여 준다. `EXTRA_DISCOUNT` 품목은 기본 OFF, 나머지는 ON이다. 스위치는 등록 여부가 아니라 ‘할인이 없어도 살 물건’ 표시다. `NEEDED`로 확정하면 ON만 등록, `ALL`이면 OFF도 포함해서 등록한다. OFF 물품을 자동 Bye 처리하거나 절약으로 적립하지 않는다.

등록 요청에 아래 `discount_review`를 추가한다. 인덱스는 `items` 배열의 0부터 시작하는 위치다. 순서 변경 시 인덱스도 갱신한다.

```json
{
  "rule": {"minimum_amount": 30000, "unit": "PCT", "value": 10},
  "needed_item_indexes": [0],
  "decision": "NEEDED"
}
```

같은 등록 키·같은 요청은 동일 결과를 반환한다. 요청 변경은 새 키를 사용한다. 모두 OFF인 상태에서 NEEDED를 선택하면 품목 0개로 완료하며 시뮬레이션을 만들지 않는다. 등록 응답의 `discount`에 전체·선택 결제액과 확정 선택이 함께 반환된다. 할인액은 장바구니 기준이며 개별 품목 가격에 임의 배분하지 않는다. 실제 구매 시 `actual_price`를 확인한다.

### 마감일과 질문 기간

- 기본: 30,000원 이하 3일 / 100,000원 미만 7일 / 그 외 14일.
- 시작: 등록 다음 서울 날짜. 일수 = `min(기본 일수, 마감일 − 오늘)`.
- 예: 10/31 등록, 11/3 마감 → 11/1~11/3, 3일. `end_date_exclusive`는 11/4.
- 오늘 마감 → 일수 0, 상태 COMPLETED, 바로 결정. AI 질문/사용률 비교 없음.
- 지난 날짜는 422. 마감일을 넘겨 보류 기간을 연장할 수 없으며, 남은 기간이 있으면 7일 이내로 연장한다.
- `simDays`를 3|7|14 열거형으로 제한하지 말고 0 이상의 정수로 받는다.

## 2. 할인 금액 계산

`POST /discount-previews`는 부작용 없는 즉시 계산이다. 입력/토글 변경 때 호출하거나 같은 공식으로 화면에서 미리 계산한다. 서버가 최종 등록 때 다시 계산한다.

```json
{
  "items": [
    {"client_id":"shirt","name":"티셔츠","price":20000,"enabled":true},
    {"client_id":"socks","name":"양말","price":10000,"purchase_reasons":["EXTRA_DISCOUNT"],"enabled":false}
  ],
  "rule": {"minimum_amount":30000,"unit":"PCT","value":10,"free_shipping_enabled":false,"shipping_fee":3000}
}
```

`needed`는 ON 품목, `all`은 모든 품목으로 각각 계산한다. 두 객체 모두 `subtotal / discount_eligible / discount / shipping / total / breakdown`을 제공한다. `breakdown`은 새 화면의 ‘상품 …원 − 할인 …원 + 배송비 …원’ 구성 내역이다. `without_discount`는 전체 품목의 할인 미적용 비교 객체로 동일 필드를 제공하며 `discount=0`이다. 금액 표시·비교에는 숫자 필드를 사용한다.

- 합계 **≥** 최소금액이면 할인. 퍼센트는 원 단위 반올림(양수 half-up), 고정액 WON은 합계 이하로 제한. 비율 0~100.
- 빈 선택은 합계·할인·배송비·결제액 모두 0.
- 배송비는 입력한 `shipping_fee`; 무료배송 토글은 `free_shipping_enabled`다. OFF에서는 기준 금액이 남아 있어도 무시하고 배송비를 적용한다. ON에서는 `free_shipping_minimum` 이상일 때만 무료이며 기준 미달이면 저장해 둔 배송비를 적용한다. 기준은 할인 전 합계다.
- `extra_spending = all.total − needed.total`. OFF가 있고 양수이면 TRAP, 그 외 SMART.
- `without_discount_total = all.subtotal + all.shipping`; `saved_amount = all.discount`.
- 무료배송 안내의 절약액은 기준에 도달하는 가상 결제액과 현재 결제액의 차이에서 계산한다. 음수 절약 문구를 표시하지 않는다.
- `shipping_notice`는 무료배송 OFF이면 `null`이므로 안내 영역을 숨긴다. ON에서는 기준까지 남은 금액 또는 조건 달성 문구다. ‘배송비를 내는 쪽이 …원 아껴요’는 `pay_shipping_saves > 0`일 때만 포함한다. 예: 합계 3만 원·3만 원부터 10% 할인·배송비 3천 원·4만 원 이상 무료배송이면 현재 결제 3만 원, 기준 도달 시 결제 3만6천 원이므로 절약은 **6천 원**이다. 단순 `남은 금액 − 배송비`는 추가 할인액을 놓치므로 사용하지 않는다.
- 예: 20,000원 ON + 10,000원 OFF, 30,000원부터 10% 할인 → 필요한 것 20,000원, 전체 27,000원, 추가 지출 7,000원.

구형 `/discount-extractions`는 410을 반환하고 AI를 호출하지 않는다. 구형 `/discount-comparisons`는 이전 기록 호환용이며 새 화면은 사용하지 않는다.

### 배송 토글 입력 계약

OFF에서는 배송비 입력란을 표시하고 ON에서는 ‘몇 원 이상 무료배송’ 입력란을 표시한다. 토글을 켜도 **기존 배송비를 지우지 않는다**. ON에서 기준 미달인 장바구니 계산에 필요하다. 두 계산(켜진 품목만/전체 품목)에 각각 조건을 적용한다.

```json
{"minimum_amount":30000,"unit":"PCT","value":10,
 "free_shipping_enabled":true,"shipping_fee":3000,"free_shipping_minimum":40000}
```

새 UI는 ON/OFF와 관계없이 `shipping_fee`를 명시하고, ON이면 `free_shipping_minimum`도 반드시 보낸다. 배송비를 입력하기 전에 ON을 선택했다면 OFF에서 배송비부터 입력하도록 안내한다. 누락 시 422이며 샘플 배송비를 대신 쓰지 않는다. 실제 무료배송이면 배송비를 0원으로 명시할 수 있다. 화면의 ‘4만 원’은 API에서 `40000`원이다.

이전 클라이언트가 토글 필드를 생략하면 기존처럼 기준 금액 유무로 ON/OFF를 판별하고 기본 배송비 0원 계약을 유지한다. 새 UI에서는 이 호환 동작에 의존하지 않는다.

## 3. 매일 질문과 로컬 저장

등록 기본 `question_mode=LOCAL`. `/today`의 `pre_purchase_questions`·`purchase_questions`에서 `answer_state=CLIENT_MANAGED`인 카드는 로컬 기록을 기준으로 완료 여부를 표시한다. 서버의 remaining_count는 처리 후보 수이며 로컬 미응답 수가 아니다.

`POST /simulations/{id}/local-question` 또는 `POST /purchases/{id}/local-question`:

```json
{"recent_templates":[{"date":"2026-11-01","template_code":"DISCOVERY"}]}
```

이전 질문의 **코드와 날짜만** 보내고 응답은 보내지 않는다. 첫날은 `{}`. 응답에는 `id, question_date, template_code, template_version, question, answer_spec, reference_text, generation_method, storage=LOCAL_ONLY`가 있다. 구매 후 사용일수 질문에는 고정 회고 구간 `usage_window`도 있다.

저장 키는 **게스트 + 구매 전/후 + simulation/purchase ID + 서울 날짜**다. 그날 생성한 질문이 있으면 API를 다시 호출하지 않는다. 서버는 질문을 저장하지 않으므로 네트워크 재요청 시 다른 문장이 나올 수 있다. 표시한 질문·선택지를 먼저 저장하고 응답을 같은 레코드에 덮어쓴다. 같은 날짜 내 수정 가능, 소급 작성 금지. 로컬 저장 실패를 성공으로 표시하지 않는다.

고정 질문 사진의 객관식·숫자·짧은 주관식·구조화 선택 형식을 유지한다. 명세의 DailyCheck 3버튼 UI는 YES/NO/UNSURE 문항에만 맞는다. 감정·이유·30일 사용 예상 문항은 `answer_spec`으로 UI를 렌더링해야 한다. 감정의 긍정 응답을 사용일수로 세지 않는다.

`clients/local-question-store.mjs`에는 질문 재사용, 동시 탭 방지, 당일 응답 수정, 게스트별 분리, 종료 요약 전송이 구현돼 있다. 웹 `localStorage` 어댑터이며 Android에서 직접 실행되는 코드는 아니다. 앱 삭제/저장소 초기화 시 원문은 서버에서 복원되지 않는다.

### 종료 요약

`POST /simulations/{id}/result-summary`는 Buy/Bye 확정 **전에**, 기간 종료 후 호출한다.

```json
{"responses":[
  {"date":"2026-11-01","template_code":"EXPECTED_USE_DAYS",
   "question":"키보드를 산 뒤 30일 동안 며칠 쓸 것 같아요?",
   "answer":{"number_value":12}}
]}
```

각 답변은 `choice_code / number_value / text_value / object_value` 중 정확히 하나다. 날짜 중복·관찰 기간 밖·질문에 맞지 않는 답변은 422. 클라이언트 기록의 실제 작성 시각까지 서버가 증명하지는 못한다.

응답은 `result`(서버 계산), `ai_summary`(문장·근거 날짜·AI/TEMPLATE·실패 코드), `raw_responses_stored=false`. 화면은 AI 요약을 저장하고 다시 사용할 수 있다. 원문·AI 문장은 서버에 보관하지 않고 세 가지 숫자 집계만 저장한다. 결과에서 계산된 숫자는 AI 문장보다 우선한다.

구매 전 월간 전망은 **30일 중 예상 사용일수**다. 14일 관찰 중 긍정 감정 답변 수로 대체하지 않는다. 마지막 관찰일은 사용일수 예측 질문을 제공한다. 응답이 없거나 모름이면 예측 null. 정상 종료·확정 응답 70% 이상인 경우에만 구매 후 실제 사용률과 비교한다. 결과 화면의 14칸은 응답 여부/질문별 응답을 표시하며 모두 사용일수로 해석하지 않는다.

구매 후 원문도 로컬 보관한다. 기존 14일 후 `/purchases/{id}/review`에 보내는 사용일수 또는 일수 범주는 리포트용 집계다. 로컬의 감정·가치·동기 원문은 해당 API에 보내지 않는다.

### 푸시

워커는 로컬 질문이 포함된 알림을 `client_managed=true`인 **data-only** 메시지로 발송한다. Android가 오늘 목록과 로컬 완료 상태를 합쳐 남은 일이 있을 때 알림을 표시한다. 알림 권한, 백그라운드 수신, 클릭 → 오늘 화면을 실기기로 검증해야 한다. v1 FCM notification payload는 백그라운드에서 시스템이 바로 표시하므로 로컬 완료 필터를 거칠 수 없다. [FCM Android 수신 문서](https://firebase.google.com/docs/cloud-messaging/android/receive-messages)

## 4. 스크린샷 이미지

기존 multipart `POST /cart-extractions` 계약을 유지한다. 각 초안에는 `name, price, category, category_source, image_crop`이 포함된다.

- 검증된 비전 경로: AI가 상품 썸네일의 정규화 좌표 `[left, top, right, bottom]`(0~1000)을 제안하고 서버가 원본에서 크롭한다. 반환 `image_crop`은 `mime_type, base64, width, height, box`이며 최대 320×320 JPEG다. 모델이 임의 외부 이미지 URL을 만들지 않는다.
- OCR 경로: 품목명·가격·카테고리를 추출하지만 상품 사진 위치는 확인할 수 없어 `image_crop=null`, `PRODUCT_IMAGE_UNAVAILABLE`을 반환한다. 카테고리 기본 이미지 또는 사용자 사진을 사용한다.
- 크롭을 기기 로컬에 보관하고 초안/등록 품목 ID와 연결한다. 원본·크롭을 서버에 영구 저장하는 업로드/스토리지 기능은 추가하지 않았다.
- Lablup 실제 배포의 이미지 지원은 `scripts.check_lablup` 결과를 확인한 뒤 활성화한다. 비전 미지원은 OCR로 전환하고 불명확한 가격은 null이다.

## 5. 목표 설정·사러 가기·진열장

첨부 `Goal.html`에는 이미 `GoalShelf.html` 링크가 있다. 목표 카드 아래 진입 줄을 유지하고 `/goals/current`의 `shelf_count`를 표시한다. 목표 설정 화면은 이름·목표 금액·상품 링크를 입력받는다. 목표 이미지 `image_url`도 함께 보낼 수 있다.

```json
{"name":"미쿠 피규어","target_amount":240000,
 "source_url":"https://shop.example.com/products/figure"}
```

`GET /goals/current`·목표 생성/수정·다음 목표 응답에 `source_url, can_shop, achieved, achieved_on, shelf_count`가 반환된다. **‘사러 가기’는 source_url을 외부 브라우저/쇼핑앱으로 여는 버튼**이다. 서버가 URL을 접속하거나 구매를 추정하지 않는다. URL은 HTTPS만 허용하고 자격 증명이 포함된 주소는 거절한다. URL 없는 이전 목표는 `can_shop=false`이므로 설정 화면에서 링크를 입력하도록 안내한다. 현재 목표 URL은 `PATCH /goals/current`로 변경·삭제(null)할 수 있다.

`GET /goals/shelf` → `count, total_target_amount, current_goal, items`. 적립이 목표 금액에 도달한 **같은 트랜잭션에서** `achieved_on`과 달성 당시 통계가 기록된다. 별도 버튼·구매 확인 요청 없이 바로 진열한다. 참기, 물품 Bye 적립, 미배정 적립금 연결, 목표 금액 수정으로 달성한 경우도 동일하다. 재전송·목록 조회는 진열 기록을 늘리지 않는다.

진열 항목에는 목표명·이미지·상품 URL·목표 금액·달성일·걸린 기간·달성 당시 참은 횟수·가장 많이 참은 물건이 있다. 실제 구매를 확인하지 않으므로 구매일·실결제액·목표 구매 후 점검 결과를 만들거나 표시하지 않는다. 진열장 합계는 **달성한 목표 금액의 합**이다.

달성 이력은 적립 취소 뒤에도 유지한다. 지갑/적립 원장은 취소를 그대로 반영하고 `currently_funded`는 현재 적립금이 목표 이상인지 나타낸다. `achieved`는 달성 이력 여부, 진열 통계는 달성 당시 값이다. 달성한 목표의 이름·목표 금액은 고정하며 상품 URL·이미지는 수정 가능하다. 다음 목표는 `/goals/current/next`로 생성한다. 새 목표의 URL을 별도로 받고 이전 목표는 진열장에 남긴다. 초과 적립금을 새 목표에 중복 적립하지 않는다.

이전 `PUT /goals/{id}/purchase`는 410 `GOAL_PURCHASE_CONFIRMATION_REMOVED`를 반환하며 실제 구매 기록을 만들지 않는다. DB의 과거 구매 확인 필드는 기존 자료 보존용으로만 남긴다. 일반 물품의 Buy/구매 후 점검은 기존 기능대로 유지한다.

### 첫 목표·목표 변경·달성 후 다음 목표

`GET /goals/setup?limit=20&offset=0`은 `mode(FIRST|CHANGE|NEXT), current_goal, unallocated_savings, candidates, limit, offset`을 반환한다. 후보는 본인이 등록한 물품만 최신순이며 `item_id, name, target_amount, source_url, image_url`이다. 칩을 누르면 해당 값을 초안에 복사하고 확인·수정한다. 조회만으로 목표를 만들거나 적립금을 옮기지 않는다.

| 진입 | 화면 | 확정 요청 |
|---|---|---|
| 목표 없음 / FIRST | GoalSetupFirst | `POST /goals` |
| Goal ‘목표 바꾸기’ / CHANGE | GoalSetup | `POST /goals/current/replacement` |
| 달성 후 ‘다음 목표 정하기’ / NEXT | GoalSetupFirst | `POST /goals/current/next` |

유효한 이름·양수 금액을 입력하면 `POST /goals/preview`로 하단 미리보기를 갱신한다. 첫 목표는 `mode=FIRST`로 보내고, CHANGE/NEXT에는 현재 목표 ID를 보낸다. 입력 중 이름·가격이 비어 있으면 미리보기 요청을 보내지 않고 확정 버튼을 비활성화한다.

```json
{"mode":"CHANGE","current_goal_id":"현재-목표-UUID",
 "name":"헤드폰","target_amount":200000,"carry_savings":true,
 "source_url":"https://shop.example.com/headphones"}
```

응답 `starting_saved_amount, display_starting_amount, remaining_amount, progress_percent, achieved, message`로 하단 카드를 구성한다. 변경 모드의 이동 스위치는 기본 ON이다. `starting_saved_amount = min(이전 목표의 현재 적립금, 새 목표 금액)`이며 OFF이면 0이다. 남은 금액은 `max(0, 새 목표 금액 − 시작 적립금)`, 표시 게이지는 최대 100%다. 처음/다음 목표는 기존 미배정 적립금만 연결하고 이전 달성 목표의 돈을 재사용하지 않는다.

변경 확정은 위 요청에서 `mode`를 빼고 `/goals/current/replacement`로 보낸다. **`Idempotency-Key`(8~128자)가 필수**이며 동일 동작의 재시도에는 같은 키를 사용한다. 응답은 `goal`(새 활성 목표), `previous_goal_id`, `carried_amount`, `retained_amount`다. 예: 이전 적립금 151,000원, 새 목표 100,000원, ON → 이동 100,000원·이전 보존 51,000원·새 목표 즉시 달성 및 진열. OFF → 이동 0원·이전 보존 151,000원.

이전 목표는 비활성 기록으로 보존한다. 옮기지 않은 돈을 미배정으로 풀거나 삭제하지 않는다. 이동은 새로운 절약/지출이 아니며 월간 절약 합계나 지갑 잔액을 바꾸지 않는다. 이전 CREDIT과 연결한 이동 기록을 남기므로 원래 참기 기록을 취소하면 해당 금액의 이전/새 목표 배분도 함께 취소된다. 목표를 여러 번 바꾼 경우에도 동일하다. 달성 이력은 기존 규칙대로 보존된다.

동일 키·다른 본문은 `409 IDEMPOTENCY_CONFLICT`. 현재 목표 ID가 바뀌었으면 `409 GOAL_CHANGED`이므로 설정 정보를 다시 조회한다. 이미 달성한 목표는 `409 GOAL_ALREADY_ACHIEVED`이며 NEXT 경로를 사용한다. 기존 `PATCH /goals/current`는 같은 목표의 정보 수정용으로 남겨두고 **‘목표 바꾸기’ 저장에 사용하지 않는다**. 재시도 응답은 최초 결과이므로 취소 등 후속 동작 뒤에는 `/goals/current`를 다시 조회한다.

### 화면의 금액과 날짜

설정 화면에는 저축액 입력을 추가하지 않는다. ‘충동구매를 N원 더 참으면…’의 N은 가상 적립금 기준 `remaining_amount`다. 월 계획 저축액을 적립금에 자동으로 더하지 않는다. 기존 Goal의 예상 달성일과 소비 지연일은 지갑의 `월 계획 저축액 / 해당 월 실제 일수`를 하루 평균 저축액으로 사용한다. 남은 날짜는 올림, 소비 지연일은 화면처럼 반올림(최소 1일)한다. 예산이 없거나 계획 저축액이 0이면 날짜는 `null`이며 예산 입력 안내로 표시한다. 날짜는 입력 예산에 따른 추정이며 계좌 잔액·실제 달성 보장이 아니다.

## 6. 배포와 검증

마이그레이션 `0007`까지 먼저 적용한다. `0006`은 기존 달성 목표의 적립 원장/이전 완료 기록으로 진열 이력을 채우고, `0007`은 목표 변경과 원래 적립금의 이동 기록 테이블을 추가한다. 기존 서버 질문/답변·목표·적립 원장은 삭제하지 않는다. 달성 이력 또는 URL이 존재하면 0006 다운그레이드를, 목표 변경 이력이 존재하면 0007 다운그레이드를 데이터 손실 방지를 위해 거절한다.

```powershell
python -m alembic upgrade head
python -m alembic check
python -m pytest
node --test clients/local-question-store.test.mjs
```

화면 흐름은 목표 설정 → Goal → 달성 시 GoalDone/GoalShelf이며, GoalDone의 ‘사러 가기’는 저장 URL을 연다. 할인 페이지는 사용자 확정 OFF/ON 입력 규칙을 사용한다. HTML 변경은 사용자가 진행한다. 목표 설정의 상품 URL 입력과 진열장 ‘산 뒤 2주 점검’ 영역 제거가 필요하다. 구매 확인 없이 실제 구매일·사용일수를 만들어 표시하지 않으며, 고정 배송비 예시는 실제 입력값으로 대체한다.
