"""How people refer to things: Nudge (an approved product, by its name), I-5 (an idea not approved yet), T-3 (a
ticket). P-2 still works when typed: it's the product's old number.

Products are named, never numbered, in anything shown. Internally everything uses the row id.
"""

import re

_REF = re.compile(r"^\s*([TPI])?\s*-?\s*(\d+)\s*$", re.IGNORECASE)
_SEP = re.compile(r"\s+[—–]\s+|:\s+|\s+-\s+")      # "Name — tagline", "Name: tagline", "Name - tagline"


def split_title(title):
    parts = _SEP.split((title or "").strip(), 1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def product_ref(product):
    """What a product is called: its name once approved (it has a repo), 'I-<id>' while it's only an idea."""
    if product.get("slug"):
        return split_title(product["title"])[0] or f"I-{product['id']}"
    return f"I-{product['id']}"


def parse_ref(text):
    """'P-2' -> ('P', 2); 'i3' -> ('I', 3); '4' -> ('T', 4) (a bare number means a ticket); None if it isn't one."""
    match = _REF.match(text or "")
    return (((match.group(1) or "T").upper()), int(match.group(2))) if match else None
