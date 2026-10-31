"""Durable daily digests. Run with ``python -m app.workers.notifications``."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, time as daytime, timedelta, timezone
from typing import Callable, Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import SEOUL, utc_now
from app.core.config import Settings, get_settings
from app.db import create_session_factory
from app.integrations.firebase import FirebaseSender, PushFailure
from app.models import Device, NotificationDispatch

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
STALE_CLAIM_SECONDS = 300


class Sender(Protocol):
    def send(
        self,
        fcm_token: str,
        digest_id: str,
        ttl_seconds: int,
        *,
        test: bool = False,
        client_managed: bool = False,
    ) -> str: ...


@dataclass
class TickResult:
    sent: int = 0
    failed: int = 0
    pending: int = 0
    unknown: int = 0
    skipped: int = 0
    deferred: int = 0


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def midnight_after(local_date: date) -> datetime:
    return datetime.combine(local_date + timedelta(days=1), daytime.min, tzinfo=SEOUL)


def _cards(session: Session, guest_id: str, local_date: date) -> dict:
    from app.services.simulations import get_today_cards

    return get_today_cards(session, guest_id, local_date)


def _has_work(cards: dict) -> bool:
    return any(
        cards.get(key)
        for key in ("questions", "decisions", "reviews", "purchase_questions", "pre_purchase_questions")
    )


def _claim(
    factory: sessionmaker,
    guest_id: str,
    local_date: date,
    now: datetime,
    cards_provider: Callable[[Session, str, date], dict],
) -> tuple[str, str, int, bool] | None:
    """Create once, then compare-and-set claim; no DB transaction spans network I/O."""
    with factory() as session:
        device = session.scalar(select(Device).where(Device.guest_id == guest_id, Device.enabled.is_(True)))
        cards = cards_provider(session, guest_id, local_date) if device else {}
        if device is None or not _has_work(cards):
            return None
        client_managed = any(
            task.get("answer_state") == "CLIENT_MANAGED"
            for key in ("pre_purchase_questions", "purchase_questions")
            for task in cards.get(key, [])
        )
        dispatch = session.scalar(
            select(NotificationDispatch).where(
                NotificationDispatch.guest_id == guest_id,
                NotificationDispatch.local_date == local_date,
                NotificationDispatch.kind == "DAILY_DIGEST",
            )
        )
        if dispatch is None:
            dispatch = NotificationDispatch(
                guest_id=guest_id,
                local_date=local_date,
                kind="DAILY_DIGEST",
                status="PENDING",
                attempts=0,
                created_at=now,
            )
            session.add(dispatch)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                # Another worker won the unique daily record; it owns this tick.
                return None
        dispatch_id = dispatch.id
        token = device.fcm_token
        claim = session.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.id == dispatch_id,
                NotificationDispatch.status == "PENDING",
                NotificationDispatch.attempts < MAX_ATTEMPTS,
                or_(
                    NotificationDispatch.next_attempt_at.is_(None),
                    NotificationDispatch.next_attempt_at <= now,
                ),
            )
            .values(status="SENDING", claimed_at=now, attempts=NotificationDispatch.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            session.rollback()
            return None
        session.flush()
        attempts = session.scalar(
            select(NotificationDispatch.attempts).where(NotificationDispatch.id == dispatch_id)
        )
        session.commit()
        return dispatch_id, token, attempts, client_managed


def _finish(
    factory: sessionmaker,
    dispatch_id: str,
    token: str,
    attempts: int,
    now: datetime,
    local_date: date,
    message_id: str | None,
    failure: PushFailure | None,
) -> str:
    with factory() as session:
        dispatch = session.get(NotificationDispatch, dispatch_id)
        if dispatch is None or dispatch.status != "SENDING":
            # Another process may have conservatively marked an abandoned claim UNKNOWN.
            return "unknown"
        dispatch.next_attempt_at = None
        if failure is None:
            dispatch.status = "SENT"
            dispatch.sent_at = now
            dispatch.message_id = message_id
            dispatch.error_code = None
        else:
            dispatch.error_code = failure.code
            if failure.token_invalid:
                # Do not deactivate a replacement token registered during this send.
                session.execute(
                    update(Device)
                    .where(
                        Device.guest_id == dispatch.guest_id,
                        Device.fcm_token == token,
                    )
                    .values(enabled=False, updated_at=now)
                )
            if failure.ambiguous:
                dispatch.status = "UNKNOWN"
            elif failure.retryable and attempts < MAX_ATTEMPTS:
                wait = max(60 * (2 ** (attempts - 1)), failure.retry_after_seconds or 0)
                retry_at = now + timedelta(seconds=wait)
                if retry_at < midnight_after(local_date):
                    dispatch.status = "PENDING"
                    dispatch.next_attempt_at = retry_at
                else:
                    dispatch.status = "FAILED"
            else:
                dispatch.status = "FAILED"
        status = dispatch.status.lower()
        session.commit()
        return status


def run_once(
    factory: sessionmaker,
    sender: Sender,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    cards_provider: Callable[[Session, str, date], dict] | None = None,
    max_runtime_seconds: float | None = None,
) -> TickResult:
    started_at = time.monotonic()
    settings = settings or get_settings()
    fixed_now = now is not None
    now = _aware(now or utc_now()).astimezone(timezone.utc)
    local_now = now.astimezone(SEOUL)
    local_date = local_now.date()
    cards_provider = cards_provider or _cards
    result = TickResult()
    with factory() as session:
        stale = session.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.status == "SENDING",
                NotificationDispatch.claimed_at <= now - timedelta(seconds=STALE_CLAIM_SECONDS),
            )
            .values(status="UNKNOWN", error_code="WORKER_INTERRUPTED", next_attempt_at=None)
        )
        result.unknown += stale.rowcount
        expired = session.execute(
            update(NotificationDispatch)
            .where(
                NotificationDispatch.status == "PENDING",
                NotificationDispatch.local_date < local_date,
            )
            .values(status="SKIPPED", error_code="EXPIRED", next_attempt_at=None)
        )
        result.skipped += expired.rowcount
        session.commit()
        if local_now.hour < settings.notifications_hour:
            return result
        guest_ids = list(session.scalars(select(Device.guest_id).where(Device.enabled.is_(True))))
    for index, guest_id in enumerate(guest_ids):
        # A serverless invocation must leave time for the final send and commit.
        # Unclaimed work remains eligible for an authenticated manual rerun.
        if max_runtime_seconds is not None and time.monotonic() - started_at >= max_runtime_seconds:
            result.deferred = len(guest_ids) - index
            break
        claim = _claim(factory, guest_id, local_date, now, cards_provider)
        if claim is None:
            continue
        dispatch_id, token, attempts, client_managed = claim
        # The deterministic `now` argument is for tests. Live ticks recheck the clock
        # so a large device list cannot carry yesterday's digests across midnight.
        send_now = now if fixed_now else utc_now()
        ttl_seconds = int((midnight_after(local_date) - send_now).total_seconds())
        failure: PushFailure | None = None
        message_id: str | None = None
        try:
            if ttl_seconds <= 0:
                raise PushFailure("EXPIRED")
            message_id = sender.send(token, str(dispatch_id), ttl_seconds, client_managed=client_managed)
        except PushFailure as exc:
            failure = exc
        except Exception:
            # Unknown adapter errors may happen after the request was transmitted.
            failure = PushFailure("SEND_UNKNOWN", ambiguous=True)
        outcome = _finish(factory, dispatch_id, token, attempts, send_now, local_date, message_id, failure)
        setattr(result, outcome, getattr(result, outcome) + 1)
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    factory = create_session_factory(settings.database_url)
    sender = FirebaseSender(settings)
    while True:
        try:
            sender.check_configuration()
            break
        except PushFailure as exc:
            # A fresh install without credentials must not burn today's jobs.
            logger.warning("notification_configuration_unavailable code=%s", exc.code)
            time.sleep(settings.notification_poll_seconds)
    while True:
        try:
            result = run_once(factory, sender, settings=settings)
            if any(vars(result).values()):
                logger.info("notification_tick %s", vars(result))
        except Exception:
            # Avoid dumping database connection strings or provider request bodies.
            logger.error("notification_tick_failed")
        time.sleep(settings.notification_poll_seconds)


if __name__ == "__main__":
    main()
