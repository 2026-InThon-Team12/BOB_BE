# Vercel 무료 배포

- API: https://bob-be.vercel.app/api/v1
- Swagger: https://bob-be.vercel.app/docs
- 상태: https://bob-be.vercel.app/health/ready
- 프로젝트: https://vercel.com/mini-yujin/bob-be

Vercel Hobby + Neon Free PostgreSQL, API와 DB 리전은 Singapore(`sin1`)다.
비상업용 해커톤 MVP의 무료 사용량 범위로 운영한다. Lablup 이용량은 Vercel 무료 한도와 별개다.
무료 한도 초과 시 계속 서비스된다고 가정하지 않는다.

## 배포 방식

Python 3.12, `app.main:app`, FastAPI preset을 사용한다. `vercel.json`과
`pyproject.toml`에 진입점·리전·최대 실행 시간·OCR 빌드 작업을 지정했다.
Docker API/워커 실행도 기존대로 지원한다.

1. Vercel CLI에서 로그인하고 `vercel link --project bob-be --scope mini-yujin`으로 연결한다.
2. Neon 연결에서 **Production 환경에만** DB를 연결한다. Preview는 별도 DB를 사용해야 한다.
3. 신규 DB 또는 스키마 변경 시 `DATABASE_URL`에 Neon의 직접 연결 URL을 설정하고
   `python -m alembic upgrade head`를 실행한다. 비밀번호가 포함된 URL을 로그에 출력하지 않는다.
4. `vercel deploy --prod --scope mini-yujin`으로 배포한다.
5. `/health/ready`와 인증된 실제 API 호출을 확인한다. 단순 `/health`만으로 DB 준비를 판단하지 않는다.

현재 배포는 CLI로 수행했다. GitHub 푸시만으로 자동 배포된다고 가정하지 않는다.
DB 마이그레이션은 빌드·요청마다 실행하지 않고 배포 전에 별도로 실행한다.

## Production 환경변수

| 이름 | 값 / 설명 |
|---|---|
| `DATABASE_URL` | Neon이 주입한 pooled URL. 표준 `postgresql://`을 psycopg 3으로 자동 변환 |
| `DATABASE_POOL_MODE` | `null`: 함수 인스턴스에서 유휴 DB 연결을 유지하지 않음 |
| `ENVIRONMENT` | `production` |
| `LABLUP_API_KEY` | 서버 전용 Secret. 현재 등록된 `Lablup`도 호환하며 표준 이름이 우선 |
| `LABLUP_BASE_URL` | `https://api.console.lablup.ai/anthropic` |
| `LABLUP_MODEL_ID` | `DeepSeek-V4.1-Flash-B300` |
| `LABLUP_VISION_ENABLED` | `false`: 이미지 지원 미검증이므로 OCR 사용 |
| `OCR_BACKEND` | `python` |
| `TESSDATA_DIR` | `vendor/tessdata` |
| `MAX_IMAGE_BYTES` | `4000000` |
| `CRON_SECRET` | 무작위 32자 이상 Secret |
| `FIREBASE_PROJECT_ID` | Android와 동일한 Firebase 프로젝트 ID |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | 서비스 계정 JSON 전체를 Secret으로 저장. 파일 업로드 불필요 |
| `NOTIFICATIONS_HOUR` | `20` |

환경변수를 변경하면 재배포해야 적용된다. API 키와 서비스 계정 JSON은 Git이나 Android에 넣지 않는다.
`.env*`, `credentials/`, `.venv/`, `tmp/`는 업로드에서 제외한다.

## 스크린샷 OCR

Vercel에서는 Dockerfile의 OS 패키지 설치를 실행하지 않는다. 대신 native Tesseract를 포함한
`tesserocr` Linux wheel을 설치하고, 빌드에서 공식 `tessdata_fast`의 `kor`·`eng` 파일을 내려받는다.
다운로드는 커밋과 SHA-256으로 고정하며, 빌드 중 실제 이미지 인식에 실패하면 배포가 실패한다.
런타임에서는 별도 프로세스에 PNG를 stdin으로 전달하고 TSV만 받는다. 최대 30초 후 종료하며
이미지 원본은 디스크에 저장하지 않는다. OCR 결과는 기존 Lablup 텍스트 분석으로 이어진다.

Android는 업로드 전에 이미지를 **4,000,000바이트 이하**로 압축해야 한다.
Vercel의 요청·응답 전체 한도는 4.5 MB이므로 multipart 여유를 둔다.
한도를 넘으면 FastAPI에 도달하기 전에 Vercel이 413을 반환할 수 있으며, 이 응답은 앱의 JSON 오류 형식과 다르다.
OCR만으로는 상품 이미지 영역을 확정할 수 없어 기존대로 `image_crop=null`일 수 있다.

## 푸시

`0 11 * * *` UTC cron이 `/internal/notifications`를 호출한다.
Hobby는 지정 시각부터 한 시간 이내에 실행하므로 한국 시간 **20:00~20:59**에 실행된다.
60초마다 실행하는 상시 워커를 Vercel에 띄우는 구조가 아니다.

- Vercel이 `Authorization: Bearer <CRON_SECRET>`를 자동으로 보낸다. 미인증 호출은 거부한다.
- Firebase가 미설정이면 503으로 종료하며 발송 기록을 소모하지 않는다.
- 기존 날짜별 유일 제약과 claim 처리를 재사용하여 성공한 알림은 재발송하지 않는다.
- 함수는 210초가 지나면 새 작업을 시작하지 않는다. 처리하지 못한 대상 수는 `deferred`로 반환한다.
- 일시적 실패의 `pending`이나 `deferred`는 **당일 자동 재호출되지 않는다**. 필요한 경우 같은 날 인증된
  운영 호출로 재실행할 수 있다. 다음 날에는 기존 만료 처리 규칙을 따른다.
- 많은 기기, 정확한 시각, 자동 재시도가 필요하면 상시 워커 또는 다른 스케줄러로 전환한다.
- 로컬에만 저장된 응답의 완료 여부는 Android가 data-only 푸시를 받은 뒤 확인한다.

Firebase 설정과 실기기 테스트가 끝나기 전에는 실제 푸시 검증 완료로 표시하지 않는다.

공식 문서: [FastAPI](https://vercel.com/docs/frameworks/backend/fastapi),
[함수 한도](https://vercel.com/docs/functions/limitations),
[Hobby Cron](https://vercel.com/docs/cron-jobs/usage-and-pricing),
[Neon Free](https://neon.com/pricing).
