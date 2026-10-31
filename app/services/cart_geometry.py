"""Store-independent selector geometry. No merchant names or coordinates.

Bounded connected components support square/round controls in light/dark UIs.
An unknown layout/state is not evidence that all products are selected.
"""

from dataclasses import dataclass
from statistics import median

from PIL import Image

from app.services.ocr_layout import Line, money_values, normalized


@dataclass
class Selector:
    left: int
    top: int
    right: int
    bottom: int
    selected: bool | None
    column_left: int = 0
    column_right: int = 0


def _candidate_selectors(image: Image.Image, *, pale: bool = False) -> list[Selector]:
    with image.convert("RGB") as small:
        small.thumbnail((640, 3500))
        width, height = small.size
        rgb = small.load()
        edge = [sum(rgb[x, y]) / 3 for x in (0, width - 1) for y in range(0, height, max(1, height // 30))]
        dark = median(edge) < 128
        if dark and pale:
            return []
        with small.convert("L") as gray:
            values = gray.tobytes()
        # Bytes/indices rather than a set of pixel tuples keep memory bounded.
        # Empty controls can use very pale borders (e.g. luminance 235-240).
        # Finding the border is separate from deciding whether its centre is
        # checked; the latter still requires substantial contrast below.
        foreground_limit = 70 if dark else 245 if pale else 235
        mask = bytearray(v > foreground_limit if dark else v < foreground_limit for v in values)
        min_size, max_size = max(8, int(width * 0.014)), int(width * 0.12)
        boxes = []
        for seed in range(len(mask)):
            if not mask[seed]:
                continue
            stack = [seed]
            mask[seed] = 0
            left = right = seed % width
            top = bottom = seed // width
            while stack:
                pos = stack.pop()
                x, y = pos % width, pos // width
                left, right, top, bottom = min(left, x), max(right, x), min(top, y), max(bottom, y)
                for nxt in (
                    pos - 1 if x else -1,
                    pos + 1 if x + 1 < width else -1,
                    pos - width if y else -1,
                    pos + width if y + 1 < height else -1,
                ):
                    if nxt >= 0 and mask[nxt]:
                        mask[nxt] = 0
                        stack.append(nxt)
            size, tall = right - left + 1, bottom - top + 1
            if not (min_size <= size <= max_size and 0.78 <= size / tall <= 1.28):
                continue

            def foreground(x: int, y: int) -> bool:
                value = values[y * width + x]
                return value > foreground_limit if dark else value < foreground_limit

            pad = max(2, size // 5)
            # Circle edges span ~80% of the opposite axis in these outer bands.
            sides = [
                sum(
                    any(foreground(x, y) for x in range(left, left + pad + 1)) for y in range(top, bottom + 1)
                ),
                sum(
                    any(foreground(x, y) for x in range(right - pad, right + 1))
                    for y in range(top, bottom + 1)
                ),
                sum(any(foreground(x, y) for y in range(top, top + pad + 1)) for x in range(left, right + 1)),
                sum(
                    any(foreground(x, y) for y in range(bottom - pad, bottom + 1))
                    for x in range(left, right + 1)
                ),
            ]
            if min(sides) < min(size, tall) * 0.65:
                continue
            inset = max(2, size // 4)
            centre = [
                rgb[x, y]
                for x in range(left + inset, right - inset + 1)
                for y in range(top + inset, bottom - inset + 1)
            ]
            outside = [
                rgb[x, y]
                for x, y in (
                    (max(0, left - 2), top),
                    (min(width - 1, right + 2), bottom),
                    (left, max(0, top - 2)),
                    (right, min(height - 1, bottom + 2)),
                )
            ]
            background = tuple(median(p[c] for p in outside) for c in range(3))
            ink = sum(max(abs(p[c] - background[c]) for c in range(3)) > 45 for p in centre) / len(centre)
            state = True if ink >= 0.12 else False if ink <= 0.025 else None
            if left == 0 or top == 0 or right == width - 1 or bottom == height - 1:
                state = None
            sx, sy = image.width / width, image.height / height
            boxes.append(
                Selector(
                    round(left * sx),
                    round(top * sy),
                    round((right + 1) * sx),
                    round((bottom + 1) * sy),
                    state,
                )
            )
    return boxes


def selectors(
    image: Image.Image, lines: list[Line] | None = None, *, word_level: bool = False
) -> list[Selector]:
    boxes = _candidate_selectors(image)
    # A permissive luminance pass also picks up badges and background fragments.
    # Admit ONLY empty outlines from it, keeping existing high-contrast controls.
    for candidate in _candidate_selectors(image, pale=True):
        if candidate.selected is not False:
            continue
        overlaps = any(
            max(0, min(candidate.right, b.right) - max(candidate.left, b.left))
            * max(0, min(candidate.bottom, b.bottom) - max(candidate.top, b.top))
            > 0.6
            * min(
                (candidate.right - candidate.left) * (candidate.bottom - candidate.top),
                (b.right - b.left) * (b.bottom - b.top),
            )
            for b in boxes
        )
        if not overlaps:
            boxes.append(candidate)
    # Internal ticks/dots are not additional selectors.
    boxes = [
        a
        for a in boxes
        if not any(
            a is not b and b.left <= a.left and b.top <= a.top and b.right >= a.right and b.bottom >= a.bottom
            for b in boxes
        )
    ]
    if lines is not None:
        # Reject glyphs embedded in OCR text, and icons without nearby product text.
        boxes = [
            b
            for b in boxes
            if not any(
                (
                    normalized(line.text) not in {"", "v", "o", "i", "x", "0", "1"}
                    if word_level
                    else len(normalized(line.text)) >= 3
                )
                and (
                    line.left <= (b.left + b.right) / 2 <= line.right
                    if word_level
                    else line.left <= b.left + 3 and line.right >= b.right - 3
                )
                and line.top <= (b.top + b.bottom) / 2 <= line.bottom
                for line in lines
            )
            # OCR sometimes drops the upper part of the square-shaped won glyph.
            # A component immediately after a price on the same baseline belongs
            # to that price, even when the OCR word box omits the currency glyph.
            and not any(
                money_values(line.text)
                and 0 <= b.left - line.right <= b.bottom - b.top
                and abs((line.top + line.bottom) / 2 - (b.top + b.bottom) / 2) < (b.bottom - b.top) * 0.6
                for line in lines
            )
        ]
    if not boxes:
        return []
    groups: list[list[Selector]] = []
    for box in sorted(boxes, key=lambda b: b.left):
        group = next(
            (
                g
                for g in groups
                if abs(median(b.left for b in g) - box.left) <= max(8, (box.right - box.left) * 0.35)
                and abs(median(b.right - b.left for b in g) - (box.right - box.left))
                <= (box.right - box.left) * 0.30
            ),
            None,
        )
        if group is None:
            groups.append([box])
        else:
            group.append(box)
    strong = [g for g in groups if len(g) >= 2]
    if strong:
        # Repeated tiny glyphs inside payment/delivery badges are not another
        # product column when much larger, consistent controls sit on an edge.
        edge_groups = [
            g
            for g in strong
            if median(b.left for b in g) < image.width * 0.18
            or median(b.right for b in g) > image.width * 0.82
        ]
        if edge_groups:
            control_size = max(median(b.right - b.left for b in g) for g in edge_groups)
            large = [g for g in edge_groups if median(b.right - b.left for b in g) == control_size]
            strong = [
                g
                for g in strong
                if median(b.right - b.left for b in g) >= control_size * 0.5
                or any(
                    abs(a.top - b.top) < max(a.bottom - a.top, b.bottom - b.top)
                    for controls in large
                    for a in controls
                    for b in g
                )
            ]
    if not strong and len(groups) > 1 and all(len(g) == 1 for g in groups):
        # A one-row grid is validated by the same alignment and pitch checks below.
        strong = groups
    if not strong:
        # A single row is supported only when the control is on a screen edge.
        strong = [
            g
            for g in groups
            if len(groups) == 1 and (g[0].left < image.width * 0.18 or g[0].right > image.width * 0.82)
        ]
    if not strong:
        return []
    strong.sort(key=lambda g: median(b.left for b in g))
    if len(strong) > 1:
        # Separate equal-width grid columns only when their selectors share rows.
        aligned = all(
            any(abs(a.top - b.top) < max(a.bottom - a.top, b.bottom - b.top) for a in strong[0] for b in g)
            for g in strong[1:]
        )
        pitch = image.width / len(strong)
        spaced = all(
            abs(median(b.left for b in strong[i]) - median(b.left for b in strong[i - 1]) - pitch)
            < pitch * 0.2
            for i in range(1, len(strong))
        )
        if not aligned or not spaced:
            # Never silently import only one of multiple ambiguous selector columns.
            return []
    output = []
    for i, group in enumerate(strong):
        for box in group:
            box.column_left = round(image.width * i / len(strong))
            box.column_right = round(image.width * (i + 1) / len(strong))
            output.append(box)
    return sorted(output, key=lambda b: (b.top, b.left))
