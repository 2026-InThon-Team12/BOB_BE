import hashlib

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db import get_session
from app.models import Guest

bearer = HTTPBearer(auto_error=False)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def get_guest(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: Session = Depends(get_session),
) -> Guest:
    if not credentials or len(credentials.credentials) > 256:
        raise AppError(401, "UNAUTHORIZED", "게스트 토큰이 필요합니다.")
    guest = session.scalar(select(Guest).where(Guest.token_hash == hash_token(credentials.credentials)))
    if guest is None:
        raise AppError(401, "UNAUTHORIZED", "유효하지 않은 게스트 토큰입니다.")
    return guest
