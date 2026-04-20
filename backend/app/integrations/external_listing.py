"""Shared dataclass for third-party listings scraped from external sources.

Used by AirbnbScraper, MagicBricksScraper, and any future source (99acres,
NoBroker, Housing.com). The `source` field is the single discriminator
that SearchService uses to drive source-aware behavior — e.g., deciding
which rows to suppress contacts on, which pill label to render.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExternalListing:
    source: str                       # "airbnb" | "magicbricks" | "99acres"
    listing_id: str
    url: str                          # canonical URL pointing back to the source
    title: str
    description: str | None = None
    city_hint: str | None = None
    neighborhood: str | None = None
    locality: str | None = None
    amenities: list[str] = field(default_factory=list)
    primary_image_url: str | None = None
    image_urls: list[str] = field(default_factory=list)
    # Contact fields — populated if a future source exposes them.
    # Today: always empty for airbnb / magicbricks / 99acres.
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    # Listing specifics — populated opportunistically per scraper. Every
    # field is optional because the exposed data varies wildly between
    # sources and between categories on the same source (an Airbnb villa
    # has `max_guests` but no `area_sqft`; a MagicBricks plot has
    # `area_sqft` but no `bedrooms`).
    price_display: str | None = None      # human-readable, e.g. "₹1.15 Cr", "₹4,200/night"
    price_value: float | None = None      # numeric INR equivalent when parseable
    price_currency: str | None = None     # ISO code when known (usually "INR")
    price_period: str | None = None       # "night" / "month" / "total" / None
    bedrooms: int | None = None
    bathrooms: int | None = None
    area_sqft: float | None = None
    max_guests: int | None = None
    property_subtype: str | None = None   # source-native label ("Apartment", "Plot", "Villa")
    # Drift-detection: top-level keys of the raw payload (JSON blob or ld+json).
    raw_top_keys: list[str] = field(default_factory=list)
