"""Source-router tests for SearchService.

Covers the three routing buckets commercial / residential / generic (defined
by `_classify_route` in `search_service.py`) and graceful degradation when
the Airbnb scraper is not configured.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.airbnb_scraper import AirbnbListing
from app.integrations.google_places import PlaceDetails, PlaceSearchResult
from app.schemas.search import SearchRequest
from app.schemas.source import SourceCreate
from app.services.briefing_service import BriefingService
from app.services.contact_service import ContactService
from app.services.dedup_service import DedupService
from app.services.discovery_service import DiscoveryService
from app.services.property_service import PropertyService
from app.services.query_bank_service import QueryBankService
from app.services.scoring_service import ScoringService
from app.services.search_service import (
    SearchService,
    _classify_route,
)
from app.services.source_service import SourceService

pytestmark = pytest.mark.asyncio


# --- Pure classifier ---


def test_classify_route_commercial() -> None:
    assert _classify_route("resort") == "commercial"
    assert _classify_route("cafe") == "commercial"
    assert _classify_route("boutique_hotel") == "commercial"


def test_classify_route_residential() -> None:
    assert _classify_route("villa") == "residential"
    assert _classify_route("bungalow") == "residential"
    assert _classify_route("farmhouse") == "residential"
    assert _classify_route("heritage_home") == "residential"


def test_classify_route_generic_fallback() -> None:
    # 'other' (from _infer_property_type when no type-word matched) + None
    # + any unknown token all fall through to the generic bucket.
    assert _classify_route("other") == "generic"
    assert _classify_route(None) == "generic"
    assert _classify_route("not_a_real_type") == "generic"


# --- Fakes ---


class FakeGoogleClient:
    def __init__(
        self,
        search_responses: dict[str, list[PlaceSearchResult]] | None = None,
        details_by_id: dict[str, PlaceDetails] | None = None,
    ) -> None:
        self.search_responses = search_responses or {}
        self.details_by_id = details_by_id or {}
        self.text_search_calls: list[str] = []

    async def text_search(self, query: str, **_: Any) -> list[PlaceSearchResult]:
        self.text_search_calls.append(query)
        return self.search_responses.get(query, [])

    async def get_place_details(self, place_id: str) -> PlaceDetails:
        return self.details_by_id[place_id]


class FakeLLMClient:
    async def assess_shoot_fit(self, **_: Any) -> Any:
        from app.integrations.llm import LLMScoreResult
        return LLMScoreResult(score=0.7, reasoning="fake", source="fallback")

    async def assess_visual_uniqueness(self, **_: Any) -> Any:
        from app.integrations.llm import LLMScoreResult
        return LLMScoreResult(score=0.7, reasoning="fake", source="fallback")

    async def generate_brief(self, **_: Any) -> Any:
        from app.integrations.llm import LLMTextResult
        return LLMTextResult(text="fake", source="fallback")


class FakeCrawlerService:
    async def crawl_property(self, candidate_id: str, website_url: str):  # type: ignore[no-untyped-def]
        from app.schemas.crawl import CrawlResult, StructuredData, UnstructuredData
        return CrawlResult(
            candidate_id=candidate_id,
            website_url=website_url,
            pages_fetched=0,
            pages_failed=0,
            snapshot_hash="",
            crawl_status="completed",  # type: ignore[arg-type]
            duration_seconds=0.01,
            errors=[],
            structured_data=StructuredData(),
            unstructured_data=UnstructuredData(),
            media_items=[],
            pages=[],
        )


class FakeDDG:
    def __init__(
        self,
        airbnb_urls: list[str] | None = None,
        magicbricks_urls: list[str] | None = None,
        acres99_urls: list[str] | None = None,
    ) -> None:
        self.airbnb_urls = airbnb_urls or []
        self.magicbricks_urls = magicbricks_urls or []
        self.acres99_urls = acres99_urls or []
        self.find_airbnb_calls: list[str] = []
        self.find_magicbricks_calls: list[str] = []
        self.find_99acres_calls: list[str] = []

    async def find_airbnb_listing_urls(
        self, query: str, *, limit: int = 10
    ) -> list[str]:
        self.find_airbnb_calls.append(query)
        return self.airbnb_urls[:limit]

    async def find_magicbricks_listing_urls(
        self, query: str, *, limit: int = 10
    ) -> list[str]:
        self.find_magicbricks_calls.append(query)
        return self.magicbricks_urls[:limit]

    async def find_99acres_listing_urls(
        self, query: str, *, limit: int = 10
    ) -> list[str]:
        self.find_99acres_calls.append(query)
        return self.acres99_urls[:limit]


class FakeAirbnbScraper:
    source_id = "airbnb"
    source_label = "Airbnb"
    exposes_contacts = False

    def __init__(self, listings_by_url: dict[str, AirbnbListing]) -> None:
        self.listings_by_url = listings_by_url
        self.scrape_calls: list[str] = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> "FakeAirbnbScraper":
        self.entered += 1
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.exited += 1

    async def scrape_listing(self, url: str) -> AirbnbListing | None:
        self.scrape_calls.append(url)
        return self.listings_by_url.get(url)


class FakeMagicBricksScraper:
    """Minimal MagicBricks stand-in. Returns pre-canned MagicBricksListing
    objects keyed by URL so tests can assert per-URL scrape calls."""
    source_id = "magicbricks"
    source_label = "MagicBricks"
    exposes_contacts = False

    def __init__(self, listings_by_url: dict[str, Any]) -> None:
        self.listings_by_url = listings_by_url
        self.scrape_calls: list[str] = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> "FakeMagicBricksScraper":
        self.entered += 1
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.exited += 1

    async def scrape_listing(self, url: str) -> Any:
        self.scrape_calls.append(url)
        return self.listings_by_url.get(url)


class FakeAcres99Scraper:
    """Minimal 99acres stand-in mirroring FakeMagicBricksScraper shape."""
    source_id = "99acres"
    source_label = "99acres"
    exposes_contacts = False

    def __init__(self, listings_by_url: dict[str, Any]) -> None:
        self.listings_by_url = listings_by_url
        self.scrape_calls: list[str] = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> "FakeAcres99Scraper":
        self.entered += 1
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.exited += 1

    async def scrape_listing(self, url: str) -> Any:
        self.scrape_calls.append(url)
        return self.listings_by_url.get(url)


# --- Helpers ---


async def _seed_google_source(db: AsyncSession) -> None:
    await SourceService(db=db).create_source(
        SourceCreate(
            source_name="google_places",
            source_type="api",
            access_policy="allowed",
            crawl_method="api_call",
            base_url="https://places.googleapis.com",
        )
    )


def _place_search(place_id: str, name: str) -> PlaceSearchResult:
    return PlaceSearchResult(
        place_id=place_id,
        name=name,
        address=f"{name}, Alibaug",
        lat=18.6414,
        lng=72.8722,
        types=["lodging"],
        primary_type="lodging",
        rating=4.5,
        review_count=100,
        raw={},
    )


def _place_details(place_id: str, name: str) -> PlaceDetails:
    return PlaceDetails(
        place_id=place_id,
        name=name,
        address=f"{name}, Alibaug, Maharashtra",
        address_components=[
            {"longText": "Alibaug", "shortText": "Alibaug", "types": ["locality"]},
        ],
        lat=18.6414,
        lng=72.8722,
        types=["lodging"],
        primary_type="lodging",
        phone="+91 9876543210",
        website="https://example.com",
        rating=4.5,
        review_count=100,
        google_maps_uri="",
        business_status="OPERATIONAL",
        raw={},
    )


def _airbnb_listing(listing_id: str, title: str, city_hint: str = "Alibaug") -> AirbnbListing:
    return AirbnbListing(
        listing_id=listing_id,
        url=f"https://www.airbnb.com/rooms/{listing_id}",
        title=title,
        description="Nice villa",
        neighborhood="Nagaon",
        city_hint=city_hint,
        amenities=["wifi", "pool"],
        host_first_name="Ayesha",
    )


def _mb_listing(listing_id: str, title: str, city_hint: str = "Alibaug") -> Any:
    from app.integrations.magicbricks_scraper import MagicBricksListing
    return MagicBricksListing(
        listing_id=listing_id,
        url=f"https://www.magicbricks.com/propertyDetails/x&id={listing_id}",
        title=title,
        description="Nice MB villa",
        city_hint=city_hint,
        locality="Kihim",
        amenities=["Parking", "Flooring"],
    )


def _acres99_listing(listing_id: str, title: str, city_hint: str = "Alibaug") -> Any:
    from app.integrations.acres99_scraper import Acres99Listing
    return Acres99Listing(
        listing_id=listing_id,
        url=f"https://www.99acres.com/x-spid-{listing_id}",
        title=title,
        description="Nice 99acres villa",
        city_hint=city_hint,
        locality="Alibaug",
    )


def _make_service(
    db: AsyncSession,
    google: FakeGoogleClient,
    *,
    airbnb_scraper: FakeAirbnbScraper | None = None,
    magicbricks_scraper: FakeMagicBricksScraper | None = None,
    acres99_scraper: FakeAcres99Scraper | None = None,
    ddg: FakeDDG | None = None,
) -> SearchService:
    property_service = PropertyService(db=db)
    contact_service = ContactService(db=db, property_service=property_service)
    discovery_service = DiscoveryService(
        db=db,
        google_client=google,  # type: ignore[arg-type]
        source_service=SourceService(db=db),
        query_bank_service=QueryBankService(db=db),
    )
    llm = FakeLLMClient()
    return SearchService(
        db=db,
        discovery_service=discovery_service,
        crawler_service=FakeCrawlerService(),  # type: ignore[arg-type]
        contact_service=contact_service,
        dedup_service=DedupService(db=db, property_service=property_service),
        property_service=property_service,
        scoring_service=ScoringService(
            db=db,
            llm_client=llm,  # type: ignore[arg-type]
            property_service=property_service,
            contact_service=contact_service,
        ),
        briefing_service=BriefingService(
            db=db,
            llm_client=llm,  # type: ignore[arg-type]
            property_service=property_service,
            contact_service=contact_service,
        ),
        airbnb_scraper=airbnb_scraper,  # type: ignore[arg-type]
        magicbricks_scraper=magicbricks_scraper,  # type: ignore[arg-type]
        acres99_scraper=acres99_scraper,  # type: ignore[arg-type]
        duckduckgo_client=ddg,  # type: ignore[arg-type]
        airbnb_max_listings_per_search=5,
        magicbricks_max_listings_per_search=5,
        acres99_max_listings_per_search=5,
    )


# --- Router integration tests ---


async def test_router_commercial_skips_airbnb(db_session: AsyncSession) -> None:
    """'resorts in Alibaug' → property_type=resort → Google only, no external sources."""
    await _seed_google_source(db_session)
    google = FakeGoogleClient(
        search_responses={"resorts in Alibaug": [_place_search("p_1", "Ocean Resort")]},
        details_by_id={"p_1": _place_details("p_1", "Ocean Resort")},
    )
    ddg = FakeDDG(airbnb_urls=["https://www.airbnb.com/rooms/42"])
    scraper = FakeAirbnbScraper(listings_by_url={})
    mb = FakeMagicBricksScraper(listings_by_url={})
    acres = FakeAcres99Scraper(listings_by_url={})

    service = _make_service(
        db_session, google, airbnb_scraper=scraper,
        magicbricks_scraper=mb, acres99_scraper=acres, ddg=ddg,
    )
    resp = await service.search(SearchRequest(query="resorts in Alibaug"))

    assert resp.inferred_property_type == "resort"
    assert google.text_search_calls == ["resorts in Alibaug"]
    # No external source was triggered for a commercial query.
    assert ddg.find_airbnb_calls == []
    assert ddg.find_magicbricks_calls == []
    assert ddg.find_99acres_calls == []
    assert scraper.scrape_calls == []
    assert mb.scrape_calls == []
    assert acres.scrape_calls == []
    assert scraper.entered == 0
    assert mb.entered == 0
    assert acres.entered == 0


async def test_router_residential_fires_all_sources_in_parallel(
    db_session: AsyncSession,
) -> None:
    """'villa in Alibaug' → Google + Airbnb + MagicBricks + 99acres all fire."""
    await _seed_google_source(db_session)
    google = FakeGoogleClient(
        search_responses={"villa in Alibaug": [_place_search("p_g", "Google Villa")]},
        details_by_id={"p_g": _place_details("p_g", "Google Villa")},
    )
    airbnb_url = "https://www.airbnb.com/rooms/42"
    mb_url = "https://www.magicbricks.com/propertyDetails/x&id=abc"
    acres99_url = "https://www.99acres.com/x-spid-K42"
    ddg = FakeDDG(
        airbnb_urls=[airbnb_url],
        magicbricks_urls=[mb_url],
        acres99_urls=[acres99_url],
    )
    scraper = FakeAirbnbScraper(
        listings_by_url={airbnb_url: _airbnb_listing("42", "Airbnb Villa")},
    )
    mb = FakeMagicBricksScraper(
        listings_by_url={mb_url: _mb_listing("abc", "MB Villa")},
    )
    acres = FakeAcres99Scraper(
        listings_by_url={acres99_url: _acres99_listing("K42", "99acres Villa")},
    )

    service = _make_service(
        db_session, google,
        airbnb_scraper=scraper, magicbricks_scraper=mb, acres99_scraper=acres, ddg=ddg,
    )
    resp = await service.search(SearchRequest(query="villa in Alibaug"))

    assert resp.inferred_property_type == "villa"
    assert google.text_search_calls == ["villa in Alibaug"]
    assert ddg.find_airbnb_calls == ["villa in Alibaug"]
    assert ddg.find_magicbricks_calls == ["villa in Alibaug"]
    assert ddg.find_99acres_calls == ["villa in Alibaug"]
    assert scraper.scrape_calls == [airbnb_url]
    assert mb.scrape_calls == [mb_url]
    assert acres.scrape_calls == [acres99_url]
    assert resp.airbnb_listings_scraped == 1
    assert resp.magicbricks_listings_scraped == 1
    assert resp.acres99_listings_scraped == 1


async def test_router_generic_skips_google_fires_external(db_session: AsyncSession) -> None:
    """'property in Karjat' → no type match → all 3 external sources, Google skipped."""
    await _seed_google_source(db_session)
    google = FakeGoogleClient()  # should never be called
    airbnb_url = "https://www.airbnb.com/rooms/99"
    mb_url = "https://www.magicbricks.com/propertyDetails/x&id=def"
    acres99_url = "https://www.99acres.com/x-spid-W99"
    ddg = FakeDDG(
        airbnb_urls=[airbnb_url],
        magicbricks_urls=[mb_url],
        acres99_urls=[acres99_url],
    )
    scraper = FakeAirbnbScraper(
        listings_by_url={airbnb_url: _airbnb_listing("99", "Karjat Home", "Karjat")},
    )
    mb = FakeMagicBricksScraper(
        listings_by_url={mb_url: _mb_listing("def", "MB Karjat Home", "Karjat")},
    )
    acres = FakeAcres99Scraper(
        listings_by_url={acres99_url: _acres99_listing("W99", "99A Karjat Home", "Karjat")},
    )

    service = _make_service(
        db_session, google,
        airbnb_scraper=scraper, magicbricks_scraper=mb, acres99_scraper=acres, ddg=ddg,
    )
    resp = await service.search(SearchRequest(query="property in Karjat"))

    assert resp.inferred_property_type == "other"
    assert google.text_search_calls == []
    assert ddg.find_airbnb_calls == ["property in Karjat"]
    assert ddg.find_magicbricks_calls == ["property in Karjat"]
    assert ddg.find_99acres_calls == ["property in Karjat"]
    assert scraper.scrape_calls == [airbnb_url]
    assert mb.scrape_calls == [mb_url]
    assert acres.scrape_calls == [acres99_url]


async def test_acres99_runs_when_others_disabled(db_session: AsyncSession) -> None:
    """If only 99acres is enabled, residential/generic still dispatches it."""
    await _seed_google_source(db_session)
    google = FakeGoogleClient(
        search_responses={"villa in Alibaug": [_place_search("p_g", "Google Villa")]},
        details_by_id={"p_g": _place_details("p_g", "Google Villa")},
    )
    acres99_url = "https://www.99acres.com/x-spid-X1"
    ddg = FakeDDG(acres99_urls=[acres99_url])
    acres = FakeAcres99Scraper(
        listings_by_url={acres99_url: _acres99_listing("X1", "Solo 99A Villa")},
    )

    service = _make_service(
        db_session, google,
        airbnb_scraper=None, magicbricks_scraper=None, acres99_scraper=acres, ddg=ddg,
    )
    resp = await service.search(SearchRequest(query="villa in Alibaug"))

    assert resp.acres99_listings_scraped == 1
    assert resp.airbnb_listings_scraped == 0
    assert resp.magicbricks_listings_scraped == 0
    assert acres.scrape_calls == [acres99_url]
    assert ddg.find_airbnb_calls == []
    assert ddg.find_magicbricks_calls == []


async def test_magicbricks_runs_when_airbnb_disabled(db_session: AsyncSession) -> None:
    """If only MagicBricks is enabled, residential/generic searches still run MB.
    The router doesn't require Airbnb to be present."""
    await _seed_google_source(db_session)
    google = FakeGoogleClient(
        search_responses={"villa in Alibaug": [_place_search("p_g", "Google Villa")]},
        details_by_id={"p_g": _place_details("p_g", "Google Villa")},
    )
    mb_url = "https://www.magicbricks.com/propertyDetails/x&id=solo"
    ddg = FakeDDG(magicbricks_urls=[mb_url])
    mb = FakeMagicBricksScraper(
        listings_by_url={mb_url: _mb_listing("solo", "MB Solo Villa")},
    )

    service = _make_service(
        db_session, google,
        airbnb_scraper=None, magicbricks_scraper=mb, ddg=ddg,
    )
    resp = await service.search(SearchRequest(query="villa in Alibaug"))

    assert resp.airbnb_listings_scraped == 0
    assert resp.magicbricks_listings_scraped == 1
    assert mb.scrape_calls == [mb_url]
    assert ddg.find_airbnb_calls == []


async def test_search_degrades_gracefully_when_airbnb_disabled(
    db_session: AsyncSession,
) -> None:
    """AIRBNB_SCRAPE_ENABLED=false → residential query runs Google only, with a
    clear warning in the errors list for the UI to display."""
    await _seed_google_source(db_session)
    google = FakeGoogleClient(
        search_responses={"villa in Alibaug": [_place_search("p_g", "Google Villa")]},
        details_by_id={"p_g": _place_details("p_g", "Google Villa")},
    )

    # No airbnb_scraper, no ddg — scraper is disabled.
    service = _make_service(db_session, google, airbnb_scraper=None, ddg=None)
    resp = await service.search(SearchRequest(query="villa in Alibaug"))

    # Google path still ran and produced a result.
    assert google.text_search_calls == ["villa in Alibaug"]
    assert resp.candidates_new == 1
    # The router surfaces an explicit warning so the UI can tell the user.
    assert any("airbnb" in e.lower() for e in resp.errors)
