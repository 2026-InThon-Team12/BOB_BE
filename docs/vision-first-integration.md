# 비전 우선 변경과 기존 Vercel 기능 통합

브랜치: `codex/vision-first-vercel-integration`

- 기존 기능 기준: `b4ebe35` (최근 선택 컨트롤·가격·상품 사진 검증 포함).
- 통합 원본: `codex/vision-first-cart`의 `9626bf1`.
- 새 브랜치는 `24b987f`에서 분기했으므로 통째로 교체하면 기존 Vercel 배포·DB URL 보정·서버리스 OCR·Firebase Secret·예약 알림 처리가 사라진다. 이 기능들은 유지하고 새 변경을 병합했다.
- 원래 `BOB_BE` 작업 폴더의 미커밋 spatial 실험 코드는 복사하거나 수정하지 않았다. 별도 worktree에서 작업했다.

## 충돌 해결

1. 기존 선택 상품 필터·가격 교차 검증·상품 사진 자르기를 유지한다. OCR은 이 검증에 먼저 사용하며, 화면에서 선택 여부가 확인되지 않으면 기존대로 자동 추출하지 않는다.
2. 검증 후 첫 AI 요청은 항상 이미지를 포함한다. `LABLUP_VISION_ENABLED=false`여도 장바구니를 텍스트 전용으로 처리하지 않는다. 상품 사진을 찾지 못하면 검증된 해당 상품 행 이미지를 AI에 전달하지만, 이를 `image_crop`으로 반환하지 않는다.
3. 모델이 이미지 입력 미지원을 명확히 응답한 경우에만 이미지 없는 OCR 텍스트로 재시도한다. 일반 400/415/422, 인증·서버 오류·시간 초과는 이 대체 조건이 아니다.
4. 원본 브랜치의 전체 화면 자유 추출로 기존 선택·가격 검증을 우회하지 않는다. 원본의 비전 우선·제한적 대체 규칙을 현재 상품별 분석 흐름에 적용했다.
5. DB 모델·마이그레이션·API 경로·Buy/Bye 결정 조건은 변경하지 않는다. 조기 Bye 허용은 별도 후속 작업이다.

## 배포

기존 `mini-yujin/bob-be` 프로젝트의 Production 환경변수를 사용한다. Production용 후보 배포를 `--skip-domain`으로 먼저 생성하고 상태·DB·AI 요청을 확인한 뒤 기존 `bob-be.vercel.app` 도메인으로 승격한다. 실제 사용자 물품을 수정하지 않고 별도 점검 계정과 합성 장바구니를 사용한다.

배포 전 복구 대상: `https://bob-i6p6iredv-mini-yujin.vercel.app` (`dpl_HyN3GXFzqwvMNW4V9h1V3xGqkjc4`).

## 로컬 검증

- Ruff 통과, Python 테스트 339개 통과·12개 건너뜀(로컬 SQLite 환경에서 PostgreSQL 등 조건부 테스트 제외).
- LOCAL 질문 저장 어댑터 Node 테스트 2개 통과.
- Vercel dry run에서 FastAPI preset 및 비밀 파일·테스트 출력 제외를 확인했다.
- 프로덕션 DB 마이그레이션은 필요하지 않으며 실행하지 않는다.
