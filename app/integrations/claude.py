"""Direct Claude API adapter using the existing bounded Messages transport.

The shared base preserves deadlines, safe errors and the existing service contract.
Credentials and request options are intentionally separate from Lablup.
"""

import httpx

from app.integrations.lablup import LablupClient


class ClaudeClient(LablupClient):
    provider = "claude"

    @property
    def api_key(self) -> str | None:
        return self.settings.claude_api_key

    @property
    def model_id(self) -> str:
        return self.settings.claude_model_id

    @property
    def endpoint(self) -> str:
        return "https://api.anthropic.com/v1/messages"

    def request_options(self, thinking: bool | None) -> dict:
        # Sonnet 5.5 rejects "disabled". The app's old thinking=False hint
        # must not override the explicitly configured Claude-compatible mode.
        mode = "adaptive" if thinking is True else self.settings.claude_thinking_mode
        options = {}
        if mode != "default":
            options["thinking"] = {"type": mode}
        if self.settings.claude_effort != "default":
            options["output_config"] = {"effort": self.settings.claude_effort}
        return options

    def image_rejected(self, response: httpx.Response, content: list[dict]) -> bool:
        if not any(block.get("type") == "image" for block in content):
            return False
        # A 400 caused by an invalid model/option is not evidence that vision
        # is unsupported; avoid a second, equally invalid OCR-only request.
        try:
            error = response.json().get("error", {})
            message = error.get("message", "") if isinstance(error, dict) else ""
            message = message.lower() if isinstance(message, str) else ""
        except (ValueError, AttributeError):
            return False
        return "image" in message and any(
            reason in message for reason in ("not support", "unsupported", "does not accept")
        )
