# BOB Backend

구매 전 예상 사용과 구매 후 실제 사용을 비교하는 FastAPI 백엔드입니다. PostgreSQL에 게스트별 기록을 저장하고, 별도 워커가 Android FCM 묶음 알림을 발송합니다.

최신 `BoB_handoff/FE_SPEC.md`와 화면별 HTML, 이후 사용자가 확정한 로컬 저장·할인 마감일 정책을 반영했습니다. 신규 Android 연동은 [최신 handoff API 계약](docs/handoff-integration.md)을 먼저 확인하세요. 이전 [디자인 반영 내역](docs/design-changes.md)은 기존 데이터의 호환 정책을 설명합니다.

수정된 [FE 구현 명세 사본](docs/frontend-spec.md)도 저장소에 포함했습니다. 프로토타입 HTML은 별도 전달 자료입니다.

제공된 사진의 구매 전 14개·구매 후 15개 고정 질문을 AI가 품목에 맞게 바꿉니다. **신규 등록 기본값은 `LOCAL`**이며 질문·답변 원문은 기기에만 보관합니다. 종료 시 로컬 기록으로 AI 요약을 요청하고 서버에는 숫자 집계만 남깁니다. [로컬 저장 참고 어댑터](clients/local-question-store.mjs)를 제공하며, 기존 `ADAPTIVE`·`LEGACY` 저장 흐름은 이전 데이터 호환용으로 유지합니다. 할인 계산과 금액 적립은 AI를 사용하지 않습니다.

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

- OCR은 흩어진 화면 텍스트를 읽는 모드(PSM 11)를 사용합니다. 반복되는 체크박스가 있는 세로 장바구니는 체크 여부와 무관하게 상품 영역을 먼저 나눕니다.
- 가격은 각 영역 안의 숫자 근거로 결정합니다. AI는 영역 ID에 맞는 이름·카테고리·질문만 제안하며, 주문 총액·취소선 가격을 상품 가격으로 사용하지 않습니다. 가격이 불확실해도 상품을 누락하지 않고 `null`을 반환합니다.
- AI가 이웃 상품 이름을 가져오거나 원문과 크게 다른 이름을 만들면 해당 영역의 OCR 제목과 기본 카테고리로 복구하고 `REVIEW_OCR_NAMES`를 반환합니다. 이 경우 이름·카테고리 확인이 필요합니다.
- Android는 일반 크기의 스크린샷을 4MB 이내 PNG로 전송해 문자를 보존하고, 크기가 큰 경우 JPEG로 전환합니다. 압축된 이미지에서는 가격을 수동 확인해야 할 수 있습니다.
- 2026-10-31 원본 PNG와 실제 Android 업로드 경로에서 세 상품의 이름·판매가 연결을 검증했습니다. JPEG 변환본은 첫 가격이 불명확해 `null`을 반환했습니다. 회귀 테스트에는 합성 레이아웃만 포함하며 사용자 원본은 커밋하지 않습니다.

## 설정과 외부 서비스 확인

| 설정 | 용도 |
|---|---|
| `DATABASE_URL` | SQLAlchemy PostgreSQL 연결 문자열. Compose에서는 내부 `db` 주소로 대체 |
| `LABLUP_API_KEY` | 서버 전용 API 키 |
| `LABLUP_BASE_URL` | 기본값 `https://api.console.lablup.ai/anthropic` |
| `LABLUP_MODEL_ID` | 기본값 `DeepSeek-V4.1-Flash-B300`; Connect의 실제 호출 ID와 일치해야 함 |
| `LABLUP_VISION_ENABLED` | 기본값 `false`. 실제 이미지 요청 검증 후에만 `true` |
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
- 하루 평균 저축액은 `계획저축 / 해당 월 일수`입니다. 목표 지연은 디자인의 `max(1, 반올림(소비금액 / 하루 평균 저축액))`을 따릅니다. 예산 누락·계획저축 0이면 `null`입니다. 목표 예상 달성일까지 남은 날은 `올림(남은 목표금액 / 하루 평균 저축액)`입니다.
- 목표는 활성 상태 한 개이며, 참은 금액은 가상 누적입니다. 실제 저축·계좌 잔액이나 월 예산을 변경하지 않습니다.
- 목표 변경 시 ON은 새 목표 금액까지만 이동하고, OFF 또는 초과 적립금은 이전 목표 기록에 보존합니다. 원래 적립 취소는 이동한 금액에도 반영하고, 이동 자체를 새 절약으로 중복 집계하지 않습니다. 설정 미리보기는 남은 금액을 표시하고 날짜 추정은 기존 지갑 예산 기준을 유지합니다.

## 테스트·마이그레이션·데모

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
