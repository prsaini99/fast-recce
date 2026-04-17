"""SearchService — user-facing live search orchestrator (product pivot).

Flow per user search:
  1. Infer (city, property_type) from the free-text query if not provided.
  2. DiscoveryService.discover_ad_hoc → Google Places Text Search + Details.
  3. For each new candidate: crawl → contacts → dedup → upsert property → score → brief.
  4. Query the properties table filtered by (city, property_type) sorted by score.
  5. Return top N results with sub-scores + features.

Per-item failures are caught and recorded; a failed crawl on one place never
blocks the user from seeing the rest of the results.

The review/outreach workflow is NOT touched by this service. Every upserted
property defaults to status='new' and is never auto-approved.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discovery import DiscoveryCandidate
from app.schemas.crawl import ExtractedContact
from app.schemas.property import PropertyUpsertFromCandidate
from app.schemas.search import (
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    SearchSubScore,
)
from app.services.briefing_service import BriefingService
from app.services.contact_service import ContactService
from app.services.crawler_service import CrawlerService
from app.services.dedup_service import DedupService
from app.services.discovery_service import DiscoveryService
from app.services.property_service import PropertyService
from app.services.scoring_service import ScoringService

from app.integrations.external_listing_source import ScraperBlockedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.integrations.external_listing import ExternalListing
    from app.integrations.external_listing_source import ExternalListingSource
    from app.integrations.duckduckgo import DuckDuckGoClient

logger = logging.getLogger(__name__)


# Property types that are listed businesses on Google Places.
_COMMERCIAL_TYPES: frozenset[str] = frozenset({
    "boutique_hotel", "resort", "cafe", "restaurant", "banquet_hall",
    "club_lounge", "office_space", "coworking_space", "school_campus",
    "theatre_studio", "rooftop_venue", "warehouse", "industrial_shed",
})

# Property types that are mostly residential rentals — Airbnb has lots
# of these, Google Places has some, we want both sources.
_RESIDENTIAL_TYPES: frozenset[str] = frozenset({
    "villa", "bungalow", "farmhouse", "heritage_home",
})

# Source prefixes on `google_place_id` → human-readable label for the
# "View on {label} ↗" pill in the UI. None for Google Places / legacy rows.
_SOURCE_LABELS: dict[str, str] = {
    "airbnb": "Airbnb",
    "magicbricks": "MagicBricks",
    "99acres": "99acres",
}

# External sources whose public pages don't expose phone/email/website.
# `_to_result_item` suppresses those fields for rows matching these
# prefixes regardless of what's stored in the DB (avoids showing stale
# cruft from older scraping eras).
_SOURCES_WITHOUT_PUBLIC_CONTACTS: frozenset[str] = frozenset({
    "airbnb", "magicbricks", "99acres",
})


class SearchService:
    def __init__(
        self,
        db: AsyncSession,
        discovery_service: DiscoveryService,
        crawler_service: CrawlerService,
        contact_service: ContactService,
        dedup_service: DedupService,
        property_service: PropertyService,
        scoring_service: ScoringService,
        briefing_service: BriefingService,
        airbnb_scraper: "ExternalListingSource | None" = None,
        magicbricks_scraper: "ExternalListingSource | None" = None,
        acres99_scraper: "ExternalListingSource | None" = None,
        duckduckgo_client: "DuckDuckGoClient | None" = None,
        airbnb_max_listings_per_search: int = 10,
        magicbricks_max_listings_per_search: int = 5,
        acres99_max_listings_per_search: int = 5,
    ) -> None:
        self.db = db
        self.discovery_service = discovery_service
        self.crawler_service = crawler_service
        self.contact_service = contact_service
        self.dedup_service = dedup_service
        self.property_service = property_service
        self.scoring_service = scoring_service
        self.briefing_service = briefing_service
        self.airbnb_scraper = airbnb_scraper
        self.magicbricks_scraper = magicbricks_scraper
        self.acres99_scraper = acres99_scraper
        self.ddg_client = duckduckgo_client
        self.airbnb_max_listings = airbnb_max_listings_per_search
        self.magicbricks_max_listings = magicbricks_max_listings_per_search
        self.acres99_max_listings = acres99_max_listings_per_search

    async def search(self, request: SearchRequest) -> SearchResponse:
        start = time.monotonic()
        errors: list[str] = []

        # Hints only — we no longer gate on city inference. Google's geocoder
        # handles "resorts in Bandra", "farmhouse Karjat", anywhere worldwide.
        # We keep the inference to:
        #   (a) pick a sensible property_type fallback for Google results
        #       that don't map cleanly to our types
        #   (b) route to Google / Airbnb / both based on property_type
        #   (c) score `location_demand` higher for known shoot-hub cities
        city_hint = request.city or _infer_city(request.query)
        property_type_hint = request.property_type or _infer_property_type(request.query)
        location_hint = _extract_location_hint(request.query) or city_hint or ""

        route = _classify_route(property_type_hint)

        # Zero-stats placeholders — filled in by whichever branches actually run.
        candidates_discovered = 0
        candidates_new = 0
        candidates_skipped_known = 0
        candidates_filtered_non_shoot = 0
        airbnb_listings_scraped = 0
        magicbricks_listings_scraped = 0
        acres99_listings_scraped = 0
        fresh_ids: list[Any] = []  # IDs of properties just persisted in this request

        # 1. Dispatch to the right sources in parallel.
        tasks: list[Any] = []
        any_external_needed = route in {"residential", "generic"}

        if route in {"commercial", "residential"}:
            tasks.append(
                self._run_google_places_path(request, city_hint, property_type_hint)
            )

        # External-listing sources (Airbnb, MagicBricks) fire for residential
        # + generic routes. Each is gated on its own feature flag via a
        # non-None scraper injection.
        if any_external_needed and self.ddg_client is not None:
            if self.airbnb_scraper is not None:
                tasks.append(
                    self._run_external_source_path(
                        request, location_hint,
                        source=self.airbnb_scraper,
                        url_finder=self.ddg_client.find_airbnb_listing_urls,
                        max_listings=self.airbnb_max_listings,
                    )
                )
            if self.magicbricks_scraper is not None:
                tasks.append(
                    self._run_external_source_path(
                        request, location_hint,
                        source=self.magicbricks_scraper,
                        url_finder=self.ddg_client.find_magicbricks_listing_urls,
                        max_listings=self.magicbricks_max_listings,
                    )
                )
            if self.acres99_scraper is not None:
                tasks.append(
                    self._run_external_source_path(
                        request, location_hint,
                        source=self.acres99_scraper,
                        url_finder=self.ddg_client.find_99acres_listing_urls,
                        max_listings=self.acres99_max_listings,
                    )
                )

        if not tasks:
            # Unreachable via `_classify_route` in practice. Defensive.
            tasks.append(
                self._run_google_places_path(request, city_hint, property_type_hint)
            )

        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                errors.append(f"source task failed: {outcome}")
                continue
            candidates_discovered += outcome["candidates_discovered"]
            candidates_new += outcome["candidates_new"]
            candidates_skipped_known += outcome["candidates_skipped_known"]
            candidates_filtered_non_shoot += outcome["candidates_filtered_non_shoot"]
            # Per-source bucket (external paths set `source_id` + `listings_scraped`).
            src = outcome.get("source_id")
            scraped = outcome.get("listings_scraped", 0)
            if src == "airbnb":
                airbnb_listings_scraped += scraped
            elif src == "magicbricks":
                magicbricks_listings_scraped += scraped
            elif src == "99acres":
                acres99_listings_scraped += scraped
            errors.extend(outcome["errors"])
            fresh_ids.extend(outcome.get("ingested_ids") or [])

        # Warn when the route wanted external sources but none were configured.
        if any_external_needed and self.ddg_client is None:
            errors.append(
                "External-listing scrapers are disabled. Residential / generic "
                "queries only surface Google Places results. Set "
                "AIRBNB_SCRAPE_ENABLED=true (and/or MAGICBRICKS_SCRAPE_ENABLED=true) "
                "to enable them."
            )
        elif any_external_needed and (
            self.airbnb_scraper is None
            and self.magicbricks_scraper is None
            and self.acres99_scraper is None
        ):
            errors.append(
                "No external-listing scrapers are enabled. Flip one of "
                "AIRBNB_SCRAPE_ENABLED / MAGICBRICKS_SCRAPE_ENABLED / "
                "ACRES99_SCRAPE_ENABLED in .env to broaden residential / "
                "generic results."
            )

        # 2. Load ranked results. Two sources merged:
        #
        #    (a) Fresh-scraped rows from THIS request, loaded by the IDs we
        #        just collected. Critical for Airbnb listings whose `city`
        #        comes from Airbnb's metadata (e.g. "Mumbai") and won't
        #        match the user's free-text hint (e.g. "kandivali") — they
        #        would be invisible to the location-hint search alone.
        #
        #    (b) Hint-matched rows from the DB — fuzzy match against city
        #        + locality + canonical_name. Surfaces previously-scraped
        #        results from earlier searches plus any Google rows whose
        #        `city` happens to match the hint.
        #
        # Fresh rows take precedence in the result order; the hint set fills
        # the remaining slots. Dedup on id so a row that was just scraped
        # AND matches the hint doesn't appear twice.
        # Hint lookup is filtered by the route's allowed property_types so
        # cached commercial rows (cafes, restaurants) don't leak into a
        # generic/residential search and vice-versa. Without this filter,
        # "property in kandivali" would surface every cached cafe in
        # Kandivali — accurate location match, wrong intent.
        hint_type_filter = _allowed_types_for_route(route)

        fresh_items = (
            await self.property_service.list_by_ids(fresh_ids)
            if fresh_ids else []
        )
        hint_items = (
            await self.property_service.find_by_location_hint(
                city_hint=location_hint,
                limit=request.max_results,
                property_types=hint_type_filter,
            )
            if location_hint else []
        )

        seen_ids: set[Any] = set()
        merged: list[Any] = []
        for row in (*fresh_items, *hint_items):
            if row.id in seen_ids:
                continue
            seen_ids.add(row.id)
            merged.append(row)
            if len(merged) >= request.max_results:
                break

        results = [self._to_result_item(row) for row in merged]

        return SearchResponse(
            query=request.query,
            inferred_city=city_hint,
            inferred_property_type=property_type_hint,
            results=results,
            candidates_discovered=candidates_discovered,
            candidates_new=candidates_new,
            candidates_skipped_known=candidates_skipped_known,
            candidates_filtered_non_shoot=candidates_filtered_non_shoot,
            airbnb_listings_scraped=airbnb_listings_scraped,
            magicbricks_listings_scraped=magicbricks_listings_scraped,
            acres99_listings_scraped=acres99_listings_scraped,
            duration_seconds=round(time.monotonic() - start, 3),
            errors=errors,
        )

    # --- Source paths ---

    async def _run_google_places_path(
        self,
        request: SearchRequest,
        city_hint: str | None,
        property_type_hint: str | None,
    ) -> dict[str, Any]:
        """Google Places → crawl → ingest (existing behaviour)."""
        errors: list[str] = []
        ingested_ids: list[Any] = []
        try:
            discovery = await self.discovery_service.discover_ad_hoc(
                query_text=request.query,
                city=city_hint,
                property_type=property_type_hint,
            )
        except Exception as exc:  # noqa: BLE001
            return _zero_path_outcome([f"Google Places discovery failed: {exc}"])

        errors.extend(discovery.errors)

        for candidate in discovery.new_candidates:
            try:
                prop_id = await self._ingest_candidate(candidate)
                if prop_id is not None:
                    ingested_ids.append(prop_id)
            except Exception as exc:  # noqa: BLE001 — per-item isolation
                errors.append(f"ingest failed for '{candidate.name}': {exc}")

        return {
            "candidates_discovered": discovery.google_results_total,
            "candidates_new": discovery.candidates_created,
            "candidates_skipped_known": discovery.candidates_skipped_known,
            "candidates_filtered_non_shoot": discovery.candidates_filtered_non_shoot,
            "source_id": None,
            "listings_scraped": 0,
            "errors": errors,
            "ingested_ids": ingested_ids,
        }

    async def _run_external_source_path(
        self,
        request: SearchRequest,
        location_hint: str,
        source: "ExternalListingSource",
        url_finder: "Callable[..., Awaitable[list[str]]]",
        max_listings: int,
    ) -> dict[str, Any]:
        """Generic: DDG for listing URLs → scrape each → persist.

        Works for any source that conforms to `ExternalListingSource`
        (Airbnb, MagicBricks, future sources). The `url_finder` callable
        is the DDG method for that source (`find_airbnb_listing_urls`,
        `find_magicbricks_listing_urls`, ...). Called with `(query, limit=)`.
        """
        label = source.source_label
        errors: list[str] = []

        # Step 1: DDG → listing URLs for this source.
        try:
            urls = await url_finder(request.query, limit=max_listings)
        except Exception as exc:  # noqa: BLE001
            return _zero_path_outcome(
                [f"DuckDuckGo {label} search failed: {exc}"],
                source_id=source.source_id,
            )

        if not urls:
            return _zero_path_outcome([], source_id=source.source_id)

        listings: list[Any] = []  # List[ExternalListing] — avoid runtime import
        # Early-abort guard: only HARD blocks count (403/429/5xx/CAPTCHA —
        # the scraper raises ScraperBlockedError in those cases). Soft
        # skips (410 Gone, 404, parse-miss — scraper returns None) are
        # common with stale DDG indexes and harmless; they MUST NOT abort
        # the batch or we'd miss live listings further down the list.
        consecutive_blocks = 0
        block_threshold = 3
        async with source as scraper:
            for url in urls:
                try:
                    listing = await scraper.scrape_listing(url)
                except ScraperBlockedError as exc:
                    errors.append(f"{label} blocked: {exc}")
                    consecutive_blocks += 1
                except Exception as exc:  # noqa: BLE001
                    # Unexpected error — log as a skip but don't count
                    # toward abort (we don't know if it's IP-level).
                    errors.append(f"{label} scrape failed for {url}: {exc}")
                else:
                    if listing is None:
                        errors.append(
                            f"{label} listing skipped (delisted / unavailable): {url}"
                        )
                        # soft skip — do NOT increment consecutive_blocks
                    else:
                        listings.append(listing)
                        consecutive_blocks = 0

                if consecutive_blocks >= block_threshold:
                    errors.append(
                        f"Stopped {label} after {consecutive_blocks} consecutive "
                        "hard blocks (403 / 429 / CAPTCHA). IP is likely rate-"
                        "limited — try again in a few hours."
                    )
                    break

        # Step 2: persist each scraped listing.
        ingested_ids: list[Any] = []
        for listing in listings:
            try:
                prop_id = await self._ingest_external_listing(listing, location_hint)
                if prop_id is not None:
                    ingested_ids.append(prop_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"{label} ingest failed for listing {listing.listing_id}: {exc}"
                )

        return {
            "candidates_discovered": len(urls),
            "candidates_new": len(listings),
            "candidates_skipped_known": 0,
            "candidates_filtered_non_shoot": 0,
            "source_id": source.source_id,
            "listings_scraped": len(listings),
            "errors": errors,
            "ingested_ids": ingested_ids,
        }

    async def _ingest_external_listing(
        self,
        listing: "ExternalListing",
        location_hint: str,
    ) -> Any:  # returns the persisted Property.id (UUID)
        """Persist a scraped external listing as a Property row.

        Works for any `ExternalListing` regardless of source — Airbnb,
        MagicBricks, etc. Phone/email stay null for these rows (all
        current sources hide contacts behind OTP); the UI suppresses
        those fields at the response layer. Users click through the
        "View on {source_label} ↗" pill to inquire via the platform.

        The `google_place_id` column doubles as a generic external ID with
        a source prefix: `airbnb:<id>`, `magicbricks:<id>`. Tech debt;
        a proper `external_source` / `external_id` split is planned.
        """
        # Sources tag listings with the parent city ("Mumbai") and rarely
        # the neighborhood. If the user typed a more specific hint
        # ("kandivali") and the scraper didn't already give us a
        # neighborhood/locality, preserve the user's intent in `locality`
        # so future searches for that hint can find this row.
        source_city = (listing.city_hint or "").strip()
        derived_locality = listing.locality or listing.neighborhood
        if not derived_locality and location_hint:
            hint = location_hint.strip()
            if hint and hint.lower() != source_city.lower():
                derived_locality = hint.title()

        host_first_name = getattr(listing, "host_first_name", None)

        payload = PropertyUpsertFromCandidate(
            candidate_id=uuid.uuid4(),  # ephemeral; no candidate row
            canonical_name=listing.title or f"{listing.source.title()} Listing",
            city=source_city or location_hint or "Unknown",
            locality=derived_locality,
            lat=None,
            lng=None,
            property_type="villa",  # external sources are almost always residential
            google_place_id=f"{listing.source}:{listing.listing_id}",
            google_rating=None,
            google_review_count=None,
            website=None,  # listing URL goes in features_json.external_url
            features_json={
                "amenities": list(listing.amenities or []),
                "feature_tags": [],
                "description": listing.description,
                "source": listing.source,
                # `external_url` is the generic key the `_to_result_item`
                # projection reads from (back-compat alias `airbnb_url`
                # written too so older rows keep rendering).
                "external_url": listing.url,
                "airbnb_url": listing.url if listing.source == "airbnb" else None,
                "primary_image_url": listing.primary_image_url,
                "image_urls": list(listing.image_urls or []),
                "airbnb_host_first_name": host_first_name,
            },
        )
        prop = await self.property_service.upsert_from_candidate(payload)

        # Score + brief still run — both have heuristic fallbacks so even a
        # thin external payload (title + city only) produces something useful.
        try:
            await self.scoring_service.score_property(prop.id)
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.briefing_service.generate_brief(prop.id)
        except Exception:  # noqa: BLE001
            pass
        return prop.id

    # --- Internals ---

    async def _ingest_candidate(self, candidate: DiscoveryCandidate) -> Any:
        """Run the crawl→contacts→dedup→upsert→score→brief pipeline for one candidate.

        Returns the persisted Property.id (UUID).
        """
        # Crawl the website if there is one.
        crawl_result = None
        if candidate.website:
            crawl_result = await self.crawler_service.crawl_property(
                str(candidate.id), candidate.website
            )

        features: dict[str, Any] = {}
        if crawl_result is not None:
            features = {
                "amenities": list(crawl_result.unstructured_data.amenities),
                "feature_tags": list(crawl_result.unstructured_data.feature_tags),
                "description": crawl_result.unstructured_data.description,
            }

        # Upsert into the canonical property table.
        payload = PropertyUpsertFromCandidate(
            candidate_id=candidate.id,
            canonical_name=candidate.name,
            city=candidate.city,
            locality=candidate.locality,
            lat=candidate.lat,
            lng=candidate.lng,
            property_type=candidate.property_type,  # type: ignore[arg-type]
            google_place_id=candidate.external_id,
            google_rating=candidate.google_rating,
            google_review_count=candidate.google_review_count,
            website=candidate.website,
            features_json=features,
        )
        prop = await self.property_service.upsert_from_candidate(payload)

        # Resolve contacts (API + crawl).
        api_contacts = _api_contacts_from_candidate(candidate)
        crawl_contacts = crawl_result.all_contacts() if crawl_result else []
        await self.contact_service.resolve_contacts(
            prop.id, api_contacts, crawl_contacts
        )

        # Score + brief so the user sees ranked, explained results.
        try:
            await self.scoring_service.score_property(prop.id)
        except Exception as exc:  # noqa: BLE001
            # Scoring fallback already built-in, but if something else blows
            # up we still want to proceed (the user can see the row anyway).
            raise exc
        try:
            await self.briefing_service.generate_brief(prop.id)
        except Exception:  # noqa: BLE001 — brief failure should not block
            pass

        # Mark the candidate as processed so an admin running the pipeline
        # later doesn't double-process it.
        candidate.processing_status = "processed"
        await self.db.flush()
        return prop.id

    def _to_result_item(self, row: Any) -> SearchResultItem:
        sub_scores: list[SearchSubScore] = []
        reason = getattr(row, "score_reason_json", None)
        if isinstance(reason, dict):
            for s in reason.get("sub_scores") or []:
                if not isinstance(s, dict):
                    continue
                try:
                    sub_scores.append(
                        SearchSubScore(
                            name=str(s["name"]),
                            value=float(s["value"]),
                            weight=float(s["weight"]),
                            source=s.get("source") or "deterministic",  # type: ignore[arg-type]
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    continue

        features = row.features_json or {}
        primary_image_url = features.get("primary_image_url")
        # External-listing rows stash their canonical URL here. Prefer the
        # generic `external_url` key (written for newer MagicBricks + Airbnb
        # rows) with a back-compat fallback to `airbnb_url` for rows
        # persisted before the rename. Google-Places rows leave both null
        # and use `canonical_website` for their actual site.
        external_url = features.get("external_url") or features.get("airbnb_url")

        # Source prefix on google_place_id is the single discriminator:
        # "airbnb:<id>"      → Airbnb listing
        # "magicbricks:<id>" → MagicBricks listing
        # anything else / None → Google Places or legacy
        source_prefix = (row.google_place_id or "").split(":", 1)[0]
        source_label = _SOURCE_LABELS.get(source_prefix)

        # External-source rows must NEVER show phone/email/website. Public
        # pages on Airbnb / MagicBricks hide contacts behind OTP gates —
        # any value in these columns is leftover cruft (Part 2's villa-
        # website chain, accidental dedup merges). Users inquire via the
        # "View on {source_label} ↗" pill instead.
        is_external_source = source_prefix in _SOURCES_WITHOUT_PUBLIC_CONTACTS
        canonical_phone = None if is_external_source else row.canonical_phone
        canonical_email = None if is_external_source else row.canonical_email
        canonical_website = None if is_external_source else row.canonical_website

        return SearchResultItem(
            id=row.id,
            canonical_name=row.canonical_name,
            city=row.city,
            locality=row.locality,
            property_type=row.property_type,
            relevance_score=row.relevance_score,
            short_brief=row.short_brief,
            canonical_phone=canonical_phone,
            canonical_email=canonical_email,
            canonical_website=canonical_website,
            google_rating=row.google_rating,
            google_review_count=row.google_review_count,
            sub_scores=sub_scores,
            features=features,
            primary_image_url=primary_image_url if isinstance(primary_image_url, str) else None,
            external_url=external_url if isinstance(external_url, str) else None,
            source_label=source_label,
        )


# --- Module-level helpers ---


# Cities that commonly appear in shoot-relevant searches (PRD + neighbors we've
# actually seen in real data). Matched case-insensitively with word boundaries.
_KNOWN_CITIES: tuple[str, ...] = (
    "Mumbai",
    "Thane",
    "Navi Mumbai",
    "Lonavala",
    "Khandala",
    "Pune",
    "Alibaug",
    "Alibag",
    "Nagaon",
    "Akshi",
    "Chaul",
    "Varasoli",
    "Kihim",
    "Goa",
    "Delhi",
    "Bangalore",
    "Bengaluru",
    "Hyderabad",
)


# Property type keyword map. First match wins.
_PROPERTY_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("heritage home", "heritage_home"),
    ("heritage", "heritage_home"),
    ("boutique hotel", "boutique_hotel"),
    ("banquet hall", "banquet_hall"),
    ("banquet", "banquet_hall"),
    ("farmhouse", "farmhouse"),
    ("farm house", "farmhouse"),
    ("farm stay", "farmhouse"),
    ("villa", "villa"),
    ("bungalow", "bungalow"),
    ("resort", "resort"),
    ("warehouse", "warehouse"),
    ("industrial shed", "industrial_shed"),
    ("rooftop", "rooftop_venue"),
    ("terrace", "rooftop_venue"),
    ("theatre", "theatre_studio"),
    ("theater", "theatre_studio"),
    ("studio", "theatre_studio"),
    ("school", "school_campus"),
    ("college", "school_campus"),
    ("coworking", "coworking_space"),
    ("co-working", "coworking_space"),
    ("office", "office_space"),
    ("club", "club_lounge"),
    ("lounge", "club_lounge"),
    ("cafe", "cafe"),
    ("café", "cafe"),
    ("restaurant", "restaurant"),
    ("hotel", "boutique_hotel"),
]


def _infer_city(query: str) -> str | None:
    """Return the first known city name found in the query (case-insensitive).

    Longer / multi-word names are matched first so 'Navi Mumbai' wins over
    'Mumbai' when both occur in the query. Result is only used as a scoring
    hint (`location_demand`) — it does NOT gate the search.
    """
    normalized = " " + query.lower() + " "
    for city in sorted(_KNOWN_CITIES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(city.lower())}(?![a-z])", normalized):
            return city
    return None


# Words to strip when deriving a location hint from the query text.
# Includes generic placeholders ("property", "place", "home", etc.) — they
# look like property types to a casual reader but they're really filler
# the user types alongside the actual location ("property IN KANDIVALI").
# Plurals are matched by trimming a trailing 's' in `_extract_location_hint`.
_LOCATION_HINT_STOP_WORDS: frozenset[str] = frozenset({
    "in", "near", "at", "around", "close", "to", "the", "a", "an",
    "some", "any", "best", "top", "nice", "good", "for", "rent",
    "rental", "booking", "stays", "stay",
    "property", "properties", "place", "places",
    "home", "homes", "house", "houses", "spot", "spots",
})


def _extract_location_hint(query: str) -> str:
    """Return the substring most likely to be a location.

    Strips known property-type keywords (villa, resort, cafe, ...) and stop-
    words (in, near, at, ...). Whatever remains is handed to
    `PropertyService.find_by_location_hint` to surface already-scraped
    properties. Works for any city in the world because we don't consult a
    hardcoded list — we simply remove the parts of the query we know
    AREN'T location.

    Examples:
        'resorts in Bandra'       → 'bandra'
        'heritage villas alibaug' → 'alibaug'
        'farmhouse near Karjat'   → 'karjat'
        'cafes'                   → ''
    """
    tokens = re.findall(r"[a-z0-9]+", query.lower())
    property_type_words: set[str] = set()
    for phrase, _mapped in _PROPERTY_TYPE_KEYWORDS:
        property_type_words.update(phrase.split())

    def _is_type_or_plural(token: str) -> bool:
        # Strip trailing 's' so 'resorts' matches 'resort', 'villas' matches 'villa'.
        stem = token.rstrip("s")
        return token in property_type_words or stem in property_type_words

    kept = [
        t for t in tokens
        if t not in _LOCATION_HINT_STOP_WORDS and not _is_type_or_plural(t)
    ]
    return " ".join(kept)


def _infer_property_type(query: str) -> str:
    """Return the first matching property type; fall back to 'other'."""
    q = query.lower()
    for keyword, mapped in _PROPERTY_TYPE_KEYWORDS:
        if keyword in q:
            return mapped
    return "other"


def _allowed_types_for_route(route: str) -> list[str] | None:
    """Property types the hint-lookup is allowed to return for each route.

    - commercial: only commercial types (cafes, hotels, etc.)
    - residential: residential + commercial (e.g. a "villa" search may
      legitimately surface a cached "boutique_hotel" lead in the same area)
    - generic: only residential types (Airbnb persists everything as
      "villa", and we don't want cafe leakage on "property in X" queries)
    """
    if route == "commercial":
        return sorted(_COMMERCIAL_TYPES)
    if route == "residential":
        return sorted(_COMMERCIAL_TYPES | _RESIDENTIAL_TYPES)
    if route == "generic":
        return sorted(_RESIDENTIAL_TYPES)
    return None  # defensive — let everything through if route is unknown


def _classify_route(property_type_hint: str | None) -> str:
    """Pick the source bucket for a query.

    Commercial types (resort, cafe, hotel, ...) are well-indexed by Google.
    Residential types (villa, bungalow, farmhouse, heritage_home) appear on
    both Google and Airbnb. Anything else ('other' / generic 'property in X')
    is only really findable on Airbnb.
    """
    if property_type_hint in _COMMERCIAL_TYPES:
        return "commercial"
    if property_type_hint in _RESIDENTIAL_TYPES:
        return "residential"
    return "generic"


def _zero_path_outcome(
    errors: list[str],
    *,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Standard empty-stats dict used when a source path short-circuits.

    `source_id` is set for external-listing paths so the aggregator in
    `search()` can tell which per-source counter to increment (even
    though the count is zero).
    """
    return {
        "candidates_discovered": 0,
        "candidates_new": 0,
        "candidates_skipped_known": 0,
        "candidates_filtered_non_shoot": 0,
        "source_id": source_id,
        "listings_scraped": 0,
        "errors": errors,
        "ingested_ids": [],
    }


def _api_contacts_from_candidate(c: DiscoveryCandidate) -> list[ExtractedContact]:
    contacts: list[ExtractedContact] = []
    if c.phone:
        contacts.append(
            ExtractedContact(
                contact_type="phone",
                value=c.phone,
                source_url="",
                extraction_method="api_structured",
                confidence=0.95,
            )
        )
    if c.website:
        contacts.append(
            ExtractedContact(
                contact_type="website",
                value=c.website,
                source_url="",
                extraction_method="api_structured",
                confidence=0.95,
            )
        )
    return contacts
