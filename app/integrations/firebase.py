"""FCM transport with explicit, conservative retry semantics.

Firebase Admin owns credentials; HTTP v1 is sent once with httpx because the
Admin SDK's default messaging transport also retries socket read failures.
The worker, not the HTTP client, decides which known failures can be retried.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.core.config import Settings, get_settings


class PushFailure(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        ambiguous: bool = False,
        token_invalid: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        # Keep provider bodies, credentials and registration tokens out of logs.
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.token_invalid = token_invalid
        self.retry_after_seconds = retry_after_seconds


def retry_after_seconds(value: str | None, now: datetime | None = None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0, int((when - (now or datetime.now(timezone.utc))).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None


def classify_fcm_response(response: httpx.Response) -> PushFailure:
    try:
        error = response.json().get("error", {})
        if not isinstance(error, dict):
            error = {}
    except (ValueError, AttributeError):
        error = {}
    status = str(error.get("status", f"HTTP_{response.status_code}"))
    details = error.get("details", [])
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and str(detail.get("@type", "")).endswith("FcmError"):
                status = str(detail.get("errorCode", status))
                break
    allowed_codes = {
        "UNREGISTERED",
        "INVALID_ARGUMENT",
        "SENDER_ID_MISMATCH",
        "QUOTA_EXCEEDED",
        "RESOURCE_EXHAUSTED",
        "UNAVAILABLE",
        "INTERNAL",
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "NOT_FOUND",
        "THIRD_PARTY_AUTH_ERROR",
    }
    code = status if status in allowed_codes else f"HTTP_{response.status_code}"
    return PushFailure(
        code,
        retryable=response.status_code in {429, 500, 503},
        token_invalid=code == "UNREGISTERED",
        retry_after_seconds=retry_after_seconds(response.headers.get("retry-after")),
    )


class FirebaseSender:
    """Lazy Firebase authentication and a single HTTP attempt per send call."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        self._client = client
        self._app: Any = None
        self._lock = threading.Lock()

    def _credentials(self) -> tuple[str, str]:
        import firebase_admin
        from firebase_admin import credentials

        try:
            with self._lock:
                if self._app is None:
                    options: dict[str, Any] = {"httpTimeout": 30}
                    if self.settings.firebase_project_id:
                        options["projectId"] = self.settings.firebase_project_id
                    if self.settings.google_application_credentials:
                        credential = credentials.Certificate(self.settings.google_application_credentials)
                    else:
                        credential = credentials.ApplicationDefault()
                    # A named app avoids mutating another library's default Firebase app.
                    self._app = firebase_admin.initialize_app(
                        credential, options=options, name=f"bob-push-{id(self)}"
                    )
                project_id = self._app.project_id
                if not project_id:
                    raise PushFailure("FIREBASE_NOT_CONFIGURED")
                access_token = self._app.credential.get_access_token().access_token
            return project_id, access_token
        except PushFailure:
            raise
        except Exception as exc:
            raise PushFailure("FIREBASE_CREDENTIAL_ERROR") from exc

    def send(
        self,
        fcm_token: str,
        digest_id: str,
        ttl_seconds: int,
        *,
        test: bool = False,
        client_managed: bool = False,
    ) -> str:
        if ttl_seconds <= 0:
            raise PushFailure("EXPIRED")
        project_id, access_token = self._credentials()
        notification = {
            "title": "BOB 알림 테스트" if test else "오늘의 소비 생각을 기록해 보세요",
            "body": "알림을 눌러 오늘 할 일을 확인해 주세요.",
        }
        message = {
            "token": fcm_token,
            "notification": notification,
            "data": {"route": "today", "digest_id": digest_id, "test": str(test).lower()},
            "android": {
                "priority": "normal",
                "ttl": f"{min(ttl_seconds, 86400)}s",
                "collapse_key": "bob-test" if test else "bob-daily-digest",
                "notification": {"tag": digest_id, "channel_id": "bob_daily"},
            },
        }
        if client_managed and not test:
            # Android must filter completed local cards before displaying a notification.
            # A notification payload would bypass that check in the background.
            message.pop("notification")
            message["android"].pop("notification")
            message["data"].update(client_managed="true", **notification)
        client = self._client or httpx.Client(timeout=30, transport=httpx.HTTPTransport(retries=0))
        try:
            response = client.post(
                f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"message": message},
            )
        except httpx.TransportError as exc:
            # Even a reset after writing the request can mean FCM accepted it.
            raise PushFailure("TRANSPORT_UNKNOWN", ambiguous=True) from exc
        finally:
            if self._client is None:
                client.close()
        if not response.is_success:
            raise classify_fcm_response(response)
        try:
            message_id = response.json()["name"]
            if not isinstance(message_id, str) or not message_id:
                raise ValueError("missing message id")
            return message_id
        except (ValueError, KeyError, TypeError) as exc:
            raise PushFailure("RESPONSE_UNKNOWN", ambiguous=True) from exc

    def check_configuration(self) -> None:
        """Verify credentials without sending or consuming a daily dispatch."""
        self._credentials()
