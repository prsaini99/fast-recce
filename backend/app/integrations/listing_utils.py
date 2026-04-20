"""Shared parsing helpers for third-party listing scrapers.

MagicBricks and 99acres both emit the same schema.org shapes for price,
bedroom/bathroom counts, and floor area; Airbnb's Niobe payload uses
different keys but still benefits from the same coercion helpers.

Keeping these here means a schema.org drift (say, `unitCode` becoming
`unitText` everywhere) is fixed in one place rather than three.
"""

from __future__ import annotations

import re
from typing import Any


def to_int(value: Any) -> int | None:
    """Best-effort int coercion. Tolerates strings like `"2 BHK"` → 2."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            try:
                return int(match.group())
            except ValueError:
                return None
    return None


def to_float(value: Any) -> float | None:
    """Best-effort float coercion. Strips commas + non-numeric prefixes."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
        if match:
            try:
                return float(match.group())
            except ValueError:
                return None
    return None


def to_sqft(value: float, unit: str) -> float:
    """Normalize a floorSize to square feet.

    Indian real-estate portals mostly render sqft but occasionally carry
    over the metric `sqm`/`m²` unit text when the broker filled in the
    form wrong. We convert when we can identify the unit; otherwise we
    assume sqft (the dominant convention) rather than silently halving
    a 1000 sqft plot to 93 sqm.
    """
    u = (unit or "").replace(" ", "").lower()
    if u in {"sqm", "m2", "m²", "sqmeter", "squaremeter", "squaremetres"}:
        return round(value * 10.7639, 1)
    return round(value, 1)


def format_inr_price(value: float) -> str:
    """Render a numeric INR price using the lakhs/crores shorthand."""
    if value >= 10_000_000:
        return f"₹{value / 10_000_000:.2f} Cr"
    if value >= 100_000:
        return f"₹{value / 100_000:.2f} L"
    return f"₹{int(value):,}"
