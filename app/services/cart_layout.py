"""Detect repeated cart selectors before asking AI to describe individual cards.

Controls delimit product cards in vertical lists and equal-width grids.
Unknown layouts require confirmation. Coordinates remain request-local.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from statistics import median

from PIL import Image

from app.services.cart_geometry import selectors
from app.services.ocr_layout import Line, group_words, money_values, normalized


@dataclass
class CartCard:
    lines: list[Line]
    price: int | None
    selected: bool | None = True
    box: tuple[int, int, int, int] | None = None
    price_evidence: Line | None = None

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


def cart_selectors(image: Image.Image) -> list[tuple[int, bool | None]]:
    return [(box.top, box.selected) for box in selectors(image)]


def selector_rows(image: Image.Image) -> list[int]:
    return [top for top, _ in cart_selectors(image)]


def _crossed_out(line: Line, image: Image.Image) -> bool:
    box = (max(0, line.left), max(0, line.top), min(image.width, line.right), min(image.height, line.bottom))
    if box[2] <= box[0] or box[3] <= box[1]:
        return True
    with image.crop(box).convert("L") as patch:
        w, h = patch.size
        pixels = patch.load()
        background = median([pixels[0, 0], pixels[w - 1, 0], pixels[0, h - 1], pixels[w - 1, h - 1]])
        # A strike covers most of the price width, unlike individual glyphs.
        return any(
            sum(abs(pixels[x, y] - background) > 35 for x in range(w)) >= w * 0.85
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
    if re.search(r"[$€¥]|USD|JPY|EUR|CNY|개월", compact, re.IGNORECASE):
        return set()
    # A price row can include a discount badge and delivery branding. Unit prices,
    # coupon savings and order totals must not compete with the item selling price.
    if re.search(
        r"쿠폰|배송비|적립|합계|총|주문|결제|할인됨|할인적용|개당|[gGlL]당|할부|카드할인|혜택가|회원가|구독",
        compact,
    ):
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
    labelled = re.findall(r"(?<![\d.,])(\d{3,})\s*원", text)
    if labelled:
        return {int(value) for value in labelled}
    if re.fullmatch(r"(?:₩\s*)?\d[\d,]*\s*[^\d\s]{1,3}", text.strip()):
        return money_values(text)
    return set()


def checkbox_cards(image: Image.Image, lines: list[Line], words: list[Line] | None = None) -> list[CartCard]:
    controls = selectors(image, words if words is not None else lines, word_level=words is not None)
    if not controls:
        return []
    cards = []
    text_height = median([line.bottom - line.top for line in lines] or [20])
    dividers = {}
    column_lines = {}
    for control in controls:
        left, right = control.column_left, control.column_right
        key = (left, right)
        if key not in dividers:
            with image.crop((left, 0, right, image.height)) as column:
                dividers[key] = horizontal_separators(column)
            column_lines[key] = (
                group_words(
                    [
                        w
                        for w in words
                        if left <= w.left
                        and w.right <= right
                        and not any(
                            c.left <= (w.left + w.right) / 2 <= c.right
                            and c.top <= (w.top + w.bottom) / 2 <= c.bottom
                            for c in controls
                        )
                    ]
                )
                if words is not None
                else lines
            )
        start = control.top
        end = min(
            (other.top for other in controls if other.column_left == left and other.top > control.bottom),
            default=image.height,
        )
        if end - start < 2 * text_height:
            continue
        end = next((y for y in dividers[key] if start + 3 * text_height < y < end), end)
        near_header = [
            line for line in lines if start - text_height / 2 <= line.top < start + 2 * text_height
        ]
        if any(
            re.search(r"전체선택|포인트받기|\d+원적립|쿠폰받기", normalized(line.text))
            for line in near_header
        ):
            continue
        region = [
            line
            for line in column_lines[key]
            if start - text_height / 2 <= line.top < end - text_height / 2
            and left <= line.left
            and line.right <= right
        ]
        footer = next(
            (
                line.top
                for line in region
                if re.search(r"쿠폰선택|총\d+개상품|합계|결제하기|주문하기|구매하기", normalized(line.text))
            ),
            end,
        )
        region = [line for line in region if line.top < footer]
        price_lines = []
        for line in region:
            values = card_price_values(line.text)
            if len(values) == 1 and not _crossed_out(line, image):
                price_lines.append((line, next(iter(values))))
        first_price = min((line.top for line, _ in price_lines), default=end)
        descriptions = [
            line
            for line in region
            if line.top <= first_price
            and len(normalized(line.text)) >= 2
            and (
                line.left >= control.right - 2 or line.right <= control.left + 2 or line.top >= control.bottom
            )
            and line.bottom - line.top >= text_height * 0.45
            and not re.search(
                r"^(옵션|한달구매|같은판매자)|도착|주문|배송|합계|선택삭제", normalized(line.text)
            )
            and not card_price_values(line.text)
        ]
        if not descriptions:
            if control.selected is not False:
                cards.append(CartCard([], None, None, (left, start, right, end)))
            continue
        amounts = {price for _, price in price_lines}
        price = next(iter(amounts)) if len(amounts) == 1 else None
        evidence = max(
            (line for line, value in price_lines if value == price),
            key=lambda line: line.bottom - line.top,
            default=None,
        )
        cards.append(CartCard(descriptions, price, control.selected, (left, start, right, end), evidence))
    return cards
