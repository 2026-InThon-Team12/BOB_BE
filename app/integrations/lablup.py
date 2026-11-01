"""Bounded Messages transport with Lablup defaults and provider-specific hooks."""

from typing import Any
import logging
import time

import httpx

from app.core.config import Settings

logger = logging.getLogger("bob.ai")


class LablupError(RuntimeError):
    """A safe public error code; never carries the upstream response or API key."""

    def __init__(self, code: str, *, image_unsupported: bool = False):
        self.code = code
        self.image_unsupported = image_unsupported
        super().__init__(code)


class LablupClient:
    provider = "lablup"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self.deadline: float | None = None
        self.last_metrics: dict[str, Any] = {}

    @property
    def api_key(self) -> str | None:
        return self.settings.lablup_api_key

    @property
    def model_id(self) -> str:
        return self.settings.lablup_model_id

    @property
    def endpoint(self) -> str:
        return self.settings.lablup_base_url.rstrip("/") + "/v1/messages"

    def request_options(self, thinking: bool | None) -> dict[str, Any]:
        if thinking is None:
            return {}
        return {"thinking": {"type": "enabled" if thinking else "disabled"}}

    def image_rejected(self, response: httpx.Response, content: list[dict]) -> bool:
        return any(block.get("type") == "image" for block in content)

    def message(
        self,
        *,
        system: str,
        content: list[dict[str, Any]],
        max_tokens: int = 4096,
        thinking: bool | None = None,
        timeout_seconds: float | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        self.last_metrics = {"provider": self.provider, "model": self.model_id}
        if not self.api_key:
            raise LablupError("AI_NOT_CONFIGURED")
        payload = {
            "model": self.model_id,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        payload.update(self.request_options(thinking))
        if response_schema is not None:
            payload.setdefault("output_config", {})["format"] = {
                "type": "json_schema",
                "schema": response_schema,
            }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        # Disable redirects so credentials cannot be forwarded to another host.
        own_client = self._client is None
        client = self._client or httpx.Client(follow_redirects=False)
        started = time.monotonic()
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
                self.endpoint,
                headers=headers,
                json=payload,
                timeout=timeout,
                follow_redirects=False,
            )
            self.last_metrics["status"] = response.status_code
            if response.status_code in (401, 403):
                raise LablupError("AI_AUTH_FAILED")
            if response.status_code == 404:
                raise LablupError("AI_MODEL_UNAVAILABLE")
            if response.status_code in (400, 415, 422):
                raise LablupError(
                    "AI_REQUEST_REJECTED",
                    image_unsupported=self.image_rejected(response, content),
                )
            if not response.is_success:
                raise LablupError("AI_UNAVAILABLE")
            try:
                body = response.json()
                usage = body.get("usage", {})
                if isinstance(usage, dict):
                    for field in ("input_tokens", "output_tokens"):
                        if type(usage.get(field)) is int:
                            self.last_metrics[field] = usage[field]
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
            if body.get("stop_reason") == "max_tokens":
                raise LablupError("AI_OUTPUT_LIMIT")
            if not result or len(result) > 100_000:
                raise LablupError("AI_INVALID_RESPONSE")
            return result
        except httpx.TimeoutException:
            raise LablupError("AI_TIMEOUT") from None
        except httpx.HTTPError:
            raise LablupError("AI_UNAVAILABLE") from None
        finally:
            self.last_metrics["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            # Never log prompts, images, upstream error bodies, or credentials.
            logger.info("ai_request %s", self.last_metrics)
            if own_client:
                client.close()
