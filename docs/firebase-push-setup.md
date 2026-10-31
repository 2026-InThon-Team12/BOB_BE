# 배포 서버와 Android 푸시 연결

서버 주소는 `https://bob-be.vercel.app`이다. 서버·Android는 같은 Firebase 프로젝트를 사용한다.
2026-10-31 기준 Firebase 프로젝트 `buy-or-bye-af0ba`의 서버 연결을 완료했다.
Vercel Production에 프로젝트 ID와 서비스 계정 Secret을 등록하고 재배포했다.
OAuth 인증, FCM `validate_only` 요청, 배포 서버의 인증된 정기 작업 호출(HTTP 200)을 확인했다.
등록된 수신 기기는 0개로 실제 알림은 발송하지 않았다. Android 앱 구현과 실기기 수신 검증은 남아 있다.
아래 Firebase 설정 절차는 재설정·인수인계용이다. 현재 연결을 위해 새 키를 다시 발급할 필요는 없다.

## Firebase와 Vercel

1. [Firebase Console](https://console.firebase.google.com/)에서 Android 담당자가 사용하는 프로젝트를 선택한다.
2. 프로젝트 설정 → 일반에서 프로젝트 ID를 확인한다.
3. 프로젝트 설정 → 서비스 계정 → Firebase Admin SDK에서 서버용 비공개 키 JSON을 발급한다.
4. [Vercel 환경변수](https://vercel.com/mini-yujin/bob-be/settings/environment-variables)의 Production에 등록한다.
   - `FIREBASE_PROJECT_ID`: 위 프로젝트 ID.
   - `FIREBASE_SERVICE_ACCOUNT_JSON`: 서비스 계정 JSON 전체. **Secret**으로 저장한다.
5. 서버를 재배포한다. JSON은 Android 코드, Git, 채팅에 넣지 않는다.
6. Firebase 프로젝트의 Cloud Messaging API(HTTP v1)가 활성화돼 있는지 확인한다.

Android용 `google-services.json`과 서버용 서비스 계정 JSON은 서로 다른 파일이다.
서버에 필요한 것은 `private_key`, `client_email`, `project_id` 등이 포함된 서비스 계정 JSON이다.

## Android 담당자 연결

1. 실제 앱의 `applicationId`로 Firebase에 Android 앱을 등록하고 `google-services.json`과 Messaging SDK를 연결한다.
2. Android 13 이상에서는 알림 권한을 요청하고 `bob_daily` 채널을 만든다.
3. 저장된 게스트 토큰으로 `PUT /api/v1/me/push-token`을 호출한다.

```json
{"token":"기기의 FCM 등록 토큰","enabled":true}
```

FCM 토큰 갱신 시와 앱 실행 시 서버에 다시 등록한다. 권한을 끄면 DELETE 또는 `enabled:false`를 전달한다.
FCM 토큰과 게스트 인증 토큰은 서로 바꿔 사용할 수 없다.

LOCAL 질문의 정기 알림은 data-only다. 수신 서비스가 `client_managed=true`를 확인한 뒤
`GET /api/v1/today`와 로컬 답변 기록을 합쳐 남은 일이 있을 때만 알림을 표시한다.
필요한 비동기 작업은 WorkManager 등으로 이어간다. 앱 실행 때도 오늘 목록을 조회한다.
`digest_id`로 중복 표시를 막고 클릭 시 `route=today`에 맞춰 오늘 화면을 연다.

## 실기기 검증

Android 담당자는 테스트 기기의 **guest_id**를 전달한다. 서버가 등록된 FCM 토큰을 조회하므로
FCM 토큰이나 게스트 인증 토큰을 채팅에 공유할 필요가 없다.

운영자는 서버의 `CRON_SECRET`으로 아래 경로를 호출한다. 이 비밀값을 Android에 넣지 않는다.

```http
POST /internal/notifications/test
Authorization: Bearer <CRON_SECRET>
Content-Type: application/json

{"guest_id":"테스트 게스트 UUID","client_managed":false}
```

- `false`: 일반 notification+data 테스트. 전경/백그라운드 표시와 클릭 이동을 확인한다.
- `true`: 정기 LOCAL 흐름과 같은 data-only 테스트. 미응답이 있으면 표시, 모두 완료했으면 표시하지 않아야 한다.
- 성공 응답 `202 {"status":"FCM_ACCEPTED","delivery_confirmed":false}`는 FCM 접수 확인이다.
  실제 기기 표시·클릭 여부를 별도로 확인해야 완료다.
- 테스트는 정기 알림 발송 기록을 생성하지 않으며 하루 한 번 발송 제한을 소모하지 않는다.
- 권한 OFF, 토큰 변경, 잘못된 프로젝트, 앱 전경·백그라운드, 클릭 이동을 함께 점검한다.

무료 Vercel cron은 한국 시간 20:00~20:59 사이 하루 한 번 실행된다.
기기 절전·네트워크 상태에 따라 실제 수신은 지연될 수 있다.

공식 문서: [Admin SDK 설정](https://firebase.google.com/docs/admin/setup),
[Android FCM 시작](https://firebase.google.com/docs/cloud-messaging/android/get-started),
[Android 메시지 수신](https://firebase.google.com/docs/cloud-messaging/android/receive-messages).
