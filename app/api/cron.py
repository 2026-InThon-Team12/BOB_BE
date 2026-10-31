"""Authenticated, bounded entrypoint for Vercel's daily cron."""

import secrets
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.integrations.firebase import PushFailure
from app.workers.notifications import run_once

router = APIRouter()


@router.get("/internal/notifications", include_in_schema=False)
def notifications(request: Request, settings: Annotated[Settings, Depends(get_settings)]):
    if not settings.cron_secret:
        raise AppError(503, "CRON_NOT_CONFIGURED", "알림 스케줄러 설정이 필요합니다.")
    expected = f"Bearer {settings.cron_secret}".encode()
    supplied = request.headers.get("Authorization", "").encode()
    if not secrets.compare_digest(expected, supplied):
        raise AppError(401, "UNAUTHORIZED", "인증이 필요합니다.")
    sender = request.app.state.notification_sender
    try:
        # Check before touching dispatch records, so missing credentials cannot consume jobs.
        sender.check_configuration()
    except PushFailure:
        raise AppError(503, "PUSH_NOT_CONFIGURED", "푸시 발송 설정을 확인해 주세요.") from None
    result = run_once(
        request.app.state.session_factory,
        sender,
        settings=settings,
        max_runtime_seconds=210,
    )
    return JSONResponse(asdict(result), headers={"Cache-Control": "no-store"})
