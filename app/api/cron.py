"""Authenticated, bounded entrypoint for Vercel's daily cron."""

import secrets
from dataclasses import asdict
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.integrations.firebase import PushFailure
from app.models import Device
from app.workers.notifications import run_once

router = APIRouter()


def require_scheduler_auth(request: Request, settings: Settings) -> None:
    if not settings.cron_secret:
        raise AppError(503, "CRON_NOT_CONFIGURED", "알림 스케줄러 설정이 필요합니다.")
    expected = f"Bearer {settings.cron_secret}".encode()
    supplied = request.headers.get("Authorization", "").encode()
    if not secrets.compare_digest(expected, supplied):
        raise AppError(401, "UNAUTHORIZED", "인증이 필요합니다.")


def configured_sender(request: Request):
    sender = request.app.state.notification_sender
    try:
        # Check before touching dispatch records, so missing credentials cannot consume jobs.
        sender.check_configuration()
    except PushFailure:
        raise AppError(503, "PUSH_NOT_CONFIGURED", "푸시 발송 설정을 확인해 주세요.") from None
    return sender


@router.get("/internal/notifications", include_in_schema=False)
def notifications(request: Request, settings: Annotated[Settings, Depends(get_settings)]):
    require_scheduler_auth(request, settings)
    sender = configured_sender(request)
    result = run_once(
        request.app.state.session_factory,
        sender,
        settings=settings,
        max_runtime_seconds=210,
    )
    return JSONResponse(asdict(result), headers={"Cache-Control": "no-store"})


class PushTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guest_id: UUID
    client_managed: bool = False


@router.post("/internal/notifications/test", include_in_schema=False, status_code=202)
def test_notification(
    body: PushTestRequest, request: Request, settings: Annotated[Settings, Depends(get_settings)]
):
    require_scheduler_auth(request, settings)
    with request.app.state.session_factory() as session:
        device = session.scalar(
            select(Device).where(Device.guest_id == str(body.guest_id), Device.enabled.is_(True))
        )
        if device is None:
            raise AppError(404, "PUSH_DEVICE_NOT_FOUND", "알림을 허용한 테스트 기기를 먼저 등록해 주세요.")
        token = device.fcm_token
    sender = configured_sender(request)
    try:
        sender.send(token, f"test-{uuid4()}", 300, test=True, client_managed=body.client_managed)
    except PushFailure as exc:
        raise AppError(502, exc.code, "테스트 발송이 확인되지 않았습니다. 기기와 Firebase 설정을 확인해 주세요.") from None
    # Provider acceptance is not device delivery. Never consume the daily digest.
    return JSONResponse(
        {"status": "FCM_ACCEPTED", "delivery_confirmed": False},
        status_code=202, headers={"Cache-Control": "no-store"},
    )
