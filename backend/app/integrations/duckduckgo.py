"""DuckDuckGo search client — used by the public search pipeline.

Finds third-party listing URLs for a free-text query. We can't hit each
site's own search (bot-protected) but Google/DDG have already indexed
them. Used by the source router's residential / generic paths.

Supported sources (one helper each):
  - Airbnb           → find_airbnb_listing_urls
  - MagicBricks      → find_magicbricks_listing_urls
  - 99acres          → find_99acres_listing_urls

(Part 3 removed the chained `find_property_website` step — these listings
are surfaced as discovery-only with a "View on {source} ↗" CTA. Users
inquire via each platform's own messaging instead of us scraping phone/
email from a villa's own website.)

Why DuckDuckGo and not Google:
  - Google HTML scraping triggers CAPTCHA under minor load.
  - DDG's HTML endpoint is bot-tolerant and free with no API key.
  - The `ddgs` library (successor to `duckduckgo-search`, renamed 2025)
    wraps the endpoint and handles retries. `duckduckgo-search` no longer
    works against current DDG — it silently returns 0 results.

All methods are async. The underlying `DDGS` client is synchronous, so we
run it in a thread via `asyncio.to_thread` to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from ddgs import DDGS

logger = logging.getLogger(__name__)


# Regex for a "real" Airbnb listing URL. Covers .com, .co.in, .co.uk, etc.
# Also covers locale subdomains (ar.airbnb.com, es.airbnb.com, ...) — DDG
# often serves those first; we canonicalize to www.airbnb.com downstream.
_AIRBNB_LISTING_URL_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)?airbnb\.[a-z.]{2,6}/rooms/(?:plus/)?(?P<id>\d+)",
    re.IGNORECASE,
)

# MagicBricks listing URLs look like:
#   https://www.magicbricks.com/propertyDetails/<slug>&id=<hex>
#   https://www.magicbricks.com/mbldp/propertyDetails/<slug>&id=<hex>
# We use the hex `id` as the stable listing ID in the Property table.
_MAGICBRICKS_LISTING_URL_RE = re.compile(
    r"^https?://(?:www\.)?magicbricks\.com/(?:mbldp/)?propertyDetails/"
    r".+?[?&]id=(?P<id>[0-9a-fA-F]{8,})",
    re.IGNORECASE,
)

# 99acres listing URLs look like:
#   https://www.99acres.com/<long-slug>-spid-<letter><digits>
# The `spid-<letter><digits>` suffix is the stable listing ID.
_ACRES99_LISTING_URL_RE = re.compile(
    r"^https?://(?:www\.)?99acres\.com/.+?-spid-(?P<id>[A-Z][0-9]{6,})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DDGResult:
    title: str
    href: str
    body: str


class DuckDuckGoClient:
    """Thin async wrapper around `duckduckgo-search`."""

    def __init__(self, per_request_delay_seconds: float = 1.0) -> None:
        self._delay = per_request_delay_seconds

    async def find_airbnb_listing_urls(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[str]:
        """Return up to `limit` deduplicated Airbnb listing URLs.

        Uses `site:airbnb.com/rooms` — the `/rooms` path prefix biases DDG
        toward individual listing pages. Without it, popular cities like
        Mumbai return only category pages (/mumbai-india/stays/villas)
        which are useless for scraping.
        """
        full_query = f"site:airbnb.com/rooms {query}"
        results = await self._search(full_query, max_results=limit * 3)

        urls: list[str] = []
        seen_listing_ids: set[str] = set()
        for r in results:
            match = _AIRBNB_LISTING_URL_RE.match(r.href)
            if match is None:
                continue
            listing_id = match.group("id")
            if listing_id in seen_listing_ids:
                continue
            seen_listing_ids.add(listing_id)
            # Canonicalize to strip query/hash.
            canonical = _canonical_airbnb_url(listing_id)
            urls.append(canonical)
            if len(urls) >= limit:
                break
        return urls

    async def find_magicbricks_listing_urls(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[str]:
        """Return up to `limit` deduplicated MagicBricks listing URLs.

        Uses `site:magicbricks.com/propertyDetails` — the `/propertyDetails`
        path prefix biases DDG toward individual listings rather than
        category/search pages.
        """
        full_query = f"site:magicbricks.com/propertyDetails {query}"
        results = await self._search(full_query, max_results=limit * 3)

        urls: list[str] = []
        seen_listing_ids: set[str] = set()
        for r in results:
            match = _MAGICBRICKS_LISTING_URL_RE.match(r.href)
            if match is None:
                continue
            listing_id = match.group("id").lower()
            if listing_id in seen_listing_ids:
                continue
            seen_listing_ids.add(listing_id)
            # Keep the original URL (the slug helps SEO / readability when
            # the user clicks through). The ID is enough for dedup.
            urls.append(r.href)
            if len(urls) >= limit:
                break
        return urls

    async def find_99acres_listing_urls(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[str]:
        """Return up to `limit` deduplicated 99acres listing URLs.

        Uses `site:99acres.com inurl:spid` — 99acres' individual-listing
        URLs all contain `spid-<id>`. Without this filter, DDG returns
        mostly `/villas-in-<city>-ffid` category pages which have no
        extractable per-listing data.
        """
        full_query = f"site:99acres.com inurl:spid {query}"
        results = await self._search(full_query, max_results=limit * 3)

        urls: list[str] = []
        seen_listing_ids: set[str] = set()
        for r in results:
            match = _ACRES99_LISTING_URL_RE.match(r.href)
            if match is None:
                continue
            listing_id = match.group("id").upper()
            if listing_id in seen_listing_ids:
                continue
            seen_listing_ids.add(listing_id)
            urls.append(r.href)
            if len(urls) >= limit:
                break
        return urls

    # --- Internals ---

    async def _search(self, query: str, *, max_results: int) -> list[DDGResult]:
        await asyncio.sleep(self._delay)

        def _run() -> list[DDGResult]:
            try:
                with DDGS() as ddgs:
                    raw = ddgs.text(query, max_results=max_results)
            except Exception as exc:  # noqa: BLE001 — return empty on DDG failure
                logger.warning("DDG search failed for %r: %s", query, exc)
                return []

            parsed: list[DDGResult] = []
            for item in raw or []:
                href = item.get("href") or item.get("url") or ""
                if not href:
                    continue
                parsed.append(
                    DDGResult(
                        title=str(item.get("title", "")),
                        href=str(href),
                        body=str(item.get("body", "")),
                    )
                )
            return parsed

        return await asyncio.to_thread(_run)


# --- Module-level helpers ---


def _canonical_airbnb_url(listing_id: str) -> str:
    """Normalize an Airbnb listing URL so dedup works across query strings.

    DDG often returns locale subdomains (ar.airbnb.com, es.airbnb.com) that
    serve translated pages; our `__NEXT_DATA__` extractor expects the English
    layout. We always rewrite to `www.airbnb.com` regardless of the TLD DDG
    gave us — the `/rooms/<id>` path is globally unique.
    """
    return f"https://www.airbnb.com/rooms/{listing_id}"
