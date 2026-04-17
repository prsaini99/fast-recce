"""FastAPI dependency providers. Wire services + external clients here."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db
from app.exceptions import ForbiddenError, UnauthorizedError
from app.integrations.acres99_scraper import Acres99Scraper
from app.integrations.airbnb_scraper import AirbnbScraper
from app.integrations.duckduckgo import DuckDuckGoClient
from app.integrations.external_listing_source import ExternalListingSource
from app.integrations.google_places import GooglePlacesClient
from app.integrations.magicbricks_scraper import MagicBricksScraper
from app.integrations.llm import LLMClient
from app.models.user import User
from app.services.analytics_service import AnalyticsService
from app.services.auth_service import TokenClaims, decode_token
from app.services.briefing_service import BriefingService
from app.services.contact_service import ContactService
from app.services.crawler_service import CrawlerService
from app.services.dedup_service import DedupService
from app.services.discovery_service import DiscoveryService
from app.services.outreach_service import OutreachService
from app.services.property_service import PropertyService
from app.services.query_bank_service import QueryBankService
from app.services.scoring_service import ScoringService
from app.services.search_service import SearchService
from app.services.source_service import SourceService
from app.services.user_service import UserService


# --- Session-backed services ---


async def get_source_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[SourceService, None]:
    yield SourceService(db=db)


async def get_query_bank_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[QueryBankService, None]:
    yield QueryBankService(db=db)


async def get_user_service(
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[UserService, None]:
    yield UserService(db=db)


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


# --- Search (product pivot) ---


async def get_search_service(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AsyncGenerator[SearchService, None]:
    """Constructs the full search pipeline with its own Google + LLM clients.

    Each request gets fresh external clients; we rely on request-scoped
    httpx/genai resources to be cleaned up when the generator completes.
    """
    google_client = GooglePlacesClient(
        api_key=settings.google_places_api_key,
        timeout_seconds=20.0,
    )
    llm_client = LLMClient(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
    )
    # External-listing scrapers (Airbnb + MagicBricks) are each gated on
    # their own env flag. DDG is shared between them — we only construct it
    # when at least one source is enabled. Residential / generic searches
    # degrade gracefully (warning in `errors`) when all are disabled.
    airbnb_scraper: ExternalListingSource | None = None
    magicbricks_scraper: ExternalListingSource | None = None
    acres99_scraper: ExternalListingSource | None = None
    duckduckgo_client: DuckDuckGoClient | None = None

    if settings.airbnb_scrape_enabled:
        airbnb_scraper = AirbnbScraper(
            request_delay_seconds=settings.airbnb_request_delay_seconds,
        )
    if settings.magicbricks_scrape_enabled:
        magicbricks_scraper = MagicBricksScraper(
            request_delay_seconds=settings.magicbricks_request_delay_seconds,
        )
    if settings.acres99_scrape_enabled:
        acres99_scraper = Acres99Scraper(
            request_delay_seconds=settings.acres99_request_delay_seconds,
        )
    if (
        airbnb_scraper is not None
        or magicbricks_scraper is not None
        or acres99_scraper is not None
    ):
        duckduckgo_client = DuckDuckGoClient()
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
            )
    finally:
        await llm_client.close()


# --- Auth dependencies ---


def _parse_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("missing or malformed Authorization header")
    return authorization[7:].strip()


async def get_current_user(
    authorization: str | None = Header(default=None),
    user_service: UserService = Depends(get_user_service),
    settings: Settings = Depends(get_settings),
) -> User:
    token = _parse_bearer_token(authorization)
    claims: TokenClaims | None = decode_token(token, settings=settings)
    if claims is None or claims.token_type != "access":
        raise UnauthorizedError("invalid or expired access token")
    user = await user_service.get(claims.user_id)
    if not user.is_active:
        raise UnauthorizedError("account disabled")
    return user


def require_role(*allowed_roles: str):  # type: ignore[no-untyped-def]
    """Dependency factory enforcing role-based access control."""

    async def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_roles and user.role != "admin":
            raise ForbiddenError(
                f"role '{user.role}' is not allowed for this action"
            )
        return user

    return _checker
