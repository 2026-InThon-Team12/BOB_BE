from datetime import datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.pool import NullPool

from app.core.clock import SEOUL
from app.core.config import Settings, get_settings
from app.db import create_session_factory
from app.integrations.firebase import FirebaseSender, PushFailure
from app.models import Device, Guest, NotificationDispatch
from app.workers.notifications import run_once

SECRET = "cron-test-secret-" * 3


def test_remote_push_requires_admin_secret(client, app, guest_headers):
    sender = Mock()
    configure_cron(app, sender)
    body = {"guest_id": "00000000-0000-0000-0000-000000000000"}
    assert client.post("/internal/notifications/test", json=body).status_code == 401
    assert client.post("/internal/notifications/test", json=body, headers=guest_headers).status_code == 401
    sender.send.assert_not_called()


@pytest.mark.parametrize("client_managed", [False, True])
def test_remote_push_acceptance_does_not_consume_daily_digest(client, app, session_factory, client_managed):
    sender = Mock()
    configure_cron(app, sender)
    guest = client.post("/api/v1/guest-sessions").json()
    auth = {"Authorization": "Bearer " + guest["access_token"]}
    assert client.put("/api/v1/me/push-token", headers=auth, json={"token": "test-device", "enabled": True}).status_code == 200
    response = client.post(
        "/internal/notifications/test",
        headers={"Authorization": f"Bearer {SECRET}"},
        json={"guest_id": guest["guest_id"], "client_managed": client_managed},
    )
    assert response.status_code == 202
    assert response.json() == {"status": "FCM_ACCEPTED", "delivery_confirmed": False}
    assert sender.send.call_args.args[0] == "test-device"
    assert sender.send.call_args.kwargs == {"test": True, "client_managed": client_managed}
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(NotificationDispatch)) == 0
    client.delete("/api/v1/me/push-token", headers=auth)
    assert client.post(
        "/internal/notifications/test", headers={"Authorization": f"Bearer {SECRET}"},
        json={"guest_id": guest["guest_id"]},
    ).status_code == 404
    sender.send.assert_called_once()


def test_lablup_secret_alias_and_standard_name_precedence(monkeypatch):
    monkeypatch.delenv("LABLUP_API_KEY", raising=False)
    monkeypatch.setenv("Lablup", "dashboard-secret")
    assert Settings(_env_file=None).lablup_api_key == "dashboard-secret"
    monkeypatch.setenv("LABLUP_API_KEY", "canonical-secret")
    assert Settings(_env_file=None).lablup_api_key == "canonical-secret"
    assert Settings(_env_file=None, lablup_api_key="explicit-test-secret").lablup_api_key == "explicit-test-secret"
    assert "canonical-secret" not in repr(Settings(_env_file=None))


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_cloud_url_preserves_credentials_and_ssl_query(scheme):
    suffix = "user:p%40ss@db.example/bob?sslmode=require&channel_binding=require"
    settings = Settings(_env_file=None, database_url=f"{scheme}://{suffix}")
    assert settings.database_url == "postgresql+psycopg://" + suffix
    assert "p%40ss" not in repr(settings)


def test_serverless_pool_does_not_hold_idle_connections():
    factory = create_session_factory("sqlite://", null_pool=True)
    assert isinstance(factory.kw["bind"].pool, NullPool)
    factory.kw["bind"].dispose()


def configure_cron(app, sender):
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, cron_secret=SECRET)
    app.state.notification_sender = sender


def test_cron_requires_configured_secret_before_provider_or_db(client, app):
    sender = Mock()
    app.state.notification_sender = sender
    assert client.get("/internal/notifications").status_code == 503
    configure_cron(app, sender)
    assert client.get("/internal/notifications").status_code == 401
    assert client.get("/internal/notifications", headers={"Authorization": "Bearer wrong"}).status_code == 401
    sender.check_configuration.assert_not_called()


def test_cron_missing_firebase_does_not_consume_dispatch(client, app, session_factory):
    sender = Mock()
    sender.check_configuration.side_effect = PushFailure("FIREBASE_CREDENTIAL_ERROR")
    configure_cron(app, sender)
    response = client.get("/internal/notifications", headers={"Authorization": f"Bearer {SECRET}"})
    assert response.status_code == 503
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(NotificationDispatch)) == 0
    sender.send.assert_not_called()


def test_cron_repeated_invocation_sends_once(client, app, session_factory, monkeypatch):
    from app.workers import notifications

    with session_factory() as session:
        guest = Guest(token_hash="a" * 64)
        session.add(guest)
        session.flush()
        session.add(Device(guest_id=guest.id, fcm_token="test-device", enabled=True))
        session.commit()
    sender = Mock()
    sender.send.return_value = "projects/test/messages/one"
    configure_cron(app, sender)
    monkeypatch.setattr(notifications, "utc_now", lambda: datetime(2026, 10, 31, 20, 10, tzinfo=SEOUL))
    monkeypatch.setattr(notifications, "_cards", lambda *_: {"questions": [{"id": "one"}]})
    headers = {"Authorization": f"Bearer {SECRET}"}
    response = client.get("/internal/notifications", headers=headers)
    assert response.status_code == 200
    assert response.json()["sent"] == 1
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/internal/notifications", headers=headers).json()["sent"] == 0
    sender.send.assert_called_once()


def test_cron_time_budget_leaves_unclaimed_work_eligible(session_factory):
    with session_factory() as session:
        guest = Guest(token_hash="b" * 64)
        session.add(guest)
        session.flush()
        session.add(Device(guest_id=guest.id, fcm_token="test-device", enabled=True))
        session.commit()
    sender = Mock()
    result = run_once(
        session_factory, sender, now=datetime(2026, 10, 31, 20, tzinfo=SEOUL), max_runtime_seconds=0
    )
    assert result.deferred == 1
    sender.send.assert_not_called()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(NotificationDispatch)) == 0


def test_inline_firebase_credentials_and_errors_hide_secret(monkeypatch):
    import firebase_admin
    from firebase_admin import credentials

    certificate = Mock()
    fake_app = Mock(project_id="bob-test")
    fake_app.credential.get_access_token.return_value.access_token = "test-token"
    monkeypatch.setattr(credentials, "Certificate", certificate)
    monkeypatch.setattr(firebase_admin, "initialize_app", Mock(return_value=fake_app))
    sender = FirebaseSender(Settings(_env_file=None, firebase_service_account_json='{"project_id":"bob-test"}'))
    assert sender._credentials() == ("bob-test", "test-token")
    certificate.assert_called_once_with({"project_id": "bob-test"})
    sender = FirebaseSender(Settings(_env_file=None, firebase_service_account_json="a-secret-invalid-json"))
    with pytest.raises(PushFailure) as caught:
        sender.check_configuration()
    assert str(caught.value) == "FIREBASE_CREDENTIAL_ERROR"
    assert caught.value.__suppress_context__
