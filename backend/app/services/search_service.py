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
from app.services.search_history_service import SearchHistoryService, normalize_query

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
        airbnb_default_enabled: bool = False,
        magicbricks_default_enabled: bool = False,
        acres99_default_enabled: bool = False,
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
        # Env-level defaults. `SearchRequest.use_*` can override per call.
        self.airbnb_default = airbnb_default_enabled
        self.magicbricks_default = magicbricks_default_enabled
        self.acres99_default = acres99_default_enabled
        self.history_service = SearchHistoryService(db=db)

    @staticmethod
    def _resolved(override: bool | None, default: bool) -> bool:
        """Honor per-request override, falling back to the env default."""
        return default if override is None else override

    async def search(self, request: SearchRequest) -> SearchResponse:
        try:
            return await self._search_impl(request)
        except Exception:
            # A single failed statement (e.g. Supabase pooler timeout) leaves
            # the AsyncSession in a rolled-back state, and every subsequent
            # query then raises PendingRollbackError. Explicitly clearing
            # the transaction here keeps the session usable for retries and
            # for `get_db`'s rollback handler to operate cleanly.
            try:
                await self.db.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise

    async def _search_impl(self, request: SearchRequest) -> SearchResponse:
        start = time.monotonic()
        errors: list[str] = []

        # Cache hit: replay previously scraped results for this query.
        # We treat any past run as a cache hit (no TTL for now) — identical
        # text queries reuse the same scraped property set. The user can
        # always trigger a fresh scrape by tweaking their query text.
        #
        # Refresh mode ("Find more results") bypasses this path entirely so
        # we actually re-invoke the pipeline. We still read `cached` below
        # because we need its `result_property_ids` set to filter scraped
        # hits down to genuinely new ones.
        normalized = normalize_query(request.query)
        cached = await self.history_service.get_by_normalized(normalized)
        if not request.refresh and cached is not None and cached.result_property_ids:
            cached_ids = [uuid.UUID(pid) for pid in cached.result_property_ids]
            cached_rows = await self.property_service.list_by_ids(cached_ids)
            if cached_rows:
                # Bump counters (last_searched_at + search_count).
                await self.history_service.record(
                    query_text=request.query,
                    normalized_query=normalized,
                    inferred_city=cached.inferred_city,
                    inferred_property_type=cached.inferred_property_type,
                    result_property_ids=cached_ids,
                    duration_seconds=cached.duration_seconds,
                )
                await self.db.commit()
                return SearchResponse(
                    query=request.query,
                    inferred_city=cached.inferred_city,
                    inferred_property_type=cached.inferred_property_type,
                    results=[self._to_result_item(row) for row in cached_rows[: request.max_results]],
                    candidates_discovered=0,
                    candidates_new=0,
                    candidates_skipped_known=len(cached_rows),
                    candidates_filtered_non_shoot=0,
                    airbnb_listings_scraped=0,
                    magicbricks_listings_scraped=0,
                    acres99_listings_scraped=0,
                    duration_seconds=round(time.monotonic() - start, 3),
                    errors=[],
                )

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

        route = _classify_route(request.query, property_type_hint)

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

        # External-listing sources (Airbnb, MagicBricks, 99acres) fire for
        # residential + generic routes. Each source is gated by the
        # per-request override if present, else the env default. Scraper
        # instances are always constructed in `build_search_service`.
        airbnb_on = self._resolved(request.use_airbnb, self.airbnb_default)
        magicbricks_on = self._resolved(request.use_magicbricks, self.magicbricks_default)
        acres99_on = self._resolved(request.use_acres99, self.acres99_default)

        if any_external_needed and self.ddg_client is not None:
            if airbnb_on and self.airbnb_scraper is not None:
                tasks.append(
                    self._run_external_source_path(
                        request, location_hint,
                        source=self.airbnb_scraper,
                        url_finder=self.ddg_client.find_airbnb_listing_urls,
                        max_listings=self.airbnb_max_listings,
                    )
                )
            if magicbricks_on and self.magicbricks_scraper is not None:
                tasks.append(
                    self._run_external_source_path(
                        request, location_hint,
                        source=self.magicbricks_scraper,
                        url_finder=self.ddg_client.find_magicbricks_listing_urls,
                        max_listings=self.magicbricks_max_listings,
                    )
                )
            if acres99_on and self.acres99_scraper is not None:
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

        # Warn when the route wanted external sources but the caller turned
        # them all off for this query (via `use_airbnb=False` etc.).
        if any_external_needed and not (airbnb_on or magicbricks_on or acres99_on):
            errors.append(
                "All property scrapers are disabled for this search — "
                "residential / generic queries will only surface Google "
                "Places results. Toggle a scraper on in the search options "
                "to broaden results."
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

        # Refresh mode: "Find more results" wants N unique-new properties
        # that AREN'T already in this query's history row. So we hide the
        # pre-existing set from `merged` before it's built. Cap with
        # `additional_results` (if provided) rather than `max_results` so
        # the popover's count is what the user gets.
        existing_query_ids: set[Any] = set()
        if request.refresh and cached is not None and cached.result_property_ids:
            for raw in cached.result_property_ids:
                try:
                    existing_query_ids.add(uuid.UUID(str(raw)))
                except (ValueError, TypeError):
                    continue
        cap = (
            request.additional_results
            if request.refresh and request.additional_results is not None
            else request.max_results
        )

        seen_ids: set[Any] = set()
        merged: list[Any] = []
        for row in (*fresh_items, *hint_items):
            if row.id in seen_ids:
                continue
            if row.id in existing_query_ids:
                # Already in this query's history; refresh wants only new.
                continue
            seen_ids.add(row.id)
            merged.append(row)
            if len(merged) >= cap:
                break

        # Annotate each result with its originating query when the property
        # was first discovered by an older search. A property stays linked to
        # the first query that surfaced it; later queries just point at the
        # earlier one via source_query_id / source_query_text.
        first_sources = await self.history_service.first_search_by_property(
            [row.id for row in merged]
        )

        results: list[SearchResultItem] = []
        for row in merged:
            item = self._to_result_item(row)
            source = first_sources.get(row.id)
            if source is not None and source.normalized_query != normalized:
                item.source_query_id = source.id
                item.source_query_text = source.query_text
            results.append(item)

        duration = round(time.monotonic() - start, 3)

        # Persist only the properties first discovered by THIS query; any
        # property that was already linked to an earlier search stays linked
        # there. This keeps the sidebar / cache-hit replay stable: clicking
        # the older query still shows those properties under it, and the new
        # query's own history row only "owns" what's genuinely new.
        new_property_ids = [
            row.id for row in merged if row.id not in first_sources
        ]
        await self.history_service.record(
            query_text=request.query,
            normalized_query=normalized,
            inferred_city=city_hint,
            inferred_property_type=property_type_hint,
            result_property_ids=new_property_ids,
            duration_seconds=duration,
        )
        await self.db.commit()

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
            duration_seconds=duration,
            errors=errors,
        )

    # --- Source paths ---

    async def _run_google_places_path(
        self,
        request: SearchRequest,
        city_hint: str | None,
        property_type_hint: str | None,
    ) -> dict[str, Any]:
        """Google Places → crawl → ingest.

        Normal mode runs one discovery against `request.query`. Refresh
        mode ("Find more results") runs that plus a couple of phrasing
        variants back-to-back so we pull in Google results that the
        original phrasing missed. Google Text Search caps at ~60 per
        unique query string, so varying the phrasing is the only way to
        keep surfacing new place_ids once the original set is exhausted.
        """
        errors: list[str] = []
        ingested_ids: list[Any] = []
        total_discovered = 0
        total_created = 0
        total_skipped_known = 0
        total_filtered_non_shoot = 0

        queries = (
            _query_variants_for_refresh(request.query)
            if request.refresh
            else [request.query]
        )
        seen_place_ids: set[str] = set()

        for q in queries:
            try:
                discovery = await self.discovery_service.discover_ad_hoc(
                    query_text=q,
                    city=city_hint,
                    property_type=property_type_hint,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"Google Places discovery failed for {q!r}: {exc}"
                )
                continue

            errors.extend(discovery.errors)
            total_discovered += discovery.google_results_total
            total_created += discovery.candidates_created
            total_skipped_known += discovery.candidates_skipped_known
            total_filtered_non_shoot += discovery.candidates_filtered_non_shoot

            for candidate in discovery.new_candidates:
                # Across variants we often re-see the same place_id; ingest
                # it once per refresh call.
                if candidate.external_id in seen_place_ids:
                    continue
                seen_place_ids.add(candidate.external_id)
                try:
                    prop_id = await self._ingest_candidate(candidate)
                    if prop_id is not None:
                        ingested_ids.append(prop_id)
                except Exception as exc:  # noqa: BLE001 — per-item isolation
                    errors.append(
                        f"ingest failed for '{candidate.name}': {exc}"
                    )

        return {
            "candidates_discovered": total_discovered,
            "candidates_new": total_created,
            "candidates_skipped_known": total_skipped_known,
            "candidates_filtered_non_shoot": total_filtered_non_shoot,
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

        # Step 2: filter obvious off-topic listings before persisting. DDG
        # occasionally surfaces results that have nothing to do with the
        # typed location — e.g. `site:airbnb.com/rooms property in jaipur`
        # has returned villas in Kosgoda (Sri Lanka). Persisting those
        # stamps the user's hint as `locality`, so every future search
        # for "jaipur" re-surfaces the Sri Lanka property. Cheap guard:
        # accept a listing only if the scraper-reported city/title/
        # description actually mentions the hint.
        ingested_ids: list[Any] = []
        for listing in listings:
            if not _listing_matches_location_hint(listing, location_hint):
                errors.append(
                    f"{label} listing skipped (location mismatch: hint={location_hint!r} "
                    f"vs city={listing.city_hint!r}): {listing.url}"
                )
                continue
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
        # the neighborhood. We used to backfill `locality` with the
        # user-typed hint when the scraper didn't supply one — but that
        # caused off-topic DDG hits (Sri-Lanka villa surfaced for "jaipur")
        # to be permanently tagged with the wrong locality.
        #
        # Now: accept the user's hint as locality ONLY when the scraped
        # title/description/city ALREADY contains the hint somewhere.
        # That keeps the original "preserve user's neighborhood intent"
        # benefit for e.g. a Mumbai villa where the listing title says
        # "Apartment in Kandivali", while rejecting nonsense matches.
        source_city = (listing.city_hint or "").strip()
        derived_locality = listing.locality or listing.neighborhood
        if (
            not derived_locality
            and location_hint
            and _listing_matches_location_hint(listing, location_hint, require_text=True)
        ):
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
                # New scraped specifics — every field is optional per
                # source; None values are kept deliberately so the result
                # card can detect "not extracted" vs "actually missing".
                "price_display": listing.price_display,
                "price_value": listing.price_value,
                "price_currency": listing.price_currency,
                "price_period": listing.price_period,
                "bedrooms": listing.bedrooms,
                "bathrooms": listing.bathrooms,
                "area_sqft": listing.area_sqft,
                "max_guests": listing.max_guests,
                "property_subtype": listing.property_subtype,
            },
        )
        prop = await self.property_service.upsert_from_candidate(payload)
        # Score + brief are now on-demand via the "Enrich" button on the
        # property card — keeping them out of the scrape pipeline makes
        # the initial search ~60% faster and avoids Gemini rate-limit
        # induced timeouts. Clients can call POST /properties/{id}/enrich
        # (or /score, /brief individually) when ranked results matter.
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

        # Pull the first photo reference from the Google Places details
        # payload and turn it into a renderable image URL. `photos[i].name`
        # has the form `places/<place_id>/photos/<ref>` — Google serves
        # the actual image at `/v1/{name}/media?key=...&maxHeightPx=...`.
        # We append the API key so the frontend can <img src=...> directly;
        # the key is restricted to the Places Photos endpoint in GCP so
        # exposing it here is acceptable for dev. Production should route
        # through a backend proxy.
        photo_url = _google_photo_url_from_candidate(candidate)
        if photo_url and not features.get("primary_image_url"):
            features["primary_image_url"] = photo_url

        # Lift rich Google-Places signals into features_json so the LLM
        # prompt can reference them without digging through raw payloads.
        google_context = _google_rich_context_from_candidate(candidate)
        if google_context:
            features.setdefault("google_context", google_context)

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

        # Score + brief are no longer run inline — they're on-demand via
        # the enrich endpoints so the scrape pipeline stays fast.

        # Mark the candidate as processed so an admin running the pipeline
        # later doesn't double-process it.
        candidate.processing_status = "processed"
        await self.db.flush()
        return prop.id

    @staticmethod
    def _to_result_item(row: Any) -> SearchResultItem:
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


def _listing_matches_location_hint(
    listing: "ExternalListing",
    location_hint: str,
    *,
    require_text: bool = False,
) -> bool:
    """Best-effort check that a scraped listing is actually at the user's location.

    DDG's site-restricted search occasionally surfaces unrelated listings
    (a Sri Lanka villa for "property in jaipur"). We reject anything
    where none of the scraped text — city / locality / neighborhood /
    title / description — mentions the user's hint.

    - Empty `location_hint` → assume match (caller handled intent elsewhere).
    - Scraper returned a `city` that loosely matches the hint → match.
    - `require_text=True` raises the bar: only trust a hit that comes
      from the scraped title/description (not just the city/locality
      fields), used for the stricter "should we stamp user hint as
      locality?" decision.
    """
    hint = (location_hint or "").strip().lower()
    if not hint or len(hint) < 3:
        return True  # nothing to filter against

    # Normalize the hint to its significant tokens (drop stop-words).
    # "kandivali west" → {"kandivali", "west"}. Any one substring hit is
    # enough; we'd rather over-admit than drop real matches.
    tokens = [t for t in re.split(r"[^a-z0-9]+", hint) if len(t) >= 3]
    if not tokens:
        return True

    city_blob = " ".join(
        str(getattr(listing, k, "") or "")
        for k in ("city_hint", "locality", "neighborhood")
    ).lower()
    text_blob = " ".join(
        str(getattr(listing, k, "") or "")
        for k in ("title", "description")
    ).lower()

    def any_hit(blob: str) -> bool:
        return any(tok in blob for tok in tokens)

    if require_text:
        return any_hit(text_blob)
    return any_hit(city_blob) or any_hit(text_blob)


def _query_variants_for_refresh(query: str, limit: int = 3) -> list[str]:
    """Build phrasing alternates for the same semantic search.

    Google's Text Search caps at ~60 results per unique query string, so
    after the first run we exhaust that set and subsequent scrapes find
    nothing new. Varying the phrasing ("cafe in manali" → "best cafes
    manali" → "coffee shops manali") pulls overlapping-but-distinct
    result sets out of Google's index so the "Find more" button can
    actually surface more properties.

    Keeps the original query first so the pipeline still re-checks the
    canonical phrasing (useful when Google's rankings shift over time).
    Dedupes case-insensitively and caps at `limit`.
    """
    q = query.strip()
    if not q:
        return [q]

    variants: list[str] = [q]

    # Split on the preposition to grab `<thing>` + `<place>` separately.
    # "resorts in Alibaug" → thing="resorts", place="Alibaug"
    parts = re.split(r"\s+(?:in|near|at|around)\s+", q, maxsplit=1)
    if len(parts) == 2:
        thing, place = parts[0].strip(), parts[1].strip()
        if thing and place:
            variants.append(f"best {thing} {place}")
            variants.append(f"top {thing} near {place}")
            variants.append(f"popular {thing} {place}")
    else:
        variants.extend([f"best {q}", f"top {q}", f"popular {q}"])

    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        key = v.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
        if len(out) >= limit:
            break
    return out


def _google_rich_context_from_candidate(
    candidate: DiscoveryCandidate,
) -> dict[str, Any] | None:
    """Extract Google-provided prose signals for the LLM enrichment prompt.

    Google Places (New) gives us three useful text-ish signals that we
    don't otherwise store: `editorialSummary` (Google's own blurb),
    `reviews` (author-written review text), `priceLevel` ("MODERATE",
    "EXPENSIVE", etc). Stashing them under `features_json.google_context`
    lets the LLM scoring/briefing prompts pull them in uniformly with
    the scraper-side description/amenities.
    """
    raw = candidate.raw_result_json or {}
    if not isinstance(raw, dict):
        return None
    details = raw.get("details")
    if not isinstance(details, dict):
        return None

    out: dict[str, Any] = {}

    summary = details.get("editorialSummary")
    if isinstance(summary, dict):
        text = summary.get("text")
        if isinstance(text, str) and text.strip():
            out["editorial_summary"] = text.strip()[:1000]

    price_level = details.get("priceLevel")
    if isinstance(price_level, str) and price_level.strip():
        out["price_level"] = price_level

    reviews = details.get("reviews")
    if isinstance(reviews, list):
        snippets: list[str] = []
        for rev in reviews[:5]:
            if not isinstance(rev, dict):
                continue
            text_block = rev.get("text")
            if isinstance(text_block, dict):
                text_value = text_block.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    snippets.append(text_value.strip()[:600])
        if snippets:
            out["review_snippets"] = snippets

    return out or None


def _google_photo_url_from_candidate(candidate: DiscoveryCandidate) -> str | None:
    """Build a renderable image URL from a Google Places details payload.

    Discovery stashes `details.raw` under `raw_result_json.details`. The
    Places API (New) returns photos as `{"name": "places/<id>/photos/<ref>", ...}`.
    We convert the first one into `/v1/{name}/media?maxHeightPx=400&key=...`,
    which Google serves as a 302 redirect to the actual CDN URL.
    """
    raw = candidate.raw_result_json or {}
    details = raw.get("details") if isinstance(raw, dict) else None
    photos = (details or {}).get("photos") if isinstance(details, dict) else None
    if not isinstance(photos, list) or not photos:
        return None
    first = photos[0] if isinstance(photos[0], dict) else None
    if not first:
        return None
    name = first.get("name")
    if not isinstance(name, str) or not name:
        return None
    from app.config import get_settings

    api_key = get_settings().google_places_api_key
    return (
        f"https://places.googleapis.com/v1/{name}/media"
        f"?maxHeightPx=400&key={api_key}"
    )


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


def _classify_route(query: str, property_type_hint: str | None) -> str:
    """Pick the source bucket for a query.

    Airbnb / MagicBricks / 99acres only list properties you can rent or buy.
    Firing them on a non-property query ("best coffee shops in Bandra") is
    both expensive and pollutes the result set with junk. So we only route
    to them when the query is explicitly about a residence.

    - commercial (Google Places only): recognized commercial type
      (cafe, resort, hotel, warehouse, ...).
    - residential (Google + external property scrapers): recognized
      residential type (villa, bungalow, farmhouse, heritage_home).
    - generic (external property scrapers only): type couldn't be
      inferred BUT the raw query contains an explicit property-intent
      keyword (`property`, `stay`, `home`, `rental`, `apartment`, ...).
    - unknown → commercial (Google only): everything else. Prevents
      "meetups in kandivali" or "coffee shops in mumbai" from silently
      triggering the property scrapers.
    """
    if property_type_hint in _COMMERCIAL_TYPES:
        return "commercial"
    if property_type_hint in _RESIDENTIAL_TYPES:
        return "residential"
    if _has_property_intent(query):
        return "generic"
    # Fall back to Google-only so we don't waste a scraper round on a
    # query with no property signal at all.
    return "commercial"


# Free-text markers that signal the user is looking for somewhere to stay,
# rent, or buy — the only queries for which Airbnb / MagicBricks / 99acres
# can return useful rows. Matched as whole words (case-insensitive) against
# the raw query when `_infer_property_type` didn't land on a known type.
_PROPERTY_INTENT_KEYWORDS: frozenset[str] = frozenset({
    "property", "properties",
    "stay", "stays", "staycation",
    "home", "homes", "house", "houses",
    "flat", "flats", "apartment", "apartments",
    "rental", "rentals", "rent",
    "airbnb", "bnb",
    "room", "rooms", "accommodation", "accommodations",
    "pg", "hostel", "hostels",
    "lodge", "lodging", "lodgings",
    "cottage", "cottages",
    "bungalow", "bungalows",
    "villa", "villas",
    "farmhouse", "farmhouses", "farmstay", "farmstays",
    "resort", "resorts",
    "homestay", "homestays",
})


def _has_property_intent(query: str) -> bool:
    """True iff the raw query contains a word that means "I want a place"."""
    tokens = set(re.findall(r"[a-z]+", query.lower()))
    return bool(tokens & _PROPERTY_INTENT_KEYWORDS)


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
