from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.core.clock import SEOUL
from app.core.config import Settings
from app.integrations.firebase import FirebaseSender, PushFailure, classify_fcm_response, retry_after_seconds
from app.models import Device, Guest, NotificationDispatch
from app.workers.notifications import run_once


NOW = datetime(2026, 10, 31, 20, 0, tzinfo=SEOUL)
SETTINGS = Settings(notifications_hour=20)


def work(session, guest_id, today):
    return {"questions": [{"id": "one"}, {"id": "two"}], "decisions": [], "reviews": [{"id": "three"}]}


def no_work(session, guest_id, today):
    return {"questions": [], "decisions": [], "reviews": []}


class FakeSender:
    def __init__(self, failures=None, callback=None):
        self.calls = []
        self.client_managed_calls = []
        self.failures = list(failures or [])
        self.callback = callback

    def send(self, token, digest_id, ttl_seconds, *, test=False, client_managed=False):
        self.calls.append((token, digest_id, ttl_seconds, test))
        self.client_managed_calls.append(client_managed)
        if self.callback:
            self.callback()
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        return f"projects/test/messages/{len(self.calls)}"


@pytest.fixture
def push_guest(session_factory):
    with session_factory() as session:
        guest = Guest(token_hash="push-guest".ljust(64, "0"))
        session.add(guest)
        session.flush()
        session.add(Device(guest_id=guest.id, fcm_token="registered-device", enabled=True))
        session.commit()
        return guest.id


def dispatch_for(session_factory, guest_id):
    with session_factory() as session:
        return session.scalar(select(NotificationDispatch).where(NotificationDispatch.guest_id == guest_id))


def tick(session_factory, sender, *, now=NOW, cards_provider=work):
    return run_once(session_factory, sender, now=now, settings=SETTINGS, cards_provider=cards_provider)


def test_daily_digest_is_one_job_for_all_tasks_and_restart(session_factory, push_guest):
    sender = FakeSender()
    assert tick(session_factory, sender, now=NOW - timedelta(seconds=1)).sent == 0
    assert tick(session_factory, sender).sent == 1
    assert tick(session_factory, sender, now=NOW + timedelta(minutes=1)).sent == 0
    assert len(sender.calls) == 1
    assert sender.calls[0][2] == 4 * 60 * 60
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(NotificationDispatch)) == 1
    dispatch = dispatch_for(session_factory, push_guest)
    assert dispatch.status == "SENT"
    assert dispatch.attempts == 1


def test_next_day_gets_its_own_digest(session_factory, push_guest):
    sender = FakeSender()
    assert tick(session_factory, sender).sent == 1
    assert tick(session_factory, sender, now=NOW + timedelta(days=1)).sent == 1
    assert len(sender.calls) == 2
    assert sender.calls[0][1] != sender.calls[1][1]


def test_local_work_uses_client_filtered_payload(session_factory, push_guest):
    sender = FakeSender()

    def local_work(*_):
        return {"pre_purchase_questions": [{"answer_state": "CLIENT_MANAGED"}]}

    assert tick(session_factory, sender, cards_provider=local_work).sent == 1
    assert sender.client_managed_calls == [True]


def test_concurrent_workers_claim_one_postgres_dispatch(session_factory, push_guest):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    if session_factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("This assertion needs PostgreSQL's concurrent transactions")
    barrier = Barrier(2)

    def simultaneous_work(session, guest_id, today):
        barrier.wait(timeout=10)
        return work(session, guest_id, today)

    sender = FakeSender()
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [
            pool.submit(tick, session_factory, sender, cards_provider=simultaneous_work) for _ in range(2)
        ]
        results = [run.result(timeout=15) for run in runs]
    assert sum(result.sent for result in results) == 1
    assert len(sender.calls) == 1


def test_no_work_or_permission_means_no_dispatch(session_factory, push_guest):
    sender = FakeSender()
    assert tick(session_factory, sender, cards_provider=no_work).sent == 0
    with session_factory() as session:
        device = session.scalar(select(Device).where(Device.guest_id == push_guest))
        device.enabled = False
        session.commit()
    assert tick(session_factory, sender).sent == 0
    assert sender.calls == []
    assert dispatch_for(session_factory, push_guest) is None


def test_timeout_is_unknown_and_is_not_retried(session_factory, push_guest):
    sender = FakeSender([PushFailure("TRANSPORT_UNKNOWN", ambiguous=True)])
    assert tick(session_factory, sender).unknown == 1
    assert tick(session_factory, sender, now=NOW + timedelta(minutes=10)).sent == 0
    assert dispatch_for(session_factory, push_guest).status == "UNKNOWN"
    assert len(sender.calls) == 1


def test_explicit_retry_respects_retry_after_and_has_limit(session_factory, push_guest):
    sender = FakeSender(
        [
            PushFailure("QUOTA_EXCEEDED", retryable=True, retry_after_seconds=120),
            PushFailure("UNAVAILABLE", retryable=True),
            PushFailure("UNAVAILABLE", retryable=True),
        ]
    )
    assert tick(session_factory, sender).pending == 1
    assert tick(session_factory, sender, now=NOW + timedelta(seconds=119)).pending == 0
    assert tick(session_factory, sender, now=NOW + timedelta(seconds=120)).pending == 1
    assert tick(session_factory, sender, now=NOW + timedelta(seconds=240)).failed == 1
    tick(session_factory, sender, now=NOW + timedelta(hours=1))
    assert len(sender.calls) == 3
    assert dispatch_for(session_factory, push_guest).status == "FAILED"


def test_unregistered_token_is_disabled_but_replacement_is_preserved(session_factory, push_guest):
    def replace_token():
        with session_factory() as session:
            device = session.scalar(select(Device).where(Device.guest_id == push_guest))
            device.fcm_token = "replacement-device"
            session.commit()

    sender = FakeSender([PushFailure("UNREGISTERED", token_invalid=True)], callback=replace_token)
    assert tick(session_factory, sender).failed == 1
    with session_factory() as session:
        device = session.scalar(select(Device).where(Device.guest_id == push_guest))
        assert device.enabled
        assert device.fcm_token == "replacement-device"


def test_unregistered_current_token_is_disabled(session_factory, push_guest):
    sender = FakeSender([PushFailure("UNREGISTERED", token_invalid=True)])
    assert tick(session_factory, sender).failed == 1
    with session_factory() as session:
        assert not session.scalar(select(Device).where(Device.guest_id == push_guest)).enabled


def test_crashed_sending_claim_is_unknown_not_resent(session_factory, push_guest):
    with session_factory() as session:
        session.add(
            NotificationDispatch(
                guest_id=push_guest,
                local_date=NOW.date(),
                status="SENDING",
                attempts=1,
                claimed_at=NOW.astimezone(timezone.utc) - timedelta(minutes=6),
            )
        )
        session.commit()
    sender = FakeSender()
    assert tick(session_factory, sender).unknown == 1
    assert sender.calls == []
    assert dispatch_for(session_factory, push_guest).status == "UNKNOWN"


def test_expired_pending_digest_does_not_send_on_new_day(session_factory, push_guest):
    with session_factory() as session:
        session.add(
            NotificationDispatch(
                guest_id=push_guest,
                local_date=date(2026, 10, 30),
                status="PENDING",
                attempts=1,
            )
        )
        session.commit()
    sender = FakeSender()
    result = tick(session_factory, sender, now=NOW.replace(hour=0))
    assert result.skipped == 1
    assert sender.calls == []
    assert dispatch_for(session_factory, push_guest).status == "SKIPPED"


def test_retry_cannot_spill_into_tomorrow(session_factory, push_guest):
    sender = FakeSender([PushFailure("UNAVAILABLE", retryable=True)])
    assert tick(session_factory, sender, now=NOW.replace(hour=23, minute=59, second=30)).failed == 1
    assert dispatch_for(session_factory, push_guest).next_attempt_at is None


def test_utc_clock_maps_to_seoul_digest_date(session_factory, push_guest):
    sender = FakeSender()
    assert tick(session_factory, sender, now=datetime(2026, 10, 31, 11, tzinfo=timezone.utc)).sent == 1
    assert dispatch_for(session_factory, push_guest).local_date == date(2026, 10, 31)


@pytest.mark.parametrize(
    "status,details,expected,invalid,retryable",
    [
        (
            404,
            [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": "UNREGISTERED"}],
            "UNREGISTERED",
            True,
            False,
        ),
        (400, [], "INVALID_ARGUMENT", False, False),
        (429, [], "RESOURCE_EXHAUSTED", False, True),
        (503, [], "UNAVAILABLE", False, True),
    ],
)
def test_fcm_failure_classification(status, details, expected, invalid, retryable):
    failure = classify_fcm_response(
        httpx.Response(status, json={"error": {"status": expected, "details": details}})
    )
    assert (failure.code, failure.token_invalid, failure.retryable) == (expected, invalid, retryable)


def test_http_transport_does_not_retry_uncertain_send(monkeypatch):
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("test timeout", request=request)

    client = httpx.Client(transport=httpx.MockTransport(timeout))
    sender = FirebaseSender(SETTINGS, client=client)
    monkeypatch.setattr(sender, "_credentials", lambda: ("project", "private-access-token"))
    with pytest.raises(PushFailure) as error:
        sender.send("device", "digest", 60)
    assert error.value.ambiguous
    assert len(calls) == 1


def test_fcm_payload_has_route_ttl_and_collapse(monkeypatch):
    captured = []

    def success(request):
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"name": "projects/project/messages/one"})

    sender = FirebaseSender(SETTINGS, client=httpx.Client(transport=httpx.MockTransport(success)))
    monkeypatch.setattr(sender, "_credentials", lambda: ("project", "private-access-token"))
    assert sender.send("device", "daily-id", 120) == "projects/project/messages/one"
    message = captured[0]["message"]
    assert message["data"]["route"] == "today"
    assert message["android"]["ttl"] == "120s"
    assert message["android"]["notification"]["tag"] == "daily-id"
    sender.send("device", "local-digest", 120, client_managed=True)
    message = captured[1]["message"]
    assert "notification" not in message and "notification" not in message["android"]
    assert message["data"]["client_managed"] == "true"
    sender.send("device", "test-local", 120, client_managed=True, test=True)
    message = captured[2]["message"]
    assert "notification" not in message
    assert message["data"]["test"] == "true"
    assert message["data"]["client_managed"] == "true"


def test_retry_after_supports_http_date():
    assert retry_after_seconds("120") == 120
    assert (
        retry_after_seconds("Sat, 31 Oct 2026 11:02:00 GMT", datetime(2026, 10, 31, 11, tzinfo=timezone.utc))
        == 120
    )
    assert retry_after_seconds("invalid") is None
