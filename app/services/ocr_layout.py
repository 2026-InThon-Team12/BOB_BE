"""Spatial evidence for cart OCR; reject prices borrowed from a different product.

The OCR engine's block traversal order is not guaranteed to be screen order.
Layout lives only in memory and is never stored or logged.
"""

import re
from dataclasses import dataclass
from statistics import median


@dataclass
class Line:
    left: int
    top: int
    right: int
    bottom: int
    text: str


def visual_lines(table: str) -> list[Line]:
    words = []
    for row in table.splitlines()[1:]:
        fields = row.split("\t", 5)
        if len(fields) != 6:
            continue
        try:
            left, top, width, height = map(int, fields[:4])
        except ValueError:
            continue
        if min(left, top) < 0 or min(width, height) <= 0 or not fields[5].strip():
            continue
        words.append(Line(left, top, left + width, top + height, fields[5].strip()))
    rows: list[list[Line]] = []
    for word in sorted(words, key=lambda w: ((w.top + w.bottom) / 2, w.left)):
        row = next(
            (
                group
                for group in reversed(rows[-4:])
                if abs((word.top + word.bottom) / 2 - median((w.top + w.bottom) / 2 for w in group))
                <= 0.5 * min(word.bottom - word.top, median(w.bottom - w.top for w in group))
            ),
            None,
        )
        if row is None:
            rows.append([word])
        else:
            row.append(word)
    lines = []
    for row in rows:
        # Preserve a large column gap instead of concatenating adjacent product cards.
        groups: list[list[Line]] = []
        gap_limit = max(40, 3 * median(w.bottom - w.top for w in row))
        for word in sorted(row, key=lambda w: w.left):
            if not groups or word.left - groups[-1][-1].right > gap_limit:
                groups.append([word])
            else:
                groups[-1].append(word)
        for group in groups:
            lines.append(
                Line(
                    min(w.left for w in group),
                    min(w.top for w in group),
                    max(w.right for w in group),
                    max(w.bottom for w in group),
                    " ".join(w.text for w in group),
                )
            )
    return sorted(lines, key=lambda line: (line.top, line.left))


def normalized(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text.lower())


def name_anchor(name: str, lines: list[Line]) -> int | None:
    target = normalized(name)
    if not target:
        return None
    # Exact textual evidence only; do not infer a title from a neighbouring card.
    matches = [i for i, line in enumerate(lines) if target in normalized(line.text)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None  # Duplicate/ambiguous names require user confirmation of price.
    for start, first in enumerate(lines):
        text = first.text
        previous = first
        count = 1
        for end in range(start + 1, min(start + 8, len(lines))):
            line = lines[end]
            if abs(line.left - first.left) > max(40, 2 * (first.bottom - first.top)):
                continue
            if line.top - previous.bottom > max(40, 2 * (first.bottom - first.top)) or money_values(
                previous.text
            ):
                break
            text += line.text
            if target in normalized(text):
                return start
            previous = line
            count += 1
            if count >= 3:
                break
    return None


def money_values(text: str) -> set[int]:
    if re.search(
        r"배송|합계|총(?:액|금액|결제|상품)|할부|쿠폰|적립|정가|주문|결제", re.sub(r"\s+", "", text)
    ):
        return set()
    # Currency-labelled values, comma-grouped amounts, or an isolated amount.
    values = re.findall(r"(?:₩\s*)(\d[\d,]*)|(?<![\d,])(\d[\d,]*)\s*원", text)
    numbers = [a or b for a, b in values]
    numbers += re.findall(r"(?<![\d,])(\d{1,3}(?:,\d{3})+)(?![\d,%])", text)
    # Korean 원 is sometimes read as 2!/9! attached to an otherwise intact
    # comma-grouped amount. Require the whole line and that specific glyph error.
    won_glyph = re.fullmatch(r"\s*(\d{1,3}(?:,\d{3})+)\s*\d[!.ㅣ)]\s*", text)
    if won_glyph:
        numbers.append(won_glyph[1])
    if re.fullmatch(r"\s*\d{3,}\s*", text):
        numbers.append(text.strip())
    return {int(n.replace(",", "")) for n in numbers if n.replace(",", "").isdigit()}


def grounded_prices(items: list[dict], lines: list[Line]) -> tuple[list[dict], bool]:
    anchors = [name_anchor(item["name"], lines) for item in items]
    checked = []
    rejected = False
    used_prices: set[int] = set()
    for item, anchor in zip(items, anchors):
        item = dict(item)
        price = item["price"]
        candidates = []
        own_amounts = []
        if price is not None and anchor is not None and anchors.count(anchor) == 1:
            title = lines[anchor]
            following = [lines[a].top for a in anchors if a is not None and lines[a].top > title.top]
            stop = min(following, default=float("inf"))
            for index, line in enumerate(lines):
                if not title.top <= line.top < stop or index in used_prices:
                    continue
                margin = max(40, 2 * (title.bottom - title.top))
                if line.right < title.left - margin or line.left > title.right + margin:
                    continue
                values = money_values(line.text)
                if not values:
                    continue
                # When two titles share a visual row, keep their price columns separate.
                peers = [
                    lines[a]
                    for a in anchors
                    if a is not None
                    and a != anchor
                    and abs(lines[a].top - title.top) < title.bottom - title.top
                ]
                if any(abs(line.left - peer.left) <= abs(line.left - title.left) for peer in peers):
                    continue
                own_amounts.append((index, values))
                if price in values:
                    candidates.append(index)
        if price is not None:
            if len(candidates) == 1:
                used_prices.add(candidates[0])
            elif len(own_amounts) == 1 and len(own_amounts[0][1]) == 1:
                # A unique amount visibly inside this title's own card is evidence,
                # not a guessed price. Never fill a model's explicitly unknown price.
                item["price"] = next(iter(own_amounts[0][1]))
                used_prices.add(own_amounts[0][0])
                rejected = True
            else:
                item["price"] = None
                rejected = True
        checked.append((anchor, item))
    # Screen order is stable even if the model changes its array order.
    checked.sort(key=lambda row: (row[0] is None, row[0] if row[0] is not None else 0))
    return [item for _, item in checked], rejected
