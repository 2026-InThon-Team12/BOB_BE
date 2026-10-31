"""Detect repeated cart selectors before asking AI to describe individual cards.

This conservative path handles vertical checkbox carts. Other layouts keep the
generic OCR path. Coordinates and image data remain request-local.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from statistics import median

from PIL import Image

from app.services.ocr_layout import Line, money_values, normalized


@dataclass
class CartCard:
    lines: list[Line]
    price: int | None
    selected: bool = True

    def literal_title(self) -> str:
        """Keep evidence from this card when AI rewrites or borrows a name."""
        candidates = [
            line
            for line in self.lines
            if len(normalized(line.text)) >= 6
            and not re.search(r"배송|출발|출알|주문|마감", normalized(line.text))
        ]
        if not candidates:
            return self.lines[0].text[:200]
        longest = max(len(normalized(line.text)) for line in candidates)
        candidates = [line for line in candidates if len(normalized(line.text)) >= longest / 2]
        first = candidates[0]
        parts = [first.text]
        previous = first
        for line in candidates[1:]:
            if line.top - previous.bottom > 2 * (first.bottom - first.top):
                break
            if abs(line.left - first.left) > 2 * (first.bottom - first.top):
                continue
            parts.append(line.text)
            previous = line
        return " ".join(parts)[:200]


def name_belongs_to_card(name: str, cards: list[CartCard], index: int) -> bool:
    """Allow small OCR spelling repairs only with stronger evidence in this card.

    A name copied from a neighbouring card cannot win this comparison. Prices
    never participate in this matching and remain tied to their image region.
    """
    target = normalized(name)
    if len(target) < 2:
        return False
    sources = [normalized(card.literal_title()) for card in cards]
    if target in sources[index]:
        return True
    scores = [
        sum(block.size for block in SequenceMatcher(None, target, source).get_matching_blocks()) / len(target)
        for source in sources
    ]
    other = max((score for i, score in enumerate(scores) if i != index), default=0)
    return scores[index] >= 0.75 and scores[index] >= other + 0.15


def cart_selectors(image: Image.Image) -> list[tuple[int, bool]]:
    with image.convert("L") as gray:
        gray.thumbnail((540, 5000))
        width, height = gray.size
        pixels = gray.load()
        lane = int(width * 0.12)
        visited: set[tuple[int, int]] = set()
        boxes = []
        for y in range(height):
            for x in range(lane):
                if (x, y) in visited or pixels[x, y] > 235:
                    continue
                stack = [(x, y)]
                visited.add((x, y))
                points = []
                while stack:
                    a, b = stack.pop()
                    points.append((a, b))
                    for c, d in ((a - 1, b), (a + 1, b), (a, b - 1), (a, b + 1)):
                        if (
                            0 <= c < lane
                            and 0 <= d < height
                            and (c, d) not in visited
                            and pixels[c, d] <= 235
                        ):
                            visited.add((c, d))
                            stack.append((c, d))
                left = min(a for a, _ in points)
                right = max(a for a, _ in points)
                top = min(b for _, b in points)
                bottom = max(b for _, b in points)
                size = right - left + 1
                if not (width * 0.025 <= size <= width * 0.10 and 0.8 <= size / (bottom - top + 1) <= 1.2):
                    continue
                # A selector has four continuous sides; letters/icons do not.
                inset = max(2, size // 5)
                sides = [
                    {b for a, b in points if a <= left + inset},
                    {b for a, b in points if a >= right - inset},
                    {a for a, b in points if b <= top + inset},
                    {a for a, b in points if b >= bottom - inset},
                ]
                if min(map(len, sides)) >= size * 0.8:
                    # Inspect the centre, excluding the empty checkbox's border.
                    pad = max(2, size // 4)
                    centre = [
                        pixels[a, b]
                        for a in range(left + pad, right - pad + 1)
                        for b in range(top + pad, bottom - pad + 1)
                    ]
                    selected = sum(value < 215 for value in centre) >= len(centre) * 0.12
                    boxes.append((left, top, size, selected))
        # Require aligned, equally sized selectors; include checked AND unchecked.
        if len(boxes) < 2:
            return []
        groups = [[b for b in boxes if abs(b[0] - a[0]) <= 3 and abs(b[2] - a[2]) <= 3] for a in boxes]
        group = max(groups, key=len)
        if len(group) < 2:
            return []
        scale = image.height / height
        return sorted((round(top * scale), selected) for _, top, _, selected in group)


def selector_rows(image: Image.Image) -> list[int]:
    return [top for top, _ in cart_selectors(image)]


def _crossed_out(line: Line, image: Image.Image) -> bool:
    box = (max(0, line.left), max(0, line.top), min(image.width, line.right), min(image.height, line.bottom))
    if box[2] <= box[0] or box[3] <= box[1]:
        return True
    with image.crop(box).convert("L") as patch:
        w, h = patch.size
        pixels = patch.load()
        # A strike covers most of the price width, unlike individual glyphs.
        return any(
            sum(pixels[x, y] < 220 for x in range(w)) >= w * 0.85
            for y in range(h // 4, max(h // 4 + 1, 3 * h // 4))
        )


def horizontal_separators(image: Image.Image) -> list[int]:
    """Full-width grey dividers separate cart cards from checkout/sticky totals.

    Preserve vertical resolution so one-pixel rules survive. Option boxes inside
    a product card do not span the screen and therefore are not boundaries.
    """
    with image.convert("RGB").resize((100, image.height)) as narrow:
        pixels = narrow.load()
        boundaries = []
        in_band = False
        for y in range(narrow.height):
            grey = (
                sum(
                    180 <= min(pixels[x, y]) < 253 and max(pixels[x, y]) - min(pixels[x, y]) < 12
                    for x in range(100)
                )
                >= 95
            )
            if grey and not in_band:
                boundaries.append(y)
            in_band = grey
        return boundaries


def card_price_values(text: str) -> set[int]:
    compact = re.sub(r"\s+", "", text)
    # A price row can include a discount badge and delivery branding. Unit prices,
    # coupon savings and order totals must not compete with the item selling price.
    if re.search(r"쿠폰|배송비|적립|합계|총|주문|결제|할인됨|할인적용|개당|[gGlL]당", compact):
        return set()
    if re.search(r"\(\d+(?:개|g|kg|ml|l)", compact, re.IGNORECASE):
        return set()
    amounts = set()
    for match in re.finditer(r"(?<![\d,])(\d{1,3}(?:,\d{3})+)(?:\s*(?:원|운|\d[!.ㅣ)]))?(?![\d,])", text):
        amounts.add(int(match[1].replace(",", "")))
    # Korean 원 may also become a lone digit after a clearly marked discount row.
    discounted = re.fullmatch(r"\d{1,2}%([0-9]{1,3}(?:,[0-9]{3})+)\d", compact)
    if discounted:
        amounts.add(int(discounted[1].replace(",", "")))
    if amounts:
        return amounts
    if re.fullmatch(r"(?:₩\s*)?\d[\d,]*\s*[^\d\s]{0,3}", text.strip()):
        return money_values(text)
    return set()


def checkbox_cards(image: Image.Image, lines: list[Line]) -> list[CartCard]:
    selectors = cart_selectors(image)
    starts = [top for top, _ in selectors]
    if len(starts) < 2:
        return []
    cards = []
    text_height = median([line.bottom - line.top for line in lines] or [20])
    separators = horizontal_separators(image)
    for (start, selected), end in zip(selectors, starts[1:] + [image.height]):
        if end - start < 4 * text_height:
            continue
        end = next((y for y in separators if start + 4 * text_height < y < end), end)
        region = [line for line in lines if start - text_height / 2 <= line.top < end - text_height / 2]
        if any("전체선택" in normalized(line.text) and line.top < start + 2 * text_height for line in region):
            continue
        footer = next(
            (
                line.top
                for line in region
                if re.search(r"쿠폰선택|총\d+개상품|합계|결제하기|주문하기", normalized(line.text))
            ),
            end,
        )
        region = [line for line in region if line.top < footer]
        price_lines = []
        for line in region:
            if line.top < start + text_height:
                continue
            values = card_price_values(line.text)
            if len(values) == 1 and not _crossed_out(line, image):
                price_lines.append((line, next(iter(values))))
        first_price = price_lines[0][0].top if price_lines else end
        # Ignore thumbnail lettering and lower option/quantity controls in AI names.
        descriptions = [
            line
            for line in region
            if line.top < min(first_price, start + image.width * 0.22)
            and line.right > image.width * 0.25
            and line.left > image.width * 0.12
            and not re.search(r"^(옵션|한달구매|같은판매자)|도착|주문", normalized(line.text))
            and not card_price_values(line.text)
        ]
        if not descriptions:
            continue
        amounts = {price for _, price in price_lines}
        cards.append(CartCard(descriptions, next(iter(amounts)) if len(amounts) == 1 else None, selected))
    return cards
