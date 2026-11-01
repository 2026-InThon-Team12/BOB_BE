"""Explicit provider selection. Missing Claude credentials never use another key."""

import httpx

from app.core.config import Settings
from app.integrations.claude import ClaudeClient
from app.integrations.lablup import LablupClient


def create_ai_client(settings: Settings, client: httpx.Client | None = None) -> LablupClient:
    client_type = ClaudeClient if settings.ai_provider == "claude" else LablupClient
    return client_type(settings, client)
