"""Shared Korean product-name wording for generated questions."""

import re


ITEM_PARTICLE_RULE = (
    ' When a product name takes the Korean object particle, write "을(를)" '
    'after the name, never bare "을" or "를" (for example, "운동화을(를)").'
)


def normalize_item_particle(question: str, item_name: str) -> str:
    """Normalize only the named product's object particle; keep other prose intact."""
    if not item_name:
        return question
    pattern = (
        r"(?<!\w)(" + re.escape(item_name) + r"[\"'”’」』]?\s*)"
        r"(?:을\(를\)|를\(을\)|[을를])(?![\w(])"
    )
    return re.sub(pattern, lambda match: match[1] + "을(를)", question)
