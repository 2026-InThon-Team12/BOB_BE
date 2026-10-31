"""Run `python -m scripts.check_lablup` after providing the server's Lablup key.

This opt-in live probe makes one text and one synthetic-image request. It never
prints credentials, queries user data, or changes LABLUP_VISION_ENABLED.
"""

import argparse
import base64
import io
import json
import re
import secrets

from PIL import Image, ImageDraw, ImageFont

from app.core.config import get_settings
from app.integrations.lablup import LablupClient, LablupError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check the configured Lablup model, without printing secrets."
    )
    parser.add_argument("--text-only", action="store_true", help="Skip the synthetic-image capability probe.")
    args = parser.parse_args()
    settings = get_settings()
    client = LablupClient(settings)
    report = {"model": settings.lablup_model_id, "text_verified": False, "vision_verified": False}
    try:
        response = client.message(
            system="Follow the user's instruction exactly.",
            content=[{"type": "text", "text": "Reply with exactly OK and nothing else."}],
            max_tokens=256,
        )
        report["text_verified"] = response.strip().strip('"') == "OK"
        if not report["text_verified"]:
            report["error"] = "TEXT_PROBE_UNEXPECTED_RESPONSE"
            print(json.dumps(report))
            return 1
        if not args.text_only:
            code = "".join(str(secrets.randbelow(10)) for _ in range(6))
            image = Image.new("RGB", (480, 150), "white")
            draw = ImageDraw.Draw(image)
            draw.text((70, 40), code, fill="black", font=ImageFont.load_default(size=52))
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            image.close()
            response = client.message(
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
                max_tokens=256,
            )
            report["vision_verified"] = re.sub(r"\D", "", response) == code
            if not report["vision_verified"]:
                report["error"] = "VISION_PROBE_NOT_VERIFIED_USE_OCR"
        report["vision_setting_changed"] = False
        print(json.dumps(report))
        return 0 if args.text_only or report["vision_verified"] else 1
    except LablupError as exc:
        report["error"] = exc.code
        report["vision_setting_changed"] = False
        print(json.dumps(report))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
