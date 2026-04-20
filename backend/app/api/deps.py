"""FastAPI dependency providers. Wire services + external clients here."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db
from app.integrations.acres99_scraper import Acres99Scraper
from app.integrations.airbnb_scraper import AirbnbScraper
from app.integrations.duckduckgo import DuckDuckGoClient
from app.integrations.external_listing_source import ExternalListingSource
from app.integrations.google_places import GooglePlacesClient
from app.integrations.llm import LLMClient
from app.integrations.magicbricks_scraper import MagicBricksScraper
from app.services.analytics_service import AnalyticsService
from app.services.briefing_service import BriefingService
from app.services.contact_service import ContactService
from app.services.crawler_service import CrawlerService
from app.services.dedup_service import DedupService
from app.services.discovery_service import DiscoveryService
from app.services.outreach_service import OutreachService
from app.services.property_service import PropertyService
from app.services.query_bank_service import QueryBankService
from app.services.scoring_service import ScoringService
from app.services.search_history_service import SearchHistoryService
from app.services.search_job_service import SearchJobService
from app.services.search_service import SearchService
from app.services.source_service import SourceService


# --- Session-backed services ---


async def get_source_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[SourceService, None]:
    yield SourceService(db=db)


async def get_query_bank_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[QueryBankService, None]:
    yield QueryBankService(db=db)


async def get_property_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[PropertyService, None]:
    yield PropertyService(db=db)


async def get_outreach_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[OutreachService, None]:
    yield OutreachService(db=db)


async def get_analytics_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[AnalyticsService, None]:
    yield AnalyticsService(db=db)


async def get_scoring_service(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AsyncGenerator[ScoringService, None]:
    """Scoring runs LLM calls, so we need an LLM client alongside the DB."""
    llm_client = LLMClient(api_key=settings.gemini_api_key, model=settings.gemini_model)
    try:
        property_service = PropertyService(db=db)
        contact_service = ContactService(db=db, property_service=property_service)
        yield ScoringService(
            db=db,
            llm_client=llm_client,
            property_service=property_service,
            contact_service=contact_service,
        )
    finally:
        await llm_client.close()


async def get_briefing_service(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AsyncGenerator[BriefingService, None]:
    llm_client = LLMClient(api_key=settings.gemini_api_key, model=settings.gemini_model)
    try:
        property_service = PropertyService(db=db)
        contact_service = ContactService(db=db, property_service=property_service)
        yield BriefingService(
            db=db,
            llm_client=llm_client,
            property_service=property_service,
            contact_service=contact_service,
        )
    finally:
        await llm_client.close()


async def get_search_history_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[SearchHistoryService, None]:
    yield SearchHistoryService(db=db)


async def get_search_job_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[SearchJobService, None]:
    yield SearchJobService(db=db)


# --- Search ---


@asynccontextmanager
async def build_search_service(
    db: AsyncSession,
) -> AsyncGenerator[SearchService, None]:
    """Construct a fully-wired SearchService for the given session.

    Shared between the FastAPI `Depends(get_search_service)` provider and
    the background job runner (which owns its own session, so can't rely
    on FastAPI's dependency graph). Callers are responsible for committing
    or rolling back the session.
    """
    settings = get_settings()
    google_client = GooglePlacesClient(
        api_key=settings.google_places_api_key,
        timeout_seconds=20.0,
    )
    llm_client = LLMClient(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    # Always construct every external scraper. Env flags are now defaults
    # that `SearchService.search()` reads per-request — if the user toggled
    # a source on for a particular query, we still need the client ready.
    airbnb_scraper: ExternalListingSource = AirbnbScraper(
        request_delay_seconds=settings.airbnb_request_delay_seconds,
    )
    magicbricks_scraper: ExternalListingSource = MagicBricksScraper(
        request_delay_seconds=settings.magicbricks_request_delay_seconds,
    )
    acres99_scraper: ExternalListingSource = Acres99Scraper(
        request_delay_seconds=settings.acres99_request_delay_seconds,
    )
    duckduckgo_client: DuckDuckGoClient = DuckDuckGoClient()
    try:
        async with google_client:
            property_service = PropertyService(db=db)
            contact_service = ContactService(db=db, property_service=property_service)
            discovery_service = DiscoveryService(
                db=db,
                google_client=google_client,
                source_service=SourceService(db=db),
                query_bank_service=QueryBankService(db=db),
            )
            crawler_service = CrawlerService()
            dedup_service = DedupService(db=db, property_service=property_service)
            scoring_service = ScoringService(
                db=db,
                llm_client=llm_client,
                property_service=property_service,
                contact_service=contact_service,
            )
            briefing_service = BriefingService(
                db=db,
                llm_client=llm_client,
                property_service=property_service,
                contact_service=contact_service,
            )
            yield SearchService(
                db=db,
                discovery_service=discovery_service,
                crawler_service=crawler_service,
                contact_service=contact_service,
                dedup_service=dedup_service,
                property_service=property_service,
                scoring_service=scoring_service,
                briefing_service=briefing_service,
                airbnb_scraper=airbnb_scraper,
                magicbricks_scraper=magicbricks_scraper,
                acres99_scraper=acres99_scraper,
                duckduckgo_client=duckduckgo_client,
                airbnb_max_listings_per_search=settings.airbnb_max_listings_per_search,
                magicbricks_max_listings_per_search=settings.magicbricks_max_listings_per_search,
                acres99_max_listings_per_search=settings.acres99_max_listings_per_search,
                airbnb_default_enabled=settings.airbnb_scrape_enabled,
                magicbricks_default_enabled=settings.magicbricks_scrape_enabled,
                acres99_default_enabled=settings.acres99_scrape_enabled,
            )
    finally:
        await llm_client.close()


async def get_search_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[SearchService, None]:
    async with build_search_service(db) as service:
        yield service
