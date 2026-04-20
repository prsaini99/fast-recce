"""Google Places API (New) client.

Wraps the two endpoints we actually use:
- Text Search for query-based discovery
- Place Details for enrichment of returned place_ids

Uses the X-Goog-FieldMask header so we only pay for the fields we need.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.exceptions import ExternalServiceError, RateLimitError

_BASE_URL = "https://places.googleapis.com/v1"

# Field mask for Text Search — minimal fields for cheap discovery.
_SEARCH_FIELDS = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.types",
        "places.primaryType",
        "places.rating",
        "places.userRatingCount",
        "nextPageToken",
    ]
)

# Field mask for Place Details — richer fields for enrichment.
_DETAILS_FIELDS = ",".join(
    [
        "id",
        "displayName",
        "formattedAddress",
        "addressComponents",
        "location",
        "types",
        "primaryType",
        "nationalPhoneNumber",
        "internationalPhoneNumber",
        "websiteUri",
        "rating",
        "userRatingCount",
        "googleMapsUri",
        "regularOpeningHours",
        "businessStatus",
        # `photos[].name` is the photo reference (format
        # `places/<id>/photos/<ref>`). We turn the first one into a
        # `primary_image_url` at ingest time via the Google Places media
        # endpoint.
        "photos",
        # Rich-context fields that we feed into the LLM at enrich time so
        # Gemini has something substantive to reason about for Google-
        # Places rows (which don't have a scrape-able canonical page).
        "editorialSummary",
        "reviews",
        "priceLevel",
    ]
)


@dataclass(frozen=True)
class PlaceSearchResult:
    """One result from Text Search — enough to decide whether to fetch Details."""

    place_id: str
    name: str
    address: str | None
    lat: float | None
    lng: float | None
    types: list[str]
    primary_type: str | None
    rating: float | None
    review_count: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class PlaceDetails:
    """Full detail response for a single place_id."""

    place_id: str
    name: str
    address: str | None
    address_components: list[dict[str, Any]]
    lat: float | None
    lng: float | None
    types: list[str]
    primary_type: str | None
    phone: str | None
    website: str | None
    rating: float | None
    review_count: int | None
    google_maps_uri: str | None
    business_status: str | None
    raw: dict[str, Any]


class GooglePlacesClient:
    """Async client for Google Places API (New)."""

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> GooglePlacesClient:
        self._http = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def text_search(
        self,
        query: str,
        location_bias: dict[str, Any] | None = None,
        max_pages: int = 3,
    ) -> list[PlaceSearchResult]:
        """Execute a Text Search query, following nextPageToken up to max_pages.

        Per Google's docs, subsequent pages must resend the original parameters
        (textQuery + locationBias) with the pageToken added. Any mismatch
        returns HTTP 400 with 'Request parameters for paging requests must
        match the initial SearchText request.'

        If page 1 succeeds but a later page fails, we return page-1 results
        rather than raising — partial data beats no data.
        """
        results: list[PlaceSearchResult] = []
        base_body: dict[str, Any] = {"textQuery": query}
        if location_bias is not None:
            base_body["locationBias"] = location_bias

        next_token: str | None = None
        for page in range(max_pages):
            body = dict(base_body)
            if next_token:
                body["pageToken"] = next_token
                await asyncio.sleep(1.2)  # Google requires a brief delay before pageToken use

            try:
                payload = await self._post(
                    "/places:searchText",
                    json=body,
                    fields=_SEARCH_FIELDS,
                )
            except ExternalServiceError:
                if page == 0:
                    raise
                break  # pagination failure — keep what page 1 gave us

            for raw in payload.get("places", []):
                results.append(_parse_search_result(raw))

            next_token = payload.get("nextPageToken")
            if not next_token:
                break

        return results

    async def get_place_details(self, place_id: str) -> PlaceDetails:
        """Fetch detail fields for a given place_id."""
        payload = await self._get(
            f"/places/{place_id}",
            fields=_DETAILS_FIELDS,
        )
        return _parse_place_details(payload)

    # --- Internals ---

    async def _post(
        self,
        path: str,
        json: dict[str, Any],
        fields: str,
    ) -> dict[str, Any]:
        return await self._request("POST", path, json=json, fields=fields)

    async def _get(self, path: str, fields: str) -> dict[str, Any]:
        return await self._request("GET", path, fields=fields)

    async def _request(
        self,
        method: str,
        path: str,
        fields: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)

        url = _BASE_URL + path
        headers = {
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": fields,
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http.request(
                    method, url, headers=headers, json=json
                )
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if resp.status_code == 429:
                raise RateLimitError(
                    "Google Places API rate-limited (HTTP 429). "
                    "Reduce query volume or wait before retrying."
                )
            if 500 <= resp.status_code < 600 and attempt < self._max_retries:
                await asyncio.sleep(min(2**attempt, 8))
                continue
            if not resp.is_success:
                raise ExternalServiceError(
                    f"Google Places API {method} {path} -> HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                )
            return resp.json() or {}

        raise ExternalServiceError(
            f"Google Places API {method} {path} failed after retries: {last_error}"
        )


# --- Helpers ---


def _parse_search_result(raw: dict[str, Any]) -> PlaceSearchResult:
    location = raw.get("location") or {}
    display = raw.get("displayName") or {}
    return PlaceSearchResult(
        place_id=raw.get("id", ""),
        name=display.get("text") or raw.get("name", ""),
        address=raw.get("formattedAddress"),
        lat=location.get("latitude"),
        lng=location.get("longitude"),
        types=list(raw.get("types") or []),
        primary_type=raw.get("primaryType"),
        rating=raw.get("rating"),
        review_count=raw.get("userRatingCount"),
        raw=raw,
    )


def _parse_place_details(raw: dict[str, Any]) -> PlaceDetails:
    location = raw.get("location") or {}
    display = raw.get("displayName") or {}
    return PlaceDetails(
        place_id=raw.get("id", ""),
        name=display.get("text") or raw.get("name", ""),
        address=raw.get("formattedAddress"),
        address_components=list(raw.get("addressComponents") or []),
        lat=location.get("latitude"),
        lng=location.get("longitude"),
        types=list(raw.get("types") or []),
        primary_type=raw.get("primaryType"),
        phone=raw.get("nationalPhoneNumber") or raw.get("internationalPhoneNumber"),
        website=raw.get("websiteUri"),
        rating=raw.get("rating"),
        review_count=raw.get("userRatingCount"),
        google_maps_uri=raw.get("googleMapsUri"),
        business_status=raw.get("businessStatus"),
        raw=raw,
    )
