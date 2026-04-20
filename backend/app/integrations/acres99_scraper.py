"""99acres scraper — best-effort extraction from public listing pages.

Design mirrors MagicBricksScraper (og / ld+json / BeautifulSoup), BUT uses
`curl_cffi` instead of `httpx` because 99acres' Akamai edge blocks plain
Python httpx at the TLS-fingerprint layer (instant 403 Access Denied,
no JS challenge, just a static deny page). `curl_cffi` impersonates a
real Chrome TLS handshake (JA3 + HTTP/2 frame ordering), which is what
Akamai is gating on. No JavaScript execution required.

Tradeoff accepted (senior-approved, 2026-04-17):
  - `curl_cffi` is one extra dep (pure wheel, ~2 MB) scoped to this one
    module.
  - Akamai can tighten further (e.g., add JS challenges) at any time.
    When that happens this scraper will stop working. The source router's
    early-abort guard will surface it clearly — we do not fall back to
    Playwright here.

Fields we extract (best-effort):
  listing_id, title, description, locality, city_hint, amenities,
  primary_image_url, image_urls

What 99acres does NOT expose publicly (same as Airbnb / MagicBricks):
  phone, email — gated behind "View Phone Number" OTP flow. SearchService
  suppresses those fields for `99acres:` rows at the response layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass

from app.integrations.listing_utils import (
    format_inr_price as _format_inr_price,
    to_float as _to_float,
    to_int as _to_int,
    to_sqft as _to_sqft,
)
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from app.integrations.external_listing import ExternalListing
from app.integrations.external_listing_source import ScraperBlockedError

logger = logging.getLogger(__name__)


# `curl_cffi` passes Chrome-like headers automatically when impersonate is
# set, but we still add Accept-Language for consistency with our other
# scrapers. The key anti-bot lever is the TLS fingerprint, not headers.
_EXTRA_HEADERS: dict[str, str] = {
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

_BLOCK_INDICATORS: tuple[str, ...] = (
    "access denied",
    "just a moment...",
    "please verify you are a human",
    "security check",
    "unusual traffic",
)

# 99acres listing URLs look like:
#   /<slug>-spid-<letter><digits>
# We extract the ID (letter + digits; case-insensitive) as the stable
# external identifier for the Property row.
_LISTING_ID_RE = re.compile(r"spid-([A-Z][0-9]{6,})", re.IGNORECASE)


@dataclass(kw_only=True)
class Acres99Listing(ExternalListing):
    """99acres-specific listing. Defaults `source` to "99acres"."""
    source: str = "99acres"


class Acres99Scraper:
    """curl_cffi-based 99acres scraper.

    Uses `curl_cffi.AsyncSession(impersonate="chrome131")` to defeat
    Akamai's TLS-fingerprint block. Drop-in shape-equivalent to
    `MagicBricksScraper` — same BeautifulSoup + ld+json extraction,
    same soft-skip vs hard-block semantics.

    Conforms to `ExternalListingSource`.
    """

    # --- ExternalListingSource protocol ---
    source_id: str = "99acres"
    source_label: str = "99acres"
    exposes_contacts: bool = False

    def __init__(
        self,
        *,
        request_delay_seconds: float = 5.0,
        jitter_seconds: float = 2.0,
        request_timeout_seconds: float = 20.0,
        impersonate: str = "chrome131",
    ) -> None:
        self.request_delay_seconds = request_delay_seconds
        self.jitter_seconds = jitter_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.impersonate = impersonate

        self._session: AsyncSession | None = None
        self._last_request_at: float = 0.0

    async def __aenter__(self) -> "Acres99Scraper":
        self._session = AsyncSession(
            headers=_EXTRA_HEADERS,
            timeout=self.request_timeout_seconds,
            impersonate=self.impersonate,  # type: ignore[arg-type]
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def scrape_listing(self, url: str) -> ExternalListing | None:
        """Fetch one 99acres listing URL and extract structured fields."""
        if self._session is None:
            raise RuntimeError("Acres99Scraper must be used as an async context manager")

        listing_id_match = _LISTING_ID_RE.search(url)
        if listing_id_match is None:
            logger.warning("99acres URL missing spid-<id>: %s", url)
            return None
        listing_id = listing_id_match.group(1).upper()

        await self._throttle()

        try:
            resp = await self._session.get(url, allow_redirects=True)
        except Exception as exc:  # noqa: BLE001 — curl_cffi raises its own hierarchy
            logger.warning("99acres GET failed for %s: %s", url, exc)
            return None

        # HARD BLOCKS — raise so the source router counts them toward
        # early-abort. 403/429/5xx = TLS-mimicry stopped working, CAPTCHA
        # HTML = Akamai escalated to JS challenge.
        if resp.status_code in (403, 429) or resp.status_code >= 500:
            logger.warning(
                "99acres blocked us: status %s for %s",
                resp.status_code, url,
            )
            raise ScraperBlockedError(f"99acres returned {resp.status_code}")

        # SOFT MISSES — return None, do NOT count toward early-abort.
        if resp.status_code in (404, 410):
            logger.info(
                "99acres listing gone (status %s): %s",
                resp.status_code, url,
            )
            return None

        html = resp.text
        if len(html) < 1000:
            logger.warning("99acres returned short body (%d bytes) for %s", len(html), url)
            return None

        lower_html = html.lower()
        if any(indicator in lower_html for indicator in _BLOCK_INDICATORS):
            logger.warning("99acres CAPTCHA / block wall detected for %s", url)
            raise ScraperBlockedError(f"99acres CAPTCHA wall for {url}")

        return _parse_listing_html(html, url=url, listing_id=listing_id)

    async def _throttle(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_at
        target = self.request_delay_seconds + random.uniform(
            -self.jitter_seconds, self.jitter_seconds
        )
        target = max(target, 1.0)
        if self._last_request_at > 0 and elapsed < target:
            await asyncio.sleep(target - elapsed)
        self._last_request_at = time.monotonic()


# --- Module-private helpers ---


def _parse_listing_html(
    html: str, *, url: str, listing_id: str,
) -> ExternalListing | None:
    """Extraction. Separate from the scraper class for easy unit testing."""
    soup = BeautifulSoup(html, "lxml")

    fields = _extract_from_ld_json(soup)
    if not fields.get("title"):
        fallback = _extract_from_html_fallback(soup)
        fields = {**fallback, **fields}

    if not fields.get("title"):
        return None

    gallery = _extract_image_gallery(soup)
    if gallery:
        fields["image_urls"] = gallery
        if not fields.get("primary_image_url"):
            fields["primary_image_url"] = gallery[0]

    return Acres99Listing(
        listing_id=listing_id,
        url=url,
        **fields,
    )


def _extract_from_ld_json(soup: BeautifulSoup) -> dict[str, Any]:
    """Pull the schema.org listing block. 99acres emits types like
    `SingleFamilyResidence`, `Apartment`, `House` — we accept any of the
    real-estate-adjacent types."""
    accepted_types = {
        "SingleFamilyResidence", "Apartment", "House",
        "Residence", "RealEstateListing", "Product",
    }
    out: dict[str, Any] = {}
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except (ValueError, TypeError):
            continue
        type_field = data.get("@type") or data.get("type")
        type_list = type_field if isinstance(type_field, list) else [type_field]
        if not any(t in accepted_types for t in type_list if isinstance(t, str)):
            continue

        if isinstance(data.get("name"), str) and data["name"].strip():
            out["title"] = data["name"].strip()
        if isinstance(data.get("description"), str):
            out["description"] = data["description"].strip()[:2000]

        # Primary image — 99acres sometimes has this, often doesn't.
        image = data.get("image")
        if isinstance(image, dict) and isinstance(image.get("url"), str):
            out["primary_image_url"] = image["url"]
        elif isinstance(image, str):
            out["primary_image_url"] = image
        elif isinstance(image, list) and image:
            first = image[0]
            if isinstance(first, str):
                out["primary_image_url"] = first
            elif isinstance(first, dict) and isinstance(first.get("url"), str):
                out["primary_image_url"] = first["url"]

        address = data.get("address") or {}
        if isinstance(address, dict):
            # 99acres puts the neighborhood in `streetAddress` and the
            # parent city in `addressLocality` (the opposite of schema.org
            # intent, but it's what they ship). Mirror into our fields.
            street = address.get("streetAddress")
            locality = address.get("addressLocality")
            if isinstance(street, str) and street.strip():
                out["locality"] = street.strip()
            if isinstance(locality, str) and locality.strip():
                out["city_hint"] = locality.strip()

        # Bedrooms / bathrooms / floor area live directly on the listing
        # block (not a nested `mainEntity` like MagicBricks).
        bedrooms = _to_int(data.get("numberOfRooms"))
        if bedrooms is not None:
            out["bedrooms"] = bedrooms
        bathrooms = _to_int(
            data.get("numberOfBathroomsTotal") or data.get("numberOfBathrooms")
        )
        if bathrooms is not None:
            out["bathrooms"] = bathrooms
        floor_size = data.get("floorSize")
        if isinstance(floor_size, dict):
            area_val = _to_float(floor_size.get("value"))
            unit = (
                floor_size.get("unitText")
                or floor_size.get("unitCode")
                or ""
            )
            if area_val is not None:
                out["area_sqft"] = _to_sqft(area_val, str(unit))

        # Pricing — schema.org `offers.price` + currency. Some listings
        # already carry a pretty string like "₹1.15 Cr"; fall back to a
        # formatted numeric for the rest.
        offers = data.get("offers") or {}
        if isinstance(offers, dict):
            currency = offers.get("priceCurrency")
            price_raw = offers.get("price")
            price_num = _to_float(price_raw)
            if price_num is not None:
                out["price_value"] = price_num
                out["price_currency"] = (
                    currency.strip() if isinstance(currency, str) else "INR"
                )
                out["price_display"] = _format_inr_price(price_num)
            elif isinstance(price_raw, str) and price_raw.strip():
                out["price_display"] = price_raw.strip()

        # Native subtype label (e.g. "Apartment", "SingleFamilyResidence")
        # from the first matching @type entry — nicer in the UI than our
        # generic "villa" fallback on the canonical property row.
        subtype = type_field if isinstance(type_field, str) else None
        if subtype is None and isinstance(type_list, list):
            subtype = next(
                (t for t in type_list if isinstance(t, str) and t in accepted_types),
                None,
            )
        if subtype:
            out["property_subtype"] = subtype

        out["raw_top_keys"] = sorted(data.keys())
        return out  # first matching block wins
    return out


def _extract_from_html_fallback(soup: BeautifulSoup) -> dict[str, Any]:
    """Shallow fallback. Only used when ld+json is missing."""
    out: dict[str, Any] = {}
    h1 = soup.find("h1")
    if h1:
        text = h1.get_text(" ", strip=True)
        if text:
            out["title"] = text
    if "title" not in out:
        title_tag = soup.find("title")
        if title_tag:
            text = title_tag.get_text(strip=True)
            # 99acres serves a generic "99acres.com - Real Estate..." on
            # delisted pages; filter that out so we don't persist it.
            if text and "99acres.com" not in text.lower()[:20]:
                out["title"] = text
    return out


def _extract_image_gallery(soup: BeautifulSoup, *, max_images: int = 20) -> list[str]:
    """Collect 99acres CDN image URLs from <img> tags. Dedup, cap.

    NOTE: 99acres lazy-loads listing photos via JS, so plain HTML scrape
    often only yields site-chrome assets (logos, icons). That's fine —
    the card falls back to the initials placeholder. Coverage improves
    naturally when listings have a gallery rendered server-side.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not isinstance(src, str):
            continue
        # Only keep hosted content / property photos — skip universalapp
        # site-chrome (logos, icons, nav).
        if "static.99acres.com" not in src and "99acres.com" not in src:
            continue
        if "/universalapp/" in src or "thumb" in src.lower() or src.endswith(".svg"):
            continue
        if src in seen:
            continue
        seen.add(src)
        urls.append(src)
        if len(urls) >= max_images:
            break
    return urls
