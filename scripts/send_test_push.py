"""Operator-only real device check: python -m scripts.send_test_push --guest-id UUID.

This explicitly sends a test notification outside the daily schedule. It never
creates or resets a production DAILY_DIGEST record.
"""

import argparse
from uuid import uuid4

from sqlalchemy import select

from app.core.config import get_settings
from app.db import create_session_factory
from app.integrations.firebase import FirebaseSender, PushFailure
from app.models import Device


def main() -> None:
    parser = argparse.ArgumentParser(description="등록된 Android 기기로 실제 테스트 푸시를 보냅니다.")
    parser.add_argument("--guest-id", required=True)
    args = parser.parse_args()
    settings = get_settings()
    factory = create_session_factory(settings.database_url)
    with factory() as session:
        device = session.scalar(
            select(Device).where(Device.guest_id == args.guest_id, Device.enabled.is_(True))
        )
        if device is None:
            parser.exit(1, "활성 푸시 토큰이 등록된 게스트가 없습니다.\n")
        token = device.fcm_token
    try:
        FirebaseSender(settings).send(token, f"test-{uuid4()}", 300, test=True)
    except PushFailure as exc:
        parser.exit(1, f"FCM 테스트 실패: {exc.code}\n")
    print("FCM이 테스트 발송을 접수했습니다. 실기기 표시와 클릭 이동을 별도로 확인하세요.")


if __name__ == "__main__":
    main()
