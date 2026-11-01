"""Opt-in live text/image probe for the explicitly selected provider.

Only a fixed text instruction and a generated digit image leave the server.
Reports latency and token counts, never credentials or customer content.
"""

import argparse
import base64
import io
import json
import re
import secrets

from PIL import Image, ImageDraw, ImageFont

from app.core.config import get_settings
from app.integrations.ai_provider import create_ai_client
from app.integrations.lablup import LablupError


def probe(settings, *, text_only=False, client=None) -> tuple[int, dict]:
    client = client or create_ai_client(settings)
    report = {
        "provider": settings.ai_provider,
        "model": settings.ai_model_id,
        "text_verified": False,
        "vision_verified": False,
        "requests": [],
    }
    try:
        reply = client.message(
            system="Follow the user's instruction exactly.",
            content=[{"type": "text", "text": "Reply with exactly OK and nothing else."}],
            max_tokens=1024,
        )
        report["requests"].append({"kind": "text", **client.last_metrics})
        report["text_verified"] = reply.strip().strip('"') == "OK"
        if not report["text_verified"]:
            report["error"] = "TEXT_PROBE_UNEXPECTED_RESPONSE"
            return 1, report
        if text_only:
            return 0, report
        digits = "".join(str(secrets.randbelow(10)) for _ in range(6))
        with Image.new("RGB", (480, 150), "white") as image:
            ImageDraw.Draw(image).text((70, 40), digits, fill="black", font=ImageFont.load_default(size=52))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        reply = client.message(
            system="Read the supplied image accurately. Reply only with the six visible digits.",
            content=[
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
                    },
                },
                {"type": "text", "text": "Which six digits are shown in the image?"},
            ],
            max_tokens=1024,
            thinking=False,
        )
        report["requests"].append({"kind": "vision", **client.last_metrics})
        report["vision_verified"] = re.sub(r"\D", "", reply) == digits
        if not report["vision_verified"]:
            report["error"] = "VISION_PROBE_UNEXPECTED_RESPONSE"
        return (0 if report["vision_verified"] else 1), report
    except LablupError as error:
        report["error"] = error.code
        report["failed_request"] = client.last_metrics
        return 2, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider", choices=["claude", "lablup"], help="Override AI_PROVIDER for this probe."
    )
    parser.add_argument("--text-only", action="store_true", help="Skip the synthetic-image request.")
    args = parser.parse_args()
    settings = get_settings()
    if args.provider:
        settings = settings.model_copy(update={"ai_provider": args.provider})
    status, report = probe(settings, text_only=args.text_only)
    print(json.dumps(report, ensure_ascii=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
