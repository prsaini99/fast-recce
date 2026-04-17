"""Unit tests for the 99acres HTML extractor.

We never hit the real network here — tests feed canned HTML into the
extraction helpers. Live scraping verification is a manual smoke step.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from app.integrations.acres99_scraper import (
    Acres99Listing,
    _extract_from_html_fallback,
    _extract_from_ld_json,
    _extract_image_gallery,
    _parse_listing_html,
)

pytestmark = pytest.mark.asyncio


_REAL_LISTING_HTML = """
<html>
<head>
  <title>1 BHK House for sale in Alibaug Raigad - 99acres</title>
  <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "SingleFamilyResidence",
      "name": "1 Bedroom House for sale in Alibaug, Raigad",
      "description": "A villa by the sea, 755 sq.ft., ready to move in.",
      "url": "https://www.99acres.com/...-spid-K90245314",
      "address": {
        "@type": "PostalAddress",
        "streetAddress": "Alibaug",
        "addressLocality": "Raigad"
      },
      "geo": {"latitude": "18.65", "longitude": "72.86"}
    }
  </script>
</head>
<body>
  <h1>1 Bedroom House for sale in Alibaug, Raigad</h1>
  <img src="https://static.99acres.com/property/photo1.jpg">
  <img src="https://static.99acres.com/property/photo2.jpg">
  <img src="https://static.99acres.com/universalapp/img/logo.png">
  <img src="https://example.com/unrelated.jpg">
</body>
</html>
"""


def test_extract_from_ld_json_happy_path() -> None:
    soup = BeautifulSoup(_REAL_LISTING_HTML, "lxml")
    fields = _extract_from_ld_json(soup)
    assert fields["title"] == "1 Bedroom House for sale in Alibaug, Raigad"
    assert fields["description"].startswith("A villa by the sea")
    # 99acres' address quirk: neighborhood in streetAddress, city in addressLocality.
    assert fields["locality"] == "Alibaug"
    assert fields["city_hint"] == "Raigad"


def test_extract_from_ld_json_returns_empty_when_missing() -> None:
    soup = BeautifulSoup("<html><body>no ld+json</body></html>", "lxml")
    assert _extract_from_ld_json(soup) == {}


def test_extract_from_html_fallback_prefers_h1() -> None:
    soup = BeautifulSoup("<html><body><h1>Nice 99acres Villa</h1></body></html>", "lxml")
    assert _extract_from_html_fallback(soup)["title"] == "Nice 99acres Villa"


def test_extract_from_html_fallback_ignores_generic_99acres_title() -> None:
    """99acres serves a generic homepage-style title on delisted pages; we
    don't want to persist that as the listing title."""
    html = (
        "<html><head><title>99acres.com - Real Estate India</title></head>"
        "<body></body></html>"
    )
    soup = BeautifulSoup(html, "lxml")
    assert _extract_from_html_fallback(soup) == {}


def test_extract_image_gallery_skips_universalapp_chrome() -> None:
    """Site-chrome images (logo.png under /universalapp/) and non-99acres
    images must be filtered out; only content photos pass."""
    soup = BeautifulSoup(_REAL_LISTING_HTML, "lxml")
    urls = _extract_image_gallery(soup)
    assert urls == [
        "https://static.99acres.com/property/photo1.jpg",
        "https://static.99acres.com/property/photo2.jpg",
    ]


def test_parse_listing_html_returns_listing_on_happy_path() -> None:
    listing = _parse_listing_html(
        _REAL_LISTING_HTML,
        url="https://www.99acres.com/...-spid-K90245314",
        listing_id="K90245314",
    )
    assert listing is not None
    assert listing.source == "99acres"
    assert listing.listing_id == "K90245314"
    assert listing.title.startswith("1 Bedroom House")
    assert listing.city_hint == "Raigad"
    assert listing.locality == "Alibaug"
    assert len(listing.image_urls) == 2


def test_parse_listing_html_returns_none_when_no_title() -> None:
    assert _parse_listing_html(
        "<html><body>no useful data</body></html>",
        url="u",
        listing_id="id",
    ) is None


def test_acres99_listing_dataclass_defaults_source() -> None:
    listing = Acres99Listing(
        listing_id="X123",
        url="https://www.99acres.com/...-spid-X123",
        title="Some Villa",
    )
    assert listing.source == "99acres"
    # Inherited ExternalListing defaults.
    assert listing.image_urls == []
    assert listing.phone is None
