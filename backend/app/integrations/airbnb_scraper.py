"""Airbnb scraper — best-effort extraction from public listing pages.

Design goals:
  - Zero cost: no proxies, no CAPTCHA solvers, no paid browser farms.
  - Zero runtime cost: a plain async HTTP GET — no Chromium subprocess,
    no 45s stealth delays, no visible browser windows. Completes in ~2s
    per listing instead of ~50s.
  - Stability: extract data from the embedded `__NEXT_DATA__` / deferred-
    state JSON blob (survives CSS class renames).
  - Graceful failure: `scrape_listing` returns None on 403 / CAPTCHA HTML /
    missing JSON / parse failure — NEVER raises. Caller treats None as skip.

Tradeoff accepted (senior-approved, 2026-04-16):
  Plain HTTP requests get blocked FAR sooner than a real browser —
  expect 20-50 successful fetches before Airbnb serves a CAPTCHA wall or
  403s our IP. That is acceptable because:
    - The project budget forbids paid proxies / CAPTCHA solvers.
    - Playwright on Windows + uvicorn hit an `asyncio` subprocess bug
      that burned more time than it saved.
    - When bans hit, the search degrades to Google-Places-only with a
      clear error message rather than crashing.

Fields we try to extract (best-effort; Airbnb may rename keys):
  listing_id, title, description, neighborhood, city_hint, amenities,
  host_first_name, primary_image_url

What we explicitly do NOT get:
  phone, email, full address — Airbnb hides these on public listing pages.
  The enrichment chain in SearchService handles that via a secondary
  DuckDuckGo search for the villa's own website.
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

import httpx

from app.integrations.external_listing import ExternalListing
from app.integrations.external_listing_source import ScraperBlockedError

logger = logging.getLogger(__name__)

# Headers that make our request look like a real Chrome navigation.
# Airbnb is stricter with obviously-bot requests (empty UA, no Accept, etc.)
# so we mimic the shape a browser sends. This does NOT defeat sophisticated
# bot detection; it just buys a few extra successful requests.
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

# Strings we treat as "blocked" when they appear in the returned HTML body.
_BLOCK_INDICATORS: tuple[str, ...] = (
    "px-captcha",
    "perimeterx",
    "please verify you are a human",
    "security check",
    "access denied",
    "unusual traffic",
)

# Match both old and new Airbnb embeds.
_NEXT_DATA_SELECTORS = (
    "script#data-deferred-state-0",
    "script#data-deferred-state",
    "script#__NEXT_DATA__",
)

# Regex to pull the raw JSON text out of whichever <script> tag Airbnb used.
_JSON_BLOB_RE = re.compile(
    r'<script[^>]*id="(?:data-deferred-state(?:-0)?|__NEXT_DATA__)"[^>]*>'
    r"(?P<json>.*?)</script>",
    re.DOTALL,
)

# Regex for the listing ID in a URL.
_LISTING_ID_RE = re.compile(r"/rooms/(?:plus/)?(\d+)")


@dataclass(kw_only=True)
class AirbnbListing(ExternalListing):
    """Airbnb-specific listing.

    Inherits the source-agnostic fields (listing_id, url, title, city_hint,
    image_urls, ...) from `ExternalListing` and adds a couple of Airbnb-
    only extras. `source` defaults to "airbnb" so existing construction
    sites / tests that don't pass it still work.
    """
    source: str = "airbnb"
    host_first_name: str | None = None
    price_per_night: str | None = None


class AirbnbScraper:
    """Plain-HTTP Airbnb scraper.

    Issues an async `httpx` GET against the listing URL, pulls the
    `__NEXT_DATA__` JSON blob out of the returned HTML, and extracts
    structured fields. No Chromium, no subprocess, no visible window.

    Conforms to `ExternalListingSource` (see external_listing_source.py).

    Usage:
        async with AirbnbScraper() as scraper:
            listing = await scraper.scrape_listing(url)
    """

    # --- ExternalListingSource protocol ---
    source_id: str = "airbnb"
    source_label: str = "Airbnb"
    exposes_contacts: bool = False

    def __init__(
        self,
        *,
        request_delay_seconds: float = 5.0,
        jitter_seconds: float = 2.0,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        self.request_delay_seconds = request_delay_seconds
        self.jitter_seconds = jitter_seconds
        self.request_timeout_seconds = request_timeout_seconds

        self._client: httpx.AsyncClient | None = None
        self._last_request_at: float = 0.0

    async def __aenter__(self) -> "AirbnbScraper":
        self._client = httpx.AsyncClient(
            headers=_BROWSER_HEADERS,
            timeout=self.request_timeout_seconds,
            follow_redirects=True,
            http2=False,  # Airbnb serves HTTP/2 but h2 is an extra dep
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def scrape_listing(self, url: str) -> AirbnbListing | None:
        """Fetch one Airbnb listing URL and extract structured fields.

        Returns None on 403 / CAPTCHA HTML / missing JSON / parse failure —
        never raises. Per-request delay is applied BEFORE the request so
        a rapid loop is naturally throttled.
        """
        if self._client is None:
            raise RuntimeError("AirbnbScraper must be used as an async context manager")

        listing_id_match = _LISTING_ID_RE.search(url)
        if listing_id_match is None:
            logger.warning("Airbnb URL missing /rooms/<id>: %s", url)
            return None
        listing_id = listing_id_match.group(1)

        await self._throttle()

        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.warning("Airbnb GET failed for %s: %s", url, exc)
            return None

        # HARD BLOCK — raise so the source router counts it toward its
        # early-abort threshold. Real "stop hammering" signals.
        if resp.status_code in (403, 429) or resp.status_code >= 500:
            logger.warning(
                "Airbnb blocked us: status %s for %s",
                resp.status_code, url,
            )
            raise ScraperBlockedError(f"Airbnb returned {resp.status_code}")

        html = resp.text
        lower_html = html.lower()
        if any(indicator in lower_html for indicator in _BLOCK_INDICATORS):
            logger.warning("Airbnb CAPTCHA / block wall detected for %s", url)
            raise ScraperBlockedError(f"Airbnb CAPTCHA wall for {url}")

        raw_json = _extract_json_blob(html)
        if raw_json is None:
            logger.warning(
                "Airbnb JSON blob missing for %s — may be block page or layout change",
                url,
            )
            return None

        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            logger.warning("Airbnb JSON parse failed for %s: %s", url, exc)
            return None

        # Airbnb wraps "this listing was removed / is private / errored" as a
        # 200 OK with a payload that has `errorData` set and `sharingConfig`
        # set to None. We detect that and skip — otherwise our DFS fallback
        # picks up the GraphQL error type ("NiobeError") as the title.
        if _is_airbnb_error_payload(data):
            logger.warning(
                "Airbnb listing returned error payload (likely delisted / private): %s",
                url,
            )
            return None

        fields = _extract_fields(data)
        return AirbnbListing(
            listing_id=listing_id,
            url=url,
            raw_top_keys=sorted(data.keys()) if isinstance(data, dict) else [],
            **fields,
        )

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


def _extract_json_blob(html: str) -> str | None:
    """Return the raw JSON text from whichever Airbnb embed is present."""
    match = _JSON_BLOB_RE.search(html)
    if match is None:
        return None
    return match.group("json").strip()


# Field-path candidates — we try each in order. Airbnb has used several
# shapes historically; any one that yields non-empty data wins.
_TITLE_PATHS: tuple[tuple[str, ...], ...] = (
    ("props", "pageProps", "listing", "name"),
    ("props", "pageProps", "listing", "title"),
    ("niobeMinimalClientData",),  # placeholder — real key varies
)

_DESCRIPTION_PATHS: tuple[tuple[str, ...], ...] = (
    ("props", "pageProps", "listing", "description"),
    ("props", "pageProps", "listing", "summary"),
    ("props", "pageProps", "listing", "sectionedDescription", "summary"),
)

_AMENITIES_PATHS: tuple[tuple[str, ...], ...] = (
    ("props", "pageProps", "listing", "amenities"),
    ("props", "pageProps", "listing", "listingAmenities"),
)

_HOST_PATHS: tuple[tuple[str, ...], ...] = (
    ("props", "pageProps", "listing", "primaryHost", "firstName"),
    ("props", "pageProps", "listing", "host", "firstName"),
)

_CITY_PATHS: tuple[tuple[str, ...], ...] = (
    ("props", "pageProps", "listing", "city"),
    ("props", "pageProps", "listing", "location", "city"),
)

_NEIGHBORHOOD_PATHS: tuple[tuple[str, ...], ...] = (
    ("props", "pageProps", "listing", "neighborhood"),
    ("props", "pageProps", "listing", "location", "neighborhood"),
)

_IMAGE_PATHS: tuple[tuple[str, ...], ...] = (
    ("props", "pageProps", "listing", "photos", 0, "xLarge"),
    ("props", "pageProps", "listing", "photos", 0, "url"),
    ("props", "pageProps", "listing", "pictures", 0, "url"),
)


def _extract_fields(data: Any) -> dict[str, Any]:
    """Best-effort extraction of listing fields from the raw JSON blob.

    Modern Airbnb (2025+) ships a Relay/Niobe cache under `niobeClientData`.
    We first try to pull `sharingConfig` out of that (cheap, reliable). If
    that's missing we fall back to the older `props.pageProps.listing.*`
    shape and finally to `_search_any_key` for keys anywhere in the tree.

    Returns a dict of kwargs to pass into AirbnbListing(...). Missing
    fields are simply omitted (the dataclass defaults handle them).
    """
    out: dict[str, Any] = {}

    # Niobe PDP section — most reliable source on the current site.
    sharing = _extract_niobe_sharing_config(data)
    if sharing is not None:
        title = sharing.get("title")
        if isinstance(title, str) and title.strip():
            out["title"] = title.strip()
        location = sharing.get("location")
        if isinstance(location, str) and location.strip():
            out["city_hint"] = location.strip()

    # Image gallery — pulled from the same Niobe payload's `sections`.
    # Falls back to `sharingConfig.imageUrl` when the gallery sections are
    # missing (rare, but worth covering).
    gallery = _extract_image_gallery(data)
    if gallery:
        out["image_urls"] = gallery
        out["primary_image_url"] = gallery[0]
    elif sharing is not None:
        share_image = sharing.get("imageUrl")
        if isinstance(share_image, str) and share_image.strip():
            out["primary_image_url"] = share_image.strip()

    # Older-shape fallbacks (still useful if Airbnb ships alternate embeds).
    if "title" not in out:
        title = _first_non_empty_str(data, _TITLE_PATHS)
        if title:
            out["title"] = title
    if "title" not in out:
        # No DFS fallback here — `_search_any_key("name")` is too greedy and
        # picks up GraphQL type names (e.g. "NiobeError") on error payloads.
        # `scrape_listing` already filters error payloads upstream, so by the
        # time we get here a missing title means a genuine layout shift —
        # better to surface a placeholder than fake data.
        out["title"] = "Airbnb Listing"

    description = _first_non_empty_str(data, _DESCRIPTION_PATHS)
    if description:
        out["description"] = description[:2000]

    amenities = _first_list_of_strings(data, _AMENITIES_PATHS)
    if amenities:
        out["amenities"] = amenities[:50]

    host = _first_non_empty_str(data, _HOST_PATHS)
    if host:
        out["host_first_name"] = host

    if "city_hint" not in out:
        city = _first_non_empty_str(data, _CITY_PATHS)
        if city:
            out["city_hint"] = city

    neighborhood = _first_non_empty_str(data, _NEIGHBORHOOD_PATHS)
    if neighborhood:
        out["neighborhood"] = neighborhood

    if "primary_image_url" not in out:
        image = _first_non_empty_str(data, _IMAGE_PATHS)
        if image:
            out["primary_image_url"] = image

    # Listing specifics — keys live inside the Niobe PDP payload. We walk
    # with a DFS-any-type helper because the page ships dozens of nested
    # overview/structured sections and the exact cache path shifts often.
    person_capacity = _search_any_key_any(data, "personCapacity")
    if isinstance(person_capacity, int) and person_capacity > 0:
        out["max_guests"] = person_capacity
    bedrooms = _search_any_key_any(data, "bedroomCount") or _search_any_key_any(
        data, "bedrooms"
    )
    if isinstance(bedrooms, int) and bedrooms >= 0:
        out["bedrooms"] = bedrooms
    bathrooms = _search_any_key_any(data, "bathrooms") or _search_any_key_any(
        data, "baths"
    )
    if isinstance(bathrooms, (int, float)) and bathrooms >= 0:
        out["bathrooms"] = int(bathrooms)

    # Human-readable nightly price. Airbnb renders it as "₹4,200 per night"
    # under `structuredDisplayPrice.primaryLine.price`; if that section is
    # missing (off-markets, login-walls), skip silently — the card still
    # has a "View on Airbnb" CTA so we don't block the user on it.
    price_display = _extract_airbnb_price(data)
    if price_display:
        out["price_display"] = price_display
        out["price_currency"] = "INR" if "₹" in price_display else None
        out["price_period"] = "night"

    room_subtype = _search_any_key_any(data, "roomTypeCategory") or _search_any_key_any(
        data, "roomType"
    )
    if isinstance(room_subtype, str) and room_subtype.strip():
        out["property_subtype"] = room_subtype.strip()

    return out


def _search_any_key_any(data: Any, key: str, *, max_depth: int = 8) -> Any:
    """DFS for the first non-null value under `key`, any type.

    `_search_any_key` (below) is string-only because it was originally built
    for titles/descriptions. This variant is for numeric fields like
    `personCapacity` and `bedroomCount` — we just need the first non-null
    hit regardless of type.
    """
    if max_depth <= 0:
        return None
    if isinstance(data, dict):
        if key in data and data[key] is not None:
            return data[key]
        for v in data.values():
            found = _search_any_key_any(v, key, max_depth=max_depth - 1)
            if found is not None:
                return found
    elif isinstance(data, list):
        for v in data:
            found = _search_any_key_any(v, key, max_depth=max_depth - 1)
            if found is not None:
                return found
    return None


def _extract_airbnb_price(data: Any) -> str | None:
    """Pull the display price string Airbnb renders on the PDP.

    Two common shapes:
      - `structuredDisplayPrice.primaryLine.price` → e.g. "₹4,200 per night"
      - `structuredDisplayPrice.primaryLine.discountedPrice` → when on sale
    Either one returned as a plain string is good enough for the card.
    """
    primary = _search_any_key_any(data, "primaryLine")
    if isinstance(primary, dict):
        for key in ("discountedPrice", "price", "originalPrice"):
            value = primary.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    # Fallback: some layouts stash a flat `priceString`.
    direct = _search_any_key_any(data, "priceString")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    return None


def _is_airbnb_error_payload(data: Any) -> bool:
    """Detect Airbnb's "delisted / private / errored" 200-OK responses.

    For listings that no longer exist (or never existed publicly) Airbnb
    returns a normal-looking JSON payload but with `sharingConfig: None`
    and a populated `errorData` block. Persisting these would create
    garbage rows ("NiobeError"-titled cards). Treat as a soft scrape miss.
    """
    if not isinstance(data, dict):
        return False
    entries = data.get("niobeClientData")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not (isinstance(entry, list) and len(entry) == 2):
            continue
        metadata = _dig(
            entry[1],
            ("data", "presentation", "stayProductDetailPage",
             "sections", "metadata"),
        )
        if not isinstance(metadata, dict):
            continue
        if metadata.get("errorData") is not None:
            return True
        # Some error payloads have errorData=None but sharingConfig=None too,
        # AND no real sections to extract. That's still useless.
        sections = _dig(
            entry[1],
            ("data", "presentation", "stayProductDetailPage",
             "sections", "sections"),
        )
        if (
            metadata.get("sharingConfig") is None
            and isinstance(sections, list)
            and len(sections) == 0
        ):
            return True
    return False


def _extract_image_gallery(data: Any, *, max_images: int = 20) -> list[str]:
    """Pull image URLs from the Niobe PDP `sections.sections` array.

    Walks every niobe entry, drills into the PDP `sections.sections` list,
    and harvests `baseUrl` from any section whose component type is one
    of HERO_DEFAULT (`previewImages`), PHOTO_TOUR_SCROLLABLE (`mediaItems`),
    or PHOTOS_DEFAULT (also `mediaItems`). Dedups while preserving order
    and caps at `max_images`. Returns an empty list when no gallery is
    present (rare — most listings have at least HERO_DEFAULT).
    """
    if not isinstance(data, dict):
        return []
    entries = data.get("niobeClientData")
    if not isinstance(entries, list):
        return []

    gallery_section_types = {
        "HERO_DEFAULT", "PHOTO_TOUR_SCROLLABLE", "PHOTOS_DEFAULT",
    }
    image_list_keys = ("previewImages", "mediaItems", "images", "photos")
    seen: set[str] = set()
    urls: list[str] = []

    for entry in entries:
        if not (isinstance(entry, list) and len(entry) == 2):
            continue
        sections = _dig(
            entry[1],
            ("data", "presentation", "stayProductDetailPage",
             "sections", "sections"),
        )
        if not isinstance(sections, list):
            continue
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            if sec.get("sectionComponentType") not in gallery_section_types:
                continue
            inner = sec.get("section")
            if not isinstance(inner, dict):
                continue
            for list_key in image_list_keys:
                items = inner.get(list_key)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("baseUrl") or item.get("url")
                    if not isinstance(url, str):
                        continue
                    url = url.strip()
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    urls.append(url)
                    if len(urls) >= max_images:
                        return urls
    return urls


def _extract_niobe_sharing_config(data: Any) -> dict[str, Any] | None:
    """Walk `niobeClientData[*][1].data...sharingConfig` regardless of index.

    `niobeClientData` is a list of `[cache_key, payload]` pairs. We don't
    know which index holds the PDP sections, so we scan all of them and
    return the first `sharingConfig` we find.
    """
    if not isinstance(data, dict):
        return None
    entries = data.get("niobeClientData")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not (isinstance(entry, list) and len(entry) == 2):
            continue
        payload = entry[1]
        sharing = _dig(
            payload,
            ("data", "presentation", "stayProductDetailPage",
             "sections", "metadata", "sharingConfig"),
        )
        if isinstance(sharing, dict):
            return sharing
    return None


def _dig(data: Any, path: tuple[str | int, ...]) -> Any:
    cur: Any = data
    for step in path:
        try:
            cur = cur[step]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def _first_non_empty_str(
    data: Any, paths: tuple[tuple[str | int, ...], ...]
) -> str | None:
    for path in paths:
        value = _dig(data, path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_list_of_strings(
    data: Any, paths: tuple[tuple[str | int, ...], ...]
) -> list[str] | None:
    for path in paths:
        value = _dig(data, path)
        if isinstance(value, list):
            names: list[str] = []
            for item in value:
                if isinstance(item, str) and item.strip():
                    names.append(item.strip())
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("title")
                    if isinstance(name, str) and name.strip():
                        names.append(name.strip())
            if names:
                return names
    return None


def _search_any_key(data: Any, key: str, *, max_depth: int = 6) -> str | None:
    """Depth-limited DFS for the first string value under `key`.

    Used as a last-resort fallback when our known paths all miss. Keeps
    us functional even if Airbnb renames keys — we just tolerate worse
    data quality until an admin updates the paths.
    """
    if max_depth <= 0:
        return None
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        for v in data.values():
            found = _search_any_key(v, key, max_depth=max_depth - 1)
            if found:
                return found
    elif isinstance(data, list):
        for v in data:
            found = _search_any_key(v, key, max_depth=max_depth - 1)
            if found:
                return found
    return None
