"""Locate photo regions within already verified cart cards, without generating images."""

import base64
import io
from statistics import median

from PIL import Image, ImageDraw, ImageFilter

from app.services.cart_layout import CartCard
from app.services.ocr_layout import Line


def thumbnail_box(image: Image.Image, card: CartCard, words: list[Line]) -> tuple[int, int, int, int] | None:
    if not card.box:
        return None
    x0, y0, x1, y1 = card.box
    with image.crop(card.box).convert("RGB") as patch:
        patch.thumbnail((400, 900))
        width, height = patch.size
        sx, sy = width / (x1 - x0), height / (y1 - y0)
        pixels = patch.load()
        edges = [pixels[x, y] for x in (0, width - 1) for y in range(0, height, max(1, height // 30))]
        background = [median(p[c] for p in edges) for c in range(3)]
        mask = Image.new("L", patch.size)
        mask.putdata(
            [
                255 if max(abs(pixels[x, y][c] - background[c]) for c in range(3)) > 35 else 0
                for y in range(height)
                for x in range(width)
            ]
        )
        # Remove text and controls before joining the remaining photo pixels.
        draw = ImageDraw.Draw(mask)
        local_words = [w for w in words if x0 <= w.left < x1 and y0 <= w.top < y1]
        text_height = median([w.bottom - w.top for w in local_words] or [20])
        for word in local_words:
            # OCR can hallucinate a tall single glyph over an entire photograph.
            if word.bottom - word.top <= max(24, text_height * 2.2):
                draw.rectangle(
                    (
                        int((word.left - x0) * sx) - 2,
                        int((word.top - y0) * sy) - 2,
                        int((word.right - x0) * sx) + 2,
                        int((word.bottom - y0) * sy) + 2,
                    ),
                    fill=0,
                )
        original = mask.tobytes()
        with mask.filter(ImageFilter.MaxFilter(9)) as expanded:
            joined = expanded.filter(ImageFilter.MinFilter(9))
        mask.close()
        data = bytearray(joined.tobytes())
        joined.close()
        candidates = []
        for seed in range(len(data)):
            if not data[seed]:
                continue
            stack = [seed]
            data[seed] = 0
            left = right = seed % width
            top = bottom = seed // width
            count = 0
            while stack:
                pos = stack.pop()
                x, y = pos % width, pos // width
                left, right = min(left, x), max(right, x)
                top, bottom = min(top, y), max(bottom, y)
                count += bool(original[pos])
                for nxt in (
                    pos - 1 if x else -1,
                    pos + 1 if x + 1 < width else -1,
                    pos - width if y else -1,
                    pos + width if y + 1 < height else -1,
                ):
                    if nxt >= 0 and data[nxt]:
                        data[nxt] = 0
                        stack.append(nxt)
            w, h = right - left + 1, bottom - top + 1
            if not (max(35, width * 0.12) <= w < width * 0.9 and h >= 35 and 0.45 <= w / h <= 2.2):
                continue
            if count / (w * h) < 0.20 or w * h > width * height * 0.7:
                continue
            candidates.append((count, left, top, right + 1, bottom + 1))
        candidates.sort(reverse=True)
        if not candidates or (len(candidates) > 1 and candidates[1][0] > candidates[0][0] * 0.7):
            return None
        _, left, top, right, bottom = candidates[0]
        return (
            max(x0, x0 + round((left - 2) / sx)),
            max(y0, y0 + round((top - 2) / sy)),
            min(x1, x0 + round((right + 2) / sx)),
            min(y1, y0 + round((bottom + 2) / sy)),
        )


def encode_thumbnail(image: Image.Image, box: tuple[int, int, int, int]) -> dict:
    with image.crop(box).convert("RGB") as thumb:
        thumb.thumbnail((320, 320))
        buffer = io.BytesIO()
        thumb.save(buffer, format="JPEG", quality=88)
        return {
            "mime_type": "image/jpeg",
            "base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
            "width": thumb.width,
            "height": thumb.height,
            "box": [
                round(v * 1000 / (image.width if i % 2 == 0 else image.height)) for i, v in enumerate(box)
            ],
        }
