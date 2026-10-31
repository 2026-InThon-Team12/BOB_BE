"""Small, bounded Anthropic Messages client for Lablup's gateway only."""

from typing import Any
import time

import httpx

from app.core.config import Settings


class LablupError(RuntimeError):
    """A safe public error code; never carries the upstream response or API key."""

    def __init__(self, code: str, *, image_unsupported: bool = False):
        self.code = code
        self.image_unsupported = image_unsupported
        super().__init__(code)


class LablupClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self.deadline: float | None = None

    def message(
        self,
        *,
        system: str,
        content: list[dict[str, Any]],
        max_tokens: int = 4096,
        thinking: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        if not self.settings.lablup_api_key:
            raise LablupError("AI_NOT_CONFIGURED")
        payload = {
            "model": self.settings.lablup_model_id,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        headers = {
            "x-api-key": self.settings.lablup_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        endpoint = self.settings.lablup_base_url.rstrip("/") + "/v1/messages"
        # Disable redirects so credentials cannot be forwarded to another host.
        own_client = self._client is None
        client = self._client or httpx.Client(follow_redirects=False)
        try:
            timeout = (
                min(timeout_seconds, 120) if timeout_seconds is not None else self.settings.ai_timeout_seconds
            )
            if self.deadline is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise LablupError("AI_TIMEOUT")
                timeout = min(timeout, remaining)
            response = client.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=timeout,
                follow_redirects=False,
            )
            if response.status_code in (401, 403):
                raise LablupError("AI_AUTH_FAILED")
            if response.status_code == 404:
                raise LablupError("AI_MODEL_UNAVAILABLE")
            if response.status_code in (400, 415, 422):
                raise LablupError(
                    "AI_REQUEST_REJECTED",
                    image_unsupported=any(block.get("type") == "image" for block in content),
                )
            if not response.is_success:
                raise LablupError("AI_UNAVAILABLE")
            try:
                body = response.json()
                blocks = body.get("content", [])
                result = "\n".join(
                    block["text"]
                    for block in blocks
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
            except (ValueError, TypeError, AttributeError):
                raise LablupError("AI_INVALID_RESPONSE") from None
            if not result and body.get("stop_reason") == "max_tokens":
                raise LablupError("AI_OUTPUT_LIMIT")
            if not result or len(result) > 100_000:
                raise LablupError("AI_INVALID_RESPONSE")
            return result
        except httpx.TimeoutException:
            raise LablupError("AI_TIMEOUT") from None
        except httpx.HTTPError:
            raise LablupError("AI_UNAVAILABLE") from None
        finally:
            if own_client:
                client.close()
