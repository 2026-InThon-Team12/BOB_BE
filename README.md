# BOB Backend

구매 전 예상 사용과 구매 후 실제 사용을 비교하는 FastAPI 백엔드입니다. PostgreSQL에 게스트별 기록을 저장하고, 별도 워커가 Android FCM 묶음 알림을 발송합니다.

최신 `BoB_handoff/FE_SPEC.md`와 화면별 HTML, 이후 사용자가 확정한 로컬 저장·할인 마감일 정책을 반영했습니다. 신규 Android 연동은 [최신 handoff API 계약](docs/handoff-integration.md)을 먼저 확인하세요. 이전 [디자인 반영 내역](docs/design-changes.md)은 기존 데이터의 호환 정책을 설명합니다.

수정된 [FE 구현 명세 사본](docs/frontend-spec.md)도 저장소에 포함했습니다. 프로토타입 HTML은 별도 전달 자료입니다.

구매 전에는 확정된 8개 기본 질문과 AI 추가 질문 중 조건에 맞는 질문을 무작위 선택해 상품별로 변형합니다. 신규 구매 전·후 질문은 모두 **예 / 잘 모르겠어요 / 아니요** 스와이프이며, 상세 계약은 [스와이프 질문 명세](docs/swipe-questions.md)를 따릅니다. **신규 등록 기본값은 `LOCAL`**이며 질문·답변 원문은 기기에만 보관합니다. 종료 시 로컬 기록으로 AI 요약을 요청하고 서버에는 숫자 집계만 남깁니다. [로컬 저장 참고 어댑터](clients/local-question-store.mjs)를 제공하며, 기존 `ADAPTIVE`·`LEGACY` 저장 흐름은 이전 데이터 호환용으로 유지합니다. 할인 계산과 금액 적립은 AI를 사용하지 않습니다.

## 실행

공개 서버: [API 문서](https://bob-be.vercel.app/docs),
[DB 연결 상태](https://bob-be.vercel.app/health/ready).
Android Base URL은 `https://bob-be.vercel.app/api/v1/`이다.
무료 호스팅 설정·업로드 제한·재배포 절차는 [Vercel 배포 문서](docs/vercel-deployment.md)를 참고한다.

Docker Engine과 Docker Compose 2.24 이상이 필요합니다. 아래 명령은 저장소 루트에서 실행합니다. 처음 설정할 때 `.env.example`을 `.env`로 복사하고 필요한 값을 채웁니다.

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose logs -f api worker
```

Linux/macOS에서는 복사 명령만 `cp .env.example .env`로 바꿉니다. Compose가 PostgreSQL 준비를 기다린 뒤 마이그레이션을 완료하고 API와 워커를 시작합니다. 데이터는 `postgres-data` 볼륨에 보관됩니다.

- API 문서: [Swagger UI](http://localhost:8000/docs), [OpenAPI JSON](http://localhost:8000/openapi.json)
- 프로세스 상태: [health](http://localhost:8000/health), DB 연결 상태: [readiness](http://localhost:8000/health/ready)
- 기본 DB 계정 `bob/bob`은 로컬 개발용입니다. 외부 서버에서는 비밀번호를 교체하고 HTTPS와 네트워크 접근 제한을 설정합니다. Compose의 비밀번호에는 URL 예약 문자가 없는 긴 임의 문자열을 사용합니다.
- AI 키가 없으면 직접 입력과 기본 질문이 동작합니다. Firebase 자격 증명이 없으면 워커가 설정을 기다리며 당일 발송 기록을 소모하지 않습니다.

### Python으로 개발

Python 3.12와 PostgreSQL 16을 기준으로 합니다. `requirements.lock`에는 테스트 도구를 포함한 의존성이 고정되어 있습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
docker compose up -d db
python -m alembic upgrade head
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

다른 터미널에서 같은 가상환경을 활성화하고 `python -m app.workers.notifications`로 워커를 실행합니다. Docker 밖에서 OCR을 실행하려면 Tesseract와 `kor`, `eng` 언어 데이터가 필요합니다. `TESSERACT_CMD`로 실행 파일 경로를 지정할 수 있습니다.

### 장바구니 OCR 검증

- OCR은 흩어진 화면 텍스트를 읽는 모드(PSM 11)를 사용합니다. 반복되는 체크박스가 있는 세로 장바구니는 모든 상품 경계와 체크 상태를 읽은 뒤 **체크된 상품만** 추출합니다. 체크 해제 상품은 AI 입력에서도 제외합니다. 모두 미선택이면 `NO_SELECTED_ITEMS`를 반환하며 전체 상품 추출로 되돌아가지 않습니다.
- 가격은 각 영역 안의 숫자 근거로 결정합니다. AI는 영역 ID에 맞는 이름·카테고리·질문만 제안하며, 주문 총액·취소선 가격을 상품 가격으로 사용하지 않습니다. 가격이 불확실해도 상품을 누락하지 않고 `null`을 반환합니다.
- 쿠팡처럼 큰 체크박스와 회색 체크 모양의 미선택 표시가 있는 화면도 선택 상태를 구분합니다. 할인율·배송 배지가 붙은 판매가를 읽고, 단위 가격·쿠폰 할인액은 제외합니다. 화면 전체 너비의 구분선으로 상품 영역과 하단 주문 합계를 나눕니다.
- AI가 이웃 상품 이름을 가져오거나 원문과 크게 다른 이름을 만들면 해당 영역의 OCR 제목과 기본 카테고리로 복구하고 `REVIEW_OCR_NAMES`를 반환합니다. 이 경우 이름·카테고리 확인이 필요합니다.
- Android는 일반 크기의 스크린샷을 4MB 이내 PNG로 전송해 문자를 보존하고, 크기가 큰 경우 JPEG로 전환합니다. 압축된 이미지에서는 가격을 수동 확인해야 할 수 있습니다.
- 2026-10-31 원본 PNG와 실제 Android 업로드 경로에서 이름·판매가 연결을 검증했습니다. 선택 정책 수정 후 원본에서 체크된 두 상품(27,500/20,500원)만 추출합니다. JPEG 변환본은 첫 가격이 불명확해 `null`을 반환할 수 있습니다. 회귀 테스트에는 합성 레이아웃만 포함하며 사용자 원본은 커밋하지 않습니다.
- 쿠팡 원본은 실제 Android 업로드에서 선택된 치아미백제 21,980원 한 개만 반환되는 것을 확인했습니다. 같은 배포에서 에이블리 두 상품 추출도 다시 통과했습니다. 관련 백엔드 테스트 67개가 통과했습니다.

## 설정과 외부 서비스 확인

| 설정 | 용도 |
|---|---|
| `DATABASE_URL` | SQLAlchemy PostgreSQL 연결 문자열. Compose에서는 내부 `db` 주소로 대체 |
| `LABLUP_API_KEY` | 서버 전용 API 키 |
| `LABLUP_BASE_URL` | 기본값 `https://api.console.lablup.ai/anthropic` |
| `LABLUP_MODEL_ID` | 기본값 `DeepSeek-V4.1-Flash-B300`; Connect의 실제 호출 ID와 일치해야 함 |
| `LABLUP_VISION_ENABLED` | 기본값 `false`. 실제 이미지 요청 검증 후에만 `true` |
| `CART_LAYOUT_VISION_ENABLED` | 기본값 `false`. 전체 화면의 상품 영역을 AI로 분석하고 픽셀·OCR로 검증하는 경로 활성화 |
| `CART_LAYOUT_TIMEOUT_SECONDS` | 전체 화면 AI 호출 제한. 기본 40초, 카트 내 모델 호출은 공유 80초 예산 사용 |
| `FIREBASE_PROJECT_ID` | Android 앱과 동일한 Firebase 프로젝트 ID |
| `GOOGLE_APPLICATION_CREDENTIALS` | Firebase 서비스 계정 JSON 파일 경로 |
| `CORS_ORIGINS` | 웹 클라이언트 사용 시 허용할 출처의 JSON 배열 |

### Lablup

```powershell
docker compose exec api python -m scripts.check_lablup
```

서버 키로 텍스트 요청과 무작위 숫자 이미지 요청을 실제로 보냅니다. `vision_verified=true`를 확인한 경우에만 `.env`의 `LABLUP_VISION_ENABLED=true`를 설정하고 `docker compose up -d api worker`로 반영합니다. 확인되지 않으면 한국어·영어 OCR 결과를 같은 Lablup 모델에 전달합니다. 다른 AI 공급자로 자동 전환하지 않습니다.

AI 결과는 등록 초안입니다. 가격을 확인할 수 없으면 `null`을 반환하며, 사용자가 가격·카테고리·구매 이유를 확인한 후 등록해야 합니다. AI 실패 시 `warnings`와 직접 입력 경로를 제공합니다. 스크린샷은 JPEG/PNG/WebP 한 장, 기본 10 MiB까지 받으며 원본을 장기 보관하지 않습니다.

### Firebase와 Android 푸시

Firebase 프로젝트에 Android 앱을 연결하고 Cloud Messaging 발송 권한이 있는 서비스 계정 JSON을 `credentials/firebase.json`에 둡니다. 이 디렉터리는 Git과 Docker 이미지에서 제외됩니다.

Compose에서는 다음 값을 설정합니다.

```dotenv
FIREBASE_PROJECT_ID=your-project-id
GOOGLE_APPLICATION_CREDENTIALS=/run/credentials/firebase.json
```

워커는 매일 서울 시간 20시 이후 남은 질문·결정·사후 점검을 한 건으로 묶습니다. 명확한 일시 오류만 제한적으로 재시도하며 수락 여부가 불명확한 타임아웃은 `UNKNOWN`으로 남깁니다. FCM 접수 성공은 실기기 표시 완료와 다릅니다.

기기 토큰 등록 후 운영자 테스트 명령으로 실제 알림을 보낼 수 있습니다. 이 명령은 일일 발송 제한과 별개의 테스트 알림입니다.

```powershell
docker compose exec api python -m scripts.send_test_push --guest-id YOUR_GUEST_ID
```

앱 알림 권한, 전경·백그라운드 표시, `today` 화면 이동을 직접 확인해야 합니다. 상세 계약은 [Android 연동 안내](docs/android-integration.md)에 있습니다.

## API 사용

모든 업무 API의 접두사는 `/api/v1`입니다. 첫 실행에서만 `POST /guest-sessions`를 호출하고 `access_token`을 보안 저장소에 보관합니다. 이후 요청에 `Authorization: Bearer <access_token>`을 넣습니다. `/metadata`는 카테고리·구매 이유 코드와 한국어 라벨을 제공합니다.

등록·소비 비교·수동 지출·할인 비교 POST와 품목 `HOLD` 결정에는 `Idempotency-Key` 헤더를 넣습니다. 사용자 동작마다 UUID 하나를 생성하고 재시도에는 같은 키를 재사용합니다. 같은 키로 다른 POST 본문을 보내면 `409 IDEMPOTENCY_CONFLICT`입니다. 답변·결정·회고 PUT은 반복해도 기록이나 적립이 늘어나지 않습니다. 새 7일 연장에는 새 키가 필요합니다.

| 기능 | 요청 |
|---|---|
| 홈 | `GET /home` |
| 오늘 할 일 | `GET /today` |
| 이름으로 AI 초안 | `POST /item-drafts` with `{"name":"키보드","price":30000}` |
| 스크린샷 AI 초안 | `POST /cart-extractions`, multipart 파일 필드 `image` |
| 입력한 상품 URL 초안 | `POST /product-url-drafts` with `{"url":"https://…"}` |
| 확인한 품목 등록 | `POST /items/batch` |
| 품목 목록·상세·수정 | `GET /items`, `GET/PATCH /items/{id}` |
| 기존 방식 답변·공통 결과 | `PUT /simulations/{id}/answers/{date}`, `GET /simulations/{id}/result` |
| 구매 전 로컬 질문·종료 요약 | `POST /simulations/{id}/local-question`, `POST /simulations/{id}/result-summary` |
| 구매 전 원본 질문·이력 | `GET /pre-purchase-question-templates`, `GET /simulations/{id}/questions` |
| 구매 결정·사후 회고 | `PUT /items/{id}/decision`, `PUT /purchases/{id}/review` |
| 구매 후 로컬 질문 | `POST /purchases/{id}/local-question` |
| 구매 후 질문 목록·이력 | `GET /question-templates`, `GET /purchases/{id}/questions` |
| 월 예산 | `GET/PUT /budgets/{YYYY-MM}` |
| 활성 목표 | `POST /goals`, `GET/PATCH /goals/current` |
| 목표 설정 초기 정보·미리보기 | `GET /goals/setup`, `POST /goals/preview` |
| 목표 변경·적립금 이동 ON/OFF | `POST /goals/current/replacement` — `Idempotency-Key` 필수 |
| 달성 후 다음 목표 | `POST /goals/current/next` |
| 목표 달성 자동 진열 | `GET /goals/shelf` — 구매 확인 없이 달성 시 등록 |
| 즉석 소비 비교·결정 | `POST /impulses`, `PUT /impulses/{id}/decision` |
| 자유 소비 잔액·구매 비교 | `GET /wallet`, `GET /wallet/compare?price=23000` |
| 수동 지출 | `GET/POST /expenses`, `PUT/DELETE /expenses/{id}` |
| 월간 리포트 | `GET /reports?month=2026-10` |
| 할인 품목 토글·등록 전 확인 | `POST /discount-previews`, `POST /registration-previews` |
| 기기 알림 | `PUT/DELETE /me/push-token` |

품목 등록 예시:

```json
{
  "items": [{
    "name": "키보드",
    "price": 30000,
    "category": "DIGITAL",
    "purchase_reasons": ["NECESSITY", "LONG_WANTED"],
    "source": "MANUAL",
    "question_mode": "LOCAL"
  }]
}
```

주요 입력 예시(기기에 저장할 예상 사용일수 답변, 구매 결정, 회고 집계, 예산, 목표, 즉석 소비 순서):

```json
{"number_value":15}
```

```json
{"decision":"BUY","purchased_on":"2026-10-31","actual_price":28000}
```

```json
{"frequency":"SOMETIMES"}
```

```json
{"income":1000000,"food":250000,"living":300000,"transport":150000,"savings":180000}
```

```json
{"name":"미쿠 피규어","target_amount":300000,"source_url":"https://shop.example.com/products/figure"}
```

```json
{"name":"치킨","amount":30000}
```

새 질문 답변은 각 `answer_spec`의 선택지·숫자·짧은 문장·시점/상황 형식을 따릅니다. `YES / NO / UNSURE` 고정 답변은 이전 방식에만 사용합니다. 품목 결정은 `BUY / HOLD / GIVE_UP`, 즉석 소비 결정은 `RESISTED / PURCHASED / CANCELLED`입니다. `GIVE_UP`과 `RESISTED`는 같은 가상 적립 원장에 쌓이며 목표가 없으면 미배정 상태로 보존합니다. `RESISTED` 취소는 `CANCELLED`이고, `BUY`와 `PURCHASED`는 실제 지출 내역에 한 번만 기록합니다.

## 계산과 날짜 정책

- 모든 일자 판단은 `Asia/Seoul` 기준입니다. 시뮬레이션은 등록 다음 날 시작하고 종료일은 미포함입니다. 가격 구간은 3만 원 이하 3일, 10만 원 미만 7일, 그 이상 14일입니다.
- 오늘 답변만 작성·수정할 수 있습니다. 시작 후 품목·가격은 고정하고 새 질문은 생성한 날짜마다 문장·선택지를 고정합니다. 조기 구매는 가능하지만 비교에서는 부분 관찰로 제외합니다.
- 새 흐름의 예상 사용률은 직접 입력한 `30일 예상 사용일수 / 30`, 기존 흐름은 `YES / (YES + NO)`입니다. 모름과 미응답은 미사용으로 바꾸지 않습니다. 정상 종료·확정 답변 70% 이상이며 숫자 예측이 있을 때만 사후 수치 비교를 제공합니다.
- `HOLD`는 요청 다음 날부터 관찰 7일을 추가합니다. 결과를 늦게 열어 생긴 공백은 관찰일수에 넣지 않습니다.
- 구매 후 새 질문은 단계별 문항을 사용하며 주간 사용은 겹치지 않는 7일 구간의 0~7일로 받습니다. 이전 14일 구간 회고는 호환 경로로 유지합니다. 같은 구매의 새 주간 답변과 이전 회고를 리포트에서 이중 집계하지 않습니다.
- 자유 소비 잔액은 `월수입 - 식비 - 생활비 - 교통비 - 계획저축 - 해당 월 기록 지출`입니다. 실제 계좌 잔액이 아닙니다.
- 하루 평균 적립은 `잔여 자유 소비 예산 / 이번 달 남은 일수(오늘 포함)`입니다. 완료된 날짜만 게이지에 반영하고, 구매는 남은 적립 여력을 먼저 줄인 뒤 부족분을 게이지에서 차감합니다. 다음 달 예산이 없으면 최근 월 예산을 반복하는 추정치입니다. 포기·참기 기록은 통계에만 남습니다. 상세: [달력 기반 목표 적립](docs/calendar-funding.md).
- 목표는 활성 상태 한 개이며, 참은 금액은 가상 누적입니다. 실제 저축·계좌 잔액이나 월 예산을 변경하지 않습니다.
- 목표 변경 시 ON은 새 목표 금액까지만 이동하고, OFF 또는 초과 적립금은 이전 목표 기록에 보존합니다. 원래 적립 취소는 이동한 금액에도 반영하고, 이동 자체를 새 절약으로 중복 집계하지 않습니다. 설정 미리보기는 남은 금액을 표시하고 날짜 추정은 기존 지갑 예산 기준을 유지합니다.

## 테스트·마이그레이션·데모

장바구니의 새 경로는 `LABLUP_VISION_ENABLED=true`, `CART_LAYOUT_VISION_ENABLED=true`에서 OCR·픽셀로 완전히 확인된 결과를 먼저 사용하고, 불명확한 화면은 전체 화면과 OCR 좌표를 AI에 전달해 상품별 영역을 제안받습니다. 서버가 체크박스 픽셀 상태·상품 경계·동일 영역의 가격 OCR을 독립 검증하며, 가격은 다시 읽어 일치해야 반환합니다. 선택 불확실 품목은 `review_items`로 분리하고 Android에서 추가/제외를 확인합니다. 이름 생성이 실패해도 확인된 가격·사진은 보존합니다. 자세한 계약과 반복 평가 방법은 [장바구니 인식 명세](docs/cart-recognition.md)를 참고하세요.

다음은 AI 영역 분석이 비활성화되었거나 실패했을 때 사용하는 OCR 대체 경로와 이전 검증 이력입니다. 이 경로는 정렬된 체크박스와 좌우 배치 등으로 영역을 나누며 AI는 선택된 상품의 이름·분류를 보조합니다.

- OCR 대체 경로에서 선택 영역이나 상태가 불확실하면 `SELECTION_UNVERIFIED`와 빈 목록을 반환합니다. 새 AI 영역 경로는 불확실한 상품만 `review_items`에 분리합니다. 어느 경로도 미확인 상품을 자동으로 선택하지 않습니다.
- 판매자별로 묶인 장바구니에서는 상품 체크박스와 반복 결제 로고를 크기·행 배치로 구분합니다. 판매자 체크 행은 짧은 구간·오른쪽 동작·구분선의 조합으로 판별하고 상품 선택 상태를 대신하지 않습니다. 배송비·주문 합계는 상품 가격에서 제외하며, 명시적인 할인 판매가를 고정 주문 합계보다 우선합니다. 할인 판매가 자체가 여러 금액으로 모호하면 가격을 비워 둡니다.
- 상품 제목·가격·사진은 같은 열과 상품 구간 안에서만 연결합니다. 다른 열의 전체 선택 문구는 영향을 주지 않으며, 상품 옆 쿠폰 버튼만으로 상품을 제외하지 않습니다. 상품 경계를 넘어간 OCR 문장은 가격 증거로 쓰지 않고, 주문 합계 아래 이미지는 상품 사진 후보에서 제외합니다.
- 연한 회색의 미선택 테두리는 별도 검출로 보완합니다. 보완 검출에서는 빈 테두리만 추가하고 기존의 진한 체크박스 판정을 보존합니다. 이름 OCR이 실패한 명확한 미선택 행도 제외 개수에 포함하며 AI에는 보내지 않습니다. 할인 판매가가 확인되면 독립 가격 OCR도 해당 판매가 영역을 사용하고, 같은 숫자의 주문 합계로 검증하지 않습니다.
- 가격이 여럿이거나 두 OCR 결과가 다르면 `price: null`, `REVIEW_PRICES_MANUALLY`로 확인을 요청합니다. 원본과 대조해 수정할 수 있습니다.
- `selection_summary`는 인식된 상품 영역의 `visible / selected / excluded` 개수입니다. 스크린샷 밖 장바구니 전체 수량을 의미하지 않습니다.
- 20개를 초과하는 선택 상품은 이미지를 나누도록 안내합니다. 원본·상품 좌표·가격 확인용 이미지는 서버에 보관하지 않습니다.
- OCR 경로에서도 선택된 상품 영역 안에서 글자·가격을 제외한 사진 영역을 찾고 원본을 잘라 `image_crop`으로 반환합니다(JPEG, 최대 320×320). 다른 상품 영역은 사용하지 않고, 사진 후보가 모호하면 기본 아이콘을 유지합니다. 잘라낸 사진은 Android가 초안과 등록 품목에 연결해 기기에 보관하며 서버에는 저장하지 않습니다.
- 장바구니 이름은 원본 제목으로 품목을 검증한 뒤 별도의 `display_name`을 생성해 앱의 `name`으로 반환합니다. 광고·판매자·사이즈·수량 표현을 줄이고 상품 종류와 색상·용도·호환 기기 등 필요한 특징을 남깁니다. 2026-11-01 사용자 요청으로 운영 환경의 `LABLUP_VISION_ENABLED=true`를 적용했습니다. 현재 Lablup 모델에 선택 상품별로 잘라낸 사진과 그 상품의 OCR 제목·선택 옵션을 함께 전달합니다. 확인 화면에서 이름을 직접 수정할 수 있습니다.
- OCR 대체 경로에서 사진을 안전하게 찾은 상품은 해당 사진을 이름 판단에도 사용합니다. 사진이 없는 상품은 제목·옵션만 사용하며, 이미지 입력을 지원하지 않는 응답이 오면 텍스트로 대체하고 `VISION_UNAVAILABLE_USING_OCR`를 반환합니다. 이 대체 경로는 사진 조각만 전달합니다. 새 AI 영역 경로는 전체 화면을 전달하므로 미선택 품목도 이미지에 포함되며, 반환할 상품은 서버 검증으로 제한합니다.
- 활성화 후 운영 API에서 승인된 원본 3건 모두 `analysis_method: vision`을 확인했습니다. 반환 이름은 `부츠컷 청바지`, `남색 후드집업`, `미백 치약`, `립앤아이 리무버 패드`였고, 선택된 상품 수·사진·검증 가격도 유지됐습니다. 리무버 가격은 기존과 동일하게 확인용 `null`입니다. 검증 이미지와 전체 응답은 Git에 저장하지 않습니다.
- 명명 지침에는 사용자가 제공한 상품명 예시를 포함합니다. 2026-11-01 Android 실제 업로드에서 에이블리의 청바지/후드집업, 쿠팡의 미백 치약, 리무버 패드 이름과 사진 디코딩을 검증했습니다. 에이블리 27,500/20,500원과 치약 21,980원은 유지됐고, 리무버는 가격 OCR 불확실성으로 `null`을 유지합니다. 이전 등록 이름은 자동으로 변경하지 않습니다.
- 2026-11-01 판매자별 그룹이 있는 네이버 원본을 운영 API와 Android 실제 업로드로 검증했습니다. 화면에 보이는 선택 상품 2개(말차 가루 30,800원, 전기히터 158,000원)의 간결한 이름·사진·가격이 반환됐습니다. 배송비 3,000원, 판매자 주문금액 33,800원, 전체 주문금액 228,700원은 상품 가격에서 제외했습니다. 에이블리·쿠팡 원본의 기존 가격도 로컬 OCR 회귀 검증을 통과했습니다.
- 합성 화면으로 체크 모양·위치·테마·해상도·격자 조합과 모호한 선택/가격의 거절을 회귀 검증합니다. 모든 쇼핑몰 화면의 인식을 보장하지 않으며, 새로운 레이아웃도 불확실할 때 추측하지 않는 정책을 적용합니다.
- `tests/test_cart_integrity.py`는 1·2·3열 × 밝은/어두운 화면 × 좌/우 체크박스 × 가격 재인식 일치/불일치를 조합합니다. AI 응답 순서를 뒤집어도 선택 상품의 이름·가격·원본 사진 픽셀이 함께 유지되고, AI 입력에서도 해당 상품의 사진만 연결되는지 검증합니다. 관련 추출/AI/API 테스트 144건과 에이블리·쿠팡·네이버 원본의 로컬 OCR 가격·선택 수·사진 경계 검증을 통과했습니다(2026-11-01).
- 이후 네이버 유모차 원본에서 연한 미선택 테두리 누락이 확인됐습니다. 테두리 대비·해상도, 미선택 행의 제목 누락, 동일 금액의 주문 합계에 대한 회귀 사례를 추가해 관련 테스트 155건을 통과했습니다. 승인된 유모차 원본의 Android 운영 업로드에서 358,000원 상품 한 개와 사진을 반환하고 미선택 볼펜을 제외했습니다. 기존 세 원본의 로컬 가격·선택·사진 검증도 유지됩니다. 합성 테스트 통과가 미검증 상품/화면의 인식 성공을 보장하지는 않습니다.

2026-10-31 운영 배포 후 Android 실제 인코딩→업로드→서버 분석 테스트 5건을 통과했습니다. 승인받은 에이블리 원본은 선택된 2개(27,500/20,500원), 쿠팡 원본은 선택된 1개(21,980원)로 반환됐습니다. 추가 합성 화면의 오른쪽 원형 체크·어두운 배경은 각각 39,900원, 2열 격자는 선택된 19,900/49,000원만 반환됐습니다. 합성 화면 검증을 다른 실제 쇼핑몰 전체 검증으로 간주하지 않습니다.

테스트는 전용 PostgreSQL DB에서 실행합니다. 다음은 기본 Compose 계정을 사용하는 PowerShell 예시입니다. DB 생성은 처음 한 번 실행합니다.

```powershell
docker compose exec db createdb -U bob bob_test
$env:TEST_DATABASE_URL = 'postgresql+psycopg://bob:bob@localhost:5432/bob_test'
python -m ruff check .
python -m pytest
```

CI는 PostgreSQL 테스트, 정적 검사, 별도 DB에서 마이그레이션 upgrade/downgrade/upgrade와 모델 차이를 확인합니다. AI와 FCM 테스트는 대역을 사용하며 외부 키를 요구하지 않습니다. 실제 모델 호출과 Android 수신은 위의 별도 명령으로 확인합니다.

```powershell
python -m alembic upgrade head
python -m scripts.seed_demo
```

데모 초기화는 별도 게스트와 과거 날짜의 시연용 기록을 만들고 CLI에 접근 토큰을 출력합니다. 이 토큰을 공유 로그에 남기지 않습니다. 일반 API로 날짜를 조작하는 기능은 없습니다.

이미 앱에서 발급받은 게스트로 시연하려면 다음 명령으로 같은 게스트에 샘플을 추가합니다. 기존 기록과 토큰은 유지하며, 명령을 다시 실행하면 샘플도 추가됩니다. 데모 초기화는 `development`, `demo`, `test` 환경에서만 허용됩니다.

```powershell
docker compose exec api python -m scripts.seed_demo --guest-id YOUR_GUEST_ID
```

할인 품목·무료배송 토글 비교, 실제 데이터 기반 리포트, 입력한 상품 URL의 OG/JSON-LD 추출, 수동 지출, 달성한 목표 진열장을 포함합니다. URL 입력 → 상품 정보 확인·수정 → 구매 이유 선택 → 최종 등록 순서이며 `source:URL`과 `source_url`을 보관합니다. `/share-drafts`는 이전 클라이언트 호환용으로만 유지합니다. 목표 설정의 `source_url`은 ‘사러 가기’ 버튼의 외부 링크입니다. 은행 연동·계정 복구·Android 화면은 별도입니다. 외부 키와 실기기가 없는 환경에서는 실제 Lablup 응답·FCM 수신을 완료로 간주하지 않습니다.
