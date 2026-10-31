"""Independent, bounded price-crop OCR. Disagreement never creates a new price."""

import base64
import io
import json
import re
import subprocess
import sys
import time
from statistics import median

from PIL import Image, ImageOps

from app.core.config import Settings
from app.services.cart_layout import CartCard, card_price_values


def read_price_crops(crops: list[Image.Image], settings: Settings) -> list[str]:
    payload = []
    for crop in crops:
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        payload.append(buffer.getvalue())
    timeout = min(settings.ai_timeout_seconds, 30)
    try:
        if settings.ocr_backend == "python":
            result = subprocess.run(
                [sys.executable, "-m", "app.integrations.ocr_worker", settings.tessdata_dir, "prices"],
                input=json.dumps([base64.b64encode(data).decode("ascii") for data in payload]).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=True,
            )
            texts = json.loads(result.stdout)
            if (
                not isinstance(texts, list)
                or len(texts) != len(crops)
                or not all(isinstance(text, str) and len(text) < 4000 for text in texts)
            ):
                return []
            return texts
        texts = []
        deadline = time.monotonic() + timeout
        for data in payload:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            result = subprocess.run(
                [settings.tesseract_cmd, "stdin", "stdout", "-l", "kor+eng", "--psm", "7"],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=remaining,
            )
            texts.append(result.stdout.decode("utf-8", errors="replace"))
        return texts
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return []


def verify_prices(
    image: Image.Image, cards: list[CartCard], settings: Settings, reader=read_price_crops
) -> bool:
    candidates = [c for c in cards if c.price is not None and c.price_evidence is not None]
    crops = []
    try:
        for card in candidates:
            line = card.price_evidence
            with image.crop((line.left, line.top, line.right, line.bottom)) as original:
                rgb = original.convert("RGB")
                corners = [
                    rgb.getpixel(p)
                    for p in (
                        (0, 0),
                        (rgb.width - 1, 0),
                        (0, rgb.height - 1),
                        (rgb.width - 1, rgb.height - 1),
                    )
                ]
                background = tuple(round(median(p[c] for p in corners)) for c in range(3))
                scale = min(2, 1400 / max(rgb.size))
                enlarged = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))))
                crops.append(ImageOps.expand(enlarged, border=20, fill=background))
                rgb.close()
                enlarged.close()
        texts = reader(crops, settings) if crops else []
        changed = False
        for index, card in enumerate(candidates):
            text = texts[index] if index < len(texts) else ""
            # Only for agreement checking: a secondary OCR dot can represent a
            # thousands separator already evidenced as a comma in the first pass.
            text = re.sub(r"(?<=\d)\.(?=\d{3}(?:\d[!.ㅣ)]|\D|$))", ",", text)
            if card_price_values(text) != {card.price}:
                card.price = None
                changed = True
        return changed
    finally:
        for crop in crops:
            crop.close()
