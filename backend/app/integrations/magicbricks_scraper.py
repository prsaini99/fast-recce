"""MagicBricks scraper — best-effort extraction from public listing pages.

Uses `curl_cffi` (TLS-fingerprint mimicry) instead of plain httpx because
MagicBricks' Akamai edge serves instant 403s to data-center IPs (Render,
AWS, GCP) when the TLS handshake doesn't look like a real Chrome browser.
Same trick as the 99acres scraper. Locally on a residential IP plain
httpx works too, but `curl_cffi` is consistent across both environments.

Design otherwise mirrors AirbnbScraper:
  - Zero cost: no proxies / CAPTCHA solvers / paid services.
  - Zero runtime cost: ~1 second per listing, no Chromium.
  - Stability: prefers the embedded schema.org `RealEstateListing` JSON-LD
    blob (SEO-driven, rarely changes) with `<title>` / `<h1>` fallbacks.
  - Graceful failure: `scrape_listing` returns None on 410 / parse miss;
    raises ScraperBlockedError on 403/429/CAPTCHA so the source router's
    early-abort guard kicks in.

Fields we extract (best-effort):
  listing_id, title, description, locality, city_hint, amenities,
  primary_image_url, image_urls

What MagicBricks does NOT expose publicly (same as Airbnb):
  phone, email — both gated behind a "Get Contact" OTP flow. SearchService
  suppresses those fields in the response for `magicbricks:` rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from app.integrations.external_listing import ExternalListing
from app.integrations.external_listing_source import ScraperBlockedError

logger = logging.getLogger(__name__)


# Request headers shared with AirbnbScraper — looking like a real Chrome
# navigation is enough to pass MagicBricks' (Akamai) bot heuristics today.
_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


# HTML markers that signal the response is a block / error page.
_BLOCK_INDICATORS: tuple[str, ...] = (
    "access denied",
    "just a moment...",
    "please verify you are a human",
    "security check",
    "unusual traffic",
)

# Listing ID lives after "&id=" in the URL (hex string). Extract for
# stable external IDs in the Property table.
_LISTING_ID_RE = re.compile(r"[?&]id=([0-9a-fA-F]{8,})")


@dataclass(kw_only=True)
class MagicBricksListing(ExternalListing):
    """MagicBricks-specific listing. Inherits all source-agnostic fields
    from ExternalListing and defaults `source` to "magicbricks"."""
    source: str = "magicbricks"


class MagicBricksScraper:
    """`curl_cffi`-based MagicBricks scraper.

    Issues an async GET via `curl_cffi.AsyncSession(impersonate="chrome131")`
    so Akamai's TLS-fingerprint bot check (which 403s plain httpx from data
    center IPs like Render's) sees a real-Chrome handshake. Then pulls the
    `RealEstateListing` JSON-LD
    blob out of the HTML (schema.org; stable across UI redesigns), with
    `<title>` / `<h1>` as fallbacks.

    Conforms to `ExternalListingSource`.
    """

    # --- ExternalListingSource protocol ---
    source_id: str = "magicbricks"
    source_label: str = "MagicBricks"
    exposes_contacts: bool = False

    def __init__(
        self,
        *,
        request_delay_seconds: float = 5.0,
        jitter_seconds: float = 2.0,
        request_timeout_seconds: float = 15.0,
        impersonate: str = "chrome131",
    ) -> None:
        self.request_delay_seconds = request_delay_seconds
        self.jitter_seconds = jitter_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.impersonate = impersonate

        self._session: AsyncSession | None = None
        self._last_request_at: float = 0.0

    async def __aenter__(self) -> "MagicBricksScraper":
        # `curl_cffi` impersonates a real Chrome TLS handshake (JA3 + HTTP/2
        # frame ordering). Akamai's bot edge — which is far stricter for data
        # center IPs (Render's Singapore servers were getting instant 403s
        # with plain httpx) — is gated on TLS fingerprint and waves through
        # the real-Chrome handshake. Same trick we use for 99acres.
        self._session = AsyncSession(
            headers=_BROWSER_HEADERS,
            timeout=self.request_timeout_seconds,
            impersonate=self.impersonate,  # type: ignore[arg-type]
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def scrape_listing(self, url: str) -> ExternalListing | None:
        """Fetch one MagicBricks listing URL and extract structured fields.

        Returns None on 410 (delisted) / parse miss; raises ScraperBlockedError
        on 403/429/5xx/CAPTCHA so the source router's early-abort kicks in.
        """
        if self._session is None:
            raise RuntimeError("MagicBricksScraper must be used as an async context manager")

        listing_id_match = _LISTING_ID_RE.search(url)
        if listing_id_match is None:
            logger.warning("MagicBricks URL missing ?id=<hex>: %s", url)
            return None
        listing_id = listing_id_match.group(1)

        await self._throttle()

        try:
            resp = await self._session.get(url, allow_redirects=True)
        except Exception as exc:  # noqa: BLE001 — curl_cffi raises its own hierarchy
            logger.warning("MagicBricks GET failed for %s: %s", url, exc)
            return None

        # HARD BLOCKS — raise so the source router counts them toward its
        # early-abort threshold. 403/429/5xx = IP rate-limited, CAPTCHA
        # HTML = bot wall. These are the real "stop hammering" signals.
        if resp.status_code in (403, 429) or resp.status_code >= 500:
            logger.warning(
                "MagicBricks blocked us: status %s for %s",
                resp.status_code, url,
            )
            raise ScraperBlockedError(
                f"MagicBricks returned {resp.status_code}"
            )

        # SOFT MISSES — return None, do NOT count toward early-abort.
        # 410 Gone / 404 = listing removed from MB's side (very common;
        # DDG's index is often days stale). Harmless.
        if resp.status_code in (404, 410):
            logger.info(
                "MagicBricks listing gone (status %s): %s",
                resp.status_code, url,
            )
            return None

        html = resp.text
        if len(html) < 1000:
            logger.warning("MagicBricks returned short body (%d bytes) for %s", len(html), url)
            return None

        lower_html = html.lower()
        if any(indicator in lower_html for indicator in _BLOCK_INDICATORS):
            logger.warning("MagicBricks CAPTCHA / block wall detected for %s", url)
            raise ScraperBlockedError(f"MagicBricks CAPTCHA wall for {url}")

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
    """Core extraction. Separate from the scraper class so it's easy to
    unit-test against canned HTML fixtures without a running client."""
    soup = BeautifulSoup(html, "lxml")

    fields = _extract_from_ld_json(soup)
    if not fields.get("title"):
        # Fallback: meta description + h1/title (still better than nothing).
        fallback = _extract_from_html_fallback(soup)
        fields = {**fallback, **fields}  # ld+json wins when it has a value

    if not fields.get("title"):
        # Nothing worth persisting.
        return None

    # Image gallery — grab MB CDN URLs from all <img> tags. Dedup, cap.
    gallery = _extract_image_gallery(soup)
    if gallery:
        fields["image_urls"] = gallery
        if not fields.get("primary_image_url"):
            fields["primary_image_url"] = gallery[0]

    return MagicBricksListing(
        listing_id=listing_id,
        url=url,
        **fields,
    )


def _extract_from_ld_json(soup: BeautifulSoup) -> dict[str, Any]:
    """Pull the schema.org `RealEstateListing` JSON-LD block, which is the
    richest structured source on MagicBricks pages. Returns a partial
    kwargs dict for `MagicBricksListing(...)` — caller merges with
    HTML fallbacks."""
    out: dict[str, Any] = {}
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
        except (ValueError, TypeError):
            continue
        type_field = data.get("@type") or data.get("type")
        type_list = type_field if isinstance(type_field, list) else [type_field]
        if "RealEstateListing" not in type_list:
            continue

        # Title / description / url live at the top of the Product blob.
        if isinstance(data.get("name"), str):
            out["title"] = data["name"].strip()
        if isinstance(data.get("description"), str):
            out["description"] = data["description"].strip()[:2000]

        image = data.get("image")
        if isinstance(image, dict) and isinstance(image.get("url"), str):
            out["primary_image_url"] = image["url"]
        elif isinstance(image, str):
            out["primary_image_url"] = image

        # Location / amenities / dimensions / price live on the nested
        # `mainEntity` (@type House/Apartment/ResidentialBuilding).
        main = data.get("mainEntity") or {}
        if isinstance(main, dict):
            address = main.get("address") or {}
            if isinstance(address, dict):
                loc = address.get("addressLocality")
                region = address.get("addressRegion")
                if isinstance(loc, str) and loc.strip():
                    out["locality"] = loc.strip()
                if isinstance(region, str) and region.strip():
                    out["city_hint"] = region.strip()
            amenities_raw = main.get("amenityFeature")
            if isinstance(amenities_raw, list):
                names: list[str] = []
                for a in amenities_raw:
                    if isinstance(a, dict):
                        n = a.get("name")
                        if isinstance(n, str) and n.strip():
                            names.append(n.strip())
                if names:
                    out["amenities"] = names[:50]
            # Bedrooms / bathrooms come back as strings occasionally (JSON-
            # LD authors aren't consistent), so coerce with tolerance.
            bedrooms = _to_int(main.get("numberOfRooms"))
            if bedrooms is not None:
                out["bedrooms"] = bedrooms
            bathrooms = _to_int(
                main.get("numberOfBathroomsTotal")
                or main.get("numberOfBathrooms")
            )
            if bathrooms is not None:
                out["bathrooms"] = bathrooms
            floor_size = main.get("floorSize")
            if isinstance(floor_size, dict):
                area_val = _to_float(floor_size.get("value"))
                unit = (floor_size.get("unitText") or floor_size.get("unitCode") or "").lower()
                if area_val is not None:
                    out["area_sqft"] = _to_sqft(area_val, unit)
            subtype = main.get("@type") or main.get("type")
            if isinstance(subtype, list):
                subtype = subtype[0] if subtype else None
            if isinstance(subtype, str) and subtype.strip():
                out["property_subtype"] = subtype.strip()

        # Pricing. MagicBricks serves it under `offers` / `price` with a
        # `priceCurrency` (usually INR). Value may already be human-readable
        # ("₹1.15 Cr") in some blobs; fall back to a formatted numeric.
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

        out["raw_top_keys"] = sorted(data.keys())
        return out  # first RealEstateListing wins
    return out


# Shared parsing helpers were moved to listing_utils.py so 99acres can
# reuse them without cross-scraper imports. Keep these thin aliases so
# the rest of this file continues to call `_to_int(...)` etc.
from app.integrations.listing_utils import (
    format_inr_price as _format_inr_price,
    to_float as _to_float,
    to_int as _to_int,
    to_sqft as _to_sqft,
)


def _extract_from_html_fallback(soup: BeautifulSoup) -> dict[str, Any]:
    """Shallow fallback — only used when ld+json is missing. Pulls title
    from <h1>, description from the listing body if we can find it."""
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
            if text and text.lower() not in ("oops... something is missing",):
                out["title"] = text
    return out


def _extract_image_gallery(soup: BeautifulSoup, *, max_images: int = 20) -> list[str]:
    """Collect MagicBricks CDN image URLs from <img> tags. Dedup, cap."""
    seen: set[str] = set()
    urls: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not isinstance(src, str):
            continue
        if "staticmb.com" not in src and "magicbricks" not in src:
            continue
        # Skip obvious thumbnails / blank pixels.
        if "thumb" in src.lower() or src.endswith(".svg"):
            continue
        if src in seen:
            continue
        seen.add(src)
        urls.append(src)
        if len(urls) >= max_images:
            break
    return urls
